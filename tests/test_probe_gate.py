"""Cache probes must not load oMLX while Canopy is generating."""

import asyncio

import pytest

from canopy import server


@pytest.fixture(autouse=True)
def _reset_gate():
    server._active_generations = 0
    server._last_probe.clear()
    server._last_sweep.clear()
    yield
    server._active_generations = 0
    server._last_probe.clear()
    server._last_sweep.clear()


async def test_generation_counter_follows_the_stream():
    async def stream():
        yield "a"
        yield "b"

    seen = []
    async for chunk in server._track_generation(stream()):
        seen.append(server._active_generations)
    assert seen == [1, 1]
    assert server._active_generations == 0


async def test_generation_counter_drops_when_client_disconnects():
    async def stream():
        while True:
            yield "x"

    tracked = server._track_generation(stream())
    await tracked.__anext__()
    assert server._active_generations == 1
    await tracked.aclose()  # what Starlette does on disconnect
    assert server._active_generations == 0


async def test_sweep_is_shared_between_callers(monkeypatch):
    calls = 0

    async def fake_settings():
        return {"active_backend": "omlx"}

    async def fake_sweep(url, key, model):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"conv": {"status": "ok"}}

    monkeypatch.setattr(server.db, "get_settings", fake_settings)
    monkeypatch.setattr(server, "_sweep_cache_status", fake_sweep)

    results = await asyncio.gather(*(server.cache_status_all(model="m") for _ in range(3)))

    assert calls == 1
    assert all(r == {"conv": {"status": "ok"}} for r in results)


async def test_sweep_during_generation_is_not_reused(monkeypatch):
    calls = 0

    async def fake_settings():
        return {"active_backend": "omlx"}

    async def fake_sweep(url, key, model):
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(server.db, "get_settings", fake_settings)
    monkeypatch.setattr(server, "_sweep_cache_status", fake_sweep)

    server._active_generations = 1
    await server.cache_status_all(model="m")
    server._active_generations = 0
    await server.cache_status_all(model="m")  # generation over: must refresh

    assert calls == 2


async def test_probe_never_reaches_omlx_while_generating(monkeypatch):
    async def fake_settings():
        return {"active_backend": "omlx"}

    async def fake_path(leaf_id):
        return [{"id": leaf_id, "role": "user", "content": "hi", "model": "m"}]

    class NoNetwork:
        def __init__(self, *a, **kw):
            raise AssertionError("probe contacted oMLX during generation")

    monkeypatch.setattr(server.db, "get_settings", fake_settings)
    monkeypatch.setattr(server.db, "get_message_path", fake_path)
    monkeypatch.setattr(server.httpx, "AsyncClient", NoNetwork)
    conv = {"id": "c", "model": "m"}

    server._active_generations = 1
    assert (await server._probe_leaf_cache(conv, "leaf"))["status"] == "skipped"

    known = {"status": "ok", "leaf_id": "leaf", "total_blocks": 4}
    server._last_probe[("leaf", "m")] = known
    assert await server._probe_leaf_cache(conv, "leaf") == known
