"""Structured output for the six analytic/record tools.

A declared ``outputSchema`` is binding: the MCP spec says servers MUST
return structured results that conform to it and clients SHOULD
validate. These tests therefore validate REAL tool output against the
REAL declared schema rather than eyeballing shapes, and they do it on
the awkward branches -- no matches, a negative net sum, a non-string
group key, an adjustment period, a known-gap year, a partial year, a
vendor with several spellings, a capped result set.

The other theme here is that this is a FINANCE server: every caveat
below changes what a dollar figure MEANS, so each is asserted to reach
BOTH channels from the same source.
"""

import json
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest

from plugins.anchorage_checkbook.config_schema import AnchorageCheckbookPluginConfig
from plugins.anchorage_checkbook.plugin import TABLES, AnchorageCheckbookPlugin


@pytest.fixture
def plugin():
    cfg = {
        "enabled": True,
        "service_url": "https://example.com/arcgis/rest/services/X/FeatureServer",
        "city_name": "Municipality of Anchorage",
        "timeout": 20,
    }
    p = AnchorageCheckbookPlugin(cfg)
    p.plugin_config = AnchorageCheckbookPluginConfig(**cfg)
    p._pubdate_cache = {i: "2026-08-02" for i in TABLES}
    return p


def validate(structured, schema):
    """Validate and return, so tests read as one expression."""
    jsonschema.validate(instance=structured, schema=schema)
    return structured


def schema_for(plugin, tool_name):
    return {t.name: t.output_schema for t in plugin.get_tools()}[tool_name]


def stat_rows(rows):
    """Fake an ArcGIS outStatistics response."""
    return rows


# ── spending_stats ──────────────────────────────────────────────────


class TestSpendingStatsStructured:
    async def _run(self, plugin, args, rows):
        with patch.object(plugin, "_query_statistics", AsyncMock(return_value=rows)):
            return await plugin._spending_stats(args)

    @pytest.mark.asyncio
    async def test_grouped_sum_conforms(self, plugin):
        text, structured = await self._run(
            plugin,
            {"table": 0, "group_by": ["Business_Area"], "fiscal_year": 2025},
            [
                {"Business_Area": "Police", "sum_Amount": 1234.5, "row_count": 10},
                {"Business_Area": "Fire", "sum_Amount": 99.0, "row_count": 2},
            ],
        )
        validate(structured, schema_for(plugin, "spending_stats"))

        assert structured["summary"]["stat_type"] == "sum"
        assert structured["summary"]["measure"] == "Amount"
        assert structured["rows"][0]["group"] == {"Business_Area": "Police"}
        assert structured["rows"][0]["value"] == 1234.5
        # The stat type lives in the structured half, not only the prose,
        # so a caller can never read a percentile as a sum.
        assert "stat_type" in structured["summary"]
        assert text

    @pytest.mark.asyncio
    async def test_no_matches_still_conforms(self, plugin):
        """The trap: a zero-result branch that advertises a schema and
        then returns nothing is a conformance break, and happy-path
        tests never see it."""
        _text, structured = await self._run(
            plugin, {"table": 0, "fiscal_year": 2019}, []
        )
        validate(structured, schema_for(plugin, "spending_stats"))

        assert structured["rows"] == []
        assert structured["summary"]["groups"] == 0
        assert structured["caveats"], "an empty result is still qualified"

    @pytest.mark.asyncio
    async def test_negative_net_sum_is_legal(self, plugin):
        """Single rows run from about -$749M to +$743M. A `minimum: 0`
        anywhere in this schema would make the server violate it on real
        data."""
        _text, structured = await self._run(
            plugin,
            {"table": 0, "group_by": ["Fund"]},
            [{"Fund": "100 : General", "sum_Amount": -749_000_000.0, "row_count": 3}],
        )
        validate(structured, schema_for(plugin, "spending_stats"))
        assert structured["rows"][0]["value"] < 0

    @pytest.mark.asyncio
    async def test_non_string_group_key_is_legal(self, plugin):
        """Group values are raw field values, not necessarily strings."""
        _text, structured = await self._run(
            plugin,
            {"table": 0, "group_by": ["Month_Fiscal_Period"]},
            [{"Month_Fiscal_Period": 3, "sum_Amount": 5.0, "row_count": 1}],
        )
        validate(structured, schema_for(plugin, "spending_stats"))
        assert structured["rows"][0]["group"]["Month_Fiscal_Period"] == 3

    @pytest.mark.asyncio
    async def test_adjustment_period_is_flagged_not_named(self, plugin):
        """Period 14 is a year-end adjustment, not February."""
        text, structured = await self._run(
            plugin,
            {"table": 0, "group_by": ["Month_Fiscal_Period"]},
            [
                {"Month_Fiscal_Period": 14, "sum_Amount": 5.0, "row_count": 1},
                {"Month_Fiscal_Period": 2, "sum_Amount": 7.0, "row_count": 1},
            ],
        )
        validate(structured, schema_for(plugin, "spending_stats"))

        adj = [c for c in structured["caveats"] if c["code"] == "ADJUSTMENT_PERIOD"]
        assert adj, "period 13-16 in the result must raise ADJUSTMENT_PERIOD"
        assert adj[0]["periods"] == [14]
        assert adj[0]["message"] in text
        # No month name anywhere in the structured half.
        blob = json.dumps(structured).lower()
        for month in ("january", "february", "march", "april"):
            assert month not in blob

    @pytest.mark.asyncio
    async def test_revenue_fy2024_gap_is_a_caveat_not_a_zero(self, plugin):
        """A missing year reads as $0 unless the server says otherwise."""
        _text, structured = await self._run(
            plugin,
            {"table": 4, "group_by": ["Fiscal_Year"]},
            [
                {"Fiscal_Year": "2023", "sum_Amount": 10.0, "row_count": 1},
                {"Fiscal_Year": "2025", "sum_Amount": 12.0, "row_count": 1},
            ],
        )
        validate(structured, schema_for(plugin, "spending_stats"))

        gaps = [c for c in structured["caveats"] if c["code"] == "KNOWN_GAP"]
        assert any("fy2024" in c.get("gap", "") for c in gaps)

    @pytest.mark.asyncio
    async def test_procurement_fy2025_outlier_is_flagged(self, plugin):
        _text, structured = await self._run(
            plugin,
            {"table": 3, "fiscal_year": 2025},
            [{"sum_Amount": 1.18e9, "row_count": 5}],
        )
        validate(structured, schema_for(plugin, "spending_stats"))
        assert any(
            c.get("gap") == "procurement_fy2025_outlier" for c in structured["caveats"]
        )

    @pytest.mark.asyncio
    async def test_partial_fy2026_is_flagged(self, plugin):
        _text, structured = await self._run(
            plugin,
            {"table": 0, "fiscal_year": 2026},
            [{"sum_Amount": 1.0, "row_count": 1}],
        )
        validate(structured, schema_for(plugin, "spending_stats"))
        assert any(c.get("gap") == "fy2026_partial" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_percentile_reports_itself(self, plugin):
        _text, structured = await self._run(
            plugin,
            {"table": 0, "stat_type": "percentile_cont", "percentile": 0.5},
            [{"percentile_cont_Amount": 42.0, "row_count": 9}],
        )
        validate(structured, schema_for(plugin, "spending_stats"))
        assert structured["summary"]["stat_type"] == "percentile_cont"
        assert structured["summary"]["percentile"] == 0.5


# ── vendor tools ────────────────────────────────────────────────────


class TestVendorStructured:
    async def _search(self, plugin, args, rows):
        with patch.object(plugin, "_query_statistics", AsyncMock(return_value=rows)):
            return await plugin._search_by_vendor(args)

    @pytest.mark.asyncio
    async def test_multiple_spellings_stay_separate_rows(self, plugin):
        """Merging spellings is the caller's judgement call, not ours."""
        _text, structured = await self._search(
            plugin,
            {"name_contains": "transunion"},
            [
                {"Vendor_Name": "TLO TRANSUNION", "net_total": 100.0, "row_count": 2},
                {
                    "Vendor_Name": "TRANSUNION SHAREAB",
                    "net_total": 50.0,
                    "row_count": 1,
                },
                {
                    "Vendor_Name": "TransUnion Risk and Alternative Data Solutions Inc",
                    "net_total": 25.0,
                    "row_count": 1,
                },
            ],
        )
        validate(structured, schema_for(plugin, "search_by_vendor"))

        names = [r["name"] for r in structured["rows"]]
        assert len(names) == 3, "distinct spellings are never merged"
        # Raw spellings, exactly as stored.
        assert "TLO TRANSUNION" in names
        assert structured["summary"]["spellings"] == 3
        # No derived/normalized key is emitted at all.
        for row in structured["rows"]:
            assert set(row) <= {"rank", "name", "net_sum", "row_count"}
        assert any(
            c["code"] == "VENDOR_SPELLING_VARIANTS" for c in structured["caveats"]
        )

    @pytest.mark.asyncio
    async def test_no_such_vendor_still_conforms(self, plugin):
        """A spelling that does not exist is one of the commonest queries
        this tool gets."""
        _text, structured = await self._search(
            plugin, {"name_contains": "zzzznotavendor"}, []
        )
        validate(structured, schema_for(plugin, "search_by_vendor"))

        assert structured["rows"] == []
        assert structured["summary"]["spellings"] == 0
        # null, not 0: no spelling matched, so there is no sum to report.
        assert structured["summary"]["combined_net_sum"] is None
        assert structured["caveats"]

    @pytest.mark.asyncio
    async def test_no_bare_total_field_anywhere(self, plugin):
        """Every figure is NET; the field name has to carry that."""
        _text, structured = await self._search(
            plugin,
            {"name_contains": "chugach"},
            [{"Vendor_Name": "CHUGACH ELECTRIC", "net_total": 10.0, "row_count": 1}],
        )
        for row in structured["rows"]:
            assert "total" not in row
            assert "net_sum" in row
        assert "combined_net_sum" in structured["summary"]

    @pytest.mark.asyncio
    async def test_capped_spelling_list_says_so(self, plugin):
        rows = [
            {"Vendor_Name": f"V{i}", "net_total": 1.0, "row_count": 1} for i in range(5)
        ]
        _text, structured = await self._search(
            plugin, {"name_contains": "v", "limit": 5}, rows
        )
        validate(structured, schema_for(plugin, "search_by_vendor"))
        assert structured["summary"]["truncated"] is True

    @pytest.mark.asyncio
    async def test_top_vendors_is_ranked_and_warns_about_grain(self, plugin):
        with patch.object(
            plugin,
            "_query_statistics",
            AsyncMock(
                return_value=[
                    {"Vendor_Name": "A", "net_total": 9.0, "row_count": 3},
                    {"Vendor_Name": "B", "net_total": 4.0, "row_count": 1},
                ]
            ),
        ):
            _text, structured = await plugin._top_vendors({"table": 0, "n": 2})

        validate(structured, schema_for(plugin, "top_vendors"))
        assert [r["rank"] for r in structured["rows"]] == [1, 2]
        # A ranking by spelling is not a ranking by entity, and the tool
        # points at the one that answers correctly.
        variants = [
            c for c in structured["caveats"] if c["code"] == "VENDOR_SPELLING_VARIANTS"
        ]
        assert variants
        assert "search_by_vendor" in variants[0]["message"]


# ── record tools ────────────────────────────────────────────────────


class TestRecordRowsStructured:
    async def _line_items(self, plugin, args, records, total):
        with (
            patch.object(
                plugin, "_query_table", AsyncMock(return_value=(records, False))
            ),
            patch.object(plugin, "_fetch_count", AsyncMock(return_value=total)),
        ):
            return await plugin._get_line_items(args)

    @pytest.mark.asyncio
    async def test_rows_conform_and_split_billing_location(self, plugin):
        _text, structured = await self._line_items(
            plugin,
            {"table": 0, "limit": 2},
            [
                {
                    "Vendor_Name": "A",
                    "Amount": -12.5,
                    "Location": "LEESBURG, VA ",
                    "Month_Fiscal_Period": 3,
                    "Fund": "100 : General",
                }
            ],
            1,
        )
        validate(structured, schema_for(plugin, "get_line_items"))

        row = structured["rows"][0]
        # Location is the VENDOR'S billing address, so it is named for
        # what it is -- nobody should build a map out of it.
        assert row["billing_city"] == "LEESBURG"
        assert row["billing_state"] == "VA"
        assert "Location" not in row
        # Negative amounts are real.
        assert row["Amount"] == -12.5
        assert any(c["code"] == "LOCATION_IS_BILLING" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_zero_matches_has_total_count_zero_not_null(self, plugin):
        """0 is a known, complete count. null means unmeasured. Conflating
        them makes a complete answer look like a sample."""
        _text, structured = await self._line_items(
            plugin, {"table": 0, "fiscal_year": 2019}, [], 0
        )
        validate(structured, schema_for(plugin, "get_line_items"))

        assert structured["rows"] == []
        assert structured["summary"]["total_count"] == 0
        assert structured["summary"]["total_count"] is not None
        assert structured["summary"]["truncated"] is False

    @pytest.mark.asyncio
    async def test_unmeasured_count_stays_null(self, plugin):
        """When the count query fails, null is the honest answer."""
        _text, structured = await self._line_items(
            plugin, {"table": 0}, [{"Vendor_Name": "A", "Amount": 1.0}], None
        )
        validate(structured, schema_for(plugin, "get_line_items"))
        assert structured["summary"]["total_count"] is None

    @pytest.mark.asyncio
    async def test_query_checkbook_conforms(self, plugin):
        with (
            patch.object(
                plugin,
                "_query_table",
                AsyncMock(return_value=([{"Vendor_Name": "A", "Amount": 3.0}], False)),
            ),
            patch.object(plugin, "_fetch_count", AsyncMock(return_value=1)),
        ):
            _text, structured = await plugin._query_checkbook(
                {"table": 0, "where": "Amount > 100"}
            )

        validate(structured, schema_for(plugin, "query_checkbook"))
        assert "Amount > 100" in structured["query"]["where"]

    @pytest.mark.asyncio
    async def test_structured_rows_are_not_clipped_by_the_text_table(self, plugin):
        """Anything the text clips for readability ships whole in the
        machine-readable channel."""
        records = [{"Vendor_Name": f"V{i}", "Amount": float(i)} for i in range(200)]
        _text, structured = await self._line_items(
            plugin, {"table": 0, "limit": 200}, records, 200
        )
        assert len(structured["rows"]) == 200


# ── list_field_values ───────────────────────────────────────────────


class TestFieldValuesStructured:
    async def _run(self, plugin, args, values):
        with patch.object(
            plugin, "_fetch_distinct_values", AsyncMock(return_value=values)
        ):
            return await plugin._list_field_values(args)

    @pytest.mark.asyncio
    async def test_coded_field_carries_code_and_label(self, plugin):
        _text, structured = await self._run(
            plugin,
            {"table": 0, "field": "Fund"},
            ["141000 : Anchorage Roads & Drainage SA", "100000 : General"],
        )
        validate(structured, schema_for(plugin, "list_field_values"))

        first = structured["values"][0]
        assert first["code"] == "141000"
        assert first["label"] == "Anchorage Roads & Drainage SA"

    @pytest.mark.asyncio
    async def test_period_values_flag_adjustment_periods(self, plugin):
        _text, structured = await self._run(
            plugin, {"table": 0, "field": "Month_Fiscal_Period"}, [1, 12, 13, 16]
        )
        validate(structured, schema_for(plugin, "list_field_values"))

        flags = {v["value"]: v["is_adjustment_period"] for v in structured["values"]}
        assert flags[1] is False
        assert flags[12] is False
        assert flags[13] is True
        assert flags[16] is True
        assert any(c["code"] == "ADJUSTMENT_PERIOD" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_empty_field_still_conforms(self, plugin):
        _text, structured = await self._run(plugin, {"table": 5, "field": "Fund"}, [])
        validate(structured, schema_for(plugin, "list_field_values"))
        assert structured["values"] == []
        assert structured["summary"]["returned"] == 0


# ── cross-cutting invariants ────────────────────────────────────────


class TestStructuredAndTextCannotDrift:
    """Both halves are rendered from ONE caveat list."""

    @pytest.mark.asyncio
    async def test_every_structured_caveat_message_appears_in_the_text(self, plugin):
        with patch.object(
            plugin,
            "_query_statistics",
            AsyncMock(
                return_value=[
                    {"Fiscal_Year": "2026", "sum_Amount": -5.0, "row_count": 1}
                ]
            ),
        ):
            text, structured = await plugin._spending_stats(
                {"table": 4, "group_by": ["Fiscal_Year"]}
            )

        assert structured["caveats"], "expected qualifications on this query"
        for caveat in structured["caveats"]:
            assert caveat["message"] in text, (
                f"caveat {caveat['code']} is in the structured half but not "
                f"the prose -- the two channels have drifted"
            )


class TestDuplicateFilterIsAlwaysDeclared:
    """The one field that explains why these numbers differ from the
    published dashboard's."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args,expected",
        [
            ({"table": 0}, "excluded"),
            ({"table": 0, "include_duplicates": True}, "included"),
        ],
    )
    async def test_query_block_states_the_filter_either_way(
        self, plugin, args, expected
    ):
        with (
            patch.object(plugin, "_query_table", AsyncMock(return_value=([], False))),
            patch.object(plugin, "_fetch_count", AsyncMock(return_value=0)),
        ):
            _text, structured = await plugin._get_line_items(args)

        validate(structured, schema_for(plugin, "get_line_items"))
        assert structured["query"]["duplicate_filter"] == expected

    def test_it_is_a_required_schema_field(self, plugin):
        for tool in plugin.get_tools():
            if not tool.output_schema:
                continue
            query_schema = tool.output_schema["properties"]["query"]
            assert "duplicate_filter" in query_schema["required"], tool.name


class TestSchemaDeclarations:
    def test_only_the_six_data_tools_declare_a_schema(self, plugin):
        declared = {t.name for t in plugin.get_tools() if t.output_schema}
        assert declared == {
            "spending_stats",
            "search_by_vendor",
            "top_vendors",
            "get_line_items",
            "list_field_values",
            "query_checkbook",
        }

    def test_discovery_tools_declare_none(self, plugin):
        """list_tables and get_table_schema exist for their prose
        guidance; a schema would commit them to a shape whose value is
        the narrative."""
        by_name = {t.name: t for t in plugin.get_tools()}
        assert by_name["list_tables"].output_schema is None
        assert by_name["get_table_schema"].output_schema is None

    def test_no_amount_field_forbids_negatives(self, plugin):
        """Single rows run to about -$749M. Any `minimum: 0` on a money
        field would make the server violate its own schema."""
        money_fields = {
            "value",
            "net_sum",
            "combined_net_sum",
            "Amount",
        }

        def walk(node, key=None):
            if isinstance(node, dict):
                if key in money_fields and "minimum" in node:
                    raise AssertionError(
                        f"{key} declares minimum={node['minimum']}; net "
                        f"amounts in this dataset are legitimately negative"
                    )
                for k, v in node.items():
                    walk(v, k)
            elif isinstance(node, list):
                for item in node:
                    walk(item, key)

        for tool in plugin.get_tools():
            if tool.output_schema:
                walk(tool.output_schema)
