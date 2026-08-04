"""Live acceptance tests for the Anchorage Open Checkbook plugin.

These are the work-order tests T1-T16, run against the REAL
MOA_OpenCheckbook_Hosted Feature Service. They are skipped unless
CHECKBOOK_LIVE_TESTS=1 so CI stays hermetic:

    CHECKBOOK_LIVE_TESTS=1 uv run pytest tests/test_anchorage_checkbook_acceptance.py -v

Tolerances: dollar figures shift slightly on each upstream ETL refresh,
so they assert within +/-2%. Row counts and structure assert exactly --
if an ETL refresh moves them, that is worth noticing (update the pinned
values after confirming the shift is a refresh, not a regression).

Pinned values verified live 2026-08-03 (PubDate snapshot 2026-08-02).

NOTE on T10: the work order labels 2,401 as the "pre-dedup" chugach row
count, but the live service shows LIKE '%chugach%' matches 2,648 rows
raw and 2,401 with Duplicate='No' -- i.e. 2,401 is the DEDUPLICATED
count (the work order's own §1 case-insensitivity check was evidently
run with the dedup filter applied). The assertions below encode the
live-verified semantics: default (deduped) -> 2,401; duplicates
included -> 2,648.
"""

import os
import re

import httpx
import pytest

from plugins.anchorage_checkbook.config_schema import AnchorageCheckbookPluginConfig
from plugins.anchorage_checkbook.plugin import AnchorageCheckbookPlugin

pytestmark = pytest.mark.skipif(
    os.environ.get("CHECKBOOK_LIVE_TESTS") != "1",
    reason="live acceptance tests hit the real MOA service; set CHECKBOOK_LIVE_TESTS=1",
)

DOLLAR_TOLERANCE = 0.02  # +/-2% per the work order


def close_enough(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= abs(expected) * DOLLAR_TOLERANCE


def parse_money(text: str) -> float:
    """'$792,430,985.65' or '-$1,234.00' -> float."""
    m = re.fullmatch(r"(-?)\$([\d,]+(?:\.\d+)?)", text.strip())
    assert m, f"not a money string: {text!r}"
    value = float(m.group(2).replace(",", ""))
    return -value if m.group(1) else value


def money_in_line(line: str) -> float:
    m = re.search(r"-?\$[\d,]+(?:\.\d+)?", line)
    assert m, f"no money in line: {line!r}"
    return parse_money(m.group(0))


@pytest.fixture
async def plugin():
    p = AnchorageCheckbookPlugin({})
    p.plugin_config = AnchorageCheckbookPluginConfig()
    p.client = httpx.AsyncClient(timeout=30)
    p._initialized = True
    yield p
    await p.client.aclose()


async def run(plugin, name, args):
    result = await plugin.execute_tool(name, args)
    assert result.success, f"{name} failed: {result.error_message}"
    return result.content[0]["text"]


# ── T1: list_tables ───────────────────────────────────────────────────


async def test_t1_list_tables(plugin):
    text = await run(plugin, "list_tables", {})
    assert text.count("### Table") == 6
    assert "### Table 6" not in text  # OC_Point never exposed as a table
    table5 = text.split("### Table 5")[1]
    assert "Status: EMPTY" in table5


# ── T2/T3: table 0 duplicate split ────────────────────────────────────


async def test_t2_table0_dedup_count(plugin):
    assert await plugin._fetch_count(0, "Duplicate = 'No'") == 306575


async def test_t3_table0_duplicates_are_fy2023(plugin):
    assert await plugin._fetch_count(0, "Duplicate = 'Yes'") == 28044
    years = await plugin._fetch_distinct_values(
        0, "Fiscal_Year", where="Duplicate = 'Yes'"
    )
    assert years == ["2023"]
    rows = await plugin._query_statistics(
        0,
        "Duplicate = 'Yes'",
        [
            {
                "statisticType": "sum",
                "onStatisticField": "Amount",
                "outStatisticFieldName": "dup_sum",
            }
        ],
    )
    assert close_enough(rows[0]["dup_sum"], 792_430_986)


# ── T4: revenue duplicates span FY2023 AND FY2026 ─────────────────────


async def test_t4_revenue_duplicates_two_years(plugin):
    """Regression guard against implementing dedup as an FY2023 filter."""
    assert await plugin._fetch_count(4, "Duplicate = 'Yes'") == 24272
    years = await plugin._fetch_distinct_values(
        4, "Fiscal_Year", where="Duplicate = 'Yes'"
    )
    assert years == ["2023", "2026"]


# ── T5: spending_stats by year proves dedup is applied ────────────────


async def test_t5_spending_stats_by_year(plugin):
    text = await run(
        plugin, "spending_stats", {"table": 0, "group_by": ["Fiscal_Year"]}
    )
    year_lines = [ln for ln in text.splitlines() if re.match(r"^20\d\d \|", ln)]
    assert len(year_lines) == 9  # 2018-2026
    fy2023 = [ln for ln in year_lines if ln.startswith("2023 |")][0]
    # ~$792M, NOT ~$1.58B -- proves Duplicate='No' is injected.
    assert close_enough(money_in_line(fy2023), 792_430_986)


# ── T6: 20 business areas ─────────────────────────────────────────────


async def test_t6_business_area_groups(plugin):
    text = await run(
        plugin, "spending_stats", {"table": 0, "group_by": ["Business_Area"]}
    )
    assert "(20 group(s)" in text


# ── T7: revenue FY2024 absence is stated ──────────────────────────────


async def test_t7_revenue_fy2024_absent(plugin):
    text = await run(
        plugin, "spending_stats", {"table": 4, "group_by": ["Fiscal_Year"]}
    )
    assert not any(ln.startswith("2024 |") for ln in text.splitlines())
    assert "FY2024 is ABSENT" in text  # says so explicitly


# ── T8: procurement FY2025 outlier ────────────────────────────────────


async def test_t8_procurement_fy2025_outlier(plugin):
    text = await run(
        plugin, "spending_stats", {"table": 3, "group_by": ["Fiscal_Year"]}
    )
    fy2025 = [ln for ln in text.splitlines() if ln.startswith("2025 |")][0]
    assert close_enough(money_in_line(fy2025), 1_178_222_185)
    assert "bulk encumbrance load" in text  # flagged as an outlier


# ── T9: union vendors (blocklist regression guard) ────────────────────


async def test_t9_union_vendors_searchable(plugin):
    text = await run(plugin, "search_by_vendor", {"name_contains": "union"})
    m = re.search(r"(\d+) distinct spelling\(s\)", text)
    assert m and int(m.group(1)) >= 14
    assert "IBEW Local Union 1547" in text
    assert "Credit Union 1" in text


async def test_t9b_union_vendor_through_escape_hatch(plugin):
    text = await run(
        plugin,
        "query_checkbook",
        {"table": 0, "where": "Vendor_Name = 'Credit Union 1'", "limit": 5},
    )
    assert "Credit Union 1" in text


# ── T10: chugach counts, case-insensitive ─────────────────────────────


async def test_t10_chugach_counts(plugin):
    # See module docstring: 2,401 is the DEDUPLICATED count (live-
    # verified); the raw count is 2,648.
    text = await run(plugin, "search_by_vendor", {"name_contains": "chugach"})
    assert re.search(r"across 2,401 rows", text)
    # Case-insensitive without UPPER(): same result for 'CHUGACH'.
    text_upper = await run(plugin, "search_by_vendor", {"name_contains": "CHUGACH"})
    assert re.search(r"across 2,401 rows", text_upper)
    # Pre-dedup (duplicates included) with the warning attached.
    text_dup = await run(
        plugin,
        "search_by_vendor",
        {"name_contains": "chugach", "include_duplicates": True},
    )
    assert re.search(r"across 2,648 rows", text_dup)
    assert "WARNING -- DUPLICATES INCLUDED" in text_dup


# ── T11: top vendors FY2025 ───────────────────────────────────────────


async def test_t11_top_vendors_2025(plugin):
    text = await run(plugin, "top_vendors", {"fiscal_year": 2025})
    rank1 = [ln for ln in text.splitlines() if ln.startswith("1 |")][0]
    assert "Premera Blue Cross US Bank 1397" in rank1
    assert close_enough(money_in_line(rank1), 58_731_839)
    # No NULL payee row anywhere in the ranking.
    rank_lines = [ln for ln in text.splitlines() if re.match(r"^\d+ \|", ln)]
    assert all("| None |" not in ln and "|  |" not in ln for ln in rank_lines)


# ── T12: PubDate date filters rejected ────────────────────────────────


async def test_t12_pubdate_filter_rejected(plugin):
    result = await plugin.execute_tool(
        "query_checkbook",
        {"table": 0, "where": "PubDate > DATE '2026-01-01'"},
    )
    assert not result.success
    assert "Fiscal_Year" in result.error_message
    assert "Month_Fiscal_Period" in result.error_message


# ── T13: fiscal periods 1-16 with adjustment note ─────────────────────


async def test_t13_period_values(plugin):
    text = await run(
        plugin,
        "list_field_values",
        {"table": 0, "field": "Month_Fiscal_Period"},
    )
    listed = {
        int(m.group(1)) for m in re.finditer(r"^- (\d+)$", text, flags=re.MULTILINE)
    }
    assert listed == set(range(1, 17))
    assert "ADJUSTMENT" in text  # 13-16 are not months


# ── T14: schema marks code:label and the Location trap ────────────────


async def test_t14_table0_schema(plugin):
    text = await run(plugin, "get_table_schema", {"table": 0})
    lines = {ln.split(" | ")[0]: ln for ln in text.splitlines() if " | " in ln}
    assert "'code : label'" in lines["Fund"]
    assert "'code : label'" in lines["G_L_Account"]
    assert "billing city/state" in lines["Location"]


# ── T15: clamp is visible in the provenance line ──────────────────────


async def test_t15_clamp_visible(plugin):
    text = await run(
        plugin,
        "query_checkbook",
        {"table": 0, "where": "Amount > 100000000", "limit": 5000},
    )
    assert "limit=500 (requested 5000, clamped)" in text


# ── T16: table 2 aggregates without an Amount field ───────────────────


async def test_t16_payroll_rollup_measure(plugin):
    text = await run(
        plugin, "spending_stats", {"table": 2, "group_by": ["Fiscal_Year"]}
    )
    assert "Total_Payroll_Cost" in text  # no missing-Amount error


# ── Sanity anchor: all-years net sum on table 0 ───────────────────────


async def test_sanity_anchor_total_net(plugin):
    text = await run(plugin, "spending_stats", {"table": 0})
    total_line = [ln for ln in text.splitlines() if ln.startswith("net_sum_Amount")]
    assert total_line, text
    value_line = text.splitlines()[text.splitlines().index(total_line[0]) + 1]
    assert close_enough(money_in_line(value_line), 9_140_437_359)
