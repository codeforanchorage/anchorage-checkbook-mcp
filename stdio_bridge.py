"""Stdio-to-HTTP bridge for connecting Claude to OpenContext MCP server.

Reads JSON-RPC messages from stdin, forwards them to the local HTTP server,
and writes responses to stdout. This bridges Claude's stdio MCP transport
to the OpenContext HTTP-based MCP server.
"""

import io
import json
import sys
import urllib.error
import urllib.request

SERVER_URL = "http://localhost:8000/mcp"


def _error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _emit(message):
    # json.dumps escapes non-ASCII by default, so the line is safe to write
    # whatever encoding stdout has.
    print(json.dumps(message), flush=True)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else SERVER_URL
    if not url.endswith("/mcp"):
        url = url.rstrip("/") + "/mcp"

    # MCP clients write UTF-8. Reading sys.stdin as text decodes with the
    # locale encoding instead (cp1252 on Windows), which turns a non-ASCII
    # argument such as an accented vendor name into mojibake that then
    # silently matches nothing. Decode the bytes as UTF-8 ourselves.
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

    for line in stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _emit(_error(None, -32700, "Parse error"))
            continue

        # Valid JSON that is not an object (a number, a string, an array)
        # is not a request this bridge can route.
        if not isinstance(request, dict):
            _emit(_error(None, -32600, "Invalid Request: expected a JSON object"))
            continue

        request_id = request.get("id")
        is_notification = request_id is None

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(request).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")

            if not body:
                if not is_notification:
                    _emit(_error(request_id, -32603, "Empty response"))
                continue

            response = json.loads(body)
            if is_notification:
                continue
            _emit(response)

        except urllib.error.HTTPError as e:
            if is_notification:
                continue
            # The server answers protocol errors (an unsupported version, a
            # refused Origin) with a JSON-RPC error body on a 4xx. Pass that
            # through so the client sees the reason, not just the status.
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                payload.setdefault("jsonrpc", "2.0")
                payload["id"] = request_id
                _emit(payload)
            else:
                _emit(_error(request_id, -32603, f"HTTP {e.code}"))

        except Exception as e:
            if is_notification:
                continue
            _emit(_error(request_id, -32603, str(e)))


if __name__ == "__main__":
    main()
