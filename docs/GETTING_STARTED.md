# Getting Started

Get your OpenContext server running in under 10 minutes.

OpenContext uses the Model Context Protocol (MCP), which connects AI assistants to external data. Your server exposes tools that AI assistants can call to search and query your open data.

## Prerequisites

- Python 3.11+
- Terraform >= 1.0 (for deployment)
- AWS CLI configured (for deployment)

## Quick Path: Local Testing

Test the server locally before deploying.

### 1. Configure Your Plugin

This fork ships its config ready to go — copy it into place:

```bash
cp config-anchorage-checkbook.yaml config.yaml
```

That enables the one plugin this fork deploys:

```yaml
plugins:
  anchorage_checkbook:
    enabled: true
    service_url: "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/MOA_OpenCheckbook_Hosted/FeatureServer"
    city_name: "Municipality of Anchorage"
    timeout: 20
```

`config.yaml` is gitignored; `config-anchorage-checkbook.yaml` is the tracked source of truth, and the two must stay identical (a test enforces it).

Each deployment connects to one data source — enabling a second plugin is a hard error. To serve another source, fork again. See [Architecture](ARCHITECTURE.md) for details.

### 2. Start the Local Server

```bash
pip install aiohttp
python3 scripts/local_server.py
```

The server runs at `http://localhost:8000/mcp`. Keep this terminal open.

### 3. Connect via Claude Connectors

Connect using **Claude Connectors** (same steps on both Claude.ai and Claude Desktop):

1. Go to **Settings** → **Connectors** (or **Customize** → **Connectors** on claude.ai)
2. Click **Add custom connector**
3. Enter a name (e.g. "Checkbook (local)") and URL: `http://localhost:8000/mcp`

**Note:** Local servers (`localhost`) only work with Claude Desktop, since the connection runs from your machine. For Claude.ai (web), use the MCP Inspector or deploy to production first (see below).

### 4. Verify

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

You can also test with Claude by asking it to search your data, or use [Testing](TESTING.md) for more options (MCP Inspector, full test script).

---

## Production Deployment

### 1. Fork & Configure

1. Clone [this repository](https://github.com/codeforanchorage/anchorage-checkbook-mcp)
2. Clone your fork
3. Create config: `cp config-example.yaml config.yaml`
4. Edit `config.yaml` with **exactly one** plugin enabled

### 2. Deploy to AWS

```bash
./scripts/deploy.sh
```

The script validates config, packages code, and deploys to AWS Lambda. You'll receive:
- **Lambda Function URL** – for testing (no auth)
- **API Gateway URL** – for production (API key, rate limiting)

AWS creates: Lambda function, Function URL, API Gateway, IAM role, CloudWatch Log Group. Cost is roughly $1/month for 100K requests. See [Deployment](DEPLOYMENT.md) for details.

### 3. Connect via Claude Connectors (Production)

Connect using **Claude Connectors** (same steps on both Claude.ai and Claude Desktop):

1. Go to **Settings** → **Connectors** (or **Customize** → **Connectors** on claude.ai)
2. Click **Add custom connector**
3. Enter a name (e.g. "Anchorage Open Checkbook") and your API Gateway URL

Get the URL:

```bash
cd terraform/aws
terraform output -raw api_gateway_url
```

The output already includes `/mcp`. Use this URL for production (rate limiting, API key). For testing without auth, use the Lambda URL from `terraform output -raw lambda_url` instead.

### 4. Updating

To update config or code: edit `config.yaml` or your code, then run `./scripts/deploy.sh` again.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: aiohttp` | `pip install aiohttp` |
| "Multiple Plugins Enabled" | Enable only one plugin in `config.yaml` |
| Claude can't connect | Verify URL includes `/mcp`, check connector is enabled in the chat |
| Lambda 500 error | Check CloudWatch logs, validate config |
| Plugin init fails | Check API URLs, keys, and network connectivity |

---

## Next Steps

- [Architecture](ARCHITECTURE.md) – System design, built-in plugins, custom plugins
- [Deployment](DEPLOYMENT.md) – AWS details, monitoring, cost
- [Testing](TESTING.md) – Local testing (Terminal, Claude, MCP Inspector)

---

## Support

[GitHub Issues](https://github.com/codeforanchorage/anchorage-checkbook-mcp/issues)
