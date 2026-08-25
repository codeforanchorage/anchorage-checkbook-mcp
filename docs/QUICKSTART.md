# Quick Start Guide

Get the Anchorage Open Checkbook MCP server running in 5 minutes.

## Prerequisites

- Python 3.11+
- Terraform >= 1.0
- AWS CLI configured with credentials
- GitHub account (to fork repository)

## Step 1: Clone

```bash
git clone https://github.com/codeforanchorage/anchorage-checkbook-mcp.git
cd anchorage-checkbook-mcp
```

## Step 2: Configure

This fork ships its config ready to go — copy it into place:

```bash
cp config-anchorage-checkbook.yaml config.yaml
```

That enables `anchorage_checkbook`, pointed at the Municipality of
Anchorage's public Open Checkbook Feature Service. Nothing else to edit.

**Important:** exactly ONE plugin may be enabled. The deploy script rejects
zero or multiple — one fork = one MCP server.

## Step 3: Deploy

Run the deployment script:

```bash
./scripts/deploy.sh --environment staging     # or: -e prod
```

`--environment` is required: it selects both the tfvars file and the
Terraform workspace (`anchorage-checkbook-staging` /
`anchorage-checkbook-prod`), which share a state bucket with other MCP
servers. Deploy to staging first.

The script will:

1. Validate configuration (ensures ONE plugin enabled)
2. Package the Lambda code, with `config.yaml` inside the bundle
3. Run `terraform plan` and show it to you
4. Apply only after you confirm
5. Output the API Gateway URL

You'll see output like:

```
✅ Deployment complete!

API Gateway URL (use for Claude Connectors):
https://xxx.execute-api.us-east-1.amazonaws.com/staging/mcp
```

## Step 4: Connect via Claude Connectors

Connect using **Claude Connectors** (same steps on both Claude.ai and Claude Desktop):

1. Go to **Settings** → **Connectors** (or **Customize** → **Connectors** on claude.ai)
2. Click **Add custom connector**
3. Enter a name (e.g. "Anchorage Open Checkbook") and your API Gateway URL

Get the URL from the deploy output, or run:

```bash
cd terraform/aws
terraform workspace select anchorage-checkbook-staging   # or -prod
terraform output -raw api_gateway_url
```

## Step 5: Test

**Test locally first (optional):**

```bash
# Start local server (routes through the same handler production uses)
python3 scripts/local_server.py

# In another terminal, test with curl. Note the /mcp path -- the root
# path is not served.
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
# -> {"jsonrpc": "2.0", "id": 1, "result": {}}
```

A healthy `ping` returns an empty `result` object; the liveness signal is
the response itself, not its body.

For a fuller check, run the smoke test against whatever you started:

```bash
python scripts/smoke_prod.py http://localhost:8000/mcp
```

**Test in Claude:**

Enable your connector in the chat (click "+" → Connectors → toggle on), then ask:

```
What did the Municipality of Anchorage spend by department in FY2025?
```

Claude will call `spending_stats` and report net figures, with the
duplicate-filter state and any known data gaps attached to the answer.

## Troubleshooting

### Deploy Script Fails: "Multiple Plugins Enabled"

**Solution:** Enable only ONE plugin in `config.yaml`. Disable all others.

### Lambda URL Not Working

**Check:**

1. Lambda function exists in AWS Console
2. Function URL is enabled
3. Configuration is correct

### Claude Can't Connect

**Check:**

1. API Gateway or Lambda URL is correct (includes `/mcp`)
2. Connector is added in Settings → Connectors
3. Connector is enabled for the conversation (click "+" → Connectors → toggle on)

## Next Steps

- Read [Architecture Guide](ARCHITECTURE.md)
- Create [Custom Plugin](CUSTOM_PLUGINS.md)
- See [`config-example.yaml`](../config-example.yaml) for every configurable option

## Getting Help

- [FAQ](FAQ.md)
- [GitHub Issues](https://github.com/codeforanchorage/anchorage-checkbook-mcp/issues)
- [Documentation](.)
