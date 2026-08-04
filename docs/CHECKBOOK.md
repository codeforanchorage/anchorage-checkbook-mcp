# Anchorage Open Checkbook MCP (`anchorage_checkbook` plugin)

Read-only MCP server over the Municipality of Anchorage **Open
Checkbook** — the public `MOA_OpenCheckbook_Hosted` Feature Service
(six attribute tables: unaudited non-payroll and payroll expenditures,
a payroll cost rollup, procurement, revenue, and an empty
revenue-vs-expenditure table). No auth, no writes, **no geometry** —
the service's `OC_Point` layer is an empty placeholder for the public
Experience Builder app and is never exposed.

This server exists because the raw service is full of traps that a
generic ArcGIS wrapper gets wrong: whole fiscal years are
double-loaded (flagged only by a `Duplicate` column), `PubDate` is an
ETL snapshot stamp rather than transaction time, amounts are net of
enormous offsetting entries, fiscal periods run 1–16, and vendor names
are unnormalized. Every one of these is encoded in tool behavior and
tool descriptions — see [METHODOLOGY.md](../METHODOLOGY.md) for the
dedup position and why totals differ from the public dashboard.

For parcel/assessment questions use the **Anchorage Parcels MCP**; for
any spatial layer or analysis use the **Anchorage GIS MCP**. The
server instructions route models there automatically.

## Tables

| id | Table | Rows (deduped) | Notes |
| -- | ----- | -------------- | ----- |
| 0 | `OC_UnauditedExpenditure_NonPayroll` | ~307k | Primary table; payee = `Vendor_Name` (~54k NULL rows are journal entries) |
| 1 | `OC_UnauditedExpenditure_Payroll` | ~80k | No payee field |
| 2 | `OC_UnauditedPayroll` | ~140 | Dept × year rollup; **no `Amount`** — measures are `Total_Payroll_Cost`, `Salaries_Wages`, `Overtime`, `Liabilities_Benefits` |
| 3 | `OC_UnauditedProcurement` | ~39k | POs; FY2025 (~$1.18B) is an outlier (likely bulk encumbrance load) |
| 4 | `OC_UnauditedRevenue` | ~69k | Payee = `Customer_Business_Name`; **no FY2024 at all**; duplicated in FY2023 *and* FY2026 |
| 5 | `OC_UnaudRev_vs_Exp` | 0 | Empty upstream; exposed with `status: empty` |

`Fund` and `G_L_Account` are `"code : label"` strings and are split
into `_code` / `_label` in every output. `Fiscal_Year` is stored as a
**string**. `Location` is the vendor's billing city/state, not MOA
geography. FY2026 is partial (through fiscal period 7).

## Tools

All tools are read-only (`readOnlyHint`), inject `Duplicate='No'`
unless `include_duplicates=True` (which prepends a warning), and end
with a provenance block: source URL + table, effective WHERE, row
counts, clamp status, and the `PubDate` snapshot date. Field names are
case-sensitive everywhere.

| Tool | What it does |
| ---- | ------------ |
| `list_tables()` | The six tables with live total/deduplicated row counts, measures, dimensions, payee field, and caveats. The discovery entry point. |
| `get_table_schema(table)` | Fields with type, distinct-value cardinality (via `returnDistinctValues` — the service's `count_distinct` is advertised but **broken**), and per-field trap notes. |
| `spending_stats(table, group_by[], …filters, measure, stat_type, order, limit)` | The workhorse: server-side net sum + row count (or count/avg/min/max/stddev/var/percentile_cont), grouped by one or more fields. Labels every figure **net**; states FY2024's absence on Revenue; flags the Procurement FY2025 outlier. |
| `search_by_vendor(name_contains, table, fiscal_year, limit)` | Distinct payee **spellings** with per-spelling net total + row count. Never merges spellings (names are unnormalized upstream); always excludes NULL payees; no `UPPER()` (backend LIKE is case-insensitive). |
| `top_vendors(fiscal_year, business_area, table, n)` | Ranking wrapper; excludes NULL payees and the `'Refunds'` label, and says so. |
| `get_line_items(table, …filters, order_by, limit, offset)` | Raw rows via structured filters only; compact pipe-delimited table; hard cap 500 with a **bookended** TRUNCATED banner quoting the true match count. |
| `list_field_values(table, field, limit)` | Distinct values of a dimension field; splits `code : label`; notes that periods 13–16 are year-end adjustments. |
| `query_checkbook(table, where, out_fields, order_by, limit, offset)` | Escape hatch. WHERE validated against injection *shapes* (`UNION SELECT`), never bare tokens — vendors like `Credit Union 1` and `IBEW Local Union 1547` are queryable. Still injects `Duplicate='No'`; rejects `PubDate` date filters. |

## Configuration

`config-anchorage-checkbook.yaml` is the reference deployment config:
it enables only this plugin, carries the model-facing `instructions:`
block (the data traps, stated once for every consumer), and names its
own Lambda (`anchorage-checkbook-mcp-staging`, us-west-2, 512 MB / 60 s).

```yaml
plugins:
  anchorage_checkbook:
    enabled: true
    service_url: "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/MOA_OpenCheckbook_Hosted/FeatureServer"
    city_name: "Municipality of Anchorage"
    timeout: 30
```

### Schema drift check

The six table schemas are vendored at
`plugins/anchorage_checkbook/schema/checkbook_tables.json`. On every
cold start the plugin diffs live field names per table against the
`TABLES` registry and logs a loud structured warning (`SCHEMA DRIFT
DETECTED`) — but keeps serving (degraded > down). To refresh the
snapshot, re-run the fetch for tables 0–5 and update both the JSON and
`TABLES` in `plugins/anchorage_checkbook/plugin.py`.

## Run and test locally (Windows / PowerShell)

```powershell
# Serve the checkbook config on http://localhost:8000/mcp
Copy-Item config-anchorage-checkbook.yaml config.yaml
$env:PYTHONUTF8 = "1"   # local_server.py prints emoji; avoids cp1252 errors
python scripts/local_server.py

# Unit tests (mocked HTTP)
python -m pytest tests/test_anchorage_checkbook_plugin.py -v

# Live acceptance tests T1-T16 against the real MOA service
$env:CHECKBOOK_LIVE_TESTS = "1"
python -m pytest tests/test_anchorage_checkbook_acceptance.py -v

# Full MCP lifecycle over streamable HTTP (Git Bash; requires jq)
./scripts/test_streamable_http.sh http://localhost:8000/mcp \
  anchorage_checkbook__list_tables '{}'
```

On macOS/Linux use `cp`/`export` instead of the PowerShell forms.

## Fork this for another city

Unlike the parcels plugin there is no `field_map` — a checkbook's
semantics (which fields are dollar measures, which field names the
payee, which are coded strings, which years are broken) are structural
to the dataset. To retarget:

1. Point `service_url` at your city's checkbook Feature Service.
2. Rewrite the `TABLES` registry in
   `plugins/anchorage_checkbook/plugin.py` — one `TableInfo` per
   table, with your measures/dimensions/entity fields and your own
   verified caveats.
3. Re-vendor `schema/checkbook_tables.json`.
4. Re-verify the traps: your ETL will have different ones. The
   acceptance-test structure in
   `tests/test_anchorage_checkbook_acceptance.py` shows how to pin
   them.

## Deploying to AWS Lambda

This fork's Terraform state lives in the shared
`anchorage-gis-opencontext-tfstate` bucket under the
`anchorage-checkbook-staging` / `anchorage-checkbook-prod` workspaces
(the parcels and GIS servers own the other workspaces — never deploy
into theirs). `scripts/deploy.sh` defaults to the checkbook
workspaces.

```bash
cp config-anchorage-checkbook.yaml config.yaml
python scripts/local_server.py            # smoke test locally first
./scripts/deploy.sh --environment staging
cd terraform/aws && terraform output -raw api_gateway_url
```

Cost controls are in the tfvars: reserved concurrency, API Gateway
rate/burst/daily quota, WAF per-IP rate limit, CloudWatch alarms, and
14-day log retention. Production adds the
`checkbook.codeforanchorage.org` custom domain (two Dreamhost CNAMEs:
ACM validation + traffic; see `terraform output acm_validation_cname_*`
and `custom_domain_target`).
