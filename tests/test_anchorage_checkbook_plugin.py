"""Tests for the Anchorage Open Checkbook plugin.

All HTTP is mocked; the live acceptance suite (work-order tests T1-T16
with real dollar figures) lives in
tests/test_anchorage_checkbook_acceptance.py and runs only when
CHECKBOOK_LIVE_TESTS=1.

Coverage here mirrors the work order's traps and patterns:
- 2.1 Duplicate='No' injected everywhere; include_duplicates warns
- 2.2 Month_Fiscal_Period 1-16 semantics
- 2.3 PubDate filters rejected; snapshot surfaced as provenance
- 2.4 entity IS NOT NULL on vendor paths; per-table entity field
- 2.7 'code : label' splitting
- 2.8 completeness-gap notices
- 3.1 injection-shape validator (bare 'UNION' in vendor names passes)
- 3.2 clamp echoed in provenance
- 3.3 compact pipe-delimited output
- 3.4 bookended truncation with true totals
"""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from core.interfaces import PluginType
from plugins.anchorage_checkbook.config_schema import (
    DEFAULT_SERVICE_URL,
    AnchorageCheckbookPluginConfig,
)
from plugins.anchorage_checkbook.plugin import (
    DUPLICATE_WARNING,
    TABLES,
    AnchorageCheckbookPlugin,
)
from plugins.anchorage_checkbook.where_validator import (
    CheckbookWhereValidator,
    OrderByValidator,
    OutFieldsValidator,
)

SNAPSHOT_PATH = AnchorageCheckbookPlugin.SCHEMA_SNAPSHOT_PATH

with open(SNAPSHOT_PATH, encoding="utf-8") as _fh:
    _SNAPSHOT = json.load(_fh)


def make_response(payload, status=200):
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = {"content-type": "application/json"}
    resp.text = json.dumps(payload)
    return resp


def feat(**attrs):
    return {"attributes": attrs}


def install_client(plugin, routes=None, default=None):
    """Install a fake httpx client that routes by URL/params.

    ``routes`` is a list of (predicate(url, params) -> bool, payload).
    The first matching payload is returned; ``default`` otherwise.
    Returns the list of (url, params) calls for assertions.
    """
    calls = []
    default_payload = default if default is not None else {"features": []}

    async def fake_get(url, params=None):
        params = dict(params or {})
        calls.append((url, params))
        for predicate, payload in routes or []:
            if predicate(url, params):
                if isinstance(payload, Exception):
                    raise payload
                return make_response(payload)
        return make_response(default_payload)

    client = Mock()
    client.get = AsyncMock(side_effect=fake_get)
    client.aclose = AsyncMock()
    plugin.client = client
    return calls


def is_count(url, params):
    return params.get("returnCountOnly") == "true"


def is_stats(url, params):
    return "outStatistics" in params


def is_distinct(url, params):
    return params.get("returnDistinctValues") == "true"


def is_pubdate_probe(url, params):
    return params.get("outFields") == "PubDate"


def is_row_query(url, params):
    return (
        url.endswith("/query")
        and "returnCountOnly" not in params
        and "outStatistics" not in params
        and params.get("returnDistinctValues") != "true"
        and params.get("outFields") != "PubDate"
    )


PUBDATE_MS = 1785628800000  # 2026-08-02


@pytest.fixture
def plugin():
    """Initialized-state plugin; live fields fall back to the registry."""
    p = AnchorageCheckbookPlugin({})
    p.plugin_config = AnchorageCheckbookPluginConfig()
    p._initialized = True
    # Pre-fill the pubdate cache so tools don't need a probe route.
    for tid in TABLES:
        p._pubdate_cache[tid] = "2026-08-02"
    return p


async def run_tool(plugin, name, args):
    return await plugin.execute_tool(name, args)


def tool_text(result):
    assert result.success, result.error_message
    return result.content[0]["text"]


# ── Plugin attributes, registry, snapshot consistency ─────────────────


class TestPluginAttributes:
    def test_plugin_attributes(self):
        p = AnchorageCheckbookPlugin({})
        assert p.plugin_name == "anchorage_checkbook"
        assert p.plugin_type == PluginType.OPEN_DATA
        assert p.plugin_version == "1.0.0"

    def test_registry_tables_zero_to_five_no_six(self):
        assert sorted(TABLES) == [0, 1, 2, 3, 4, 5]

    def test_registry_matches_vendored_snapshot(self):
        """all_fields mirrors the vendored schema snapshot exactly."""
        for tid, info in TABLES.items():
            snap_fields = [f["name"] for f in _SNAPSHOT[str(tid)]["fields"]]
            assert list(info.all_fields) == snap_fields, tid
            assert info.name == _SNAPSHOT[str(tid)]["name"]

    def test_registry_semantics(self):
        # Table 2 has four measures and NO Amount field.
        assert "Amount" not in TABLES[2].all_fields
        assert TABLES[2].default_measure == "Total_Payroll_Cost"
        assert set(TABLES[2].measure_fields) == {
            "Total_Payroll_Cost",
            "Salaries_Wages",
            "Overtime",
            "Liabilities_Benefits",
        }
        # Table 4's payee field is Customer_Business_Name.
        assert TABLES[4].entity_field == "Customer_Business_Name"
        assert TABLES[0].entity_field == "Vendor_Name"
        assert TABLES[1].entity_field is None
        # Table 5 is empty upstream.
        assert TABLES[5].status == "empty"
        # Coded fields.
        assert set(TABLES[0].code_label_fields) == {"Fund", "G_L_Account"}

    def test_default_service_url(self):
        cfg = AnchorageCheckbookPluginConfig()
        assert cfg.service_url == DEFAULT_SERVICE_URL
        assert cfg.service_url.endswith("/FeatureServer")

    @pytest.mark.parametrize(
        "bad_url", ["not-a-url", "", "ftp://example.com/svc", "https://"]
    )
    def test_config_rejects_bad_urls(self, bad_url):
        with pytest.raises(Exception):
            AnchorageCheckbookPluginConfig(service_url=bad_url)

    def test_config_rejects_unknown_keys_and_strips_slash(self):
        with pytest.raises(Exception):
            AnchorageCheckbookPluginConfig(bogus_key=1)
        cfg = AnchorageCheckbookPluginConfig(
            service_url="https://example.com/FeatureServer/"
        )
        assert cfg.service_url == "https://example.com/FeatureServer"

    def test_table_info_rejects_bad_ids(self):
        for bad in (6, -1, "x", None):
            with pytest.raises(ValueError, match="list_tables"):
                AnchorageCheckbookPlugin._table_info(bad)
        assert AnchorageCheckbookPlugin._table_info("3") is TABLES[3]

    def test_fmt_money(self):
        fmt = AnchorageCheckbookPlugin._fmt_money
        assert fmt(792430985.65) == "$792,430,985.65"
        assert fmt(-749447668) == "-$749,447,668.00"
        assert fmt(None) == "--"
        assert fmt("junk") == "junk"

    def test_ms_to_iso_smart(self):
        conv = AnchorageCheckbookPlugin._ms_to_iso_smart
        assert conv(1785628800000) == "2026-08-02"  # midnight -> date only
        assert conv(1785628800000 + 3_600_000) == "2026-08-02T01:00:00Z"
        assert conv(None) is None
        assert conv("garbage") == "garbage"


class TestGetTools:
    def test_eight_tools_in_order(self, plugin):
        tools = plugin.get_tools()
        assert [t.name for t in tools] == [
            "list_tables",
            "get_table_schema",
            "spending_stats",
            "search_by_vendor",
            "top_vendors",
            "get_line_items",
            "list_field_values",
            "query_checkbook",
        ]
        for t in tools:
            assert t.annotations == {"readOnlyHint": True, "openWorldHint": True}
            assert t.description
            # idempotentHint is documented as meaningful only when
            # readOnlyHint is false, so it must not be advertised here.
            assert "idempotentHint" not in t.annotations, t.name

    def test_every_tool_has_a_title(self, plugin):
        """`title` is the display name clients show instead of the prefixed
        wire name. A tool added without an entry in TOOL_TITLES would fall
        back to `anchorage_checkbook__whatever` silently, so fail loudly."""
        tools = plugin.get_tools()

        assert tools, "expected at least one tool"
        for t in tools:
            assert t.title, f"{t.name} has no title (add it to TOOL_TITLES)"

    def test_no_stale_title_entries(self, plugin):
        """A renamed or removed tool must not leave a dangling title."""
        names = {t.name for t in plugin.get_tools()}
        stale = set(type(plugin).TOOL_TITLES) - names
        assert not stale, f"TOOL_TITLES has entries for missing tools: {stale}"

    def test_include_duplicates_exposed_and_defaults_false(self, plugin):
        tools = {t.name: t for t in plugin.get_tools()}
        for name in (
            "spending_stats",
            "search_by_vendor",
            "top_vendors",
            "get_line_items",
            "list_field_values",
            "query_checkbook",
        ):
            prop = tools[name].input_schema["properties"]["include_duplicates"]
            assert prop["default"] is False, name

    def test_descriptions_carry_the_traps(self, plugin):
        tools = {t.name: t.description for t in plugin.get_tools()}
        # 2.2 in the period-exposing tool.
        assert "13-16" in tools["list_field_values"]
        # 2.5 net labeling.
        assert "NET" in tools["spending_stats"]
        # 2.9 vendor normalization.
        assert "not normalized" in tools["search_by_vendor"].lower()
        # 2.3 PubDate redirection on the escape hatch.
        assert "PubDate" in tools["query_checkbook"]
        # 3.1 union vendors are legal.
        assert "Credit Union 1" in tools["query_checkbook"]

    async def test_unknown_tool_result(self, plugin):
        result = await plugin.execute_tool("nope", {})
        assert not result.success
        assert "Unknown tool" in result.error_message


# ── 3.1 The WHERE validator (blocklist-bug regression) ────────────────


class TestCheckbookWhereValidator:
    @pytest.mark.parametrize(
        "where",
        [
            "Vendor_Name = 'Credit Union 1'",
            "Vendor_Name = 'IBEW Local Union 1547'",
            "Vendor_Name = 'Plumbers & Steamfitters Union Local 367'",
            "Vendor_Name = 'UAA STUDENT UNION' AND Fiscal_Year = '2023'",
            "Vendor_Name LIKE '%union%'",
            "Vendor_Name = 'O''BRIEN; DROP TABLE' AND Amount > 0",
            "Amount BETWEEN -100 AND 100",
        ],
    )
    def test_real_queries_pass(self, where):
        assert CheckbookWhereValidator.validate(where) == where

    @pytest.mark.parametrize(
        "where",
        [
            "1=1 UNION SELECT * FROM users",
            "1=1 union all select password from t",
            "Amount > 0; DROP TABLE x",
            "Amount > 0 -- comment",
            "Amount > 0 /* comment */",
            "1=1 AND EXISTS (SELECT 1 FROM t)",
            "Vendor_Name = 'unbalanced",
            "WAITFOR DELAY '0:0:5'",
            "1=1 AND SLEEP(5)",
        ],
    )
    def test_injection_shapes_rejected(self, where):
        with pytest.raises(ValueError):
            CheckbookWhereValidator.validate(where)

    def test_empty_becomes_1_eq_1(self):
        assert CheckbookWhereValidator.validate("") == "1=1"
        assert CheckbookWhereValidator.validate(None) == "1=1"

    def test_schema_validation_suggests_field(self):
        with pytest.raises(ValueError, match="Vendor_Name"):
            CheckbookWhereValidator.validate_against_schema(
                "vendor_name = 'x'", TABLES[0].all_fields
            )

    def test_out_fields_and_order_by(self):
        assert OutFieldsValidator.validate("Vendor_Name, Amount") == (
            "Vendor_Name,Amount"
        )
        with pytest.raises(ValueError):
            OutFieldsValidator.validate("Amount; DROP")
        assert OrderByValidator.validate("Amount DESC") == "Amount DESC"
        with pytest.raises(ValueError):
            OrderByValidator.validate("Amount DESC; DROP")


# ── Trap layer ────────────────────────────────────────────────────────


class TestTrapLayer:
    def test_dedup_injected_by_default(self, plugin):
        clause, warning = plugin._dedup_parts({})
        assert clause == "Duplicate = 'No'"
        assert warning is None

    def test_include_duplicates_warns(self, plugin):
        clause, warning = plugin._dedup_parts({"include_duplicates": True})
        assert clause is None
        assert warning == DUPLICATE_WARNING
        assert "FY2023 AND FY2026" in warning  # never a one-year filter

    def test_pubdate_filter_rejected_with_redirect(self, plugin):
        with pytest.raises(ValueError) as exc:
            plugin._reject_pubdate_filter("PubDate > DATE '2026-01-01'")
        msg = str(exc.value)
        assert "Fiscal_Year" in msg and "Month_Fiscal_Period" in msg
        # Non-PubDate clauses pass through.
        plugin._reject_pubdate_filter("Fiscal_Year = '2023'")

    def test_period_validation(self, plugin):
        assert plugin._validate_period(16) == 16
        for bad in (0, 17, "x", None):
            with pytest.raises(ValueError, match="ADJUSTMENT"):
                plugin._validate_period(bad)

    def test_fiscal_year_validation(self, plugin):
        assert plugin._validate_fiscal_year(2023) == "2023"
        assert plugin._validate_fiscal_year(" 2026 ") == "2026"
        with pytest.raises(ValueError):
            plugin._validate_fiscal_year("23")

    def test_entity_field_resolution(self, plugin):
        assert plugin._entity_not_null_clause(TABLES[0]) == ("Vendor_Name IS NOT NULL")
        assert plugin._entity_not_null_clause(TABLES[4]) == (
            "Customer_Business_Name IS NOT NULL"
        )
        with pytest.raises(ValueError, match="Customer_Business_Name"):
            plugin._require_entity_field(TABLES[1])

    def test_split_code_label(self, plugin):
        assert plugin._split_code_label("141000 : Anchorage Roads & Drainage SA") == (
            "141000",
            "Anchorage Roads & Drainage SA",
        )
        assert plugin._split_code_label("540610 : Discounts Lost") == (
            "540610",
            "Discounts Lost",
        )
        # Plain labels (Business_Area) and non-strings pass through.
        assert plugin._split_code_label("Police Department") == (
            None,
            "Police Department",
        )
        assert plugin._split_code_label(None) == (None, None)

    def test_expand_code_labels(self, plugin):
        records = [{"Fund": "141000 : Roads", "Amount": 5, "Vendor_Name": "X"}]
        out = plugin._expand_code_labels(records, TABLES[0])
        assert out == [
            {
                "Fund_code": "141000",
                "Fund_label": "Roads",
                "Amount": 5,
                "Vendor_Name": "X",
            }
        ]
        # Tables without coded fields are returned unchanged.
        assert plugin._expand_code_labels(records, TABLES[3]) is records

    def test_fiscal_notices(self, plugin):
        # Unfiltered revenue: FY2024 gap + FY2026 partial.
        notes = plugin._fiscal_notices(TABLES[4], None)
        assert any("FY2024" in n for n in notes)
        assert any("FY2026 is partial" in n for n in notes)
        # Revenue pinned to 2023: silent.
        assert plugin._fiscal_notices(TABLES[4], ["2023"]) == []
        # Procurement touching 2025: outlier flag.
        assert any(
            "encumbrance" in n for n in plugin._fiscal_notices(TABLES[3], ["2025"])
        )
        # Any table touching 2026: partial-year note.
        assert any("FY2026" in n for n in plugin._fiscal_notices(TABLES[0], ["2026"]))
        assert plugin._fiscal_notices(TABLES[0], ["2022"]) == []


# ── WHERE composition & formatting patterns ───────────────────────────


class TestComposition:
    def test_structured_where_escapes_and_quotes(self, plugin):
        clauses, years = plugin._structured_where(
            TABLES[0],
            {
                "fiscal_year": 2023,
                "business_area": "Police",
                "vendor_contains": "O'Brien",
            },
        )
        assert "Fiscal_Year = '2023'" in clauses  # string-typed year
        assert "Business_Area LIKE '%Police%'" in clauses  # no UPPER()
        assert "Vendor_Name LIKE '%O''Brien%'" in clauses  # escaped quote
        assert "Vendor_Name IS NOT NULL" in clauses  # trap 2.4
        assert years == {"2023"}

    def test_structured_where_rejects_inapplicable_field(self, plugin):
        with pytest.raises(ValueError, match="no Fund field"):
            plugin._structured_where(TABLES[2], {"fund": "141000"})

    def test_combine_where(self, plugin):
        assert plugin._combine_where(None, "", "1=1") == "1=1"
        assert plugin._combine_where("A=1") == "A=1"
        assert plugin._combine_where("A=1", "B=2") == "(A=1) AND (B=2)"

    def test_clamp_echo(self, plugin):
        limit, requested = plugin._clamp_limit(
            {"limit": 5000}, default=200, maximum=500
        )
        assert (limit, requested) == (500, 5000)
        assert plugin._limit_echo(limit, requested) == (
            "limit=500 (requested 5000, clamped)"
        )
        assert plugin._limit_echo(200, 200) == "limit=200"

    def test_default_out_fields_skip_noise(self, plugin):
        fields = plugin._default_out_fields(TABLES[0]).split(",")
        assert "OBJECTID" not in fields
        assert "PubDate" not in fields
        assert "Duplicate" not in fields
        # With duplicates included, the flag column matters.
        assert "Duplicate" in plugin._default_out_fields(
            TABLES[0], include_duplicates=True
        ).split(",")

    def test_out_fields_typo_suggestion(self, plugin):
        with pytest.raises(ValueError, match="Vendor_Name"):
            plugin._validate_out_fields(TABLES[0], "vendor_name")

    def test_bookended_truncation(self, plugin):
        records = [{"Vendor_Name": "A", "Amount": 5}]
        text = plugin._format_rows_response(
            TABLES[0],
            records,
            where="(Duplicate = 'No')",
            limit=1,
            requested=5000,
            total_count=100,
            pubdate="2026-08-02",
        )
        assert text.count("**TRUNCATED:**") == 2  # top AND bottom
        assert "showing 1 of 100 matching rows" in text
        assert "TOTAL rows matching the filter: 100" in text
        assert "limit=1 (requested 5000, clamped)" in text
        assert "Data snapshot (PubDate): 2026-08-02" in text

    def test_compact_table_format(self, plugin):
        records = [
            {"Vendor_Name": "A | B", "Amount": 5},
            {"Vendor_Name": "C", "Amount": -7},
        ]
        lines = plugin._format_table(records, 0)
        assert lines[0] == "Vendor_Name | Amount"
        assert lines[1] == "A \\| B | 5"
        assert len(lines) == 3  # header + 2 rows, no per-record blocks


# ── Tool behavior (mocked HTTP) ───────────────────────────────────────


class TestListTables:
    async def test_list_tables_structure(self, plugin):
        install_client(plugin, routes=[(is_count, {"count": 100})])
        text = tool_text(await run_tool(plugin, "list_tables", {}))
        # T1 shape: six tables, no table 6, table 5 empty.
        assert text.count("### Table") == 6
        assert "### Table 6" not in text
        assert "### Table 5" in text and "Status: EMPTY" in text
        assert "Customer_Business_Name" in text
        assert "Total_Payroll_Cost" in text
        assert "Data snapshot (PubDate): 2026-08-02" in text


class TestSpendingStats:
    async def test_dedup_injected_into_query(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (
                    is_stats,
                    {
                        "features": [
                            feat(
                                Fiscal_Year="2023",
                                sum_Amount=792430985.65,
                                row_count=28044,
                            )
                        ]
                    },
                )
            ],
        )
        text = tool_text(
            await run_tool(
                plugin,
                "spending_stats",
                {"table": 0, "group_by": ["Fiscal_Year"]},
            )
        )
        stats_calls = [p for _, p in calls if "outStatistics" in p]
        assert stats_calls, "no statistics query issued"
        assert "Duplicate = 'No'" in stats_calls[0]["where"]
        assert stats_calls[0]["groupByFieldsForStatistics"] == "Fiscal_Year"
        entries = json.loads(stats_calls[0]["outStatistics"])
        assert entries[0]["statisticType"] == "sum"
        assert entries[0]["onStatisticField"] == "Amount"
        assert entries[1]["statisticType"] == "count"
        # Net labeling (trap 2.5) and money formatting.
        assert "Net sum of Amount" in text
        assert "NET" in text
        assert "$792,430,985.65" in text

    async def test_include_duplicates_drops_filter_and_warns(self, plugin):
        calls = install_client(plugin, routes=[(is_stats, {"features": []})])
        text = tool_text(
            await run_tool(
                plugin,
                "spending_stats",
                {"table": 0, "include_duplicates": True},
            )
        )
        stats_calls = [p for _, p in calls if "outStatistics" in p]
        assert "Duplicate" not in stats_calls[0]["where"]
        assert "WARNING -- DUPLICATES INCLUDED" in text

    async def test_revenue_fy2024_absence_stated(self, plugin):
        # T7 shape: grouped revenue years skip 2024 -> explicit notice.
        rows = [
            feat(Fiscal_Year=y, sum_Amount=1.0, row_count=1)
            for y in ("2022", "2023", "2025", "2026")
        ]
        install_client(plugin, routes=[(is_stats, {"features": rows})])
        text = tool_text(
            await run_tool(
                plugin,
                "spending_stats",
                {"table": 4, "group_by": ["Fiscal_Year"]},
            )
        )
        assert "FY2024 is ABSENT" in text
        assert "DATA GAP" in text

    async def test_table2_defaults_to_total_payroll_cost(self, plugin):
        # T16 shape: no Amount field, no error.
        calls = install_client(plugin, routes=[(is_stats, {"features": []})])
        tool_text(await run_tool(plugin, "spending_stats", {"table": 2}))
        entries = json.loads(
            [p for _, p in calls if "outStatistics" in p][0]["outStatistics"]
        )
        assert entries[0]["onStatisticField"] == "Total_Payroll_Cost"

    async def test_table2_rejects_amount_measure(self, plugin):
        result = await run_tool(
            plugin, "spending_stats", {"table": 2, "measure": "Amount"}
        )
        assert not result.success
        assert "Total_Payroll_Cost" in result.error_message

    async def test_procurement_outlier_flagged(self, plugin):
        install_client(
            plugin,
            routes=[
                (
                    is_stats,
                    {
                        "features": [
                            feat(
                                Fiscal_Year="2025",
                                sum_Amount=1178222185.0,
                                row_count=3990,
                            )
                        ]
                    },
                )
            ],
        )
        text = tool_text(
            await run_tool(
                plugin,
                "spending_stats",
                {"table": 3, "group_by": ["Fiscal_Year"]},
            )
        )
        assert "bulk encumbrance load" in text

    async def test_code_label_groups_split(self, plugin):
        install_client(
            plugin,
            routes=[
                (
                    is_stats,
                    {
                        "features": [
                            feat(Fund="141000 : Roads", sum_Amount=10.0, row_count=2)
                        ]
                    },
                )
            ],
        )
        text = tool_text(
            await run_tool(plugin, "spending_stats", {"table": 0, "group_by": ["Fund"]})
        )
        assert "Fund_code | Fund_label" in text
        assert "141000 | Roads" in text


class TestSearchByVendor:
    async def test_union_vendors_and_not_null(self, plugin):
        # T9 shape: the blocklist-bug regression at the tool level.
        rows = [
            feat(Vendor_Name=n, net_total=100.0, row_count=2)
            for n in ("IBEW Local Union 1547", "Credit Union 1", "UAA STUDENT UNION")
        ]
        calls = install_client(plugin, routes=[(is_stats, {"features": rows})])
        text = tool_text(
            await run_tool(plugin, "search_by_vendor", {"name_contains": "union"})
        )
        where = [p for _, p in calls if "outStatistics" in p][0]["where"]
        assert "Vendor_Name LIKE '%union%'" in where
        assert "Vendor_Name IS NOT NULL" in where  # trap 2.4
        assert "UPPER(" not in where  # backend LIKE is case-insensitive
        assert "Duplicate = 'No'" in where
        assert "IBEW Local Union 1547" in text
        assert "3 distinct spelling(s)" in text
        assert "NOT normalized" in text  # trap 2.9

    async def test_revenue_uses_customer_field(self, plugin):
        calls = install_client(plugin, routes=[(is_stats, {"features": []})])
        tool_text(
            await run_tool(
                plugin,
                "search_by_vendor",
                {"name_contains": "x", "table": 4},
            )
        )
        params = [p for _, p in calls if "outStatistics" in p][0]
        assert "Customer_Business_Name LIKE" in params["where"]
        assert params["groupByFieldsForStatistics"] == "Customer_Business_Name"

    async def test_tables_without_entity_error(self, plugin):
        result = await run_tool(
            plugin, "search_by_vendor", {"name_contains": "x", "table": 1}
        )
        assert not result.success
        assert "no vendor/payee field" in result.error_message


class TestTopVendors:
    async def test_excludes_null_and_refunds(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (
                    is_stats,
                    {
                        "features": [
                            feat(
                                Vendor_Name="Premera Blue Cross US Bank 1397",
                                net_total=58731838.5,
                                row_count=47,
                            )
                        ]
                    },
                )
            ],
        )
        text = tool_text(await run_tool(plugin, "top_vendors", {"fiscal_year": 2025}))
        where = [p for _, p in calls if "outStatistics" in p][0]["where"]
        assert "Vendor_Name IS NOT NULL" in where
        assert "Vendor_Name <> 'Refunds'" in where
        assert "Fiscal_Year = '2025'" in where
        assert "excluded" in text  # says so in the response
        assert "1 | Premera Blue Cross US Bank 1397 | $58,731,838.50 | 47" in text


class TestGetLineItems:
    async def test_truncation_bookended_with_true_total(self, plugin):
        rows = [
            feat(
                SourceFile="f",
                Fund="141000 : Roads",
                Location="DALLAS, TX ",
                Business_Area="Police Department",
                Fiscal_Year="2025",
                G_L_Account="540610 : Discounts Lost",
                Month_Fiscal_Period=3,
                Vendor_Name="X",
                Amount=1.0,
            )
        ]
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 12345}),
                (is_row_query, {"features": rows}),
            ],
        )
        text = tool_text(
            await run_tool(
                plugin,
                "get_line_items",
                {"table": 0, "fiscal_year": 2025, "limit": 1},
            )
        )
        assert text.count("**TRUNCATED:**") == 2  # 3.4 bookend
        assert "12,345" in text
        assert "Fund_code" in text and "G_L_Account_label" in text  # 2.7
        assert "MORE PAGES AVAILABLE" in text

    async def test_hard_cap_500_echoed(self, plugin):
        install_client(
            plugin,
            routes=[(is_count, {"count": 0}), (is_row_query, {"features": []})],
        )
        text = tool_text(
            await run_tool(plugin, "get_line_items", {"table": 0, "limit": 9999})
        )
        assert "limit=500 (requested 9999, clamped)" in text


class TestListFieldValues:
    async def test_periods_carry_adjustment_note(self, plugin):
        # T13 shape.
        rows = [feat(Month_Fiscal_Period=i) for i in range(1, 17)]
        install_client(plugin, routes=[(is_distinct, {"features": rows})])
        text = tool_text(
            await run_tool(
                plugin,
                "list_field_values",
                {"table": 0, "field": "Month_Fiscal_Period"},
            )
        )
        assert "ADJUSTMENT" in text
        assert "16 distinct value(s)" in text
        assert "count_distinct statistic is broken" in text

    async def test_code_label_split_in_values(self, plugin):
        rows = [feat(Fund="141000 : Roads"), feat(Fund="151000 : Fire SA")]
        install_client(plugin, routes=[(is_distinct, {"features": rows})])
        text = tool_text(
            await run_tool(plugin, "list_field_values", {"table": 0, "field": "Fund"})
        )
        assert "code | label" in text
        assert "141000 | Roads" in text

    async def test_measure_field_redirected_to_stats(self, plugin):
        result = await run_tool(
            plugin, "list_field_values", {"table": 0, "field": "Amount"}
        )
        assert not result.success
        assert "spending_stats" in result.error_message


class TestGetTableSchema:
    async def test_schema_marks_traps(self, plugin):
        # T14 shape.
        install_client(plugin, routes=[(is_distinct, {"features": []})])
        text = tool_text(await run_tool(plugin, "get_table_schema", {"table": 0}))
        fund_line = [ln for ln in text.splitlines() if ln.startswith("Fund |")][0]
        gl_line = [ln for ln in text.splitlines() if ln.startswith("G_L_Account |")][0]
        loc_line = [ln for ln in text.splitlines() if ln.startswith("Location |")][0]
        assert "'code : label'" in fund_line
        assert "'code : label'" in gl_line
        assert "billing city/state" in loc_line
        pub_line = [ln for ln in text.splitlines() if ln.startswith("PubDate |")][0]
        assert "NOT transaction time" in pub_line


class TestQueryCheckbook:
    async def test_pubdate_filter_rejected(self, plugin):
        # T12.
        result = await run_tool(
            plugin,
            "query_checkbook",
            {"table": 0, "where": "PubDate > DATE '2026-01-01'"},
        )
        assert not result.success
        assert "Fiscal_Year" in result.error_message
        assert "Month_Fiscal_Period" in result.error_message

    async def test_union_vendor_passes_dedup_still_injected(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 1}),
                (
                    is_row_query,
                    {"features": [feat(Vendor_Name="Credit Union 1", Amount=5.0)]},
                ),
            ],
        )
        text = tool_text(
            await run_tool(
                plugin,
                "query_checkbook",
                {"table": 0, "where": "Vendor_Name = 'Credit Union 1'"},
            )
        )
        row_calls = [p for _, p in calls if is_row_query("x/query", p)]
        assert "Duplicate = 'No'" in row_calls[0]["where"]
        assert "Credit Union 1" in text

    async def test_injection_rejected(self, plugin):
        result = await run_tool(
            plugin,
            "query_checkbook",
            {"table": 0, "where": "1=1 UNION SELECT * FROM x"},
        )
        assert not result.success

    async def test_field_typo_suggestion(self, plugin):
        result = await run_tool(
            plugin,
            "query_checkbook",
            {"table": 0, "where": "vendor_name = 'x'"},
        )
        assert not result.success
        assert "Vendor_Name" in result.error_message

    async def test_clamp_visible_in_provenance(self, plugin):
        # T15 shape.
        install_client(
            plugin,
            routes=[(is_count, {"count": 0}), (is_row_query, {"features": []})],
        )
        text = tool_text(
            await run_tool(
                plugin,
                "query_checkbook",
                {"table": 0, "where": "1=1", "limit": 5000},
            )
        )
        assert "limit=500 (requested 5000, clamped)" in text


# ── Lifecycle ─────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_initialize_captures_live_fields(self):
        p = AnchorageCheckbookPlugin({})
        meta = {
            "fields": [
                {"name": f, "type": "esriFieldTypeString"} for f in TABLES[0].all_fields
            ]
        }

        async def fake_get(url, params=None):
            return make_response(meta)

        import unittest.mock as um

        with um.patch("httpx.AsyncClient") as client_cls:
            client = Mock()
            client.get = AsyncMock(side_effect=fake_get)
            client.aclose = AsyncMock()
            client_cls.return_value = client
            assert await p.initialize()
        assert p.is_initialized
        assert p._live_fields[0] == set(TABLES[0].all_fields)

    async def test_initialize_survives_unreachable_service(self):
        import httpx as httpx_mod
        import unittest.mock as um

        p = AnchorageCheckbookPlugin({})
        with um.patch("httpx.AsyncClient") as client_cls:
            client = Mock()
            client.get = AsyncMock(side_effect=httpx_mod.ConnectError("down"))
            client.aclose = AsyncMock()
            client_cls.return_value = client
            assert await p.initialize()  # degraded > down
        assert p.is_initialized

    async def test_initialize_rejects_bad_config(self):
        p = AnchorageCheckbookPlugin({"service_url": "not-a-url"})
        assert not await p.initialize()

    async def test_shutdown(self, plugin):
        install_client(plugin)
        await plugin.shutdown()
        assert not plugin.is_initialized

    async def test_health_check(self, plugin):
        install_client(plugin, default={"fields": []})
        assert await plugin.health_check()
