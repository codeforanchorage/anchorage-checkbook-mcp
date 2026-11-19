# Architecture

## Overview

This repository is a fork of OpenContext that deploys one MCP server with one
plugin: `anchorage_checkbook`, the Municipality of Anchorage Open Checkbook.
OpenContext is a plugin-based framework in which each deployment runs **one**
server with **one** plugin, which keeps deployments simple, independently
scalable, and easy to maintain.

## One Fork = One Server

**Enforcement:**
- `scripts/deploy.sh` validates the config before deployment
- `core/validators.py` rejects a config with more than one plugin enabled
- `core/plugin_manager.py` fails at load time if more than one is enabled

**Multiple servers:** fork again per plugin and deploy each separately.

## Components

`core/` and `server/` are **not in this repository**. They come from the
[`opencontext-core`](https://github.com/codeforanchorage/opencontext-core)
package, pinned to a tag in `requirements.txt` and `pyproject.toml`.
`scripts/deploy.sh` installs the package next to `plugins/` in the Lambda zip.
Do not re-create either directory here: a local copy shadows the package.

```
opencontext-core (installed package)
├── core/
│   ├── interfaces.py       # MCPPlugin, DataPlugin, ToolDefinition, ToolInputError
│   ├── plugin_manager.py   # Discovery, loading, routing
│   ├── mcp_server.py       # MCP JSON-RPC handler (legacy and 2026-07-28 eras)
│   └── validators.py       # Config loading and validation
└── server/
    ├── adapters/aws_lambda.py   # Lambda entry point
    └── http_handler.py          # Cloud-agnostic HTTP handling

this repository
├── plugins/anchorage_checkbook/
│   ├── plugin.py            # The eight tools
│   ├── where_validator.py   # Validation for the raw-WHERE escape hatch
│   ├── config_schema.py
│   └── schema/              # Table registry and the generated code-label map
├── custom_plugins/template/ # Starting point for a new plugin
├── scripts/                 # deploy.sh, local_server.py, smoke_prod.py, ...
├── terraform/aws/           # Lambda, API Gateway, alarms, WAF association
├── client/                  # Go stdio-to-HTTP client (optional)
├── stdio_bridge.py          # Python stdio-to-HTTP bridge (alternative to the Go client)
└── tests/
```

### Request Flow

```
Claude (stdio) → Go client (client/) or stdio_bridge.py → HTTP POST /mcp
  → API Gateway → Lambda (server/adapters/aws_lambda.py)
    or scripts/local_server.py
  → server/http_handler.py → core/mcp_server.py (JSON-RPC 2.0)
  → core/plugin_manager.py → anchorage_checkbook plugin
  → MOA Open Checkbook ArcGIS Feature Service
```

Claude.ai and Claude Desktop connectors skip the stdio step and POST to `/mcp`
directly.

## The plugin

`anchorage_checkbook` is the only plugin in this fork. Its tools, the dataset's
traps and how the tools handle them are documented in
[CHECKBOOK.md](CHECKBOOK.md); the security model is in
[SECURITY.md](SECURITY.md).

The generic `ckan`, `arcgis` and `socrata` plugins of upstream OpenContext were
deleted from this fork because it does not use them. They remain in
[CityOfBoston/OpenContext](https://github.com/CityOfBoston/OpenContext).

### Custom plugins

A plugin placed in `custom_plugins/<name>/plugin.py` is auto-discovered.

- Inherit from `MCPPlugin` (or `DataPlugin` for data sources)
- Set `plugin_name`, `plugin_type`, `plugin_version`
- Return bare tool names from `get_tools()`; the plugin manager adds the
  `pluginname__` prefix

**Required methods:**

```python
def __init__(self, config: Dict[str, Any]) -> None
async def initialize() -> bool
async def shutdown() -> None
def get_tools() -> List[ToolDefinition]
async def execute_tool(tool_name, arguments) -> ToolResult
async def health_check() -> bool
```

**DataPlugin:** also implement `search_datasets`, `get_dataset`, and
`query_data`.

**Errors:** raise `ToolInputError` for anything the caller got wrong, so it
logs as a warning and not as a server fault with a traceback.

See [Custom Plugins](CUSTOM_PLUGINS.md) and the
[plugin template](../custom_plugins/template/plugin_template.py).

## Endpoints

| Endpoint | Protection | Use |
|----------|------------|-----|
| API Gateway `/mcp` | Shared fleet WAF rate limit, gateway throttle, Origin allowlist | Production and staging |
| `scripts/local_server.py` | Same handler, same Origin and protocol checks | Local development |

There is no Lambda Function URL; API Gateway is the only public entry point.

## Configuration

`config-anchorage-checkbook.yaml` is the tracked source of truth. `config.yaml`
is a gitignored, byte-identical copy that `scripts/deploy.sh` ships **inside**
the Lambda package, where `server/http_handler.py` reads it at runtime. It is
not passed through the `OPENCONTEXT_CONFIG` environment variable, which AWS
caps at 4 KB. The config does not support `${ENV_VAR}` substitution, and no
secret belongs in it.

## Security and scalability

- **Rate limiting:** the shared `mcp-fleet-waf` ACL plus API Gateway throttling
- **Timeouts:** 29 s gateway > 28 s Lambda > 20 s plugin HTTP, so a hung
  upstream returns a readable tool error
- **Stateless:** no shared state; Lambda scales up to its reserved concurrency
- **Logging:** CloudWatch, structured JSON, request IDs; caller errors log at
  WARNING without a traceback
