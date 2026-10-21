"""Smoke test for the Anchorage Open Checkbook MCP server.

Exercises the JSON-RPC surface, the transport rules, the caller-error codes,
the structured-output contract and the data-quality guardrails end-to-end
against a running deployment. Read-only; paces calls to stay under the API
Gateway rate limit (5 rps) and the WAF per-IP cap (300/5min).

    python scripts/smoke_prod.py                       # prod (custom domain)
    python scripts/smoke_prod.py http://localhost:8000/mcp
    python scripts/smoke_prod.py https://<id>.execute-api.us-west-2.amazonaws.com/staging/mcp

WHAT THIS DOES NOT ASSERT, on purpose
-------------------------------------
The MOA republishes these tables periodically -- the PubDate snapshot moved
from 2026-08-02 to 2026-08-24 while this was being written -- so anything
pinned to a dollar figure or a row count is a test that will one day fail
for being right. There are no assertions here that spending in a year
equals N, that a vendor has M spellings, or that a grouping has K buckets.
A previous smoke test in this family pinned `buckets == 3`, which depended
on how a filter narrowed the query, and then failed against correct
behaviour.

What is asserted instead is the CAPABILITY, and the invariants that make a
number safe to read: that a known vendor returns at least one spelling,
that a fiscal year returns a numeric net value of EITHER SIGN, that the
duplicate-filter state is always declared, that structuredContent is
present on both a hit and a miss, and that a query matching nothing says so
rather than rendering as $0. Those hold whatever this week's data says.
"""

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://checkbook.codeforanchorage.org/mcp"
URL = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL

PASS = "PASS"
FAIL = "FAIL"
results = []
_id = [0]

try:
    from jsonschema import ValidationError, validate

    HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover - smoke script runs outside the venv too
    HAVE_JSONSCHEMA = False


def check(label, ok, detail=""):
    results.append((label, ok))
    mark = PASS if ok else FAIL
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


def raw(body, headers=None, method="POST"):
    """Return (status, parsed_body_or_None). Does not raise on 4xx/5xx."""
    req = urllib.request.Request(
        URL,
        data=body.encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        status = e.code
    time.sleep(0.4)  # pace under 5 rps
    try:
        return status, json.loads(payload) if payload else None
    except json.JSONDecodeError:
        return status, None


def rpc(method, params=None, headers=None):
    _id[0] += 1
    payload = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        payload["params"] = params
    _status, body = raw(json.dumps(payload), headers=headers)
    return body or {}


def call_tool(name, args):
    return rpc(
        "tools/call",
        {"name": f"anchorage_checkbook__{name}", "arguments": args},
    )


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def structured_of(resp):
    return resp.get("result", {}).get("structuredContent")


print(f"Smoke testing {URL}\n")

# ── 1. transport ───────────────────────────────────────────────────────────

try:
    r = rpc("ping")
    # The spec defines the ping result as an empty object -- the liveness
    # signal is the successful response itself, not its body.
    check(
        "ping returns {}",
        r.get("result") == {} and "error" not in r,
        str(r.get("result")),
    )
except Exception as e:
    check("ping returns {}", False, repr(e))

try:
    r = rpc(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1.0"},
        },
    )
    si = r["result"]["serverInfo"]
    pv = r["result"]["protocolVersion"]
    # The current revision must be echoed back, not negotiated down.
    check(
        "initialize negotiates 2025-11-25",
        pv == "2025-11-25" and si["name"] == "Anchorage Open Checkbook MCP",
        json.dumps({"serverInfo": si, "protocolVersion": pv}),
    )
    # The instructions block is where the dataset's traps reach every
    # consumer before a single tool is called.
    instructions = r["result"].get("instructions", "")
    check(
        "initialize carries the data-trap instructions",
        "Duplicate" in instructions and "NET" in instructions,
        f"{len(instructions)} chars",
    )
except Exception as e:
    check("initialize negotiates 2025-11-25", False, repr(e))

PING = '{"jsonrpc":"2.0","id":99,"method":"ping"}'

for label, headers, expect in [
    ("no Origin is allowed (native clients)", {}, 200),
    ("allowlisted Origin is allowed", {"Origin": "https://claude.ai"}, 200),
    ("claude.com Origin is allowed", {"Origin": "https://claude.com"}, 200),
    ("disallowed Origin is refused", {"Origin": "https://evil.example"}, 403),
    (
        "unknown protocol version is refused",
        {"MCP-Protocol-Version": "1999-01-01"},
        400,
    ),
    (
        "supported protocol version passes",
        {"MCP-Protocol-Version": "2025-11-25"},
        200,
    ),
]:
    try:
        status, _body = raw(PING, headers=headers)
        check(label, status == expect, f"HTTP {status} (expected {expect})")
    except Exception as e:
        check(label, False, repr(e))

try:
    # A version served in neither era gets the 2026-07-28
    # UnsupportedProtocolVersionError, not the bare -32600 this server
    # used while it was legacy-only, and lists what it does serve so the
    # client can retry with one of those.
    status, body = raw(PING, headers={"MCP-Protocol-Version": "1999-01-01"})
    err = (body or {}).get("error", {})
    check(
        "unknown protocol version -> -32022 with the supported list",
        status == 400
        and err.get("code") == -32022
        and "2026-07-28" in err.get("data", {}).get("supported", []),
        f"HTTP {status} code={err.get('code')}",
    )
except Exception as e:
    check("unknown protocol version -> -32022 with the supported list", False, repr(e))

try:
    status, _body = raw(None, method="GET")
    check("GET /mcp is not allowed", status == 405, f"HTTP {status}")
except Exception as e:
    check("GET /mcp is not allowed", False, repr(e))


# ── 2. caller errors are caller errors, not server faults ──────────────────

try:
    r = rpc("server/discover")
    err = r.get("error", {})
    check(
        "unknown method -> -32601",
        err.get("code") == -32601,
        str(err.get("code")),
    )
except Exception as e:
    check("unknown method -> -32601", False, repr(e))

try:
    r = rpc(
        "tools/call",
        {"name": "anchorage_checkbook__no_such_tool", "arguments": {}},
    )
    err = r.get("error", {})
    # The available-tool list rides in `data` so a model can self-correct.
    check(
        "unknown tool -> -32602 with the available list",
        err.get("code") == -32602 and "spending_stats" in str(err.get("data")),
        str(err.get("code")),
    )
except Exception as e:
    check("unknown tool -> -32602 with the available list", False, repr(e))

try:
    r = rpc("tools/call", {"arguments": {}})
    check(
        "tools/call with no name -> -32602",
        r.get("error", {}).get("code") == -32602,
        str(r.get("error")),
    )
except Exception as e:
    check("tools/call with no name -> -32602", False, repr(e))

try:
    r = rpc(
        "tools/call",
        {"name": "anchorage_checkbook__spending_stats", "arguments": "table=0"},
    )
    err = r.get("error", {})
    # Before the fix this reached the plugin and came back as a tool RESULT
    # reading "'str' object has no attribute 'get'".
    check(
        "non-object arguments -> -32602, not a tool result",
        err.get("code") == -32602 and "result" not in r,
        str(err.get("code")),
    )
except Exception as e:
    check("non-object arguments -> -32602, not a tool result", False, repr(e))

try:
    # 'FY25' is exactly what a model sends for a fiscal year.
    r = call_tool("spending_stats", {"table": 0, "fiscal_year": "FY25"})
    msg = text_of(r)
    check(
        "a bad fiscal_year names the argument, not Python internals",
        "fiscal_year" in msg and "FY25" in msg and "invalid literal" not in msg,
        msg[:90],
    )
except Exception as e:
    check("a bad fiscal_year names the argument", False, repr(e))

try:
    # PubDate is the ETL snapshot stamp; filtering on it is a caller error,
    # not a fault, and the message must redirect to the real time axis.
    r = call_tool(
        "query_checkbook", {"table": 0, "where": "PubDate > DATE '2026-01-01'"}
    )
    msg = text_of(r)
    check(
        "a PubDate filter is refused with a redirect",
        r["result"].get("isError") is True and "Fiscal_Year" in msg,
        msg[:90],
    )
except Exception as e:
    check("a PubDate filter is refused with a redirect", False, repr(e))

try:
    # The reason this fork does not use the arcgis WHERE blocklist: 'UNION'
    # is real vendor data here, and rejecting it would be a false positive.
    r = call_tool(
        "query_checkbook",
        {"table": 0, "where": "Vendor_Name LIKE '%Union%'", "limit": 3},
    )
    check(
        "a vendor named 'Union' is queryable, not an injection",
        r["result"].get("isError") is not True,
        text_of(r)[:80],
    )
except Exception as e:
    check("a vendor named 'Union' is queryable", False, repr(e))


# ── 3. tool metadata ───────────────────────────────────────────────────────

SCHEMA_TOOLS = {
    "spending_stats",
    "search_by_vendor",
    "top_vendors",
    "get_line_items",
    "list_field_values",
    "query_checkbook",
}
schemas = {}

try:
    tools = rpc("tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    # Assert the CAPABILITY -- every tool is titled and prefixed -- not a
    # tool count, which changes whenever a tool is added.
    check(
        "every tool is prefixed and titled",
        names and all(t["name"].startswith("anchorage_checkbook__") for t in tools)
        and all(t.get("title") for t in tools),
        f"{len(tools)} tools",
    )
    check(
        "no tool advertises idempotentHint",
        all("idempotentHint" not in (t.get("annotations") or {}) for t in tools),
        "meaningful only when readOnlyHint is false",
    )
    declared = {
        t["name"].split("__", 1)[1] for t in tools if "outputSchema" in t
    }
    check(
        "the six data tools declare an outputSchema",
        declared == SCHEMA_TOOLS,
        f"declared: {sorted(declared)}",
    )
    check(
        "the two discovery tools declare none",
        not declared & {"list_tables", "get_table_schema"},
        "their value is the prose guidance",
    )
    for t in tools:
        if "outputSchema" in t:
            schemas[t["name"].split("__", 1)[1]] = t["outputSchema"]
except Exception as e:
    check("tools/list", False, repr(e))


def validate_structured(label, tool, structured):
    """Validate against the schema the SERVER advertised, not a local copy,
    so a schema and its data cannot drift apart without this failing."""
    if not HAVE_JSONSCHEMA:
        check(f"{label}: schema validation", False, "jsonschema not installed")
        return
    if tool not in schemas:
        check(f"{label}: schema validation", False, "no advertised schema")
        return
    try:
        validate(instance=structured, schema=schemas[tool])
        check(f"{label}: conforms to the advertised outputSchema", True)
    except ValidationError as e:
        path = "/".join(str(x) for x in e.absolute_path)
        check(
            f"{label}: conforms to the advertised outputSchema",
            False,
            f"at {path}: {e.message[:90]}",
        )


# ── 4. the tools, and the invariants that make a number safe to read ───────

try:
    r = call_tool("list_tables", {})
    text = text_of(r)
    check(
        "list_tables describes the tables and the duplicate default",
        "OC_UnauditedExpenditure_NonPayroll" in text and "Duplicate" in text,
        f"{len(text)} chars",
    )
    # A discovery tool must NOT emit structuredContent -- it declares no
    # schema, and emitting one anyway would be the inverse conformance bug.
    check(
        "list_tables emits no structuredContent",
        structured_of(r) is None,
        "declares no outputSchema",
    )
except Exception as e:
    check("list_tables", False, repr(e))

try:
    r = call_tool("get_table_schema", {"table": 0})
    text = text_of(r)
    check(
        "get_table_schema names fields and their traps",
        "Vendor_Name" in text and "PubDate" in text,
        f"{len(text)} chars",
    )
except Exception as e:
    check("get_table_schema", False, repr(e))

try:
    # A net value of EITHER SIGN is correct; the figure itself is not pinned.
    r = call_tool("spending_stats", {"table": 0, "fiscal_year": 2025})
    sc = structured_of(r)
    value = sc["rows"][0]["value"]
    check(
        "spending_stats returns a numeric net value of either sign",
        isinstance(value, (int, float)),
        f"{value!r} ({sc['summary']['stat_type']} of {sc['summary']['measure']})",
    )
    check(
        "the stat type travels with the number",
        sc["summary"]["stat_type"] == "sum",
        "so a percentile can never be read as a sum",
    )
    validate_structured("spending_stats", "spending_stats", sc)
except Exception as e:
    check("spending_stats returns a numeric net value", False, repr(e))

try:
    r = call_tool(
        "spending_stats",
        {"table": 0, "group_by": ["Business_Area"], "fiscal_year": 2025},
    )
    sc = structured_of(r)
    rows = sc["rows"]
    # Every row accounted for: a group key and a row_count on each.
    check(
        "every grouped row carries its key and its line-item count",
        bool(rows)
        and all("Business_Area" in row["group"] for row in rows)
        and all(row["row_count"] is not None for row in rows),
        f"{len(rows)} groups",
    )
    validate_structured("spending_stats grouped", "spending_stats", sc)
except Exception as e:
    check("spending_stats grouped", False, repr(e))

try:
    # A known vendor returns >= 1 spelling. Which spellings, and how much
    # each received, is data that moves.
    r = call_tool("search_by_vendor", {"name_contains": "chugach"})
    sc = structured_of(r)
    rows = sc["rows"]
    check(
        "a known vendor returns at least one spelling",
        len(rows) >= 1,
        f"{len(rows)} spelling(s)",
    )
    check(
        "every spelling row carries a raw name and a net sum",
        all("name" in row and "net_sum" in row for row in rows),
        "raw stored spellings, never merged",
    )
    check(
        "no row is labelled with a bare 'total'",
        all("total" not in row for row in rows),
        "every figure here is NET",
    )
    validate_structured("search_by_vendor", "search_by_vendor", sc)
except Exception as e:
    check("search_by_vendor on a known vendor", False, repr(e))

try:
    r = call_tool("top_vendors", {"table": 0, "n": 5})
    sc = structured_of(r)
    rows = sc["rows"]
    check(
        "top_vendors ranks and excludes the NULL-payee bucket",
        bool(rows)
        and [row["rank"] for row in rows] == list(range(1, len(rows) + 1))
        and all(row["name"] for row in rows),
        f"{len(rows)} payees",
    )
    validate_structured("top_vendors", "top_vendors", sc)
except Exception as e:
    check("top_vendors", False, repr(e))

try:
    r = call_tool("get_line_items", {"table": 0, "fiscal_year": 2025, "limit": 5})
    sc = structured_of(r)
    summary = sc["summary"]
    check(
        "get_line_items reports the true match count, not the page size",
        summary["total_count"] is not None
        and summary["total_count"] >= summary["returned"],
        f"returned {summary['returned']} of {summary['total_count']}",
    )
    check(
        "a truncated page says so",
        summary["truncated"] == (summary["total_count"] > summary["returned"]),
        f"truncated={summary['truncated']}",
    )
    # Location is the vendor's billing address, named so nobody maps it.
    check(
        "Location is exposed as billing_city/billing_state",
        all("Location" not in row for row in sc["rows"])
        and any("billing_city" in row for row in sc["rows"]),
        "not Anchorage geography",
    )
    validate_structured("get_line_items", "get_line_items", sc)
except Exception as e:
    check("get_line_items", False, repr(e))

try:
    r = call_tool("list_field_values", {"table": 0, "field": "Month_Fiscal_Period"})
    sc = structured_of(r)
    values = sc["values"]
    adjustment = [v for v in values if v.get("is_adjustment_period")]
    check(
        "fiscal periods are numbers with an adjustment-period flag",
        bool(values) and all("value" in v for v in values),
        f"{len(values)} periods, {len(adjustment)} flagged as adjustment",
    )
    # Never a month name: a consumer handed one WILL map period 14 to it.
    blob = json.dumps(sc).lower()
    check(
        "no month name appears in the structured half",
        not any(m in blob for m in ("january", "february", "march", "april")),
        "periods 13-16 are year-end adjustments, not months",
    )
    validate_structured("list_field_values", "list_field_values", sc)
except Exception as e:
    check("list_field_values", False, repr(e))

try:
    r = call_tool(
        "query_checkbook",
        {"table": 0, "where": "Amount > 1000000", "limit": 3},
    )
    sc = structured_of(r)
    check(
        "query_checkbook returns rows for a raw WHERE",
        bool(sc["rows"]),
        f"{len(sc['rows'])} rows",
    )
    validate_structured("query_checkbook", "query_checkbook", sc)
except Exception as e:
    check("query_checkbook", False, repr(e))


# ── 5. the guardrails that stop a confidently wrong figure ─────────────────

try:
    # The single difference between this server's numbers and the published
    # dashboard's. It must be declared on EVERY response, either way.
    filtered = structured_of(call_tool("get_line_items", {"table": 0, "limit": 1}))
    included = structured_of(
        call_tool(
            "get_line_items",
            {"table": 0, "limit": 1, "include_duplicates": True},
        )
    )
    check(
        "the duplicate-filter state is declared on every response",
        filtered["query"]["duplicate_filter"] == "excluded"
        and included["query"]["duplicate_filter"] == "included",
        "required field of the query block",
    )
    check(
        "including duplicates carries a warning caveat",
        any(c["code"] == "DUPLICATES_INCLUDED" for c in included["caveats"]),
        "sums over that response double-count",
    )
except Exception as e:
    check("the duplicate-filter state is declared", False, repr(e))

try:
    # structuredContent must be present on a MISS as well as a hit --
    # a declared outputSchema is binding on every branch.
    r = call_tool("search_by_vendor", {"name_contains": "zzzznotavendorzzzz"})
    sc = structured_of(r)
    text = text_of(r)
    check(
        "a vendor miss still returns structuredContent",
        sc is not None and sc["rows"] == [],
        "the zero-result branch conforms too",
    )
    # And a miss is ABSENT DATA, not a finding of zero.
    # The useful advice inside search_by_vendor's OWN output is to widen
    # the fragment -- pointing at itself would be no help.
    check(
        "a vendor miss is reported as absent, not as $0",
        "ABSENT DATA" in text and "SHORTER" in text,
        "spellings are unnormalized; a miss may mean a different spelling",
    )
    validate_structured("vendor miss", "search_by_vendor", sc)
except Exception as e:
    check("a vendor miss still returns structuredContent", False, repr(e))

try:
    r = call_tool("get_line_items", {"table": 0, "fiscal_year": 2011, "limit": 5})
    sc = structured_of(r)
    check(
        "a year with no rows reports a complete count of 0, not null",
        sc is not None and sc["summary"]["total_count"] == 0,
        "0 means a known zero; null would mean unmeasured",
    )
    check(
        "a year with no rows says ABSENT DATA, not $0",
        "ABSENT DATA" in text_of(r),
        "a false zero is this server's worst failure mode",
    )
    validate_structured("year miss", "get_line_items", sc)
except Exception as e:
    check("a year with no rows", False, repr(e))

try:
    # The NULL-payee bucket outweighs every real vendor on table 0; grouped
    # and unlabelled it reads as the largest vendor in Anchorage.
    r = call_tool(
        "spending_stats",
        {"table": 0, "group_by": ["Vendor_Name"], "fiscal_year": 2025, "limit": 5},
    )
    sc = structured_of(r)
    text = text_of(r)
    has_null_group = any(row["group"].get("Vendor_Name") is None for row in sc["rows"])
    check(
        "a NULL payee group is labelled, never left blank",
        (not has_null_group)
        or ("no payee" in text and "must never be reported as a vendor" in text),
        f"null group present: {has_null_group}",
    )
    check(
        "the count column names its grain",
        "line_items" in text,
        "line items, not distinct vendors",
    )
except Exception as e:
    check("a NULL payee group is labelled", False, repr(e))

try:
    # Revenue has no FY2024 at all. A missing year must not read as zero.
    r = call_tool("spending_stats", {"table": 4, "fiscal_year": 2024})
    sc = structured_of(r)
    check(
        "the Revenue FY2024 gap is reported as a gap",
        any(c["code"] == "KNOWN_GAP" for c in sc["caveats"]),
        "a DATA GAP, not zero revenue",
    )
except Exception as e:
    check("the Revenue FY2024 gap", False, repr(e))

try:
    # Every response's figures are net; the caveat must always say so.
    sc = structured_of(call_tool("spending_stats", {"table": 0, "fiscal_year": 2025}))
    check(
        "every figure is declared NET of offsetting entries",
        any(c["code"] == "NET_OF_OFFSETS" for c in sc["caveats"]),
        "gross totals are meaningless here",
    )
except Exception as e:
    check("every figure is declared NET", False, repr(e))


# ── 7. modern era (MCP 2026-07-28: stateless, per-request _meta) ──────────
# Everything above rides the legacy initialize handshake. claude.ai and
# Claude Code open with THIS era instead, so a regression here is invisible
# to the legacy checks yet breaks every real session.

SERVER_NAME = "Anchorage Open Checkbook MCP"
N_TOOLS = 8
MODERN = "2026-07-28"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "smoke", "version": "1.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def modern(method, params=None, rid=None, name=None, version=MODERN):
    """One modern-era request: _meta in the body, the mirrored headers the
    Streamable HTTP transport requires, and (status, body) back."""
    _id[0] += 1
    params = dict(params or {})
    params["_meta"] = {
        **MODERN_META,
        "io.modelcontextprotocol/protocolVersion": version,
    }
    headers = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if name:
        headers["Mcp-Name"] = name
    payload = {
        "jsonrpc": "2.0",
        "id": rid if rid is not None else _id[0],
        "method": method,
        "params": params,
    }
    status, body = raw(json.dumps(payload), headers=headers)
    return status, body or {}


# 20. server/discover: the modern replacement for initialize
try:
    status, r = modern("server/discover")
    res = r.get("result", {})
    versions = res.get("supportedVersions", [])
    server_info = res.get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {})
    check(
        "modern: server/discover",
        status == 200
        and versions[:1] == [MODERN]
        and "2025-03-26" in versions
        and res.get("resultType") == "complete"
        and res.get("cacheScope") == "public"
        and isinstance(res.get("ttlMs"), int)
        and server_info.get("name") == SERVER_NAME
        and bool(res.get("instructions")),
        f"status={status} versions={versions}",
    )
except Exception as e:
    check("modern: server/discover", False, repr(e))

# 21. tools/list carries the CacheableResult hints and resultType
try:
    status, r = modern("tools/list")
    res = r.get("result", {})
    tools = res.get("tools", [])
    names = [t["name"] for t in tools]
    check(
        "modern: tools/list cache hints + titles",
        status == 200
        and len(names) == N_TOOLS
        and res.get("resultType") == "complete"
        and res.get("ttlMs") == 300_000
        and res.get("cacheScope") == "public"
        and all("title" in t and "annotations" in t for t in tools),
        f"status={status} n={len(names)} ttlMs={res.get('ttlMs')}",
    )
    # Deterministic order (spec SHOULD): a second listing must match.
    _status2, r2 = modern("tools/list")
    names2 = [t["name"] for t in r2.get("result", {}).get("tools", [])]
    check(
        "modern: tools/list order is deterministic",
        names == names2,
        "identical" if names == names2 else "order differs between calls",
    )
except Exception as e:
    check("modern: tools/list cache hints + titles", False, repr(e))

# 22. tools/call with the mirrored Mcp-Name header; result stamped resultType
try:
    tool = "anchorage_checkbook__list_tables"
    status, r = modern("tools/call", {"name": tool, "arguments": {}}, name=tool)
    res = r.get("result", {})
    check(
        "modern: tools/call list_tables",
        status == 200
        and res.get("resultType") == "complete"
        and not res.get("isError")
        and "OC_UnauditedExpenditure_NonPayroll" in res["content"][0]["text"]
        and "io.modelcontextprotocol/serverInfo" in res.get("_meta", {}),
        f"status={status}",
    )
except Exception as e:
    check("modern: tools/call list_tables", False, repr(e))

# 23. ping is gone in this era: 404 + -32601, not a silent {}
try:
    status, r = modern("ping")
    check(
        "modern: ping -> 404/-32601",
        status == 404 and r.get("error", {}).get("code") == -32601,
        f"status={status} body={json.dumps(r)[:80]}",
    )
except Exception as e:
    check("modern: ping -> 404/-32601", False, repr(e))

# 24. unsupported version -> 400 / -32022 listing what we serve, id echoed
try:
    status, r = modern("tools/list", rid=4242, version="2099-01-01")
    err = r.get("error", {})
    check(
        "modern: unsupported version -> 400/-32022 (id echoed)",
        status == 400
        and err.get("code") == -32022
        and err.get("data", {}).get("requested") == "2099-01-01"
        and MODERN in err.get("data", {}).get("supported", [])
        and r.get("id") == 4242,
        f"status={status} id={r.get('id')} code={err.get('code')}",
    )
except Exception as e:
    check("modern: unsupported version -> 400/-32022 (id echoed)", False, repr(e))

# 25. header/body disagreement -> 400 / -32020 HeaderMismatch
try:
    status, r = raw(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": MODERN_META},
            }
        ),
        headers={"MCP-Protocol-Version": MODERN, "Mcp-Method": "ping"},
    )
    check(
        "modern: header mismatch -> 400/-32020",
        status == 400 and (r or {}).get("error", {}).get("code") == -32020,
        f"status={status}",
    )
except Exception as e:
    check("modern: header mismatch -> 400/-32020", False, repr(e))

# 26. missing clientCapabilities -> malformed, 400 / -32602
try:
    status, r = raw(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {"io.modelcontextprotocol/protocolVersion": MODERN}
                },
            }
        ),
        headers={"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"},
    )
    check(
        "modern: missing clientCapabilities -> 400/-32602",
        status == 400 and (r or {}).get("error", {}).get("code") == -32602,
        f"status={status}",
    )
except Exception as e:
    check("modern: missing clientCapabilities -> 400/-32602", False, repr(e))

# 27. transport hygiene in this era: no standalone SSE stream (GET is 405
#     even when asking for text/event-stream); a disallowed Origin is 403
try:
    status, _ = raw(None, headers={"Accept": "text/event-stream"}, method="GET")
    check("modern: GET /mcp (event-stream) -> 405", status == 405, f"status={status}")
    status, _ = raw(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        headers={"Origin": "https://evil.example"},
    )
    check("modern: bad Origin -> 403", status == 403, f"status={status}")
except Exception as e:
    check("modern: transport hygiene", False, repr(e))


# ── summary ────────────────────────────────────────────────────────────────

print()
failed = [label for label, ok in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("\nFAILED:")
    for label in failed:
        print(f"  - {label}")
sys.exit(1 if failed else 0)
