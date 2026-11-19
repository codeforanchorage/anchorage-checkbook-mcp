# Frequently Asked Questions

## General

### What is OpenContext?

OpenContext is an extensible MCP framework template that governments can fork to deploy MCP servers for their civic data platforms. Each fork deploys exactly ONE MCP server with ONE plugin enabled.

### Why "One Fork = One MCP Server"?

This architecture keeps deployments simple, independently scalable, and easy to maintain. See [Architecture Guide](ARCHITECTURE.md) for details.

### Can I deploy multiple plugins?

No. Each fork must deploy exactly ONE plugin. To deploy multiple plugins, fork the repository multiple times (one fork per plugin).

### What is MCP?

MCP (Model Context Protocol) is a protocol for connecting AI assistants to external data sources. Learn more at [modelcontextprotocol.io](https://modelcontextprotocol.io).

## Configuration

### How do I enable a plugin?

Edit `config.yaml` and set `enabled: true` for ONE plugin:

```yaml
plugins:
  anchorage_checkbook:
    enabled: true # Only ONE plugin should be enabled
```

This fork ships its config ready to use: `cp config-anchorage-checkbook.yaml config.yaml`. The two files must stay byte-identical.

### Can I use environment variables in config.yaml?

No. The config is read with a plain `yaml.safe_load`, so a `${VAR}` reaches the plugin as that literal string. A plugin that needs a secret reads it from `os.environ` in `initialize()`. Never put a secret in `config.yaml`: it ships inside the Lambda package.

### What if I need to change configuration after deployment?

Edit `config-anchorage-checkbook.yaml`, copy it to `config.yaml`, commit, and run `./scripts/deploy.sh -e <staging|prod>` again. The config ships inside the Lambda package, so a config change is a code deploy.

## Plugins

### What plugins are available?

This fork ships one plugin:

- **anchorage_checkbook** - the Municipality of Anchorage Open Checkbook (see [CHECKBOOK.md](CHECKBOOK.md))

The generic CKAN, ArcGIS and Socrata plugins of upstream OpenContext are not in this fork; they remain in [CityOfBoston/OpenContext](https://github.com/CityOfBoston/OpenContext).

You can also create custom plugins in `custom_plugins/`.

### How do I create a custom plugin?

See [Custom Plugins Guide](CUSTOM_PLUGINS.md) for detailed instructions.

### Can I modify built-in plugins?

No. Built-in plugins are part of the core framework. Create a custom plugin instead.

## Deployment

### What AWS resources are created?

- Lambda function
- API Gateway REST API, stage and usage plan
- IAM role and policies
- CloudWatch log groups and alarms
- An association with the shared fleet WAF

### How much does it cost?

Typical costs: ~$1/month for 100K requests. See [Deployment Guide](DEPLOYMENT.md) for details.

### Can I deploy to a different cloud provider?

The current implementation is AWS-specific. Contributions for other providers are welcome!

### How do I update an existing deployment?

Commit your changes and run `./scripts/deploy.sh -e <staging|prod>` again. The script refuses a working tree with uncommitted changes, so the live code always matches a commit.

## Usage

### How do I connect to it with Claude?

Connect via **Claude Connectors** (same steps on both Claude.ai and Claude Desktop):

1. Go to **Settings** → **Connectors** (or **Customize** → **Connectors** on claude.ai)
2. Click **Add custom connector**
3. Enter a name and your API Gateway URL (from `terraform output -raw api_gateway_url`)

Enable the connector in your conversation by clicking "+" → Connectors → toggle it on.

### Can I use it without Claude Desktop?

Yes. POST MCP JSON-RPC to the API Gateway URL (it ends in `/mcp`).

### How do I test my deployment?

```bash
curl -X POST https://your-api-id.execute-api.us-west-2.amazonaws.com/staging/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Troubleshooting

### Deploy script fails: "Multiple Plugins Enabled"

**Solution:** Enable only ONE plugin in `config.yaml`. Disable all others.

### Lambda returns 500 error

**Check:**

1. CloudWatch logs for errors
2. Configuration is valid
3. Plugin initialization succeeded

### Claude can't connect

**Check:**

1. The API Gateway URL is correct (includes `/mcp`)
2. Connector is added in Settings → Connectors
3. Connector is enabled for the conversation (click "+" → Connectors → toggle on)

### Plugin initialization fails

**Check:**

1. API URLs are correct
2. API keys are valid (if required)
3. Network connectivity from Lambda
4. CloudWatch logs for specific errors

## Development

### How do I test locally?

**Option 1: Use the local server**

```bash
# Install aiohttp if needed
pip install aiohttp

# Start local server
python3 scripts/local_server.py

# In another terminal, test with curl
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

**Option 2: Test plugins directly**

Create a test script that imports and initializes your plugin:

```python
import asyncio
from plugins.anchorage_checkbook.plugin import AnchorageCheckbookPlugin

async def test():
    # An empty config uses the default service URL.
    plugin = AnchorageCheckbookPlugin({})
    await plugin.initialize()
    tools = plugin.get_tools()
    print(f"Tools: {[t.name for t in tools]}")
    await plugin.shutdown()

asyncio.run(test())
```

**Option 3: Run unit tests**

```bash
pip install -r requirements-dev.txt
pytest tests/ -n auto
```

### Can I contribute?

Yes! Contributions are welcome. Please open an issue or pull request.

### Where is the code?

[GitHub Repository](https://github.com/codeforanchorage/anchorage-checkbook-mcp). The framework this fork is built on is [OpenContext](https://github.com/thealphacubicle/OpenContext).

## Support

### Where can I get help?

- [GitHub Issues](https://github.com/codeforanchorage/anchorage-checkbook-mcp/issues)
- [Documentation](.)
- [FAQ](FAQ.md) (this page)

### How do I report a bug?

Open an issue on GitHub with:

- Description of the problem
- Steps to reproduce
- Error messages/logs
- Configuration (redact secrets)

### How do I request a feature?

Open an issue on GitHub with:

- Description of the feature
- Use case
- Proposed implementation (if you have one)
