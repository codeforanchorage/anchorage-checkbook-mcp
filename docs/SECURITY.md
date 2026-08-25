# Security Model — Anchorage Open Checkbook MCP

This document describes the threat model and the defenses currently
enforced for the public deployment at
`https://checkbook.codeforanchorage.org/mcp`. Future contributors should
read this before changing anything in `plugins/anchorage_checkbook/`,
`server/`, or `terraform/aws/`.

> Provenance: this file began as the Anchorage GIS fork's security model.
> The network, CORS, logging and configuration sections describe `server/`
> and `terraform/aws/`, which are shared across the fork family and still
> apply verbatim. The plugin-specific sections have been rewritten — this
> server has a different upstream and a different attack surface.

## Threat model

- **Endpoint:** publicly reachable, unauthenticated, read-only over
  HTTPS. Anyone can issue JSON-RPC calls. Browsers, native MCP clients,
  and arbitrary HTTP clients all reach the same Lambda.
- **Data:** all of it is already public. The MCP proxies exactly one
  upstream, the Municipality of Anchorage's `MOA_OpenCheckbook_Hosted`
  Feature Service on `services2.arcgis.com`, which publishes unaudited
  municipal expenditure, payroll, procurement and revenue tables. No
  write paths exist; no secrets are stored.
- **Realistic risks:**
  1. Denial of wallet (Lambda invocations, upstream Esri API spam).
  2. Query abuse through the `query_checkbook` escape hatch, which is
     the one tool that accepts a caller-authored WHERE clause.
  3. **Misinformation.** Unusually for a read-only public-data server,
     the highest-impact failure here is not disclosure but a confidently
     wrong number: these are municipal finance figures, and a caller who
     reports a net total as a gross one, or a duplicate-inflated year as
     real growth, has been misled by us. The data-quality guardrails are
     therefore treated as part of the security posture, not as polish.
     See [METHODOLOGY.md](../METHODOLOGY.md).

## Defenses in place

### Network and request shape

- **WAF per-IP rate limit** at 300 req / 5 min, blocking mode. As of
  2026-08-04 (prod) and 2026-08-24 (staging) this is the **shared
  fleet ACL** `mcp-fleet-waf`, owned by the `mcp-stats` repo and
  resolved from SSM `/mcp-fleet/waf/web_acl_arn`, not a dedicated ACL
  (`terraform/aws/waf.tf`, `use_shared_waf = true` in both tfvars).
  **Change the rate limit in mcp-stats, not in this repo's tfvars** —
  once `use_shared_waf` is true, `waf_rate_limit_per_5min` here is no
  longer read. Staging has no custom domain, so its raw execute-api
  host is a non-member of the fleet ACL's Host-scoped rules and lands
  on the `rate-limit-unmatched-host` catch-all at the same 300/5min.
- **API Gateway throttling** at 5 rps / 10 burst with a 3000 req/day
  quota in prod, 1000/day in staging (`terraform/aws/api_gateway.tf`,
  `<env>.tfvars`).
- **AWS managed rule sets** (`KnownBadInputsRuleSet`, `CommonRuleSet`)
  in block mode, carried by the fleet ACL.
- **Lambda reserved concurrency** capped at 10 (prod) / 5 (staging) to
  bound cost and blast radius if the WAF is bypassed (`main.tf`,
  `<env>.tfvars`). On an unauthenticated endpoint this cap, not the
  WAF, is the real denial-of-wallet control. Verify it with
  `aws lambda get-function-concurrency` — `get-function-configuration`
  omits the field and makes a set cap look absent.
- **Timeout ladder**, each layer strictly under the one above:
  API Gateway 29s (a hard, non-adjustable AWS limit) > Lambda 28s >
  plugin HTTP 20s. Before 2026-08-25 the Lambda ran at 60s, so a slow
  request returned 504 to the client while continuing to hold a
  reserved-concurrency slot for another 31 seconds — ten such calls
  could lock prod. Pinned by `tests/test_deployment_config.py`.
- **HTTP method allowlist:** only `POST` and `OPTIONS` on `/mcp` and
  `/mcp-gcc` reach plugin code; `GET`/`DELETE` return 405
  (`server/http_handler.py`).

### Cross-origin and session

- **Disallowed browser Origins are REFUSED with 403**, on both the
  request path and the OPTIONS preflight
  (`server/http_handler.py::_origin_rejected`). This is the Streamable
  HTTP transport's DNS-rebinding defense and a spec MUST. Until
  2026-08-25 this server merely withheld CORS headers from unknown
  origins, which is **not** the same thing: the request was still
  served and its side effects still happened, and only the browser's
  reading of the reply was blocked.
- **Only a present Origin can be invalid.** Native MCP clients (Claude
  Desktop/Code, the claude.ai backend connector, curl, Lambda console
  tests) send no Origin and are unaffected by design.
- **CORS allowlist** is enforced in Lambda (not API Gateway) so the
  preflight honors the same list as the actual request
  (`_get_cors_headers`; `terraform/aws/api_gateway.tf` — OPTIONS uses
  AWS_PROXY, not MOCK).
- **`MCP-Protocol-Version` is validated** on post-handshake requests;
  an unrecognized value returns 400 with `-32600`
  (`server/http_handler.py`). Deliberately not the 2026-07-28
  `-32022`, so a dual-era client falls back to `initialize` rather
  than retrying a handshake this server does not implement.
- **`mcp-session-id` is a logging/tracing identifier**, not an auth
  token. Do not extend authorization decisions to depend on it without
  adding a real signing/verification step.

### Upstream calls

This server's upstream surface is far narrower than the GIS fork's, and
the SSRF/tenant-scope-creep checks that dominated that document do not
apply here:

- **One pinned service.** `service_url` is a single Feature Service
  root supplied by config and validated as a well-formed http(s) URL
  (`config_schema.py::validate_service_url`). Tools address tables as
  `<service_url>/<table_id>` where `table_id` is constrained to the
  integers 0-5 by the `TABLES` registry. There is no caller-supplied
  host, item ID, or portal lookup anywhere in this plugin, so there is
  no open-proxy surface to close.
- **Table 6 (`OC_Point`) is deliberately unexposed** — an empty
  geometry placeholder for the public Experience Builder app.

### Input validation

- **WHERE clauses** (the `query_checkbook` escape hatch only) go
  through `plugins/anchorage_checkbook/where_validator.py`. It
  deliberately does **not** copy the GIS fork's keyword denylist: that
  validator rejects the bare token `UNION`, which on this dataset
  breaks real queries — 68 rows across 14+ vendors contain "UNION"
  (`IBEW Local Union 1547`, `Credit Union 1`, `Plumbers & Steamfitters
  Union Local 367`). Instead it masks string-literal contents, then
  scans the remainder for injection **shapes** with word-boundary
  regexes (`UNION ... SELECT`, `INSERT INTO`, `DROP TABLE`, `EXEC(`,
  …), plus forbidden substrings (`;`, `--`, `/*`, `@@`, `xp_`, `sp_`)
  and a 2000-char cap. A quoted data value can therefore never trip
  the scan, and a bare token that is legal in a vendor name never
  matches. **A regression here fails closed in the wrong direction:**
  it would start calling real municipal vendors SQL injection, so
  `tests/test_caller_error_logging.py` pins that
  `Vendor_Name = 'Credit Union 1'` stays a legal clause.
- **Every identifier in a raw WHERE is checked against the table's
  real schema** before it reaches ArcGIS, with a difflib suggestion on
  a miss (`validate_against_schema`).
- **`out_fields` and `order_by`** validated for syntax via
  `OutFieldsValidator` and `OrderByValidator` (identifier-only, so no
  denylist problem).
- **Structured-parameter tools never see caller SQL.** The other seven
  tools take typed filters and the server composes the WHERE clause
  itself with doubled-quote escaping (`_sql_quote`, `_escape_like`).
- **`PubDate` filters are rejected** wherever they appear — it is the
  ETL snapshot stamp, identical on every row, so a date filter on it
  matches everything or nothing.
- **Caller mistakes are classified, not conflated with faults.**
  `ToolInputError` / `UnknownToolError` / `InvalidToolParamsError` map
  to `-32602`/`-32601` and log at WARNING without a traceback, so a
  `-32603` with a stack trace again means the server actually broke.
  This is a security property in practice: before 2026-08-25 routine
  client probes filled the error logs with tracebacks, which is what
  makes a real fault unfindable.

### Logs

- **Sensitive headers and request-body keys redacted** by name match
  in `core/logging_utils.py` (`api_key`, `authorization`, `cookie`,
  `password`, `secret`, `token`, etc.).
- **CloudWatch retention** is 14 days (Lambda logs) and 30 days (API
  Gateway access logs) — `terraform/aws/main.tf`,
  `terraform/aws/access_logs.tf`.

### Configuration and secrets

- **No secrets in `config.yaml`.** The config is shipped **inside the
  deployment package** and read at runtime by
  `server/http_handler.py::_packaged_config_path` — not via the
  `OPENCONTEXT_CONFIG` environment variable, which AWS caps at 4KB and
  which this server's `instructions` block now exceeds. Either way it
  is plaintext. This is an **invariant**: any future plugin secret must
  go via AWS Secrets Manager or SSM Parameter Store with KMS, never the
  package or the env var.

## Architectural invariants

These are enforced by code today; do not relax them without explicit
discussion.

1. **One fork = one MCP server.** The single-plugin rule is enforced at
   config validation (`core/validators.py`) and at runtime
   (`PluginManager.load_plugins()`). Multiple enabled plugins is a hard
   error.
2. **No write paths.** No tool calls `applyEdits`, `/admin/`, or any
   non-`/query` Feature Service path, and the WHERE validator blocks
   DML shapes. If a plugin ever needs writes, treat it as a
   security-review-required change.
3. **Upstream scope = the one configured Feature Service, tables 0-5.**
   No caller input selects a host, an org, or an item.
4. **The data-quality defaults are load-bearing.** `Duplicate='No'` is
   injected into every query, amounts are reported as net, and the
   filter state is a required field of every structured response. A
   change that makes any of these silent is a correctness regression
   with the same standing as a security one.

## Deferred / known gaps

| # | Issue | Status |
|---|---|---|
| 1 | `/mcp` is fully unauthenticated; rate-limit bypass via rotating IPs is feasible | accepted given public-data scope; reserved concurrency bounds the cost |
| 2 | CORS allowlist includes `localhost:6274` (MCP Inspector) in prod | accepted; revisit when non-public tools land |
| 3 | Config shipped in the package in plaintext | accepted as invariant; see above |
| 4 | No request-body size cap at API Gateway (Lambda's 6 MB ceiling applies) | open |
| 5 | Access-log `sourceIp` is raw; not hashed | open |
| 6 | The fleet WAF ARN is resolved from one SSM parameter shared by ~15 services; whoever can write it can silently repoint the whole fleet's WAF | open — integrity concern, not confidentiality; audit who holds `ssm:PutParameter` |
| 7 | Terraform authenticates as a single long-lived IAM **user** shared across the fleet, so CloudTrail carries no per-service attribution | open — per-repo roles with GitHub OIDC is the durable fix |

Gaps 6 and 7 are fleet-wide rather than specific to this repo; they
arrived with the WAF consolidation that traded ~$8/mo per service for a
shared control plane.

## Verifying a change

Before merging anything that touches `plugins/anchorage_checkbook/`,
`server/`, `terraform/aws/`, or `core/`:

1. `pytest tests/ -q` — all must pass, including
   `tests/test_caller_error_logging.py` (the WHERE validator's
   caller-vs-fault split and the `Credit Union 1` regression),
   `tests/test_structured_output.py`, and
   `tests/test_deployment_config.py` (the timeout ladder).
2. Smoke-test locally, which now exercises the same
   `UniversalHTTPHandler` production runs:
   ```bash
   PYTHONIOENCODING=utf-8 python scripts/local_server.py
   python scripts/smoke_prod.py http://localhost:8000/mcp
   ```
3. For transport changes, confirm all six cases: no-Origin 200,
   `https://claude.ai` 200, `https://evil.example` **403**,
   `MCP-Protocol-Version: 2026-07-28` **400**, `2025-11-25` 200,
   `GET /mcp` 405. `scripts/smoke_prod.py` checks all of them.
4. Deploy with `./scripts/deploy.sh --environment <env>`; it plans
   first and requires explicit `yes` before applying. Verify the
   Terraform workspace — this repo owns `anchorage-checkbook-staging`
   and `anchorage-checkbook-prod` in a state bucket shared with other
   MCPs.
5. After any apply, re-confirm the WAF is still attached:
   ```bash
   aws wafv2 get-web-acl-for-resource \
     --resource-arn "arn:aws:apigateway:us-west-2::/restapis/<api-id>/stages/<stage>" \
     --region us-west-2 --query "WebACL.Name" --output text
   ```
   It must print `mcp-fleet-waf`.

## Reporting issues

Email `brendanbabb@gmail.com` for sensitive reports. Public issues can go
to <https://github.com/codeforanchorage/anchorage-checkbook-mcp/issues>.
