# WORK ORDER — anchorage-checkbook-mcp

> **Kickoff line to paste into Claude Code:**
> Read CHECKBOOK_MCP_TASK.md and implement it. This repo is a clone of
> anchorage-parcels-mcp; retarget it at the MOA Open Checkbook FeatureServer.
> Work through Phases 1–5 in order. Do not skip Phase 2 — those traps are the
> entire point of this server. Stop and show me the diff after each phase.

---

## 0. Context — READ THIS BEFORE TOUCHING ANY FILE

This repo is a fork of `codeforanchorage/anchorage-parcels-mcp`, which is really
**OpenContext** — a plugin framework (MIT, by Srihari Raman, City of Boston DoIT).
The framework's own doctrine is **One Fork = One MCP Server**: each deployment enables
exactly one plugin.

So the job here is **additive, not destructive**:

- **CREATE** `plugins/anchorage_checkbook/` — a new plugin, modeled on
  `plugins/anchorage_parcels/plugin.py`.
- **CREATE** `config-anchorage-checkbook.yaml` — modeled on
  `config-anchorage-parcels.yaml`.
- **SET** `anchorage_checkbook.enabled: true` and `anchorage_parcels.enabled: false`
  (and `ckan`, `arcgis` false) in that config.
- **DO NOT** modify `core/`, `plugins/ckan/`, `plugins/arcgis/`, or
  `plugins/anchorage_parcels/`. Those are shared, reusable, and other cities fork them.
  Anything the checkbook plugin needs that the parcels plugin has, **copy** into the
  new plugin — do not refactor the parcels plugin to share it.
- There is **no spatial machinery to delete**, because you are not editing the parcels
  plugin. Simply do not implement any: no geometry params, no `outSR`/`inSR`, no
  point-in-polygon. This service has no geometry.

Read `docs/ARCHITECTURE.md`, `docs/PARCELS.md`, and `CLAUDE.md` first. Follow the
existing plugin contract exactly — tool registration, config loading, and error
handling all come from `core/`.

Backing app (for reference only — do not scrape it):
`https://experience.arcgis.com/experience/42e4cb81516840549d521b0665385bb1/page/Non-payroll-Expenditures`

---

## 1. Data source — VERIFIED LIVE 2026-08-03

**Service root**
```
https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/MOA_OpenCheckbook_Hosted/FeatureServer
```
Org ID `Ce3DhLRthdwbHlfF`. Public, no auth. `capabilities: Query,Extract`.

**Service limits (confirmed):**
- `maxRecordCount: 2000`, `standardMaxRecordCount: 32000`, `tileMaxRecordCount: 8000`
- `supportsPagination: true`, `supportsStatistics: true`, `supportsOrderBy: true`,
  `supportsDistinct: true`, `supportsHavingClause: true`, `supportsPercentileStatistics: true`
- **`count_distinct` is advertised (`supportsCountDistinct: true`) but ERRORS in
  practice.** Do not use it. Use `returnDistinctValues=true` and count client-side.
- `LIKE` is **case-insensitive** on this backend (verified: `'%chugach%'` and
  `UPPER(...) LIKE '%CHUGACH%'` both return 2,401 rows). Do not wrap in `UPPER()`.

**Tables**

| id | name | rows | notes |
|----|------|------|-------|
| 0 | `OC_UnauditedExpenditure_NonPayroll` | 334,619 | **primary table** — the linked app page |
| 1 | `OC_UnauditedExpenditure_Payroll` | 89,011 | no `Vendor_Name` |
| 2 | `OC_UnauditedPayroll` | 151 | dept×year rollup; **no `Amount` field** |
| 3 | `OC_UnauditedProcurement` | 42,653 | has `Purchase_Order`, `PO_Description`, `Process_Type` |
| 4 | `OC_UnauditedRevenue` | 93,137 | uses `Customer_Business_Name`, not `Vendor_Name` |
| 5 | `OC_UnaudRev_vs_Exp` | **0** | empty. Expose in `list_tables` as `status: empty` |
| 6 | `OC_Point` | **0** | placeholder so Experience Builder can mount a map widget. **Do not expose.** |

**Fields**

- `[0]` `OBJECTID, SourceFile, Fund, Location, Duplicate, Business_Area, Fiscal_Year, G_L_Account, Month_Fiscal_Period, Vendor_Name, PubDate, Amount`
- `[1]` `OBJECTID, SourceFile, Business_Area, Fiscal_Year, Fund, G_L_Account, Month_Fiscal_Period, Duplicate, PubDate, Amount`
- `[2]` `OBJECTID, SourceFile, Business_Area, Fiscal_Year, Duplicate, PubDate, Liabilities_Benefits, Overtime, Salaries_Wages, Total_Payroll_Cost`
- `[3]` `OBJECTID, SourceFile, Location, Duplicate, Business_Area, Fiscal_Year, Month_Fiscal_Period, PO_Description, Process_Type, Purchase_Order, Vendor_Name, PubDate, Amount`
- `[4]` `OBJECTID, SourceFile, Business_Area, Customer_Business_Name, Fiscal_Year, Fund, G_L_Account, Month_Fiscal_Period, Duplicate, PubDate, Amount`

**Do not hardcode a single `Amount` field name.** Table 2 has none — its measures are
`Total_Payroll_Cost`, `Salaries_Wages`, `Overtime`, `Liabilities_Benefits`. Build a
per-table config map: `{table_id: {measure_fields, dimension_fields, entity_field}}`.

---

## 2. Traps — MANDATORY, these are the reason this server exists

Every one of these is verified live. A generic ArcGIS wrapper gets all of them wrong;
encoding them in tool behavior and tool descriptions is this server's whole value.

### 2.1 The `Duplicate` double-load
Inject `Duplicate='No'` into **every** query by default, on every table. Expose an
`include_duplicates: bool = False` param that must be set explicitly to disable it.
When a caller sets it True, prepend a warning to the response.

Verified duplicate sets (exact shadow copies — the `Yes` sum equals the `No` sum for
the same year):

| table | duplicate rows | fiscal years affected | duplicate $ |
|---|---|---|---|
| 0 NonPayroll | 28,044 | FY2023 | $792,430,986 |
| 1 ExpPayroll | 9,496 | FY2023 | $423,380,749 |
| 2 PayrollRollup | 14 | FY2023 | $423,380,749 |
| 3 Procurement | 3,642 | FY2023 | $316,568,561 |
| 4 Revenue | 20,110 + 4,162 | **FY2023 AND FY2026** | $3,029,908,147 + $983,428,780 |

**Do not implement this as an FY2023 filter.** Revenue proves the double-load recurs.
Filter on the flag, always.

### 2.2 `Month_Fiscal_Period` runs 1–16
Periods 13–16 are year-end adjustment periods, not calendar months. Never map period
number to a month name. Never build a date-range filter on it. If a tool exposes
period filtering, its description must say periods 13–16 are adjustments.

### 2.3 `PubDate` is a snapshot stamp, not transaction time
Single value across all 334,619 rows (currently `2026-08-02`). It is when the ETL ran.
**Reject any date filter on `PubDate`** with an error that redirects the caller to
`Fiscal_Year` + `Month_Fiscal_Period`. Surface the value once, as provenance.

### 2.4 `Vendor_Name` NULLs
59,877 NULL rows in table 0 (54,329 after dedup) — journal entries, fund transfers,
accounting lines. Stored as true NULL. Any vendor-facing tool must add
`Vendor_Name IS NOT NULL`. `'Refunds'` (3,860 rows) is a real non-vendor label; leave
it in but note it.

### 2.5 Amounts are NET and include huge offsetting entries
Table 0 `Amount` ranges **−$749,447,668 to +$742,679,431**. Gross totals are
meaningless. Every stats response must label the figure **net**. Do not build a
"largest transactions" tool without an attached caveat about offsetting entries.

### 2.6 `Location` is the vendor's city/state, not a municipal location
Values look like `'LEESBURG, VA '`, `'DALLAS, TX '`, and junk such as
`'(208) 454-055, ID '`. Trailing whitespace is common. **Build no geographic tool on
this field.** The field description must say explicitly that it is the vendor's
billing city and is not MOA geography.

### 2.7 Coded fields are `code : label` strings
`Fund = "141000 : Anchorage Roads & Drainage SA"`,
`G_L_Account = "540610 : Discounts Lost"`. Split on `" : "` and return both `code` and
`label` as separate keys. `Business_Area` is a plain label — 20 values, no code:
BRU Gas Operations, CIVIC, Clearing BA Cash Pool, Clearing BA Payroll, Development
Services, Disposal, Electric, Fire Department, General Government, Health and Human
Services, Hydroelectric, Merrill Field, Parks & Recreation, Police Department, Port,
Public Transportation, Public Works, Refuse, Wastewater Utility, Water Utility.

### 2.8 Known completeness gaps — put these in tool descriptions
- **Revenue has no FY2024 at all.** Years present: 2018–2023, 2025, 2026.
- **Procurement FY2025 = $1,178,222,185** vs $310–530M in every other year. Likely a
  bulk encumbrance load. Flag it in any Procurement time series.
- FY2026 is partial (through fiscal period 7). Never compare FY2026 to a full year
  without saying so.

### 2.9 Vendor name normalization is not done upstream
The same entity appears under multiple spellings (`TLO TRANSUNION`,
`TRANSUNION SHAREAB`, `TransUnion Risk and Alternative Data Solutions Inc`). Do **not**
silently merge them. `search_by_vendor` returns distinct spellings and the tool
description states that vendor names are unnormalized, so a single-string total may
undercount an entity.

---

## 3. Patterns NOT to copy from the parcels plugin

You are writing a new plugin, so these are things to deliberately do differently when
you crib from `plugins/anchorage_parcels/plugin.py`. Do not "fix" them in the parcels
plugin as part of this task — if any look worth fixing there too, list them at the end
and I'll open separate issues.

### 3.1 The `WHERE` sanitizer's keyword blocklist (CRITICAL)
The parcels plugin rejects any `where` containing the substring `UNION`, plus `;` and
`--`. On parcels this was a minor false positive. **On Checkbook it breaks real
queries**: 68 rows across 14 distinct vendors contain "UNION", including
`IBEW Local Union 1547`, `Credit Union 1`, `Plumbers & Steamfitters Union Local 367`,
`United Food and Commercial Workers Union Local No.1496`, `UAA STUDENT UNION`,
`TransUnion Risk and Alternative Data Solutions Inc`.

Replace the substring blocklist with either:
- (preferred) structured params — callers pass `business_area`, `fund`,
  `fiscal_year`, `vendor_contains` and the server composes the WHERE with proper
  escaping (double single-quotes), never string-concatenating raw user SQL; or
- a word-boundary regex (`\bUNION\b\s+\bSELECT\b`) that targets the actual injection
  shape rather than the token.

Keep `query_checkbook` as an escape hatch, but route it through the same escaping,
and cap it.

### 3.2 Silent limit clamping
The parcels fork clamps `limit` without telling anyone. Follow the **eBird pattern**:
echo the effective limit in the response's provenance line, e.g.
`Query: table=0, where=..., limit=1000 (requested 5000, clamped)`.

### 3.3 Verbose `Record N:` block format
Replace with a compact table. Checkbook rows are 12 short fields; the parcels format
costs ~30 bytes/record of pure formatting. Use pipe-delimited rows with a single
header line.

### 3.4 Truncation must be bookended
Follow the **Census pattern**: when results are capped, emit the `TRUNCATED` warning
at both the top and the bottom of the response, and state the true total row count
from a `returnCountOnly` call so the caller knows what they are missing.

---

## 4. Tool surface — 8 tools

### 4.0 First: the `instructions:` block in the config

`config-anchorage-parcels.yaml` carries an `instructions:` block that OpenContext
returns in the MCP `initialize` response — it reaches every consumer before any tool is
called. **This is the single highest-leverage place to put the Section 2 traps.** Write
it before writing the tools.

It must state, in the model-agnostic voice used by the parcels config:
- All figures are **net** of offsetting entries; gross totals are meaningless here.
- Duplicate rows are filtered by default; totals will therefore **not match the public
  MOA Open Checkbook dashboard** unless that user also filters `Duplicate`.
- The time axis is `Fiscal_Year` + `Month_Fiscal_Period` (1–16, where 13–16 are year-end
  adjustments). `PubDate` is an ETL snapshot stamp, not transaction time.
- Vendor names are unnormalized; one entity may appear under several spellings.
- `Location` is the vendor's billing city/state, **not** MOA geography.
- Revenue has no FY2024; FY2026 is partial; Procurement FY2025 is an outlier.
- Aggregate questions are `spending_stats` calls, not record listings — with a worked
  example, mirroring the `parcel_stats` percentile example in the parcels config.
- All tools are READ-ONLY and safe to call without confirmation.
- This server covers **only** Open Checkbook financial tables. Route parcel/assessment
  questions to the Anchorage Parcels MCP and spatial questions to the Anchorage GIS MCP.

### 4.1 Tools

The parcels plugin's `parcel_stats` already supports
`stat_type` of `count/sum/avg/min/max/stddev/var/percentile_cont` with `stat_field` and
`group_by`. Reuse that shape for `spending_stats` rather than inventing a new one.

Every tool's response ends with a provenance line: service URL, table id, effective
WHERE (including the injected `Duplicate='No'`), row count, clamp status, and
`PubDate` snapshot date.

1. **`list_tables`** — the six real tables with row counts, measure fields, dimension
   fields, and per-table caveats (Revenue missing FY2024; Payroll rollup has no
   `Amount`; table 5 empty). Omit table 6. Zero args. This is the discovery entry
   point since there is no GIS-style discovery server in front of this service.

2. **`get_table_schema(table)`** — fields, types, whether the field is `code : label`,
   and cardinality via `returnDistinctValues` (never `count_distinct`).

3. **`spending_stats(table, group_by[], fiscal_year?, business_area?, fund?, vendor_contains?, measure?, order?, limit?)`**
   — the workhorse. Server-side `groupByFieldsForStatistics` with sum + count.
   Multi-field group_by allowed. Splits `code : label` in output. Labels the result
   **net**. Direct descendant of `parcel_stats`.

4. **`search_by_vendor(name_contains, table=0, fiscal_year?, limit=50)`** — descendant
   of `search_by_owner`. Adds `Vendor_Name IS NOT NULL`. No `UPPER()` (backend is
   case-insensitive). Returns distinct spellings with per-spelling net totals and row
   counts. Description carries the normalization caveat from §2.9.
   On table 4, the entity field is `Customer_Business_Name` — handle via the per-table
   config map, do not special-case in the tool body.

5. **`top_vendors(fiscal_year?, business_area?, table=0, n=20)`** — convenience wrapper
   over `spending_stats`. Excludes NULL and `'Refunds'` by default, says so in the
   response.

6. **`get_line_items(table, filters..., limit=100)`** — descendant of
   `get_parcel_details`. Raw rows, compact table format, hard cap at 500 with the
   bookended truncation warning.

7. **`list_field_values(table, field, limit=100)`** — distinct values for
   `Business_Area`, `Fund`, `G_L_Account`, `Process_Type`. Uses `returnDistinctValues`.
   Splits `code : label`.

8. **`query_checkbook(table, where, out_fields?, limit=200)`** — escape hatch. Properly
   escaped per §3.1, capped, and it still injects `Duplicate='No'` unless
   `include_duplicates=True`.

---

## 5. Acceptance tests — exact expected values, verified 2026-08-03

Write these as real tests. Figures shift slightly on each ETL refresh, so assert with
a ±2% tolerance on dollars and exact match on row counts and structure.

```
T1  list_tables                          -> 6 tables, table 6 absent, table 5 status=empty
T2  count table 0, Duplicate='No'        -> 306,575 rows
T3  count table 0, Duplicate='Yes'       -> 28,044 rows, all FY2023, $792,430,986
T4  count table 4, Duplicate='Yes'       -> 24,272 rows across FY2023 AND FY2026
                                            (regression guard against FY2023-only filtering)
T5  spending_stats(0, group_by=Fiscal_Year)
                                         -> 9 years 2018-2026; FY2023 = $792,430,986
                                            (NOT ~$1.58B — proves dedup is applied)
T6  spending_stats(0, group_by=Business_Area)
                                         -> 20 groups
T7  spending_stats(4, group_by=Fiscal_Year)
                                         -> FY2024 ABSENT; response says so explicitly
T8  spending_stats(3, group_by=Fiscal_Year)
                                         -> FY2025 = $1,178,222,185, flagged as an outlier
T9  search_by_vendor("union")            -> succeeds, >=14 distinct spellings incl.
                                            "IBEW Local Union 1547", "Credit Union 1"
                                            (REGRESSION GUARD for the blocklist bug)
T10 search_by_vendor("chugach")          -> 2,401 rows pre-dedup; case-insensitive
T11 top_vendors(fiscal_year=2025)        -> #1 "Premera Blue Cross US Bank 1397"
                                            ~$58,731,839; no NULL row present
T12 any tool with a PubDate date filter  -> rejected with a message pointing at
                                            Fiscal_Year + Month_Fiscal_Period
T13 list_field_values(0,"Month_Fiscal_Period")
                                         -> 1..16, description notes 13-16 = adjustments
T14 get_table_schema(0)                  -> Fund and G_L_Account marked as code:label;
                                            Location described as vendor billing city
T15 query_checkbook(limit=5000)          -> clamped AND the clamp is visible in the
                                            provenance line
T16 spending_stats(2, ...)               -> uses Total_Payroll_Cost, does not error on
                                            a missing Amount field
```

Sanity anchor: table 0, `Duplicate='No'`, all years, net sum = **$9,140,437,359**.

---

## 6. Deploy

Use the repo's existing tooling — do not invent a new deploy path:

```bash
cp config-anchorage-checkbook.yaml config.yaml   # exactly one plugin enabled
python3 scripts/local_server.py                  # smoke test locally first
./scripts/deploy.sh
cd terraform/aws && terraform output -raw api_gateway_url
```

Set `aws.lambda_name: anchorage-checkbook-mcp-staging` and keep `region: us-west-2` to
match the parcels deployment. `lambda_memory: 512`, `lambda_timeout: 60` are fine —
every query is a single upstream call.

Apply `COST_CONTROLS.md` before the first public request: reserved concurrency, stage
throttle, log retention, request-spike alarm, fleet tag. Target host
`checkbook.codeforanchorage.org/mcp`. Then add it as a custom connector in Claude
(Settings → Connectors → Add custom connector).

Also update the fork's `README.md` and add `docs/CHECKBOOK.md` alongside
`docs/PARCELS.md`, so the plugin is documented the same way the parcels one is.

Add a `METHODOLOGY.md` documenting the dedup decision — the official MOA app does not
filter `Duplicate` server-side, it exposes the column and leaves the choice to the
user. This server takes the opposite position and defaults to correct. That difference
needs to be written down and defensible, because totals from this server will not match
the public dashboard unless the dashboard user also filters.
