"""Caller mistakes must not be logged as server faults.

A traceback is a claim that the server broke. Spending one on "that
fiscal year isn't a number" is what makes real faults hard to find: in
CloudWatch they look identical to argument validation.

ToolInputError is an explicit marker rather than an inference from
ValueError, and these tests pin the two cases where the inference would
have been wrong, plus the sweep that keeps new coercion sites from
drifting back in.

Scope: the enabled plugin, ``anchorage_checkbook``, and its
``where_validator``. The other plugins in this fork are ``enabled:
false`` and are deliberately not converted.
"""

import ast
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.interfaces import ToolInputError
from core.mcp_server import MCPServer
from plugins.anchorage_checkbook.config_schema import AnchorageCheckbookPluginConfig
from plugins.anchorage_checkbook.plugin import AnchorageCheckbookPlugin
from plugins.anchorage_checkbook.where_validator import (
    CheckbookWhereValidator,
    OrderByValidator,
    OutFieldsValidator,
)

PLUGIN_SOURCE = Path(AnchorageCheckbookPlugin.__module__.replace(".", "/") + ".py")


@pytest.fixture
def plugin():
    cfg = {
        "enabled": True,
        "service_url": "https://example.com/arcgis/rest/services/X/FeatureServer",
        "city_name": "Municipality of Anchorage",
        "timeout": 20,
    }
    p = AnchorageCheckbookPlugin(cfg)
    p.plugin_config = AnchorageCheckbookPluginConfig(**cfg)
    return p


class TestValidatorsRaiseToolInputError:
    """Every rejection in where_validator.py is caller input by
    construction -- a WHERE clause, an out_fields list and an order_by
    list arrive from the tool call and nowhere else."""

    @pytest.mark.parametrize(
        "bad",
        [
            "1=1; DROP TABLE x",
            "1=1 UNION SELECT * FROM y",
            "1=1 OR 1=1--",
            "Vendor_Name = 'unbalanced",
            "x" * 2001,
        ],
    )
    def test_where_rejections_are_caller_errors(self, bad):
        with pytest.raises(ToolInputError):
            CheckbookWhereValidator.validate(bad)

    def test_unknown_field_rejection_is_a_caller_error(self):
        with pytest.raises(ToolInputError):
            CheckbookWhereValidator.validate_against_schema(
                "Vendr_Name = 'X'", ["Vendor_Name", "Amount"]
            )

    def test_out_fields_rejection_is_a_caller_error(self):
        with pytest.raises(ToolInputError):
            OutFieldsValidator.validate("a; DROP TABLE x")

    def test_order_by_rejection_is_a_caller_error(self):
        with pytest.raises(ToolInputError):
            OrderByValidator.validate("name; DROP TABLE x")

    def test_still_catchable_as_valueerror(self):
        """Subclassing ValueError keeps every existing handler working."""
        with pytest.raises(ValueError):
            CheckbookWhereValidator.validate("1=1; DROP TABLE x")

    def test_a_vendor_named_union_is_still_queryable(self):
        """The reason this validator exists at all -- guarding against a
        regression that would turn real data into a "caller error"."""
        clause = "Vendor_Name = 'Credit Union 1'"
        assert CheckbookWhereValidator.validate(clause) == clause


class TestNotInferredFromValueError:
    """The two cases where treating ValueError as a caller error is wrong."""

    def test_json_decode_error_is_not_a_tool_input_error(self):
        """json.JSONDecodeError subclasses ValueError. If ValueError were
        the marker, a malformed upstream payload would be misfiled as a
        caller mistake and silently lose its stack trace."""
        try:
            json.loads("{not json")
        except json.JSONDecodeError as e:
            assert isinstance(e, ValueError)
            assert not isinstance(e, ToolInputError)
        else:
            pytest.fail("expected JSONDecodeError")

    def test_upstream_non_json_stays_a_plain_valueerror(self):
        """The ArcGIS wrapper raises plain ValueError for a non-JSON
        response -- a genuine upstream fault whose traceback we want.

        The COUNT is asserted so a newly added plain ValueError has to be
        classified deliberately rather than drift in unnoticed.
        """
        import inspect

        src = inspect.getsource(AnchorageCheckbookPlugin)
        remaining = [
            line for line in src.splitlines() if "raise ValueError(" in line
        ]
        assert len(remaining) == 1, (
            f"expected exactly the 1 non-JSON upstream raise, found "
            f"{len(remaining)} -- classify the new one deliberately "
            f"(ToolInputError for caller input, plain ValueError for an "
            f"upstream fault)"
        )

    @pytest.mark.asyncio
    async def test_upstream_fault_keeps_error_level_and_traceback(
        self, plugin, caplog
    ):
        """Pinned end to end: the surviving ValueError must still read as
        a server-side problem, not a caller mistake."""
        with patch.object(
            plugin,
            "_list_tables",
            AsyncMock(side_effect=ValueError("Feature Service returned non-JSON")),
        ):
            with caplog.at_level(logging.DEBUG):
                result = await plugin.execute_tool("list_tables", {})

        assert result.success is False
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "an upstream fault must still log at ERROR"
        assert any(r.exc_info for r in errors), "with its traceback"


class TestOuterHandlerLogging:
    @pytest.mark.asyncio
    async def test_caller_error_logs_warning_without_traceback(
        self, plugin, caplog
    ):
        with patch.object(
            plugin,
            "_spending_stats",
            AsyncMock(side_effect=ToolInputError("fiscal_year must be a 4-digit year")),
        ):
            with caplog.at_level(logging.DEBUG):
                result = await plugin.execute_tool("spending_stats", {})

        assert result.success is False
        assert "4-digit year" in result.error_message
        records = [
            r for r in caplog.records if "spending_stats" in r.getMessage()
        ]
        assert records, "expected a log record"
        assert all(r.levelno == logging.WARNING for r in records)
        assert not any(r.exc_info for r in records), "no traceback for caller errors"

    @pytest.mark.asyncio
    async def test_server_fault_still_logs_error_with_traceback(
        self, plugin, caplog
    ):
        """The quiet path must not swallow genuine failures."""
        with patch.object(
            plugin,
            "_spending_stats",
            AsyncMock(side_effect=RuntimeError("upstream exploded")),
        ):
            with caplog.at_level(logging.DEBUG):
                result = await plugin.execute_tool("spending_stats", {})

        assert result.success is False
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "a real fault must still log at ERROR"
        assert any(r.exc_info for r in errors), "with a traceback"

    @pytest.mark.asyncio
    async def test_malformed_json_body_logs_warning_not_error(self, caplog):
        """-32700 already tells the caller; our parse traceback adds nothing."""
        from core.plugin_manager import PluginManager

        server = MCPServer(MagicMock(spec=PluginManager))
        with caplog.at_level(logging.DEBUG):
            response = await server.handle_http_request("{not json")

        assert json.loads(response["body"])["error"]["code"] == -32700
        records = [r for r in caplog.records if "Invalid JSON" in r.getMessage()]
        assert records
        assert all(r.levelno == logging.WARNING for r in records)
        assert not any(r.exc_info for r in records)


class TestNumericCoercion:
    """'FY25' is exactly what a model sends for a fiscal year."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool,args,argname,bad",
        [
            ("get_line_items", {"table": 0, "limit": "lots"}, "limit", "lots"),
            ("get_line_items", {"table": 0, "offset": "next"}, "offset", "next"),
            ("query_checkbook", {"table": 0, "where": "1=1", "offset": "x"}, "offset", "x"),
            ("top_vendors", {"table": 0, "n": "many"}, "n", "many"),
            (
                "spending_stats",
                {"table": 0, "stat_type": "percentile_cont", "percentile": "half"},
                "percentile",
                "half",
            ),
            ("get_line_items", {"table": 0, "fiscal_period": "Q1"}, "fiscal_period", "Q1"),
            ("spending_stats", {"table": 0, "fiscal_year": "FY25"}, "fiscal_year", "FY25"),
        ],
    )
    async def test_bad_numeric_argument_names_itself(
        self, plugin, tool, args, argname, bad
    ):
        result = await plugin.execute_tool(tool, args)

        assert result.success is False
        # The caller has to be able to tell WHICH argument, and what it saw.
        assert argname in result.error_message
        assert bad in result.error_message
        # And never Python's own internals.
        assert "invalid literal" not in result.error_message
        assert "could not convert" not in result.error_message

    @pytest.mark.asyncio
    async def test_explicit_null_means_default_not_error(self, plugin):
        """A client that serializes an unset optional as null should get
        the default, not a validation failure."""
        assert plugin._int_arg({"limit": None}, "limit", 25) == 25
        assert plugin._float_arg({"percentile": None}, "percentile", 0.5) == 0.5


class TestNoUnroutedCoercionRemains:
    """An AST sweep, because a line-oriented grep misses the inline ones.

    Three of this plugin's coercion sites were inline expressions like
    ``max(0, int(args.get("offset", 0)))`` -- invisible to a grep for
    ``= int(``, and the ones most likely to reach a caller as
    "invalid literal for int() with base 10".
    """

    @staticmethod
    def _coercions_over_caller_args():
        src = PLUGIN_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("int", "float")
            ):
                continue
            # Does this coercion read the caller's argument dict directly?
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Name)
                    and inner.id in ("args", "arguments")
                ):
                    offenders.append(
                        (node.lineno, ast.get_source_segment(src, node))
                    )
                    break
        return offenders

    def test_zero_remaining(self):
        offenders = self._coercions_over_caller_args()
        assert offenders == [], (
            "caller arguments must be coerced through _int_arg/_float_arg "
            f"so the error names the argument: {offenders}"
        )
