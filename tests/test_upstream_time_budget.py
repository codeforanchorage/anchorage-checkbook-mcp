"""A hung upstream must cost a readable error, not the Lambda.

The ladder is 29s gateway > 28s Lambda > 20s plugin. Two things used to
break the bottom rung, both found by review rather than in production:

- `_request_json` retried a timed-out request up to three times with no
  overall deadline, so one call could take about 61s.
- The Lambda adapter builds a new plugin object for every request, and
  `initialize()` fetched six tables' metadata each time -- so every
  request, `ping` included, made six upstream calls first, and with a hung
  upstream even the handshake timed out.
"""

import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from plugins.anchorage_checkbook import plugin as module
from plugins.anchorage_checkbook.config_schema import AnchorageCheckbookPluginConfig
from plugins.anchorage_checkbook.plugin import TABLES, AnchorageCheckbookPlugin

CFG = {
    "enabled": True,
    "service_url": "https://example.com/arcgis/rest/services/X/FeatureServer",
    "city_name": "Municipality of Anchorage",
    "timeout": 20,
}


@pytest.fixture
def plugin():
    p = AnchorageCheckbookPlugin(CFG)
    p.plugin_config = AnchorageCheckbookPluginConfig(**CFG)
    p.client = Mock()
    return p


async def _hang(*args, **kwargs):
    await asyncio.sleep(30)


class TestOneBudgetCoversEveryAttempt:
    @pytest.mark.asyncio
    async def test_a_hung_upstream_is_cut_off_at_the_deadline(self, plugin):
        plugin.client.get = AsyncMock(side_effect=_hang)
        plugin._request_deadline = time.monotonic() + 0.05
        started = time.monotonic()
        with pytest.raises(RuntimeError) as err:
            await plugin._request_json("https://example.com/x", {"f": "json"})
        assert time.monotonic() - started < 2
        # Not retried: the budget is spent, and a second attempt could
        # only outlive the Lambda.
        assert plugin.client.get.call_count == 1
        assert "did not answer within" in str(err.value)

    @pytest.mark.asyncio
    async def test_no_attempt_starts_after_the_deadline(self, plugin):
        plugin.client.get = AsyncMock(side_effect=_hang)
        plugin._request_deadline = time.monotonic() - 1
        with pytest.raises(RuntimeError) as err:
            await plugin._request_json("https://example.com/x", {"f": "json"})
        assert plugin.client.get.call_count == 0
        assert "did not answer within" in str(err.value)

    @pytest.mark.asyncio
    async def test_fast_transient_failures_are_still_retried(self, plugin):
        ok = Mock(status_code=200)
        ok.json = Mock(return_value={"count": 1})
        plugin.client.get = AsyncMock(side_effect=[httpx.ConnectError("blip"), ok])
        with patch.object(module.asyncio, "sleep", AsyncMock()):
            assert await plugin._request_json("https://example.com/x", {}) == {
                "count": 1
            }
        assert plugin.client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_a_tool_call_sets_and_clears_the_budget(self, plugin):
        seen = {}

        async def handler(args):
            seen["deadline"] = plugin._request_deadline
            return "ok"

        with patch.object(plugin, "_list_tables", handler):
            result = await plugin.execute_tool("list_tables", {})
        assert result.success
        assert 0 < seen["deadline"] - time.monotonic() <= CFG["timeout"]
        assert plugin._request_deadline is None


def _client(get):
    client = Mock()
    client.get = get
    client.aclose = AsyncMock()
    return client


class TestMetadataIsFetchedOncePerWarmContainer:
    @pytest.mark.asyncio
    async def test_a_second_plugin_object_reuses_the_metadata(self):
        resp = Mock(status_code=200)
        resp.json = Mock(return_value={"fields": [{"name": "Amount"}]})
        get = AsyncMock(return_value=resp)
        with patch("httpx.AsyncClient", return_value=_client(get)):
            first = AnchorageCheckbookPlugin(CFG)
            assert await first.initialize()
            assert get.call_count == len(TABLES)
            await first.shutdown()
            # What the Lambda adapter does on the next request.
            second = AnchorageCheckbookPlugin(CFG)
            assert await second.initialize()
        assert get.call_count == len(TABLES)
        assert second._live_fields[0] == {"Amount"}

    @pytest.mark.asyncio
    async def test_a_failed_fetch_is_held_off_then_retried(self):
        get = AsyncMock(side_effect=httpx.ConnectError("down"))
        with (
            patch("httpx.AsyncClient", return_value=_client(get)),
            patch.object(module.asyncio, "sleep", AsyncMock()),
        ):
            assert await AnchorageCheckbookPlugin(CFG).initialize()
            failed_calls = get.call_count
            assert failed_calls >= len(TABLES)
            # During the outage the next request does not pay for it again.
            assert await AnchorageCheckbookPlugin(CFG).initialize()
            assert get.call_count == failed_calls
            # Once the hold-off has elapsed, it is tried again.
            module._SHARED_STATE[CFG["service_url"]].meta_retry_at = 0.0
            assert await AnchorageCheckbookPlugin(CFG).initialize()
            assert get.call_count > failed_calls

    @pytest.mark.asyncio
    async def test_a_hung_upstream_cannot_stall_startup(self):
        get = AsyncMock(side_effect=_hang)
        with (
            patch("httpx.AsyncClient", return_value=_client(get)),
            patch.object(module, "STARTUP_META_BUDGET_S", 0.05),
        ):
            started = time.monotonic()
            p = AnchorageCheckbookPlugin(CFG)
            assert await p.initialize()  # degraded > down
        assert time.monotonic() - started < 2
        assert p._request_deadline is None

    @pytest.mark.asyncio
    async def test_the_snapshot_date_is_shared_too(self):
        resp = Mock(status_code=200)
        resp.json = Mock(return_value={"fields": []})
        with patch(
            "httpx.AsyncClient", return_value=_client(AsyncMock(return_value=resp))
        ):
            first = AnchorageCheckbookPlugin(CFG)
            await first.initialize()
            first._pubdate_cache[0] = "2026-11-16"
            second = AnchorageCheckbookPlugin(CFG)
            await second.initialize()
        assert second._pubdate_cache[0] == "2026-11-16"

    def test_shared_state_expires(self):
        state = module._shared_state("u")
        state.meta_loaded = True
        state.created -= module.SHARED_STATE_TTL_S + 1
        assert module._shared_state("u").meta_loaded is False


class TestAKeptPluginObjectStillRefreshes:
    """Since opencontext-core 1.5.0 one plugin object serves a warm
    container for hours, and initialize() runs once. The shared state's
    lifetime has to be honoured by the object that outlives it."""

    def _ok_client(self, fields):
        resp = Mock(status_code=200)
        resp.json = Mock(return_value={"fields": [{"name": f} for f in fields]})
        return _client(AsyncMock(return_value=resp))

    @pytest.mark.asyncio
    async def test_an_expired_state_is_replaced_on_the_next_tool_call(self):
        client = self._ok_client(["Amount"])
        with patch("httpx.AsyncClient", return_value=client):
            p = AnchorageCheckbookPlugin(CFG)
            assert await p.initialize()
        p._pubdate_cache[0] = "2026-10-16"
        loads = client.get.call_count

        async def handler(args):
            return "ok"

        # Still fresh: the same state, and no new upstream call.
        with patch.object(p, "_list_tables", handler):
            await p.execute_tool("list_tables", {})
        assert p._pubdate_cache == {0: "2026-10-16"}
        assert client.get.call_count == loads

        # Expired: the snapshot date is forgotten and metadata reloaded,
        # without initialize() running again.
        module._SHARED_STATE[CFG["service_url"]].created -= (
            module.SHARED_STATE_TTL_S + 1
        )
        with patch.object(p, "_list_tables", handler):
            await p.execute_tool("list_tables", {})
        assert p._pubdate_cache == {}
        assert client.get.call_count == loads + len(TABLES)
        assert p._live_fields[0] == {"Amount"}
        assert p._request_deadline is None

    @pytest.mark.asyncio
    async def test_a_failed_startup_fetch_is_retried_by_a_later_tool_call(self):
        get = AsyncMock(side_effect=httpx.ConnectError("down"))
        with (
            patch("httpx.AsyncClient", return_value=_client(get)),
            patch.object(module.asyncio, "sleep", AsyncMock()),
        ):
            p = AnchorageCheckbookPlugin(CFG)
            assert await p.initialize()
            assert p._live_fields == {}

            async def handler(args):
                return "ok"

            # Inside the hold-off: no retry.
            failed = get.call_count
            with patch.object(p, "_list_tables", handler):
                await p.execute_tool("list_tables", {})
            assert get.call_count == failed

            # Hold-off over and the upstream is back.
            resp = Mock(status_code=200)
            resp.json = Mock(return_value={"fields": [{"name": "Amount"}]})
            get.side_effect = None
            get.return_value = resp
            module._SHARED_STATE[CFG["service_url"]].meta_retry_at = 0.0
            with patch.object(p, "_list_tables", handler):
                await p.execute_tool("list_tables", {})
        assert p._live_fields[0] == {"Amount"}

    @pytest.mark.asyncio
    async def test_an_uninitialized_plugin_is_left_alone(self, plugin):
        plugin._pubdate_cache = {0: "2026-08-24"}

        async def handler(args):
            return "ok"

        with patch.object(plugin, "_list_tables", handler):
            await plugin.execute_tool("list_tables", {})
        assert plugin._pubdate_cache == {0: "2026-08-24"}
