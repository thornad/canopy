"""Tool specs are part of the cached prompt prefix, so their order must be stable."""

import pytest

from canopy import mcp


@pytest.fixture(autouse=True)
def _fake_clients(monkeypatch):
    tools_by_server = {
        "alpha": [{"name": "zeta"}, {"name": "beta"}],
        "bravo": [{"name": "search"}],
    }

    async def fake_start(self):
        self._tools = list(tools_by_server[self.name])
        self._proc = type("P", (), {"returncode": None})()

    async def fake_stop(self):
        self._proc = None

    monkeypatch.setattr(mcp.MCPClient, "start", fake_start)
    monkeypatch.setattr(mcp.MCPClient, "stop", fake_stop)


def _cfg(name, enabled=True):
    return {"name": name, "command": "x", "enabled": enabled}


def _names(registry):
    return [s["function"]["name"] for s in registry.tool_specs()]


async def test_toggling_a_server_keeps_tool_order():
    registry = mcp.MCPRegistry()
    await registry.sync([_cfg("alpha"), _cfg("bravo")])
    before = _names(registry)

    await registry.sync([_cfg("alpha", enabled=False), _cfg("bravo")])
    await registry.sync([_cfg("alpha"), _cfg("bravo")])

    assert _names(registry) == before


async def test_tools_within_a_server_are_sorted_by_name():
    registry = mcp.MCPRegistry()
    await registry.sync([_cfg("alpha")])
    sep = mcp.NAMESPACE_SEP
    assert _names(registry) == [f"alpha{sep}beta", f"alpha{sep}zeta"]
