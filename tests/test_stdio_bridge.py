"""The Python stdio bridge must not change what the client sent.

MCP clients write UTF-8. The bridge used to read stdin as text, which on
Windows decodes with cp1252: an accented vendor name arrived at the
server as mojibake and the query silently matched nothing.
"""

import io
import json
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import stdio_bridge


def run_bridge(stdin_bytes, urlopen):
    """Run main() over raw stdin bytes; return (sent bodies, stdout lines)."""
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        return urlopen(req)

    fake_stdin = MagicMock()
    fake_stdin.buffer = io.BytesIO(stdin_bytes)
    out = io.StringIO()
    with (
        patch.object(sys, "stdin", fake_stdin),
        patch.object(sys, "stdout", out),
        patch.object(sys, "argv", ["stdio_bridge.py"]),
        patch.object(stdio_bridge.urllib.request, "urlopen", fake_urlopen),
    ):
        stdio_bridge.main()
    return sent, [json.loads(line) for line in out.getvalue().splitlines()]


def ok_response(payload):
    def urlopen(req):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    return urlopen


def test_non_ascii_arguments_survive_whatever_the_locale_is():
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"arguments": {"name_contains": "café Álvarez"}},
    }
    line = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
    sent, out = run_bridge(line, ok_response({"jsonrpc": "2.0", "id": 1, "result": {}}))
    assert sent[0]["params"]["arguments"]["name_contains"] == "café Álvarez"
    assert out == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_json_that_is_not_an_object_gets_an_error_not_a_crash():
    sent, out = run_bridge(b"42\n[1]\n", ok_response({}))
    assert sent == []
    assert [o["error"]["code"] for o in out] == [-32600, -32600]


def test_a_json_rpc_error_on_a_4xx_is_passed_through():
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32022, "message": "Unsupported protocol version"},
        }
    ).encode("utf-8")

    def urlopen(req):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, io.BytesIO(body)
        )

    line = b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n'
    _, out = run_bridge(line, urlopen)
    assert out[0]["id"] == 7
    assert out[0]["error"]["code"] == -32022


def test_a_notification_produces_no_output():
    line = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    _, out = run_bridge(line, ok_response({}))
    assert out == []
