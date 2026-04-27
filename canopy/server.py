"""Canopy FastAPI server — chat UI, conversation API, SSE proxy to oMLX."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import database as db
from .documents import parse_document
from .mcp import MCPClient, MCPError, parse_args_string, parse_env_string, registry as mcp_registry
from .models import (
    ChatRequest,
    ConversationCreate,
    ConversationUpdate,
    MCPServerCreate,
    MCPServerUpdate,
    MessageCreate,
    SettingsUpdate,
)

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = getattr(app.state, "db_path", None)
    await db.init_db(db_path)
    try:
        await mcp_registry.sync(await db.list_mcp_servers())
    except Exception:
        pass
    yield
    await mcp_registry.stop_all()
    await db.close_db()


app = FastAPI(title="Canopy", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --- Pages ---


@app.get("/", response_class=RedirectResponse)
async def index():
    return RedirectResponse(url="/chat")


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    # New starlette signature (request, name, context). The legacy form
    # `TemplateResponse("chat.html", {"request": request})` crashes with
    # newer Jinja2 caching ("unhashable type: 'dict'").
    return templates.TemplateResponse(request, "chat.html", {})


# --- Health ---


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# --- Settings ---


@app.get("/api/settings")
async def get_settings():
    return await db.get_settings()


@app.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    updates = body.model_dump(exclude_none=True)
    if updates:
        await db.update_settings(updates)
    return await db.get_settings()


# --- MCP servers ---


def _mcp_row_out(row: dict) -> dict:
    """DB row → API shape (join args/env back to strings for editing)."""
    return {
        "id": row["id"],
        "name": row["name"],
        "command": row["command"],
        "args": " ".join(row.get("args") or []),
        "env": "\n".join(f"{k}={v}" for k, v in (row.get("env") or {}).items()),
        "enabled": bool(row.get("enabled")),
    }


async def _resync_mcp():
    try:
        await mcp_registry.sync(await db.list_mcp_servers())
    except Exception:
        pass


@app.get("/api/mcp/servers")
async def list_mcp():
    servers = await db.list_mcp_servers()
    status = mcp_registry.server_status()
    out = []
    for s in servers:
        info = _mcp_row_out(s)
        info["status"] = status.get(s["name"], {"running": False, "tool_count": 0, "tools": []})
        out.append(info)
    return out


@app.post("/api/mcp/servers", status_code=201)
async def add_mcp(body: MCPServerCreate):
    if not body.name.strip() or not body.command.strip():
        raise HTTPException(400, "name and command are required")
    try:
        server = await db.add_mcp_server(
            name=body.name.strip(),
            command=body.command.strip(),
            args=parse_args_string(body.args),
            env=parse_env_string(body.env),
            enabled=body.enabled,
        )
    except Exception as e:
        raise HTTPException(400, f"Could not add server: {e}")
    await _resync_mcp()
    status = mcp_registry.server_status().get(
        server["name"], {"running": False, "tool_count": 0, "tools": []}
    )
    return {**_mcp_row_out(server), "status": status}


@app.patch("/api/mcp/servers/{server_id}")
async def patch_mcp(server_id: str, body: MCPServerUpdate):
    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.command is not None:
        updates["command"] = body.command.strip()
    if body.args is not None:
        updates["args"] = parse_args_string(body.args)
    if body.env is not None:
        updates["env"] = parse_env_string(body.env)
    if body.enabled is not None:
        updates["enabled"] = body.enabled
    server = await db.update_mcp_server(server_id, **updates)
    if server is None:
        raise HTTPException(404, "MCP server not found")
    await _resync_mcp()
    status = mcp_registry.server_status().get(
        server["name"], {"running": False, "tool_count": 0, "tools": []}
    )
    return {**_mcp_row_out(server), "status": status}


@app.delete("/api/mcp/servers/{server_id}")
async def delete_mcp(server_id: str):
    ok = await db.delete_mcp_server(server_id)
    if not ok:
        raise HTTPException(404, "MCP server not found")
    await _resync_mcp()
    return {"deleted": True}


@app.post("/api/mcp/servers/{server_id}/test")
async def test_mcp(server_id: str):
    server = await db.get_mcp_server(server_id)
    if server is None:
        raise HTTPException(404, "MCP server not found")
    probe = MCPClient(
        name=server["name"],
        command=server["command"],
        args=server["args"],
        env=server["env"],
    )
    try:
        await probe.start()
        tools = [{"name": t["name"], "description": t.get("description", "")} for t in probe.tools]
        return {"ok": True, "tool_count": len(tools), "tools": tools}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await probe.stop()


# --- Conversations ---


@app.get("/api/conversations")
async def list_conversations():
    return await db.list_conversations()


@app.post("/api/conversations", status_code=201)
async def create_conversation(body: ConversationCreate):
    return await db.create_conversation(
        title=body.title,
        system_prompt=body.system_prompt,
        model=body.model,
    )


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = await db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    return conv


@app.patch("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, body: ConversationUpdate):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    ok = await db.update_conversation(conv_id, **updates)
    if not ok:
        raise HTTPException(404, "Conversation not found")
    return await db.get_conversation(conv_id)


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    ok = await db.delete_conversation(conv_id)
    if not ok:
        raise HTTPException(404, "Conversation not found")
    return {"deleted": True}


# --- Messages ---


@app.post("/api/conversations/{conv_id}/messages", status_code=201)
async def add_message(conv_id: str, body: MessageCreate):
    conv = await db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    msg = await db.add_message(
        conversation_id=conv_id,
        role=body.role,
        content=body.content,
        parent_id=body.parent_id,
        model=body.model,
    )
    return msg


@app.delete("/api/messages/{msg_id}")
async def delete_message(msg_id: str):
    """Delete a message and all its descendants."""
    ok = await db.delete_message_tree(msg_id)
    if not ok:
        raise HTTPException(404, "Message not found")
    return {"deleted": True}


# --- SSE Proxy to oMLX ---


def _path_to_openai_messages(path: list[dict]) -> list[dict]:
    """Rebuild an OpenAI-style messages array from a DB path.

    Handles tool rows (``role='tool'``) and assistant rows that carry
    ``tool_calls`` so probes and chat requests replay the exact sequence
    oMLX saw originally — otherwise the cache hashes diverge after any
    tool-calling turn.
    """
    out: list[dict] = []
    for msg in path:
        role = msg.get("role") or "user"
        content = msg.get("content", "")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    content = parsed
            except (json.JSONDecodeError, TypeError):
                pass
        if role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id") or "",
                "content": content,
            })
            continue
        entry: dict = {"role": role, "content": content}
        raw_tc = msg.get("tool_calls")
        if raw_tc:
            try:
                entry["tool_calls"] = json.loads(raw_tc) if isinstance(raw_tc, str) else raw_tc
            except json.JSONDecodeError:
                pass
        out.append(entry)
    return out


MCP_MAX_TURNS = 10


async def _generate_with_tools(
    body: ChatRequest,
    messages: list[dict],
    tool_specs: list[dict],
    omlx_url: str,
    headers: dict,
):
    """Iterative tool-calling loop with streaming pass-through.

    Each turn is streamed from oMLX and forwarded raw to the UI so the
    existing client-side parser sees ``reasoning_content`` and ``content``
    deltas exactly as it would without tools. In parallel we accumulate
    ``tool_calls`` across deltas; when a turn finishes with ``tool_calls``
    we dispatch them via MCP, append the tool messages, and loop. When it
    finishes with ``stop`` we save the final message and emit ``done``.
    """
    accumulated_reasoning = ""
    final_content = ""
    usage_total: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    # Walk the parent chain as we save each intermediate turn so a future
    # probe / next send replays the exact sequence oMLX just cached.
    current_parent = body.parent_id

    def _merge_usage(u: dict) -> None:
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = u.get(k)
            if v is not None:
                usage_total[k] = (usage_total.get(k) or 0) + v

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            for _ in range(MCP_MAX_TURNS):
                payload = {
                    "model": body.model,
                    "messages": messages,
                    "tools": tool_specs,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }

                turn_reasoning = ""
                turn_content = ""
                turn_tool_calls: dict[int, dict] = {}
                finish_reason: Optional[str] = None

                async with client.stream(
                    "POST",
                    f"{omlx_url}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        err = (await response.aread()).decode(errors="replace")
                        yield f"data: {json.dumps({'error': err})}\n\n"
                        return

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        # Swallow the per-turn [DONE] — we emit a single one at the
                        # very end so the UI's done handling fires exactly once.
                        if line.strip() == "data: [DONE]":
                            continue
                        # Forward deltas raw so the existing client parser
                        # handles reasoning/content streaming unchanged.
                        yield line + "\n\n"

                        if not line.startswith("data: "):
                            continue
                        try:
                            data = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

                        if "usage" in data and data["usage"]:
                            _merge_usage(data["usage"])

                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        ch0 = choices[0]
                        delta = ch0.get("delta") or {}
                        if delta.get("reasoning_content"):
                            turn_reasoning += delta["reasoning_content"]
                        if delta.get("content"):
                            turn_content += delta["content"]
                        for tc_delta in delta.get("tool_calls") or []:
                            idx = tc_delta.get("index", 0)
                            entry = turn_tool_calls.setdefault(
                                idx,
                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                            )
                            if tc_delta.get("id"):
                                entry["id"] = tc_delta["id"]
                            fn = tc_delta.get("function") or {}
                            if fn.get("name"):
                                entry["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                entry["function"]["arguments"] += fn["arguments"]
                        fr = ch0.get("finish_reason")
                        if fr:
                            finish_reason = fr

                accumulated_reasoning += turn_reasoning

                if finish_reason == "tool_calls" and turn_tool_calls:
                    tool_calls_list = [turn_tool_calls[i] for i in sorted(turn_tool_calls)]
                    messages.append(
                        {
                            "role": "assistant",
                            "content": turn_content,
                            "tool_calls": tool_calls_list,
                        }
                    )
                    # Persist the assistant turn so the prompt path replays the
                    # same token sequence oMLX just cached. We deliberately
                    # strip any reasoning_content from the saved content — the
                    # next turn sends only the visible text + tool_calls,
                    # which is what oMLX's cache was keyed on.
                    assistant_msg = await db.add_message(
                        conversation_id=body.conversation_id,
                        role="assistant",
                        content=turn_content,
                        parent_id=current_parent,
                        model=body.model,
                        tool_calls=tool_calls_list,
                    )
                    current_parent = assistant_msg["id"]

                    for tc in tool_calls_list:
                        fn = tc.get("function") or {}
                        name = fn.get("name", "")
                        raw_args = fn.get("arguments", "") or "{}"
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except json.JSONDecodeError:
                            args = {}
                        yield f"data: {json.dumps({'type': 'tool_call', 'name': name, 'arguments': args})}\n\n"
                        try:
                            result = await mcp_registry.call(name, args or {})
                        except MCPError as e:
                            result = f"[error] {e}"
                        except Exception as e:
                            result = f"[error] {e}"
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "content": result,
                            }
                        )
                        tool_msg = await db.add_message(
                            conversation_id=body.conversation_id,
                            role="tool",
                            content=result,
                            parent_id=current_parent,
                            tool_call_id=tc.get("id", ""),
                        )
                        current_parent = tool_msg["id"]
                        yield f"data: {json.dumps({'type': 'tool_result', 'name': name, 'content': result})}\n\n"
                    continue

                # No tool calls → this turn produced the final answer.
                final_content = turn_content
                break
            else:
                yield f"data: {json.dumps({'error': 'Exceeded max tool-call turns'})}\n\n"
                return
    except httpx.ConnectError:
        yield f"data: {json.dumps({'error': f'Cannot connect to oMLX at {omlx_url}'})}\n\n"
        return
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        return

    yield "data: [DONE]\n\n"

    full_content = ""
    if accumulated_reasoning:
        full_content += f"<think>{accumulated_reasoning}</think>"
    full_content += final_content

    if full_content:
        saved = await db.add_message(
            conversation_id=body.conversation_id,
            role="assistant",
            content=full_content,
            parent_id=current_parent,
            model=body.model,
            token_count=len(full_content) // 4,
        )
        done_event = {
            "type": "done",
            "message_id": saved["id"],
            "usage": usage_total,
        }
        yield f"data: {json.dumps(done_event)}\n\n"


@app.post("/api/chat")
async def chat_proxy(body: ChatRequest):
    """Stream chat completion from oMLX and save the assistant message."""
    settings = await db.get_settings()
    omlx_url = settings.get("omlx_url", "http://localhost:8000")
    api_key = settings.get("omlx_api_key", "")

    # Build messages array by walking tree from root to parent_id
    path = await db.get_message_path(body.parent_id)
    if not path:
        raise HTTPException(400, "Invalid parent_id — no message path found")

    # Prepend system prompt if set
    system_prompt = settings.get("system_prompt", "")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(_path_to_openai_messages(path))

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    tool_specs = mcp_registry.tool_specs()

    if tool_specs:
        return StreamingResponse(
            _generate_with_tools(body, messages, tool_specs, omlx_url, headers),
            media_type="text/event-stream",
        )

    payload = {
        "model": body.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    async def generate():
        full_content = ""
        in_thinking = False
        usage_data = {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    f"{omlx_url}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        yield f"data: {json.dumps({'error': error_body.decode()})}\n\n"
                        return

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        yield line + "\n\n"

                        # Accumulate content for DB save (with <think> wrappers)
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                data = json.loads(line[6:])
                                # Capture usage (comes in final chunk with empty choices)
                                if "usage" in data and data["usage"]:
                                    usage_data = data["usage"]
                                # Parse content delta
                                choices = data.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    if "reasoning_content" in delta:
                                        if not in_thinking:
                                            full_content += "<think>"
                                            in_thinking = True
                                        full_content += delta["reasoning_content"]
                                    if "content" in delta:
                                        if in_thinking:
                                            full_content += "</think>"
                                            in_thinking = False
                                        full_content += delta["content"]
                            except (json.JSONDecodeError, IndexError, KeyError):
                                pass
                    # Close unclosed thinking block
                    if in_thinking:
                        full_content += "</think>"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'error': f'Cannot connect to oMLX at {omlx_url}'})}\n\n"
            return
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        # Save assistant message to DB
        if full_content:
            msg = await db.add_message(
                conversation_id=body.conversation_id,
                role="assistant",
                content=full_content,
                parent_id=body.parent_id,
                model=body.model,
                token_count=len(full_content) // 4,
            )
            done_event = {
                'type': 'done',
                'message_id': msg['id'],
                'usage': usage_data,
            }
            yield f"data: {json.dumps(done_event)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- Models proxy ---


@app.get("/api/models/status")
async def models_status():
    """Proxy to oMLX /v1/models/status for context window info."""
    settings = await db.get_settings()
    omlx_url = settings.get("omlx_url", "http://localhost:8000")
    api_key = settings.get("omlx_api_key", "")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{omlx_url}/v1/models/status", headers=headers)
            return resp.json()
    except Exception as e:
        return {"models": []}


@app.get("/api/models")
async def list_models():
    """Proxy to oMLX /v1/models."""
    settings = await db.get_settings()
    omlx_url = settings.get("omlx_url", "http://localhost:8000")
    api_key = settings.get("omlx_api_key", "")

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{omlx_url}/v1/models", headers=headers)
            return resp.json()
    except Exception as e:
        raise HTTPException(502, f"Cannot reach oMLX: {e}")


@app.get("/api/omlx-stats")
async def omlx_stats():
    """Proxy oMLX cache and performance stats."""
    settings = await db.get_settings()
    omlx_url = settings.get("omlx_url", "http://localhost:8000")
    api_key = settings.get("omlx_api_key", "")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Login to get session cookie
            login_resp = await client.post(
                f"{omlx_url}/admin/api/login",
                json={"api_key": api_key},
            )
            cookies = login_resp.cookies

            # Fetch stats with session
            resp = await client.get(
                f"{omlx_url}/admin/api/stats",
                cookies=cookies,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "cache_efficiency": data.get("cache_efficiency", 0),
                    "total_cached_tokens": data.get("total_cached_tokens", 0),
                    "total_prompt_tokens": data.get("total_prompt_tokens", 0),
                    "total_completion_tokens": data.get("total_completion_tokens", 0),
                    "total_requests": data.get("total_requests", 0),
                    "avg_prefill_tps": data.get("avg_prefill_tps", 0),
                    "avg_generation_tps": data.get("avg_generation_tps", 0),
                    "runtime_cache": data.get("runtime_cache", {}),
                }
            return {"error": "Stats unavailable"}
    except Exception as e:
        return {"error": str(e)}


async def _probe_leaf_cache(
    conv: dict, leaf_id: str, *, client: Optional[httpx.AsyncClient] = None,
    cookies: Optional[httpx.Cookies] = None,
    model_override: Optional[str] = None,
) -> dict:
    """Probe cache state for a specific leaf (message tip) in a conversation.

    Cache status is per-branch, not per-conversation: each leaf walks a unique
    path from the root and therefore has its own tokenization and cache hash
    sequence. Callers that already hold an auth cookie (e.g. the bulk
    endpoint) can pass ``client`` and ``cookies`` to avoid re-logging in for
    every probe.

    When ``model_override`` is provided, the probe uses that model (and thus
    its hashes) instead of the conversation's historical model. This is how
    the UI shows "what would be cached if I sent this with my currently
    selected model" — cache hashes are per-model, so switching models in
    the dropdown invalidates the visible cache state until re-probed.
    """
    settings = await db.get_settings()
    omlx_url = settings.get("omlx_url", "http://localhost:8000")
    api_key = settings.get("omlx_api_key", "")

    path = await db.get_message_path(leaf_id)
    if not path:
        return {"status": "empty", "leaf_id": leaf_id}

    if model_override:
        model = model_override
    else:
        # Resolve the model from the conversation / message tags. Older
        # conversations may not have ``conv.model`` set — fall back to
        # whatever model tagged the most recent message in the path so
        # the probe still works.
        model = conv.get("model") or ""
        if not model:
            for msg in reversed(path):
                if msg.get("model"):
                    model = msg["model"]
                    break
    if not model:
        return {"status": "no_model", "leaf_id": leaf_id}

    # Rebuild OAI-formatted messages (same path the chat proxy uses so the
    # hashes line up with what the scheduler would actually see at prefill).
    system_prompt = settings.get("system_prompt", "")
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(_path_to_openai_messages(path))

    # oMLX v0.3.6+ includes ``tools`` in the chat-template hash, so a probe
    # that omits them won't match the cache entries written by a real chat
    # turn that *did* pass tools. Mirror what /api/chat sends so the dots
    # reflect the actual prefill path.
    probe_tools = mcp_registry.tool_specs() or None

    async def _send(c: httpx.AsyncClient, ck: httpx.Cookies) -> dict:
        body: dict = {"model_id": model, "messages": messages}
        if probe_tools:
            body["tools"] = probe_tools
        resp = await c.post(
            f"{omlx_url}/admin/api/cache/probe",
            cookies=ck,
            json=body,
        )
        if resp.status_code != 200:
            return {"status": "error", "detail": resp.text, "leaf_id": leaf_id}
        return {"status": "ok", "leaf_id": leaf_id, **resp.json()}

    try:
        if client is not None and cookies is not None:
            return await _send(client, cookies)
        async with httpx.AsyncClient(timeout=10.0) as c:
            login_resp = await c.post(
                f"{omlx_url}/admin/api/login",
                json={"api_key": api_key},
            )
            return await _send(c, login_resp.cookies)
    except Exception as e:
        return {"status": "error", "detail": str(e), "leaf_id": leaf_id}


@app.get("/api/conversations/{conv_id}/cache-probe")
async def cache_probe(
    conv_id: str,
    leaf_id: Optional[str] = None,
    model: Optional[str] = None,
):
    """Probe cache status for one conversation's active tip.

    ``leaf_id`` picks a specific branch; ``model`` overrides the chat's
    historical model so the UI can reflect "cache status if I sent this
    with my currently selected model" (hashes are per-model).
    """
    conv = await db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")

    if leaf_id is None:
        latest = await db.get_latest_leaf(conv_id)
        if latest is None:
            return {"status": "empty"}
        leaf_id = latest["id"]

    return await _probe_leaf_cache(conv, leaf_id, model_override=model)


@app.post("/api/conversations/{conv_id}/cache-probe-batch")
async def cache_probe_batch(conv_id: str, body: dict):
    """Probe cache status for many message tips in one conversation.

    Used by the tree view to render a cache dot on every node — each node
    is treated as a potential tip, so the dot reflects "what's cached if I
    were at this point in the branch". Reuses a single oMLX auth cookie
    across all probes to keep the round-trip cost bounded for large trees.

    Body: ``{"message_ids": [...], "model": "optional-override"}``.
    """
    conv = await db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")

    message_ids = body.get("message_ids") or []
    model_override = body.get("model")
    if not isinstance(message_ids, list):
        raise HTTPException(400, "message_ids must be a list")
    if not message_ids:
        return {}

    settings = await db.get_settings()
    omlx_url = settings.get("omlx_url", "http://localhost:8000")
    api_key = settings.get("omlx_api_key", "")

    results: dict = {}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            login_resp = await client.post(
                f"{omlx_url}/admin/api/login",
                json={"api_key": api_key},
            )
            cookies = login_resp.cookies
            for msg_id in message_ids:
                try:
                    results[msg_id] = await _probe_leaf_cache(
                        conv, msg_id, client=client, cookies=cookies,
                        model_override=model_override,
                    )
                except Exception as e:
                    results[msg_id] = {"status": "error", "detail": str(e)}
    except Exception as e:
        return {"_error": str(e)}
    return results


@app.get("/api/cache-status")
async def cache_status_all(model: Optional[str] = None):
    """Probe cache status for every conversation (best-effort).

    Returns a map of conversation_id → probe result for the *latest* leaf in
    each chat, so the sidebar can show at-a-glance cache state for all chats
    without N round-trips from the UI. The UI can still probe a specific
    branch via ``/api/conversations/{id}/cache-probe?leaf_id=...``.

    When ``model`` is set, every probe uses that model override (for "what
    would be cached if I switched to this model and sent?").
    """
    settings = await db.get_settings()
    omlx_url = settings.get("omlx_url", "http://localhost:8000")
    api_key = settings.get("omlx_api_key", "")

    conversations = await db.list_conversations()
    results: dict = {}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            login_resp = await client.post(
                f"{omlx_url}/admin/api/login",
                json={"api_key": api_key},
            )
            cookies = login_resp.cookies
            for conv in conversations:
                try:
                    latest = await db.get_latest_leaf(conv["id"])
                    if latest is None:
                        results[conv["id"]] = {"status": "empty"}
                        continue
                    results[conv["id"]] = await _probe_leaf_cache(
                        conv, latest["id"], client=client, cookies=cookies,
                        model_override=model,
                    )
                except Exception as e:
                    results[conv["id"]] = {"status": "error", "detail": str(e)}
    except Exception as e:
        # Total failure (oMLX down, login failed) — return empty so the UI
        # doesn't error out; dots will render as 'unknown'.
        return {"_error": str(e)}
    return results


# --- Document parsing ---


@app.post("/api/documents/parse")
async def parse_doc(file: UploadFile, conversation_id: Optional[str] = None):
    """Upload and parse a document, optionally saving to a conversation."""
    file_bytes = await file.read()
    result = parse_document(file_bytes, file.filename or "document")

    if conversation_id:
        doc = await db.add_document(
            conversation_id=conversation_id,
            filename=result["filename"],
            content=result["content"],
            token_estimate=result["token_estimate"],
        )
        result["id"] = doc["id"]

    return result


# --- Export ---


@app.get("/api/conversations/{conv_id}/export")
async def export_conversation(conv_id: str, format: str = "markdown"):
    """Export a conversation's active branch as markdown or JSON."""
    conv = await db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")

    messages = conv.get("messages", [])
    if not messages:
        raise HTTPException(400, "No messages to export")

    # Find active path: follow most-recent children from root
    active_path = _compute_active_path(messages)

    if format == "json":
        return {
            "title": conv["title"],
            "model": conv.get("model", ""),
            "messages": active_path,
        }

    # Markdown export — strip thinking blocks
    import re
    think_re = re.compile(r"<think>[\s\S]*?</think(?:\s+data-elapsed=\"\d+\")?>")

    lines = [f"# {conv['title']}\n"]
    for msg in active_path:
        role = msg["role"].capitalize()
        content = think_re.sub("", msg["content"]).strip()
        if content:
            lines.append(f"## {role}\n\n{content}\n")

    markdown = "\n".join(lines)
    from fastapi.responses import Response
    return Response(
        content=markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename=\"{conv['title']}.md\""},
    )


def _compute_active_path(messages: list[dict]) -> list[dict]:
    """Compute the active branch by following most-recent children from root."""
    by_parent: dict[Optional[str], list[dict]] = {}
    by_id: dict[str, dict] = {}
    for msg in messages:
        by_id[msg["id"]] = msg
        parent = msg.get("parent_id")
        by_parent.setdefault(parent, []).append(msg)

    # Start from root (parent_id is None)
    roots = by_parent.get(None, [])
    if not roots:
        return []

    # Follow most recent child at each level
    path = []
    current = roots[-1]  # most recent root
    while current:
        path.append(current)
        children = by_parent.get(current["id"], [])
        current = children[-1] if children else None

    return path
