"""Reasoning of tool-calling turns: kept per turn, sent back, replayed as sent."""

import json
import tempfile
from pathlib import Path

import httpx
import pytest

from canopy import database as db
from canopy import server
from canopy.models import ChatRequest
from canopy.server import _generate_with_tools, _path_to_openai_messages


@pytest.fixture(autouse=True)
async def setup_db():
    with tempfile.TemporaryDirectory() as tmp:
        await db.init_db(Path(tmp) / "test.db")
        yield
        await db.close_db()


def _sse(*deltas: dict, finish: str) -> bytes:
    chunks = [{"choices": [{"delta": d}]} for d in deltas]
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"


TOOL_TURN = _sse(
    {"reasoning_content": "I should search for karma."},
    {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "s__search", "arguments": '{"q": "karma"}'}}]},
    finish="tool_calls",
)
FINAL_TURN = _sse({"reasoning_content": "The results suffice."}, {"content": "Karma is..."}, finish="stop")


@pytest.fixture
def fake_omlx(monkeypatch):
    """Answer each chat request with the next scripted turn; record requests."""
    turns = [TOOL_TURN, FINAL_TURN]
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=turns[len(requests) - 1])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        server.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    async def fake_call(name, args):
        return "search hits"

    monkeypatch.setattr(server.mcp_registry, "call", fake_call)
    return requests


async def _run(send_reasoning: bool) -> list[dict]:
    conv = await db.create_conversation()
    user = await db.add_message(conv["id"], "user", "what is karma?")
    body = ChatRequest(conversation_id=conv["id"], parent_id=user["id"], model="m")
    messages = [{"role": "user", "content": "what is karma?"}]
    final_id = None
    async for event in _generate_with_tools(body, messages, [], "http://omlx", {}, send_reasoning=send_reasoning):
        if '"type": "done"' in event:
            final_id = json.loads(event[len("data: "):])["message_id"]
    return await db.get_message_path(final_id)


def _second_request_assistant(requests: list[dict]) -> dict:
    return next(m for m in requests[1]["messages"] if m["role"] == "assistant")


async def test_tool_turn_reasoning_is_sent_back_on_the_next_turn(fake_omlx):
    await _run(send_reasoning=True)
    assert _second_request_assistant(fake_omlx)["content"] == "<think>I should search for karma.</think>"


async def test_each_turn_is_saved_with_only_its_own_reasoning(fake_omlx):
    path = await _run(send_reasoning=True)
    tool_turn, final = path[1], path[-1]
    assert tool_turn["content"] == "<think>I should search for karma.</think>"
    assert final["content"] == "<think>The results suffice.</think>Karma is..."


async def test_replay_matches_what_the_loop_sent(fake_omlx):
    path = await _run(send_reasoning=True)
    replayed = _path_to_openai_messages(path[:-1])
    sent = fake_omlx[1]["messages"]
    assert [m["content"] for m in replayed] == [m["content"] for m in sent]


async def test_ds4_loop_sends_no_reasoning_and_its_replay_matches(fake_omlx):
    path = await _run(send_reasoning=False)
    assert _second_request_assistant(fake_omlx)["content"] == ""
    replayed = _path_to_openai_messages(path[:-1], strip_reasoning=True)
    assert [m["content"] for m in replayed] == [m["content"] for m in fake_omlx[1]["messages"]]
    # Still saved, for display.
    assert path[1]["content"] == "<think>I should search for karma.</think>"
