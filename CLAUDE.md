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
uv run pytest tests/test_ckan_plugin.py -v                      # Single file
uv run pytest tests/test_ckan_plugin.py::TestClass::test_name -v  # Single test
uv run pytest tests/ --cov=core --cov=plugins --cov-report=term-missing  # With coverage (80% minimum)

# Linting (ruff)
uv run ruff check core/ plugins/ server/ tests/      # Check
uv run ruff check core/ plugins/ server/ tests/ --fix # Auto-fix
uv run ruff format core/ plugins/ server/ tests/      # Format

# Pre-commit hooks
pre-commit run --all-files

# Go client (requires Go 1.21+)
cd client && make build

# Deploy to AWS
./scripts/deploy.sh --environment staging
```

## Architecture

**Core rule: One Fork = One MCP Server.** Each deployment runs exactly ONE plugin. This is enforced at config validation time (`core/validators.py`) and at runtime (`PluginManager.load_plugins()`). To deploy multiple MCP servers, fork the repo per plugin.

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
- `core/mcp_server.py` — Handles MCP JSON-RPC methods: `initialize`, `tools/list`, `tools/call`, `ping`
- `core/validators.py` — Loads config from `config.yaml` (local) or `OPENCONTEXT_CONFIG` env var (Lambda). Enforces single-plugin rule.
- `server/adapters/aws_lambda.py` — the AWS Lambda entry point (handler: `server.adapters.aws_lambda.lambda_handler`), and the only one. It is a thin adapter onto `UniversalHTTPHandler`; `scripts/local_server.py` is the aiohttp mirror of it.
- `server/http_handler.py` — Cloud-agnostic HTTP handler shared by Lambda and local server
- `stdio_bridge.py` — Python stdio-to-HTTP bridge for connecting Claude Desktop/Code to the local server (alternative to Go client)

**Built-in plugins** (`plugins/`): `ckan`, `arcgis`, `socrata` — each implements `DataPlugin` with `search_datasets`, `get_dataset`, `query_data`. Custom plugins go in `custom_plugins/` and are auto-discovered.

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

Copy `config-anchorage-checkbook.yaml` to `config.yaml` (this fork ships its config ready to go). Enable exactly one plugin. Config supports `${ENV_VAR}` substitution. For Lambda, `config.yaml` is shipped *inside* the deployment package and read at runtime by `server/http_handler.py::_packaged_config_path` — not via the `OPENCONTEXT_CONFIG` env var, which AWS caps at 4KB.

`config.yaml` is gitignored; `config-anchorage-checkbook.yaml` is the tracked source of truth and the two must stay byte-identical (pinned by `tests/test_deployment_config.py`). deploy.sh packages `config.yaml`, so a fix applied to only one of them ships half-applied.

Two AWS sizing values are read from `config.yaml` in preference to `terraform/aws/<env>.tfvars` — `lambda_memory` and `lambda_timeout` (see the `locals` block in `terraform/aws/main.tf`). Editing them in the tfvars alone silently does nothing, and because one config file feeds both environments, a value set there applies to staging *and* prod. `lambda_name` uses the opposite precedence — the tfvars win — so `aws.lambda_name` in config.yaml does NOT decide which environment a deploy targets; `./scripts/deploy.sh -e staging|prod` does, by selecting the tfvars file and the Terraform workspace. Check `main.tf` per variable rather than assuming.

**`terraform/aws/config.yaml` is a BUILD ARTIFACT, not a source file.** `scripts/deploy.sh` copies the repo-root `config.yaml` over it during packaging, and it is gitignored. Two consequences: edits made directly to it vanish on the next deploy, and a bare `terraform plan` run inside `terraform/aws/` (without the packaging steps) reads the STALE copy — so a config change shows up as nothing but a code-hash diff, and a "timeout fix" can appear to apply while changing nothing. Always go through `./scripts/deploy.sh -e <env>`, which repackages before planning.

**Timeout ladder** — each layer must sit under the one above it:

| Layer | Value | Why |
|---|---|---|
| API Gateway integration | 29s | hard REST limit, not adjustable |
| Lambda (`lambda_timeout`) | 28s | self-terminates before the gateway gives up |
| Plugin HTTP (`plugins.anchorage_checkbook.timeout`) | 20s | a hung upstream returns a readable tool error instead of the Lambda being killed mid-flight |

## Structured output

Six of the eight tools (`spending_stats`, `search_by_vendor`, `top_vendors`, `get_line_items`, `list_field_values`, `query_checkbook`) declare an MCP `outputSchema` and return `structuredContent` alongside the markdown. `list_tables` and `get_table_schema` deliberately do not — their value is the prose guidance, and a schema would commit them to a shape whose point is the narrative.

A declared `outputSchema` is **binding**: the spec says servers MUST return conforming results. `tests/test_structured_output.py` validates real tool output against the real schema on the awkward branches, and `scripts/smoke_prod.py` re-checks it against the deployed server.

Two invariants worth knowing before editing a tool:

- **Build the structured half OUTSIDE any `if rows:` branch.** Every converted tool renders its markdown table inside a non-empty branch; building `structured` there too would mean a query that matched nothing advertises a schema and returns nothing — a conformance break invisible to happy-path tests, and zero-result queries are common here (a vendor spelling that does not exist, a fiscal year with no rows).
- **Caveats come from ONE list.** `_caveat()` builds them; the text prints `_caveat_messages()` and the structured half emits the objects. A test asserts every structured caveat message appears in the text, so the two channels cannot drift.

Caveat codes are stable and callers may branch on them: `NET_OF_OFFSETS`, `DUPLICATES_FILTERED`, `DUPLICATES_INCLUDED`, `ADJUSTMENT_PERIOD`, `VENDOR_SPELLING_VARIANTS`, `KNOWN_GAP`, `LOCATION_IS_BILLING`, `PERIOD_SCALE`, `TABLE_EMPTY`, `REFUNDS_LABEL`, `TRUNCATED`.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on push to `main`/`develop` and on PRs:

| job | what it runs |
|---|---|
| `lint` | `ruff check core/ plugins/ server/ tests/` |
| `test` | `pytest -n auto` with coverage, gated at `--cov-fail-under=80` |
| `security` | `pip-audit -r requirements.txt` (the runtime surface that ships in the Lambda) |
| `go-client` | `go vet` + `go test` in `client/` |
| `terraform` | `terraform fmt -check -recursive terraform/` |

Two deliberate omissions. **`ruff format` is not enforced** — this repo is hand-wrapped at ~79 columns and the formatter disagrees with 15 existing files, so gating on it would demand a thousand-line reformat; the linter is enforced. **`terraform validate` is not run** — it requires `terraform init` against the S3 backend, and CI holds no AWS credentials by design.

CI runs without `config.yaml`, which is gitignored, so the suite must never depend on it. `tests/test_deployment_config.py::TestConfigCopiesInSync` skips there by design: the drift it guards against is local, between the tracked config and the untracked copy `deploy.sh` ships. The live acceptance tests skip unless `CHECKBOOK_LIVE_TESTS=1`, so a CI run is never gated on the MOA service being up.
