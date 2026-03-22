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
from .models import (
    ChatRequest,
    ConversationCreate,
    ConversationUpdate,
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
    yield
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
    return templates.TemplateResponse("chat.html", {"request": request})


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

    for msg in path:
        content = msg["content"]
        # Try to parse JSON content (for multimodal)
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                content = parsed
        except (json.JSONDecodeError, TypeError):
            pass
        messages.append({"role": msg["role"], "content": content})

    payload = {
        "model": body.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

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
