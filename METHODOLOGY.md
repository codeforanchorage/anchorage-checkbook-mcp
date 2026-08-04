# Methodology: why this server's totals differ from the MOA dashboard

This server is a read-only MCP interface to the Municipality of
Anchorage Open Checkbook Feature Service. It makes one deliberate
methodological choice that the official MOA app does not: **duplicate
rows are excluded by default.** This document records what that means,
why we chose it, and how to reproduce both sets of numbers.

## The data problem

The upstream ETL that publishes the Open Checkbook has loaded entire
fiscal years twice. The second load is not a merge or a replace — it
lands as a complete set of shadow rows, exact copies of the originals,
distinguishable only by the `Duplicate` column (`'No'` for originals,
`'Yes'` for the second load).

Verified live against the service (2026-08-03, PubDate snapshot
2026-08-02) — for each affected year, the sum of the `'Yes'` rows
equals the sum of the `'No'` rows to the cent:

| Table | Duplicate rows | Fiscal years affected | Duplicate $ |
| ----- | -------------- | --------------------- | ----------- |
| 0 Non-payroll expenditures | 28,044 | FY2023 | $792,430,986 |
| 1 Payroll expenditures | 9,496 | FY2023 | $423,380,749 |
| 2 Payroll rollup | 14 | FY2023 | $423,380,749 |
| 3 Procurement | 3,642 | FY2023 | $316,568,561 |
| 4 Revenue | 20,110 + 4,162 | **FY2023 AND FY2026** | $3,029,908,147 + $983,428,780 |

A naive `SUM(Amount) GROUP BY Fiscal_Year` therefore reports FY2023
non-payroll spending as ~$1.58B when the corrected figure is ~$792M —
silently, with no error.

## The two defensible positions

**The official MOA Open Checkbook app** exposes the `Duplicate` column
and applies no server-side filter. The user decides. That is a
defensible position for a dashboard where a human sees the column in
the grid and can toggle it.

**This server filters `Duplicate = 'No'` into every query by
default.** That is the right position for an MCP server, where the
consumer is a language model composing totals on a user's behalf: the
model may never look at the flag column, the user never sees the raw
grid, and a silently doubled year is exactly the kind of error that
survives into a published number. Defaults should be correct; opting
into raw data should be explicit.

## Consequences

1. **Totals from this server will NOT match the public dashboard**
   unless the dashboard user also filters the `Duplicate` column. The
   server states this in its MCP `instructions`, in tool descriptions,
   and in `list_tables` output.
2. The escape stays open: every data tool takes
   `include_duplicates=True`, which disables the filter and prepends a
   warning banner to the response.
3. The filter is on the **flag**, never on a fiscal year. Revenue is
   duplicated in FY2023 *and* FY2026, which proves the double-load is
   a recurring ETL failure mode, not a one-time FY2023 event. If a
   future year is double-loaded (and flagged), the default handles it
   with no code change. Acceptance test T4 guards this.

## Known residual risk

The approach trusts the upstream flag. If a future double-load arrives
*unflagged* (both copies marked `'No'`), the filter cannot catch it.
The live acceptance suite pins known-good per-year figures (±2%), so a
doubled year in a fresh ETL drop would show up as a test failure; a
cheaper ongoing guard would be a periodic check for any year whose
total lands within ~1% of exactly double a prior snapshot.

## Reproducing both numbers

Corrected (this server's default):

```
.../MOA_OpenCheckbook_Hosted/FeatureServer/0/query
  ?where=Fiscal_Year='2023' AND Duplicate='No'
  &outStatistics=[{"statisticType":"sum","onStatisticField":"Amount","outStatisticFieldName":"s"}]
→ $792,430,986
```

Dashboard-equivalent (no filter):

```
  ?where=Fiscal_Year='2023'
→ $1,584,861,971 (doubled)
```

## Other corrections applied by default (same philosophy)

- `PubDate` is an ETL snapshot stamp (one identical value on every
  row), not transaction time. Date filters on it are **rejected** with
  a redirect to `Fiscal_Year` + `Month_Fiscal_Period`; the snapshot
  date is surfaced once per response as provenance.
- All dollar figures are labeled **net**: the tables contain huge
  offsetting entries (single rows from −$749M to +$743M), so gross
  totals are meaningless.
- Vendor-facing tools exclude NULL payees (journal entries, fund
  transfers) and never merge distinct vendor spellings — the same
  entity appears under multiple names upstream, and merging them
  silently would be a data claim this server has no basis to make.
