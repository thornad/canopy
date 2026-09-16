"""Tests for rebuilding the OpenAI messages array from a stored path."""

import json

from canopy.server import EARLIER_TOOL_RESULT_PLACEHOLDER, _path_to_openai_messages

SEARCH_HITS = json.dumps([{"text": "passage", "ga": "300", "url": "https://x"}])


def _tool_loop_path(first_result: str = SEARCH_HITS) -> list[dict]:
    call = json.dumps([{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}])
    return [
        {"id": "u1", "role": "user", "content": "first question"},
        {"id": "a1", "role": "assistant", "content": "", "tool_calls": call},
        {"id": "t1", "role": "tool", "content": first_result, "tool_call_id": "c1"},
        {"id": "a2", "role": "assistant", "content": "answer one"},
        {"id": "u2", "role": "user", "content": "follow-up"},
        {"id": "a3", "role": "assistant", "content": "", "tool_calls": call.replace("c1", "c2")},
        {"id": "t2", "role": "tool", "content": SEARCH_HITS, "tool_call_id": "c2"},
    ]


def _tool_contents(messages: list[dict]) -> list:
    return [m["content"] for m in messages if m["role"] == "tool"]


def test_json_list_tool_results_replay_as_the_original_text():
    # A list of search hits is data, not content parts; parsing it made oMLX
    # drop every item and the model saw an empty result.
    assert _tool_contents(_path_to_openai_messages(_tool_loop_path())) == [SEARCH_HITS, SEARCH_HITS]


def test_other_tool_result_shapes_replay_unchanged():
    for raw in ('{"a": 1}', '["x", "y"]', "plain text", '[{"type": "text", "text": "hi"}]'):
        assert _tool_contents(_path_to_openai_messages(_tool_loop_path(raw)))[0] == raw


def test_content_parts_on_user_messages_are_still_restored():
    parts = [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "data:"}}]
    out = _path_to_openai_messages([{"role": "user", "content": json.dumps(parts)}])
    assert out[0]["content"] == parts


def test_non_content_part_lists_on_user_messages_stay_text():
    raw = json.dumps([1, 2, 3])
    assert _path_to_openai_messages([{"role": "user", "content": raw}])[0]["content"] == raw


def test_omit_replaces_only_results_from_earlier_questions():
    out = _path_to_openai_messages(_tool_loop_path(), omit_earlier_tool_results=True)
    assert _tool_contents(out) == [EARLIER_TOOL_RESULT_PLACEHOLDER, SEARCH_HITS]


def test_omit_keeps_results_of_the_current_question_when_resuming():
    # Continue after the tool-turn limit: the path ends on tool rows of the
    # current question, which must all stay intact.
    path = _tool_loop_path()[:3]
    assert _tool_contents(_path_to_openai_messages(path, omit_earlier_tool_results=True)) == [SEARCH_HITS]


def test_tool_calls_and_ids_survive():
    out = _path_to_openai_messages(_tool_loop_path())
    assert out[1]["tool_calls"][0]["id"] == "c1"
    assert out[2]["tool_call_id"] == "c1"
