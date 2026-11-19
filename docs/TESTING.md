# Testing Guide

This guide covers three ways to test your OpenContext server locally.

## Prerequisites

Before testing:

1. Create `config.yaml`: `cp config-anchorage-checkbook.yaml config.yaml`
2. Install dependencies: `pip install -r requirements-dev.txt`
3. Start the server: `python3 scripts/local_server.py`

The server runs at `http://localhost:8000/mcp`. Keep it running while you test.

---

## Method 1: Terminal (cURL)

Use the terminal to send requests directly to the server.

**Ping:**

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

**List tools:**

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

**Call a tool:**

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"anchorage_checkbook__search_by_vendor","arguments":{"name_contains":"chugach","limit":3}}}'
```

For a full test (initialize, list tools, call tool), run:

```bash
./scripts/test_streamable_http.sh
```

---

## Method 2: Claude (Connectors)

1. Connect via Claude Connectors (see [Getting Started](GETTING_STARTED.md))
2. Add a custom connector with URL: `http://localhost:8000/mcp`
3. Enable the connector in your conversation (click "+" → Connectors → toggle on)
4. Ask Claude to search your data or list available tools

**Note:** Localhost only works with Claude Desktop. For Claude.ai (web), use MCP Inspector or deploy first.

---

## Method 3: MCP Inspector

MCP Inspector is a web-based tool for testing MCP servers.

1. With the server running, open a new terminal
2. Run: `npx @modelcontextprotocol/inspector`
3. The Inspector UI opens in your browser (typically `http://localhost:6274`)
4. In the Inspector, select **streamable-http** as the transport
5. Enter the URL: `http://localhost:8000/mcp`
6. Use the Tools tab to list and call tools

---

## Quick Checks

Optional checks before starting the server.

**Config validation:**

```bash
python3 -c "
import yaml
from core.validators import load_and_validate_config
config = load_and_validate_config('config.yaml')
print('Config valid:', config['server_name'])
"
```

**Plugin loading:**

```bash
python3 -c "
import asyncio, yaml
from core.plugin_manager import PluginManager
async def t():
    with open('config.yaml') as f: config = yaml.safe_load(f)
    pm = PluginManager(config)
    await pm.load_plugins()
    print('Tools:', [t['name'] for t in pm.get_all_tools()])
    await pm.shutdown()
asyncio.run(t())
"
```

---

## Unit Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -n auto
pytest tests/test_anchorage_checkbook_plugin.py -v
pytest tests/ --cov=plugins --cov-report=term-missing
```

The live acceptance tests call the MOA service and are skipped unless `CHECKBOOK_LIVE_TESTS=1`:

```bash
CHECKBOOK_LIVE_TESTS=1 pytest tests/test_anchorage_checkbook_acceptance.py
```

---

## Testing Against Production

`scripts/smoke_prod.py` checks a deployed server end to end (protocol, error codes, every tool, schema conformance). With no argument it targets prod; pass a URL for staging or a local server:

```bash
python scripts/smoke_prod.py
python scripts/smoke_prod.py https://your-api-id.execute-api.us-west-2.amazonaws.com/staging/mcp
```

For a single request, POST to the API Gateway URL:

```bash
MCP_URL="https://your-api-id.execute-api.us-west-2.amazonaws.com/staging/mcp"
curl -X POST $MCP_URL \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

See [Deployment](DEPLOYMENT.md) for how to get the URL.
