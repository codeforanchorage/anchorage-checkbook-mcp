# Scripts Directory

This directory contains utility scripts for OpenContext MCP server.

## Scripts

### `deploy.sh`

Deployment script that validates configuration and deploys the MCP server to AWS Lambda.

**Usage:**
```bash
./scripts/deploy.sh --environment <staging|prod> [--tfworkspace <name>] [--yes] [--allow-dirty]
```

**Options:**

| Flag | Short | Required | Description |
|------|-------|----------|-------------|
| `--environment` | `-e` | Yes | `staging` or `prod` |
| `--tfworkspace` | `-w` | No | Terraform workspace name (default: `anchorage-checkbook-staging` or `anchorage-checkbook-prod`) |
| `--yes` | | No | Apply the plan without the interactive confirmation |
| `--allow-dirty` | | No | Package a working tree with uncommitted changes (emergencies only) |
| `--help` | `-h` | No | Show help |

**Examples:**
```bash
./scripts/deploy.sh --environment staging
./scripts/deploy.sh -e prod
./scripts/deploy.sh --environment staging --yes
```

**What it does:**
- Refuses a working tree with uncommitted changes, so the live code always matches a commit
- Validates that exactly ONE plugin is enabled
- Packages the code for Lambda deployment, installing the versions in `uv.lock` when `uv` is available
- Writes `BUILD_INFO.json` (commit, dirty, locked) into the package
- Selects (or creates) the specified Terraform workspace
- Deploys to AWS using Terraform
- Outputs the API Gateway URL

**Requirements:**
- Python 3.11+
- AWS CLI configured
- Terraform installed
- Valid `config.yaml` in project root (`cp config-anchorage-checkbook.yaml config.yaml`)

### `local_server.py`

Local development server for testing the MCP server without deploying to Lambda.

**Usage:**
```bash
python3 scripts/local_server.py
```

**What it does:**
- Starts a local HTTP server on `http://localhost:8000/mcp` (and `/mcp-gcc`)
- Supports Streamable HTTP transport with session management
- Provides detailed logging for debugging, including tool names and arguments
- Routes every request through `UniversalHTTPHandler` — the *same* handler
  `server/adapters/aws_lambda.py` uses — so the Origin allowlist, the
  MCP-Protocol-Version check, path/method validation and CORS all behave
  locally exactly as they do in production. It is the only local entry
  point; a second copy at the repo root used to bypass the handler and has
  been removed.

**Requirements:**
- Python 3.11+
- `aiohttp` package (`pip install aiohttp`)
- Valid `config.yaml` in project root (`cp config-anchorage-checkbook.yaml config.yaml`)

**Testing:**
```bash
# Test with curl
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'

# Or use the test script
./scripts/test_streamable_http.sh
```

### `test_streamable_http.sh`

Test script for Streamable HTTP transport. Tests the full MCP lifecycle.

**Usage:**
```bash
./scripts/test_streamable_http.sh [BASE_URL] [TOOL_NAME] [TOOL_ARGS_JSON]
```

**Default:** `http://localhost:8000/mcp`

**What it tests:**
1. Initialize connection and extract session ID
2. List available tools
3. Call a tool (default: `anchorage_checkbook__search_by_vendor`)

**Requirements:**
- `jq` installed (`brew install jq` on macOS)
- MCP server running (local or deployed)

**Example:**
```bash
# Test local server
./scripts/test_streamable_http.sh

# Test a deployed server
./scripts/test_streamable_http.sh https://your-api-id.execute-api.us-west-2.amazonaws.com/staging/mcp
```

## Notes

- All scripts should be run from the project root directory
- Scripts automatically handle path resolution relative to their location
- Make sure scripts are executable: `chmod +x scripts/*.sh`
