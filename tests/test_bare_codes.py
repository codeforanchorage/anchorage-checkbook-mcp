"""FY2026 stores Fund / G_L_Account / Business_Area as bare codes.

Every earlier year stores ``'101000 : Areawide General'`` and ``'Police
Department'``; FY2026 stores ``'101000'``, ``'0000200050'`` and ``'4000'``.
Before these tests' fix that produced three silent wrong answers at the
year boundary, each reproduced live on prod at the 2026-11-16 snapshot:

* ``business_area="Fire"`` with ``fiscal_year=2026`` matched nothing;
* grouping table 0 by Business_Area listed 35 "departments" for 20;
* FY2026 rows rendered a code in the ``_label`` column and no code.

The unit suite mocks HTTP, so the shapes below are the real stored forms
copied from the service, and the maps are the real vendored ones.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from plugins.anchorage_checkbook.config_schema import AnchorageCheckbookPluginConfig
from plugins.anchorage_checkbook.plugin import (
    TABLES,
    AnchorageCheckbookPlugin,
)


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
    p._pubdate_cache = {i: "2026-11-16" for i in TABLES}
    return p


def gaps(structured):
    return {c.get("gap") for c in structured["caveats"] if c.get("gap")}


class TestVendoredMap:
    def test_business_area_crosswalk_is_one_to_one(self, plugin):
        ba = plugin._code_labels("Business_Area")
        assert ba["4000"] == "Police Department"
        assert ba["3000"] == "Fire Department"
        assert ba["8700"] == "Water Utility"
        assert len(set(ba.values())) == len(ba)

    def test_fund_and_gl_codes_are_unpadded(self, plugin):
        assert plugin._code_labels("Fund")["101000"] == "Areawide General"
        gl = plugin._code_labels("G_L_Account")
        assert gl["200050"] == "Goods Received/Invoice Received"
        assert not any(code.startswith("0") for code in gl)

    def test_missing_map_degrades_to_the_old_behaviour(self, plugin, tmp_path):
        plugin.CODE_LABELS_PATH = tmp_path / "absent.json"
        assert plugin._code_labels("Fund") == {}
        # No map: the filter is the plain LIKE it always was, and a bare
        # code is still recognised as a code rather than as a label.
        assert plugin._coded_contains_clause("Fund", "Roads") == ("Fund LIKE '%Roads%'")
        assert plugin._split_coded("Fund", "101000") == ("101000", None)


class TestSplit:
    def test_both_storage_forms_split_identically(self, plugin):
        old = plugin._split_coded("Fund", "101000 : Areawide General")
        new = plugin._split_coded("Fund", "101000")
        assert old == new == ("101000", "Areawide General")

    def test_zero_padded_gl_code(self, plugin):
        assert plugin._split_coded("G_L_Account", "0000200050") == (
            "200050",
            "Goods Received/Invoice Received",
        )

    def test_unknown_code_is_never_guessed(self, plugin):
        assert plugin._split_coded("Fund", "999999") == ("999999", None)
        assert plugin._canonical_coded("Fund", "999999") == "999999"
        assert plugin._canonical_coded("Business_Area", "9999") == "9999"

    def test_names_and_labels_pass_through(self, plugin):
        assert plugin._canonical_coded("Business_Area", "Police Department") == (
            "Police Department"
        )
        assert plugin._canonical_coded("Business_Area", "4000") == "Police Department"
        assert plugin._canonical_coded("Fund", None) is None
        # A digit-only value in a field that is not coded is left alone.
        assert plugin._canonical_coded("Fiscal_Year", "2026") == "2026"


class TestFilters:
    def test_label_filter_reaches_fy2026(self, plugin):
        clause = plugin._coded_contains_clause("Business_Area", "Fire")
        assert clause == "Business_Area LIKE '%Fire%' OR Business_Area IN ('3000')"

    def test_gl_filter_covers_padded_and_unpadded(self, plugin):
        clause = plugin._coded_contains_clause("G_L_Account", "Gasoline Issues")
        assert "G_L_Account LIKE '%Gasoline Issues%'" in clause
        assert "'511330'" in clause and "'0000511330'" in clause

    def test_code_fragment_needs_no_expansion(self, plugin):
        # LIKE '%141000%' already matches both forms.
        assert plugin._coded_contains_clause("Fund", "141000") == (
            "Fund LIKE '%141000%'"
        )

    def test_or_is_parenthesised_when_combined(self, plugin):
        clauses, _ = plugin._structured_where(
            TABLES[0], {"fiscal_year": 2026, "business_area": "Fire"}
        )
        where = plugin._combine_where("Duplicate = 'No'", *clauses)
        assert "(Business_Area LIKE '%Fire%' OR Business_Area IN ('3000'))" in where, (
            where
        )

    async def test_long_filter_goes_by_post(self, plugin):
        response = Mock(status_code=200)
        response.json = Mock(return_value={"count": 1})
        plugin.client = Mock()
        plugin.client.get = AsyncMock(return_value=response)
        plugin.client.post = AsyncMock(return_value=response)
        short = {"f": "json", "where": "1=1"}
        long = {"f": "json", "where": "x" * (plugin.MAX_GET_PARAM_CHARS + 1)}
        await plugin._request_json("https://example.com/0/query", short)
        await plugin._request_json("https://example.com/0/query", long)
        plugin.client.get.assert_awaited_once()
        plugin.client.post.assert_awaited_once()
        assert plugin.client.post.await_args.kwargs["data"] == long


class TestGrouping:
    async def _stats(self, plugin, rows, **args):
        with patch.object(
            plugin, "_query_statistics", AsyncMock(return_value=rows)
        ) as query:
            text, structured = await plugin._spending_stats({"table": 0, **args})
        return text, structured, query

    async def test_one_department_is_one_group(self, plugin):
        rows = [
            {"Business_Area": "Fire Department", "sum_Amount": 100.0, "row_count": 3},
            {"Business_Area": "3000", "sum_Amount": 40.0, "row_count": 2},
            {"Business_Area": "Port", "sum_Amount": 120.0, "row_count": 1},
        ]
        text, structured, _ = await self._stats(
            plugin, rows, group_by=["Business_Area"]
        )
        assert structured["rows"] == [
            {
                "group": {"Business_Area": "Fire Department"},
                "value": 140.0,
                "row_count": 5,
            },
            {"group": {"Business_Area": "Port"}, "value": 120.0, "row_count": 1},
        ]
        assert structured["summary"]["groups"] == 2
        assert "fy2026_bare_codes" in gaps(structured)
        assert "3000" not in text.split("---")[0].split("Business_Area |")[1]

    async def test_merge_happens_before_the_limit(self, plugin):
        """Each half ranks below 'Port'; together they rank above it."""
        rows = [
            {"Fund": "570000 : Port", "sum_Amount": 90.0, "row_count": 1},
            {"Fund": "101000 : Areawide General", "sum_Amount": 60.0, "row_count": 1},
            {"Fund": "101000", "sum_Amount": 50.0, "row_count": 1},
        ]
        _, structured, query = await self._stats(
            plugin, rows, group_by=["Fund"], limit=1
        )
        assert structured["rows"][0]["group"] == {"Fund": "101000 : Areawide General"}
        assert structured["rows"][0]["value"] == 110.0
        assert structured["summary"]["truncated"] is True
        # Every group was fetched, not just the top `limit`.
        sent = query.await_args.kwargs["extra_params"]["resultRecordCount"]
        assert sent == str(plugin.SERVER_PAGE_SIZE)

    async def test_avg_is_recomputed_not_averaged(self, plugin):
        rows = [
            {
                "Fund": "101000 : Areawide General",
                "avg_Amount": 10.0,
                "row_count": 9,
                "merge_sum": 90.0,
                "merge_n": 9,
            },
            {
                "Fund": "101000",
                "avg_Amount": 110.0,
                "row_count": 1,
                "merge_sum": 110.0,
                "merge_n": 1,
            },
        ]
        _, structured, _ = await self._stats(
            plugin, rows, group_by=["Fund"], stat_type="avg"
        )
        assert structured["rows"] == [
            {
                "group": {"Fund": "101000 : Areawide General"},
                "value": 20.0,  # 200 / 10, not (10 + 110) / 2
                "row_count": 10,
            }
        ]

    async def test_min_and_max_merge(self, plugin):
        for stat, expected in (("min", -5.0), ("max", 7.0)):
            rows = [
                {"Fund": "101000 : Areawide General", f"{stat}_Amount": 7.0},
                {"Fund": "101000", f"{stat}_Amount": -5.0},
            ]
            for row in rows:
                row["row_count"] = 1
            _, structured, _ = await self._stats(
                plugin, rows, group_by=["Fund"], stat_type=stat
            )
            assert [r["value"] for r in structured["rows"]] == [expected]

    async def test_percentile_is_left_unmerged_and_says_so(self, plugin):
        rows = [
            {
                "Fund": "101000 : Areawide General",
                "percentile_cont_Amount": 10.0,
                "row_count": 9,
            },
            {"Fund": "101000", "percentile_cont_Amount": 30.0, "row_count": 1},
        ]
        text, structured, _ = await self._stats(
            plugin, rows, group_by=["Fund"], stat_type="percentile_cont"
        )
        assert len(structured["rows"]) == 2
        assert "fy2026_bare_codes_unmerged" in gaps(structured)
        for caveat in structured["caveats"]:
            assert caveat["message"] in text

    async def test_incomplete_fetch_is_not_merged(self, plugin):
        """A full server page means more groups exist; a partial merge
        would under-report, so the rows are only relabelled."""
        rows = [
            {"Fund": "101000", "sum_Amount": 1.0, "row_count": 1}
            for _ in range(plugin.SERVER_PAGE_SIZE)
        ]
        _, structured, _ = await self._stats(plugin, rows, group_by=["Fund"], limit=3)
        assert len(structured["rows"]) == 3
        assert structured["summary"]["truncated"] is True

    async def test_ungrouped_or_uncoded_groupings_are_untouched(self, plugin):
        rows = [{"Fiscal_Year": "2026", "sum_Amount": 5.0, "row_count": 1}]
        _, structured, query = await self._stats(
            plugin, rows, group_by=["Fiscal_Year"], limit=7
        )
        assert query.await_args.kwargs["extra_params"] == {"resultRecordCount": "7"}
        assert "fy2026_bare_codes" not in gaps(structured)


class TestRowsAndValues:
    async def test_line_items_resolve_codes(self, plugin):
        records = [
            {
                "Fund": "101000",
                "Business_Area": "4000",
                "G_L_Account": "0000200050",
                "Fiscal_Year": "2026",
                "Amount": 10.0,
            }
        ]
        with (
            patch.object(
                plugin, "_query_table", AsyncMock(return_value=(records, False))
            ),
            patch.object(plugin, "_fetch_count", AsyncMock(return_value=1)),
        ):
            text, structured = await plugin._get_line_items(
                {"table": 0, "fiscal_year": 2026}
            )
        row = structured["rows"][0]
        assert row["Fund_code"] == "101000"
        assert row["Fund_label"] == "Areawide General"
        assert row["G_L_Account_code"] == "200050"
        assert row["Business_Area"] == "Police Department"
        assert "fy2026_bare_codes" in gaps(structured)
        for caveat in structured["caveats"]:
            assert caveat["message"] in text

    async def test_older_rows_carry_no_caveat(self, plugin):
        records = [
            {
                "Fund": "101000 : Areawide General",
                "Business_Area": "Police Department",
                "Fiscal_Year": "2025",
                "Amount": 10.0,
            }
        ]
        with (
            patch.object(
                plugin, "_query_table", AsyncMock(return_value=(records, False))
            ),
            patch.object(plugin, "_fetch_count", AsyncMock(return_value=1)),
        ):
            _, structured = await plugin._get_line_items(
                {"table": 0, "fiscal_year": 2025}
            )
        assert "fy2026_bare_codes" not in gaps(structured)

    async def test_field_values_list_each_code_once(self, plugin):
        values = ["4000", "8700", "Police Department", "Water Utility", "CIVIC"]
        with patch.object(
            plugin, "_fetch_distinct_values", AsyncMock(return_value=values)
        ):
            _, structured = await plugin._list_field_values(
                {"table": 0, "field": "Business_Area"}
            )
        assert [v["value"] for v in structured["values"]] == [
            "Police Department",
            "Water Utility",
            "CIVIC",
        ]
        assert structured["summary"]["returned"] == 3
        assert "fy2026_bare_codes" in gaps(structured)


class TestNewKnownGaps:
    """Two load errors the Duplicate flag does not mark (2026-11-16)."""

    def test_payroll_fy2023_is_double_loaded(self, plugin):
        for table in (1, 2):
            notes = plugin._fiscal_notices(TABLES[table], ["2023"])
            assert "payroll_fy2023_double_loaded" in {n["gap"] for n in notes}
        notes = plugin._fiscal_notices(TABLES[1], ["2025"])
        assert "payroll_fy2023_double_loaded" not in {n["gap"] for n in notes}
        notes = plugin._fiscal_notices(TABLES[0], ["2023"])
        assert "payroll_fy2023_double_loaded" not in {n["gap"] for n in notes}

    def test_revenue_fy2026_carries_the_2024_file(self, plugin):
        gap = "revenue_fy2026_includes_2024_file"
        for years in (["2026"], ["2024"], None):
            notes = plugin._fiscal_notices(TABLES[4], years)
            assert gap in {n["gap"] for n in notes}
        notes = plugin._fiscal_notices(TABLES[4], ["2025"])
        assert gap not in {n["gap"] for n in notes}
