"""Ways this server can be confidently wrong about money.

Each test here corresponds to a defect reproduced against the live
service before it was fixed. The shared property is that the WRONG output
looked exactly as trustworthy as the right one: a dash where there should
be an absence, a blank cell that ranks first, a count whose grain is not
what the reader assumes.
"""

from unittest.mock import AsyncMock, patch

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
    p._pubdate_cache = {i: "2026-08-24" for i in TABLES}
    return p


def codes(structured):
    return {c["code"] for c in structured["caveats"]}


class TestFalseZeros:
    """ "Not recorded that way" must never render as a finding of zero.

    Reproduced live before the fix: spending_stats(table=0,
    fiscal_year=2011) returned a table reading `-- | 0` with no
    indication that nothing had matched. A model asked "how much did MOA
    spend in 2011?" would read that as an answer.
    """

    async def _stats(self, plugin, args, rows):
        with patch.object(plugin, "_query_statistics", AsyncMock(return_value=rows)):
            return await plugin._spending_stats(args)

    @pytest.mark.asyncio
    async def test_no_matching_rows_says_absent_not_zero(self, plugin):
        text, structured = await self._stats(
            plugin,
            {"table": 0, "fiscal_year": 2011},
            [{"sum_Amount": None, "row_count": 0}],
        )

        assert "NO ROWS MATCHED" in text
        assert "ABSENT DATA" in text
        assert "not $0" in text
        assert "NO_ROWS_MATCHED" in codes(structured)

    @pytest.mark.asyncio
    async def test_a_year_miss_points_at_the_tool_that_answers(self, plugin):
        text, _structured = await self._stats(
            plugin,
            {"table": 4, "fiscal_year": 2024},
            [{"sum_Amount": None, "row_count": 0}],
        )
        assert "list_field_values" in text
        assert "Fiscal_Year" in text

    @pytest.mark.asyncio
    async def test_a_vendor_miss_points_at_search_by_vendor(self, plugin):
        """The commonest false zero on this dataset: the entity exists,
        under a spelling the caller did not guess."""
        text, _structured = await self._stats(
            plugin,
            {"table": 0, "vendor_contains": "TRANSUNION SHAREABLE"},
            [{"sum_Amount": None, "row_count": 0}],
        )
        assert "search_by_vendor" in text
        assert "SHORTER" in text
        assert "not normalized" in text.lower()

    @pytest.mark.asyncio
    async def test_a_real_result_carries_no_false_zero_caveat(self, plugin):
        _text, structured = await self._stats(
            plugin,
            {"table": 0, "fiscal_year": 2025},
            [{"sum_Amount": 1000.0, "row_count": 5}],
        )
        assert "NO_ROWS_MATCHED" not in codes(structured)

    @pytest.mark.asyncio
    async def test_row_tools_say_it_too(self, plugin):
        with (
            patch.object(plugin, "_query_table", AsyncMock(return_value=([], False))),
            patch.object(plugin, "_fetch_count", AsyncMock(return_value=0)),
        ):
            text, structured = await plugin._get_line_items(
                {"table": 0, "fiscal_year": 2011}
            )
        assert "ABSENT DATA" in text
        assert "NO_ROWS_MATCHED" in codes(structured)

    @pytest.mark.asyncio
    async def test_vendor_search_miss_is_not_a_finding_of_zero(self, plugin):
        with patch.object(plugin, "_query_statistics", AsyncMock(return_value=[])):
            text, structured = await plugin._search_by_vendor(
                {"name_contains": "zzzznotavendor"}
            )
        assert "ABSENT DATA" in text
        assert "SHORTER" in text
        assert "NO_ROWS_MATCHED" in codes(structured)

    @pytest.mark.asyncio
    async def test_top_vendors_miss_is_not_a_finding_of_zero(self, plugin):
        with patch.object(plugin, "_query_statistics", AsyncMock(return_value=[])):
            text, structured = await plugin._top_vendors(
                {"table": 0, "fiscal_year": 2011}
            )
        assert "ABSENT DATA" in text
        assert "NO_ROWS_MATCHED" in codes(structured)


class TestNullPayeeIsNotAVendor:
    """Reproduced live: grouping table 0 by Vendor_Name for FY2025 put
    the NULL bucket first at $642,635,280.85 across 5,651 rows, rendered
    as a BLANK cell. Read straight off, that is "the top vendor in
    Anchorage received $642.6M" -- and it is not a vendor at all.
    """

    async def _grouped(self, plugin, rows):
        with patch.object(plugin, "_query_statistics", AsyncMock(return_value=rows)):
            return await plugin._spending_stats(
                {"table": 0, "group_by": ["Vendor_Name"], "fiscal_year": 2025}
            )

    @pytest.mark.asyncio
    async def test_null_group_is_labelled_not_blank(self, plugin):
        text, structured = await self._grouped(
            plugin,
            [
                {"Vendor_Name": None, "sum_Amount": 642_635_280.85, "row_count": 5651},
                {"Vendor_Name": "Premera", "sum_Amount": 58_731_838.5, "row_count": 47},
            ],
        )

        assert "(no payee -- journal entries, fund transfers, accounting lines)" in text
        assert "NULL_PAYEE_GROUP" in codes(structured)
        assert "must never be reported as a vendor" in text
        # And the tool that gives a correct ranking is named.
        assert "top_vendors" in text

    @pytest.mark.asyncio
    async def test_the_money_is_not_silently_dropped(self, plugin):
        """Labelling it is right; excluding it would lose $642M of real
        spending from an answer about total spending."""
        _text, structured = await self._grouped(
            plugin,
            [
                {"Vendor_Name": None, "sum_Amount": 642_635_280.85, "row_count": 5651},
            ],
        )
        assert structured["rows"][0]["value"] == pytest.approx(642_635_280.85)
        # Raw null preserved in the structured half -- the label is a
        # rendering concern, not a data change.
        assert structured["rows"][0]["group"]["Vendor_Name"] is None

    @pytest.mark.asyncio
    async def test_no_null_group_means_no_caveat(self, plugin):
        _text, structured = await self._grouped(
            plugin, [{"Vendor_Name": "Premera", "sum_Amount": 5.0, "row_count": 1}]
        )
        assert "NULL_PAYEE_GROUP" not in codes(structured)


class TestCountingGrain:
    """A count is only meaningful if the reader knows what it counts."""

    @pytest.mark.asyncio
    async def test_the_count_column_names_its_grain(self, plugin):
        """It was `rows`, which reads as "how many vendors"."""
        with patch.object(
            plugin,
            "_query_statistics",
            AsyncMock(
                return_value=[
                    {"Business_Area": "Police", "sum_Amount": 5.0, "row_count": 12}
                ]
            ),
        ):
            text, _structured = await plugin._spending_stats(
                {"table": 0, "group_by": ["Business_Area"]}
            )

        assert "line_items" in text

    @pytest.mark.asyncio
    async def test_entity_groups_are_reported_as_spellings(self, plugin):
        """Grouping by the payee field counts SPELLINGS, and an entity
        split across several ranks below its true total."""
        with patch.object(
            plugin,
            "_query_statistics",
            AsyncMock(
                return_value=[
                    {
                        "Vendor_Name": "TLO TRANSUNION",
                        "sum_Amount": 4092.0,
                        "row_count": 10,
                    },
                    {
                        "Vendor_Name": "TRANSUNION SHAREAB",
                        "sum_Amount": 550.0,
                        "row_count": 16,
                    },
                ]
            ),
        ):
            text, structured = await plugin._spending_stats(
                {"table": 0, "group_by": ["Vendor_Name"]}
            )

        assert "spelling(s) of the payee field" in text
        assert "line item(s)" in text
        assert "VENDOR_SPELLING_VARIANTS" in codes(structured)
        assert "search_by_vendor" in text

    @pytest.mark.asyncio
    async def test_non_entity_groups_are_not_called_spellings(self, plugin):
        with patch.object(
            plugin,
            "_query_statistics",
            AsyncMock(
                return_value=[
                    {"Business_Area": "Police", "sum_Amount": 5.0, "row_count": 1}
                ]
            ),
        ):
            text, structured = await plugin._spending_stats(
                {"table": 0, "group_by": ["Business_Area"]}
            )
        assert "spelling(s) of the payee field" not in text
        assert "VENDOR_SPELLING_VARIANTS" not in codes(structured)


class TestNullRendering:
    """A blank cell and an empty string are different facts."""

    def test_null_renders_as_a_marker_not_an_empty_cell(self, plugin):
        assert plugin._table_cell(None) == "--"
        assert plugin._table_cell("") == ""

    def test_a_null_row_is_visibly_null(self, plugin):
        lines = plugin._format_table(
            [{"Vendor_Name": None, "Location": "", "Amount": 5}], 0
        )
        # header + one row
        assert lines[1].startswith("-- | ")


class TestQualificationsTravelWithTheNumber:
    """A caveat three paragraphs above a figure is a caveat a model can
    miss. These are rendered ahead of the table, in the same response."""

    @pytest.mark.asyncio
    async def test_procurement_outlier_precedes_its_own_numbers(self, plugin):
        with patch.object(
            plugin,
            "_query_statistics",
            AsyncMock(
                return_value=[
                    {"Vendor_Name": "Manson", "net_total": 8.08e8, "row_count": 1}
                ]
            ),
        ):
            text, structured = await plugin._top_vendors(
                {"table": 3, "fiscal_year": 2025, "n": 3}
            )

        assert "KNOWN_GAP" in codes(structured)
        outlier = next(
            c
            for c in structured["caveats"]
            if c.get("gap") == "procurement_fy2025_outlier"
        )
        # Stated BEFORE the figures it qualifies.
        assert text.index(outlier["message"]) < text.index("Manson")

    @pytest.mark.asyncio
    async def test_net_ness_is_in_the_column_name(self, plugin):
        """Not just in a note -- in the label attached to the number."""
        with patch.object(
            plugin,
            "_query_statistics",
            AsyncMock(return_value=[{"sum_Amount": 5.0, "row_count": 1}]),
        ):
            text, _structured = await plugin._spending_stats({"table": 0})
        assert "net_sum_Amount" in text
