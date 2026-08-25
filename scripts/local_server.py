# run_local_server.py
"""Run OpenContext MCP server locally for testing (no Lambda needed)."""

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

# Add project root to Python path so we can import from core
project_root = Path(__file__).parent.parent.resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import yaml
from aiohttp import web

from core.logging_utils import configure_json_logging
from core.validators import get_logging_config
from server import http_handler
from server.http_handler import UniversalHTTPHandler

logger = logging.getLogger(__name__)

# Load config (OPENCONTEXT_CONFIG env var for tests; default config.yaml)
_config_path = os.environ.get("OPENCONTEXT_CONFIG", "config.yaml")
with open(_config_path) as f:
    config = yaml.safe_load(f)

# Configure JSON logging - use pretty format for local development
logging_config = get_logging_config(config)
configure_json_logging(
    level=logging_config.get("level", "INFO"),
    pretty=True,  # Pretty-print JSON for better local readability
)

# The single request handler, identical to the one the Lambda adapter
# uses. Deliberately NOT a local PluginManager/MCPServer pair: this
# script used to call MCPServer.handle_http_request directly, which
# skipped the Origin allowlist, the MCP-Protocol-Version check and
# path/method validation, so local dev could not reproduce -- or catch a
# regression in -- any of them.
_handler = UniversalHTTPHandler()


async def init_server():
    """Warm the handler's plugins so startup failures surface immediately.

    UniversalHTTPHandler initializes lazily on first request; doing it
    here instead means a bad config fails at launch rather than on the
    first curl, and lets us print what actually loaded.
    """
    print("🚀 Initializing OpenContext MCP Server locally...")

    await http_handler._initialize_server()

    plugin_manager = http_handler._plugin_manager
    print("✅ Server initialized successfully")
    print(f"Loaded plugins: {list(plugin_manager.plugins.keys())}")
    print(f"Available tools: {len(plugin_manager.get_all_tools())}")


async def handle_mcp_request(request):
    """Adapt an aiohttp request onto UniversalHTTPHandler.

    Thin on purpose -- the mirror of server/adapters/aws_lambda.py. All
    protocol behaviour (Origin allowlist, MCP-Protocol-Version check,
    path/method validation, session IDs, CORS, request logging) lives in
    the handler, so local dev exercises exactly what prod runs.
    """
    body = await request.text()

    # HTTP header names are case-insensitive; the handler reads them
    # lowercased, the same normalization the Lambda adapter applies.
    headers = {k.lower(): v for k, v in request.headers.items()}

    # Local-only convenience: surface the tool and its arguments up front.
    # The handler logs the request too, but not at this granularity, and
    # seeing the arguments is most of the value of running locally.
    try:
        request_json = json.loads(body)
        if request_json.get("method") == "tools/call":
            params = request_json.get("params", {})
            logger.info(
                "Incoming tool call",
                extra={
                    "method": "tools/call",
                    "tool_name": params.get("name"),
                    "tool_arguments": params.get("arguments") or None,
                },
            )
    except (json.JSONDecodeError, AttributeError):
        pass

    status_code, response_headers, response_body = await _handler.handle_request(
        method=request.method,
        path=request.path,
        body=body,
        headers=headers,
        request_id=str(uuid.uuid4()),
    )

    return web.Response(
        text=response_body,
        status=status_code,
        headers=response_headers,
    )


async def handle_mcp_options(request):
    """CORS preflight, delegated to the same handler as prod."""
    status_code, response_headers, response_body = _handler.handle_options(
        request_id=str(uuid.uuid4()),
        request_origin=request.headers.get("Origin"),
    )
    return web.Response(
        text=response_body,
        status=status_code,
        headers=response_headers,
    )


async def start_server():
    """Start local HTTP server."""
    await init_server()

    app = web.Application()
    app.router.add_post("/mcp", handle_mcp_request)
    app.router.add_options("/mcp", handle_mcp_options)
    # The hardened GCC route shares this handler in prod; serving it here
    # too keeps the local surface identical. Its API-key requirement is
    # enforced at API Gateway, so locally it behaves like /mcp.
    app.router.add_post("/mcp-gcc", handle_mcp_request)
    app.router.add_options("/mcp-gcc", handle_mcp_options)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 8000)
    await site.start()

    # Generate server name from config variables
    server_name = None
    if "plugins" in config:
        # Try to get city_name from enabled plugin
        for plugin_name, plugin_config in config["plugins"].items():
            if plugin_config.get("enabled"):
                if "city_name" in plugin_config:
                    city_name = plugin_config["city_name"].lower().replace(" ", "-")
                    server_name = f"{city_name}-opendata"
                    break
                elif "organization" in plugin_config:
                    org_name = plugin_config["organization"].lower().replace(" ", "-")
                    server_name = f"{org_name}-opendata"
                    break

        # Fallback to lambda_name or server_name from config
        if not server_name:
            if "aws" in config and "lambda_name" in config["aws"]:
                lambda_name = config["aws"]["lambda_name"]
                # Remove -mcp suffix if present
                server_name = lambda_name.replace("-mcp", "")
            elif "server_name" in config:
                server_name = (
                    config["server_name"].lower().replace(" ", "-").replace("'", "")
                )

    # Default fallback
    if not server_name:
        server_name = "opencontext-mcp"

    print("\n" + "=" * 50)
    print("🌐 Local MCP Server running!")
    print("=" * 50)
    print("URL: http://localhost:8000/mcp")
    print("\n" + "=" * 50)
    print("📋 Connect via Claude Connectors")
    print("=" * 50)
    print("\n1. Go to Settings → Connectors (or Customize → Connectors on claude.ai)")
    print("2. Click 'Add custom connector'")
    print("3. Enter a name and URL: http://localhost:8000/mcp")
    print("\nNote: Localhost works with Claude Desktop only (web needs a deployed URL).")
    print("\n" + "=" * 50)
    print("\nTest with:")
    print("  ./scripts/test_streamable_http.sh")
    print(
        '  or curl -X POST http://localhost:8000/mcp -H \'Content-Type: application/json\' -d \'{"jsonrpc":"2.0","id":1,"method":"ping"}\''
    )
    print("\nPress Ctrl+C to stop")
    print("=" * 50 + "\n")

    # Keep running
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        if http_handler._plugin_manager is not None:
            await http_handler._plugin_manager.shutdown()


if __name__ == "__main__":
    asyncio.run(start_server())
