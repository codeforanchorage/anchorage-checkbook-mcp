# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
# Install dependencies (uv preferred, pip fallback)
uv sync                              # or: pip install -r requirements.txt

# Run local MCP server (no Lambda needed)
python3 scripts/local_server.py      # Serves on http://localhost:8000/mcp
# The only local entry point. It routes through UniversalHTTPHandler, the
# same handler the Lambda adapter uses, so Origin/protocol-version/path
# checks behave locally exactly as they do in prod.

# Validate config
python3 -c "from core.validators import load_and_validate_config; load_and_validate_config('config.yaml')"

# Tests
uv run pytest tests/ -n auto                                    # All tests, parallel
uv run pytest tests/test_anchorage_checkbook_plugin.py -v                      # Single file
uv run pytest tests/test_anchorage_checkbook_plugin.py::TestClass::test_name -v  # Single test
uv run pytest tests/ --cov=plugins --cov-report=term-missing  # With coverage (80% minimum)

# Linting (ruff)
uv run ruff check plugins/ tests/      # Check
uv run ruff check plugins/ tests/ --fix # Auto-fix
uv run ruff format plugins/ tests/      # Format

# Pre-commit hooks
pre-commit run --all-files

# Go client (requires Go 1.21+)
cd client && make build

# Deploy to AWS
./scripts/deploy.sh --environment staging
```

## Architecture

**Core rule: One Fork = One MCP Server.** Each deployment runs exactly ONE plugin. This is enforced at config validation time (`core/validators.py`) and at runtime (`PluginManager.load_plugins()`). To deploy multiple MCP servers, fork the repo per plugin.

**The MCP server core is a dependency, not code in this repo.** `core/` and `server/` come from the [`opencontext-core`](https://github.com/codeforanchorage/opencontext-core) package, pinned to a tag in `requirements.txt` and `pyproject.toml` (keep the two in step; `tests/test_core_dependency.py` checks). To change protocol or transport behaviour, change it there, tag a release, and move the pin here. Do not re-create `core/` or `server/` in this repo: a local copy shadows the package. `scripts/deploy.sh` installs the package next to `plugins/` in the Lambda zip, so the handler string is unchanged. To work on both at once: `pip install -e ../opencontext-core`. Run commands from the repo root, which is where the installed core looks for `plugins/`.

**Request flow:**
```
Claude (stdio) → Go client (client/) or stdio_bridge.py → HTTP POST /mcp
  → Lambda (server/adapters/aws_lambda.py) or scripts/local_server.py
  → server/http_handler.py → core/mcp_server.py (JSON-RPC 2.0)
  → core/plugin_manager.py → Plugin → External API
```

**Key modules:**
- `core/interfaces.py` — Abstract bases: `MCPPlugin`, `DataPlugin`, plus `ToolDefinition`, `ToolResult`, `PluginType` enum
- `core/plugin_manager.py` — Discovers plugins by scanning `plugins/` and `custom_plugins/` for `plugin.py` files. Registers tools with `pluginname__toolname` prefix. Routes `tools/call` to the correct plugin.
- `core/mcp_server.py` — Handles MCP JSON-RPC methods. Dual-era: legacy revisions (≤2025-11-25) get `initialize`, `tools/list`, `tools/call`, `ping` on a session; the modern revision (2026-07-28) is stateless — every request carries `_meta` (protocol version, client info/capabilities), `server/discover` replaces `initialize`, results carry `resultType` and cache hints, and the Streamable HTTP headers (`Mcp-Method`, `Mcp-Name`, `MCP-Protocol-Version`) must mirror the body or the request is a 400/-32020
- `core/validators.py` — Loads config from `config.yaml` (local) or `OPENCONTEXT_CONFIG` env var (Lambda). Enforces single-plugin rule.
- `server/adapters/aws_lambda.py` — the AWS Lambda entry point (handler: `server.adapters.aws_lambda.lambda_handler`), and the only one. It is a thin adapter onto `UniversalHTTPHandler`; `scripts/local_server.py` is the aiohttp mirror of it.
- `server/http_handler.py` — Cloud-agnostic HTTP handler shared by Lambda and local server
- `stdio_bridge.py` — Python stdio-to-HTTP bridge for connecting Claude Desktop/Code to the local server (alternative to Go client)

**Plugins** (`plugins/`): `anchorage_checkbook`. This fork keeps only what it runs or imports; the generic `ckan`, `arcgis` and `socrata` plugins of upstream OpenContext that it did not use were deleted (they remain in [CityOfBoston/OpenContext](https://github.com/CityOfBoston/OpenContext)). A plugin implements `MCPPlugin` (or `DataPlugin` with `search_datasets`, `get_dataset`, `query_data`). Custom plugins go in `custom_plugins/` and are auto-discovered.

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

Copy `config-anchorage-checkbook.yaml` to `config.yaml` (this fork ships its config ready to go). Enable exactly one plugin. Config does **NOT** support `${ENV_VAR}` substitution: `core/validators.py::load_and_validate_config` is a plain `yaml.safe_load`, so a `${VAR}` reaches the plugin as that literal string. Read secrets from `os.environ` in the plugin's `initialize()` instead, and never put one in `config.yaml` -- it is deployed with the Lambda. For Lambda, `config.yaml` is shipped *inside* the deployment package and read at runtime by `server/http_handler.py::_packaged_config_path` — not via the `OPENCONTEXT_CONFIG` env var, which AWS caps at 4KB.

`config.yaml` is gitignored; `config-anchorage-checkbook.yaml` is the tracked source of truth and the two must stay byte-identical (pinned by `tests/test_deployment_config.py`). deploy.sh packages `config.yaml`, so a fix applied to only one of them ships half-applied.

Two AWS sizing values are read from `config.yaml` in preference to `terraform/aws/<env>.tfvars` — `lambda_memory` and `lambda_timeout` (see the `locals` block in `terraform/aws/main.tf`). Editing them in the tfvars alone silently does nothing, and because one config file feeds both environments, a value set there applies to staging *and* prod. `lambda_name` uses the opposite precedence — the tfvars win — so `aws.lambda_name` in config.yaml does NOT decide which environment a deploy targets; `./scripts/deploy.sh -e staging|prod` does, by selecting the tfvars file and the Terraform workspace. Check `main.tf` per variable rather than assuming.

**`terraform/aws/config.yaml` is a BUILD ARTIFACT, not a source file.** `scripts/deploy.sh` copies the repo-root `config.yaml` over it during packaging, and it is gitignored. Two consequences: edits made directly to it vanish on the next deploy, and a bare `terraform plan` run inside `terraform/aws/` (without the packaging steps) reads the STALE copy — so a config change shows up as nothing but a code-hash diff, and a "timeout fix" can appear to apply while changing nothing. Always go through `./scripts/deploy.sh -e <env>`, which repackages before planning.

**Timeout ladder** — each layer must sit under the one above it:

| Layer | Value | Why |
|---|---|---|
| API Gateway integration | 29s | hard REST limit, not adjustable |
| Lambda (`lambda_timeout`) | 28s | self-terminates before the gateway gives up |
| Plugin HTTP (`plugins.anchorage_checkbook.timeout`) | 20s | a hung upstream returns a readable tool error instead of the Lambda being killed mid-flight |

The 20s is a budget for the **whole tool call**, not per HTTP request: `execute_tool` sets one deadline and `_request_json` cuts every attempt and retry off at it (`tests/test_upstream_time_budget.py`). The startup metadata fetch has its own 5s budget (`STARTUP_META_BUDGET_S`), because it runs in the same Lambda invocation as the request that follows: 5 + 20 stays under 28.

**How long the plugin object lives.** Since `opencontext-core` 1.5.0 the Lambda adapter keeps the plugin manager, the plugin and its HTTP client for the life of a warm container (set `server_reuse_across_invocations: false` to rebuild per request, as every earlier version did). State on `self` therefore persists across requests now: give any new cache a lifetime, and reset per-call state in a `finally`, as `_request_deadline` is. The module-level `_SHARED_STATE` in `plugin.py` (table metadata and the PubDate snapshot, 15-minute lifetime, 60s hold-off after a failed fetch) and the lazily created HTTP client (`_http()`) date from the per-request era; they are kept because they are also what bounds the metadata's age and makes a failed startup fetch cheap.

## Deploying

`./scripts/deploy.sh -e staging|prod` is the only supported path (`--yes` skips the confirmation prompt). Three rules it enforces, each of which changes how you work:

- **`config.yaml` must equal `config-anchorage-checkbook.yaml`.** The shipped file is gitignored, so the clean-tree check cannot see it; the script compares the two and stops if they differ.
- **The working tree must be a commit.** The script refuses to package uncommitted or untracked changes, so the live code always matches something in git. Commit first, then deploy. `--allow-dirty` overrides this and is for emergencies only.
- **Dependencies come from `uv.lock`**, the versions CI tested, not from the lower bounds in `requirements.txt`. To move a dependency: `uv lock --upgrade`, run the tests, commit `uv.lock`, then deploy. The script finds `uv` on PATH or as `python -m uv`; without either it falls back to pip and `requirements.txt` with a warning, and the build is marked unlocked.
- **Every package carries `BUILD_INFO.json`** (`commit`, `dirty`, `locked`) at its root. To see what an environment runs, read that file out of the deployed zip (`aws lambda get-function --query Code.Location`) instead of inferring it from timestamps or `serverInfo.version`.

Deploy to staging first, run `python scripts/smoke_prod.py <staging-url>`, then prod and `python scripts/smoke_prod.py` (no argument targets prod). The Terraform workspaces are `anchorage-checkbook-staging` and `anchorage-checkbook-prod`, in the state bucket `anchorage-gis-opencontext-tfstate` that the other Anchorage MCP servers share. Never point this repo at another server's workspace, and never regenerate `terraform/aws/backend.tf`: a new backend would start from empty state and plan to create everything again.

## Structured output

Six of the eight tools (`spending_stats`, `search_by_vendor`, `top_vendors`, `get_line_items`, `list_field_values`, `query_checkbook`) declare an MCP `outputSchema` and return `structuredContent` alongside the markdown. `list_tables` and `get_table_schema` deliberately do not — their value is the prose guidance, and a schema would commit them to a shape whose point is the narrative.

A declared `outputSchema` is **binding**: the spec says servers MUST return conforming results. `tests/test_structured_output.py` validates real tool output against the real schema on the awkward branches, and `scripts/smoke_prod.py` re-checks it against the deployed server.

Two invariants worth knowing before editing a tool:

- **Build the structured half OUTSIDE any `if rows:` branch.** Every converted tool renders its markdown table inside a non-empty branch; building `structured` there too would mean a query that matched nothing advertises a schema and returns nothing — a conformance break invisible to happy-path tests, and zero-result queries are common here (a vendor spelling that does not exist, a fiscal year with no rows).
- **Caveats come from ONE list.** `_caveat()` builds them; the text prints `_caveat_messages()` and the structured half emits the objects. A test asserts every structured caveat message appears in the text, so the two channels cannot drift.

Caveat codes are stable and callers may branch on them: `NET_OF_OFFSETS`, `DUPLICATES_FILTERED`, `DUPLICATES_INCLUDED`, `ADJUSTMENT_PERIOD`, `VENDOR_SPELLING_VARIANTS`, `KNOWN_GAP`, `LOCATION_IS_BILLING`, `PERIOD_SCALE`, `TABLE_EMPTY`, `REFUNDS_LABEL`, `TRUNCATED`, `NO_ROWS_MATCHED`, `NULL_PAYEE_GROUP`. A `KNOWN_GAP` caveat also carries a `gap` value naming which gap; the output schema lists them, and a test fails if the plugin emits one the schema does not describe.

## FY2026 bare codes

FY2026 rows store `Fund`, `G_L_Account` and `Business_Area` as bare codes
(`'101000'`, `'0000200050'`, `'4000'`); earlier years store `code : label`
strings and department names. `plugins/anchorage_checkbook/schema/code_labels.json`
is the code→label map that repairs this, and it is **generated** — run
`python scripts/refresh_code_labels.py` after a republish rather than editing it.
Three things depend on it, each pinned in `tests/test_bare_codes.py`:

- **Filters** (`_coded_contains_clause`): a label fragment also matches the codes
  carrying that label, via an `IN (...)` arm. A long list is sent as a POST.
- **Groupings** (`_merge_coded_groups`): `spending_stats` fetches every group, merges
  the two storage forms of one code, then ranks and applies the limit. `stddev`,
  `var` and `percentile_cont` cannot be recombined and are left unmerged, with a caveat.
- **Rows and value lists** show the canonical form (`code : label`, or the department
  name). An unknown code is shown as the code, never guessed.

A filter value may be a label fragment, a code, or the displayed canonical
`code : label` string copied back out of a result; all three reach both forms.

A raw `query_checkbook` WHERE is NOT rewritten. Two `KNOWN_GAP` caveats cover
it: `gap: fy2026_bare_codes` whenever bare codes were resolved in the rows, and
`gap: fy2026_bare_codes_raw_where` whenever the WHERE itself names a coded
field -- the case where FY2026 rows drop out and no bare code comes back to
trigger the first one.

The structured `query` block echoes only filters the tool APPLIED
(`_query_block` takes the applied subset, never the raw arguments), so an
ignored `fiscal_period` is never reported as a filter.

## MCP conformance invariants

These are contracts, not preferences — each is enforced by a test. If you change
the behaviour, change the test deliberately rather than deleting it.

- **Protocol negotiation is dual-era.** `MCPServer.SUPPORTED_PROTOCOL_VERSIONS` is
  `MODERN_PROTOCOL_VERSIONS` (2026-07-28) + `LEGACY_PROTOCOL_VERSIONS`, newest
  first. The era is picked per request (`MCPServer.request_era`): `initialize` is
  legacy; a body whose `params._meta` carries
  `io.modelcontextprotocol/protocolVersion` is modern; a modern
  `MCP-Protocol-Version` header with no `_meta` is a malformed modern request, not
  a legacy one to serve quietly. `initialize` negotiates ONLY legacy versions:
  it echoes the client's `protocolVersion` when that is a legacy revision we
  serve, answers the spec-defined default 2025-03-26 when the client names
  none, and answers the newest legacy revision (2025-11-25) for anything else
  -- so a client asking for 2026-07-28 on the handshake gets 2025-11-25. An
  absent `MCP-Protocol-Version` header means 2025-03-26 and is NOT an error; a
  header naming a version served in neither era is HTTP 400 with `-32022` and
  the supported list. That header check applies to `initialize` too: the
  version to negotiate goes in the body, and a client that also sends an
  unsupported header on the handshake is refused before it.
- **Modern-era rules** (spec 2026-07-28, tested in `tests/test_modern_protocol.py`):
  `_meta.protocolVersion` and `_meta.clientCapabilities` are required (`-32602`,
  HTTP 400 when missing); a version not in `MODERN_PROTOCOL_VERSIONS` — including
  our own legacy ones — is `-32022`/400; `server/discover`, `tools/list` and
  `tools/call` are served, everything else (`ping`, `initialize`, `resources/*`)
  is `-32601` on HTTP **404** (legacy keeps `-32601` inside a 200); every modern
  result carries `resultType: "complete"` and `_meta` serverInfo, and
  `server/discover` / `tools/list` carry `ttlMs` + `cacheScope`. `Mcp-Method`,
  `MCP-Protocol-Version` and (for `tools/call`) `Mcp-Name` must be present and
  mirror the body, `=?base64?…?=` sentinel included, else `-32020`/400. No
  session is minted or echoed on a modern request. Legacy clients must see no
  change at all.
- **Error codes** — `ping` returns `{}` (legacy). Unknown method is `-32601`. Unknown
  tool and malformed `tools/call` (non-object `params`, missing `name`, non-object
  `arguments`) are `-32602` on HTTP 200 in both eras — a bad tool argument is not a
  malformed envelope. Accepted notifications are HTTP 202 with an empty body.
- **Caller errors never log a traceback.** Raise `ToolInputError` (a `ValueError`
  subclass) for anything the caller got wrong; it logs at WARNING. Genuine server or
  upstream faults keep `ERROR` plus `exc_info` — that is what those logs are for.
  `tests/test_caller_error_logging.py` pins this for every caller-error class.
- **The Origin allowlist is enforced** with HTTP 403 before any routing or plugin
  code, not merely reflected in CORS headers. Requests with NO Origin are allowed —
  DNS rebinding is browser-only and every native client sends none.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on push to `main`/`develop` and on PRs:

| job | what it runs |
|---|---|
| `lint` | `ruff check` **and** `ruff format --check` over `plugins/ tests/` |
| `test` | `pytest -n auto` with coverage, gated at `--cov-fail-under=80` |
| `security` | `pip-audit -r requirements.txt` (the runtime surface that ships in the Lambda) |
| `go-client` | `go vet` + `go test` in `client/` |
| `terraform` | `terraform fmt -check -recursive terraform/` |

One deliberate omission: **`terraform validate` is not run** — it requires `terraform init` against the S3 backend, and CI holds no AWS credentials by design. `terraform fmt` needs neither and is enforced.

Formatting **is** enforced (`ruff format --check`), matching the `ruff-format` hook `.pre-commit-config.yaml` has always run. Line length is ruff's default 88, not 79. The ruff version is pinned to 0.15.11 in both `pyproject.toml` `[dev]` and the pre-commit rev — keep them equal, or the hook, a local run and CI can disagree about the same file.

CI runs without `config.yaml`, which is gitignored, so the suite must never depend on it. `tests/test_deployment_config.py::TestConfigCopiesInSync` skips there by design: the drift it guards against is local, between the tracked config and the untracked copy `deploy.sh` ships. The live acceptance tests skip unless `CHECKBOOK_LIVE_TESTS=1`, so a CI run is never gated on the MOA service being up.
