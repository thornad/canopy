"""SQLite database for Canopy conversations and messages."""

import json
import time
import uuid
from pathlib import Path
from typing import Optional

import aiosqlite

_DB_PATH: Optional[Path] = None
_db: Optional[aiosqlite.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    system_prompt TEXT DEFAULT '',
    model TEXT DEFAULT '',
    folder_id TEXT REFERENCES folders(id) ON DELETE SET NULL,
    created_at REAL,
    updated_at REAL
);
-- Note: idx_conversations_folder is created in init_db() *after* the
-- folder_id migration runs, so legacy DBs (which lack the column when
-- this script executes) don't trip CREATE INDEX on a missing column.

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES messages(id),
    role TEXT CHECK(role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT,
    model TEXT,
    token_count INTEGER DEFAULT 0,
    cache_hit INTEGER DEFAULT 0,
    tool_calls TEXT,
    tool_call_id TEXT,
    created_at REAL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent
    ON messages(parent_id);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    filename TEXT,
    content TEXT,
    token_estimate INTEGER DEFAULT 0,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    command TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '[]',
    env TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL
);
"""

DEFAULT_SETTINGS = {
    "omlx_url": "http://localhost:8000",
    "omlx_api_key": "",
    "theme": "light",
    "system_prompt": "",
}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


async def init_db(db_path: Optional[Path] = None):
    """Initialize database connection and create schema."""
    global _db, _DB_PATH
    if db_path:
        _DB_PATH = db_path
    if _DB_PATH is None:
        _DB_PATH = Path.home() / ".canopy" / "canopy.db"
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    _db = await aiosqlite.connect(str(_DB_PATH))
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")
    await _db.executescript(SCHEMA)

    # Migrate pre-existing DBs: older schema restricted role to the three base
    # roles and had no tool_calls/tool_call_id columns. Rebuild the table if
    # the new columns are missing so we can persist MCP tool turns alongside
    # normal messages. Safe to run on every start — it's a no-op after the
    # first migration.
    cursor = await _db.execute("PRAGMA table_info(messages)")
    cols = {row["name"] for row in await cursor.fetchall()}
    if "tool_calls" not in cols:
        await _db.executescript(
            """
            ALTER TABLE messages RENAME TO _messages_old;
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                parent_id TEXT REFERENCES messages(id),
                role TEXT CHECK(role IN ('system', 'user', 'assistant', 'tool')),
                content TEXT,
                model TEXT,
                token_count INTEGER DEFAULT 0,
                cache_hit INTEGER DEFAULT 0,
                tool_calls TEXT,
                tool_call_id TEXT,
                created_at REAL
            );
            INSERT INTO messages
                (id, conversation_id, parent_id, role, content, model, token_count, cache_hit, created_at)
            SELECT
                id, conversation_id, parent_id, role, content, model, token_count, cache_hit, created_at
            FROM _messages_old;
            DROP TABLE _messages_old;
            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
            """
        )

    # Migrate pre-existing DBs that lack the conversations.folder_id column.
    # SQLite supports adding nullable columns in-place, so a simple ALTER is
    # safe and runs only once (the PRAGMA check makes it a no-op afterwards).
    cursor = await _db.execute("PRAGMA table_info(conversations)")
    conv_cols = {row["name"] for row in await cursor.fetchall()}
    if "folder_id" not in conv_cols:
        await _db.execute(
            "ALTER TABLE conversations ADD COLUMN folder_id TEXT "
            "REFERENCES folders(id) ON DELETE SET NULL"
        )
    # Index lives here (not in SCHEMA) so legacy DBs that get the column
    # added via the ALTER above still get the index — without erroring on
    # the executescript path before the column existed.
    await _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_folder "
        "ON conversations(folder_id)"
    )

    # Seed default settings
    for key, value in DEFAULT_SETTINGS.items():
        await _db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    await _db.commit()


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


def _get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db


# --- Settings ---


async def get_settings() -> dict:
    db = _get_db()
    cursor = await db.execute("SELECT key, value FROM settings")
    rows = await cursor.fetchall()
    return {row["key"]: row["value"] for row in rows}


async def update_settings(updates: dict):
    db = _get_db()
    for key, value in updates.items():
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
    await db.commit()


# --- Conversations ---


async def list_conversations() -> list[dict]:
    db = _get_db()
    cursor = await db.execute(
        "SELECT id, title, model, folder_id, created_at, updated_at "
        "FROM conversations ORDER BY updated_at DESC"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def create_conversation(
    title: str = "New Chat",
    system_prompt: str = "",
    model: str = "",
) -> dict:
    db = _get_db()
    conv_id = _new_id()
    now = time.time()
    await db.execute(
        "INSERT INTO conversations (id, title, system_prompt, model, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (conv_id, title, system_prompt, model, now, now),
    )
    await db.commit()
    return {
        "id": conv_id,
        "title": title,
        "system_prompt": system_prompt,
        "model": model,
        "created_at": now,
        "updated_at": now,
    }


async def get_conversation(conv_id: str) -> Optional[dict]:
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    conv = dict(row)

    # Fetch all messages for this conversation
    msg_cursor = await db.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
        (conv_id,),
    )
    msg_rows = await msg_cursor.fetchall()
    conv["messages"] = [dict(r) for r in msg_rows]

    # Fetch documents
    doc_cursor = await db.execute(
        "SELECT id, filename, token_estimate, created_at FROM documents "
        "WHERE conversation_id = ? ORDER BY created_at",
        (conv_id,),
    )
    doc_rows = await doc_cursor.fetchall()
    conv["documents"] = [dict(r) for r in doc_rows]

    return conv


async def update_conversation(conv_id: str, **kwargs) -> bool:
    db = _get_db()
    allowed = {"title", "system_prompt", "model", "folder_id"}
    # folder_id is the only field where None is meaningful (= unfile from
    # folder), so allow it through; other fields treat None as "no change".
    updates = {
        k: v for k, v in kwargs.items()
        if k in allowed and (v is not None or k == "folder_id")
    }
    if not updates:
        return False
    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [conv_id]
    await db.execute(
        f"UPDATE conversations SET {set_clause} WHERE id = ?", values
    )
    await db.commit()
    return True


async def delete_conversation(conv_id: str) -> bool:
    db = _get_db()
    cursor = await db.execute(
        "DELETE FROM conversations WHERE id = ?", (conv_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


# --- Folders ---


async def list_folders() -> list[dict]:
    db = _get_db()
    cursor = await db.execute(
        "SELECT id, name, created_at, updated_at "
        "FROM folders ORDER BY name COLLATE NOCASE"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def create_folder(name: str) -> dict:
    db = _get_db()
    folder_id = _new_id()
    now = time.time()
    await db.execute(
        "INSERT INTO folders (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (folder_id, name, now, now),
    )
    await db.commit()
    return {"id": folder_id, "name": name, "created_at": now, "updated_at": now}


async def update_folder(folder_id: str, name: str) -> bool:
    db = _get_db()
    cursor = await db.execute(
        "UPDATE folders SET name = ?, updated_at = ? WHERE id = ?",
        (name, time.time(), folder_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete_folder(folder_id: str) -> bool:
    # ON DELETE SET NULL on conversations.folder_id detaches the chats; the
    # chats themselves stay intact in the unfiled top-level list.
    db = _get_db()
    cursor = await db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    await db.commit()
    return cursor.rowcount > 0


# --- Messages ---


async def add_message(
    conversation_id: str,
    role: str,
    content: str,
    parent_id: Optional[str] = None,
    model: str = "",
    token_count: int = 0,
    cache_hit: bool = False,
    tool_calls: Optional[list] = None,
    tool_call_id: Optional[str] = None,
) -> dict:
    db = _get_db()
    msg_id = _new_id()
    now = time.time()
    tc_json = json.dumps(tool_calls) if tool_calls else None
    await db.execute(
        "INSERT INTO messages "
        "(id, conversation_id, parent_id, role, content, model, token_count, cache_hit, "
        "tool_calls, tool_call_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg_id, conversation_id, parent_id, role, content, model,
            token_count, int(cache_hit), tc_json, tool_call_id, now,
        ),
    )
    # Touch conversation updated_at
    await db.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (now, conversation_id),
    )
    await db.commit()
    return {
        "id": msg_id,
        "conversation_id": conversation_id,
        "parent_id": parent_id,
        "role": role,
        "content": content,
        "model": model,
        "token_count": token_count,
        "cache_hit": cache_hit,
        "tool_calls": tc_json,
        "tool_call_id": tool_call_id,
        "created_at": now,
    }


async def get_message_path(message_id: str) -> list[dict]:
    """Walk from a message back to the root, return path root→message."""
    db = _get_db()
    path = []
    current_id = message_id
    while current_id is not None:
        cursor = await db.execute(
            "SELECT * FROM messages WHERE id = ?", (current_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            break
        path.append(dict(row))
        current_id = row["parent_id"]
    path.reverse()
    return path


async def get_children(message_id: str) -> list[dict]:
    """Get all direct children of a message."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM messages WHERE parent_id = ? ORDER BY created_at",
        (message_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_root_messages(conversation_id: str) -> list[dict]:
    """Get messages with no parent (roots of the tree)."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND parent_id IS NULL "
        "ORDER BY created_at",
        (conversation_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_latest_leaf(conversation_id: str) -> Optional[dict]:
    """Return the most recently created message in a conversation.

    Used to probe the "active tip" of a branched chat for cache status:
    branches are fine-grained, but the newest leaf is the one most likely to
    correspond to what the user is currently viewing, and therefore the most
    useful single path to report cache status for.
    """
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM messages WHERE conversation_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (conversation_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def delete_message_tree(message_id: str) -> bool:
    """Delete a message and all its descendants recursively."""
    db = _get_db()
    # Check message exists
    cursor = await db.execute("SELECT id FROM messages WHERE id = ?", (message_id,))
    if not await cursor.fetchone():
        return False

    # Collect all descendant IDs via BFS
    to_delete = []
    queue = [message_id]
    while queue:
        current = queue.pop(0)
        to_delete.append(current)
        child_cursor = await db.execute(
            "SELECT id FROM messages WHERE parent_id = ?", (current,)
        )
        children = await child_cursor.fetchall()
        queue.extend(row["id"] for row in children)

    # Delete all
    placeholders = ",".join("?" for _ in to_delete)
    await db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", to_delete)
    await db.commit()
    return True


# --- Documents ---


async def add_document(
    conversation_id: str,
    filename: str,
    content: str,
    token_estimate: int = 0,
) -> dict:
    db = _get_db()
    doc_id = _new_id()
    now = time.time()
    await db.execute(
        "INSERT INTO documents (id, conversation_id, filename, content, token_estimate, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (doc_id, conversation_id, filename, content, token_estimate, now),
    )
    await db.commit()
    return {
        "id": doc_id,
        "conversation_id": conversation_id,
        "filename": filename,
        "content": content,
        "token_estimate": token_estimate,
        "created_at": now,
    }


async def get_document(doc_id: str) -> Optional[dict]:
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM documents WHERE id = ?", (doc_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


# --- MCP Servers ---


def _decode_mcp_row(row) -> dict:
    d = dict(row)
    try:
        d["args"] = json.loads(d.get("args") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["args"] = []
    try:
        d["env"] = json.loads(d.get("env") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["env"] = {}
    d["enabled"] = bool(d.get("enabled"))
    return d


async def list_mcp_servers() -> list[dict]:
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM mcp_servers ORDER BY created_at"
    )
    rows = await cursor.fetchall()
    return [_decode_mcp_row(r) for r in rows]


async def get_mcp_server(server_id: str) -> Optional[dict]:
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM mcp_servers WHERE id = ?", (server_id,)
    )
    row = await cursor.fetchone()
    return _decode_mcp_row(row) if row else None


async def add_mcp_server(
    name: str,
    command: str,
    args: list[str],
    env: dict[str, str],
    enabled: bool = True,
) -> dict:
    db = _get_db()
    sid = _new_id()
    now = time.time()
    await db.execute(
        "INSERT INTO mcp_servers (id, name, command, args, env, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, name, command, json.dumps(args), json.dumps(env), int(enabled), now),
    )
    await db.commit()
    return await get_mcp_server(sid)  # type: ignore[return-value]


async def update_mcp_server(server_id: str, **kwargs) -> Optional[dict]:
    db = _get_db()
    allowed = {"name", "command", "args", "env", "enabled"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return await get_mcp_server(server_id)
    if "args" in updates:
        updates["args"] = json.dumps(updates["args"])
    if "env" in updates:
        updates["env"] = json.dumps(updates["env"])
    if "enabled" in updates:
        updates["enabled"] = int(bool(updates["enabled"]))
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [server_id]
    await db.execute(
        f"UPDATE mcp_servers SET {set_clause} WHERE id = ?", values
    )
    await db.commit()
    return await get_mcp_server(server_id)


async def delete_mcp_server(server_id: str) -> bool:
    db = _get_db()
    cursor = await db.execute(
        "DELETE FROM mcp_servers WHERE id = ?", (server_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


async def sync_mcp_from_json(path: Path) -> dict:
    """Mirror an LM Studio / Claude Desktop style mcp.json into the mcp_servers table.

    JSON is authoritative: servers in the file are inserted/updated; servers in
    the DB whose name is not in the file are deleted. Returns counts.
    """
    data = json.loads(path.read_text())
    spec = (data.get("mcpServers") or data.get("servers") or {})
    existing = {s["name"]: s for s in await list_mcp_servers()}
    desired: set[str] = set()
    added = updated = removed = 0

    for name, conf in spec.items():
        if not isinstance(conf, dict):
            continue
        command = conf.get("command")
        if not command:
            continue
        desired.add(name)
        args = list(conf.get("args") or [])
        env = dict(conf.get("env") or {})
        enabled = bool(conf.get("enabled", True))
        if conf.get("disabled") is True:
            enabled = False

        cur = existing.get(name)
        if cur is None:
            await add_mcp_server(name=name, command=command, args=args, env=env, enabled=enabled)
            added += 1
        elif (cur["command"] != command or cur["args"] != args
              or cur["env"] != env or cur["enabled"] != enabled):
            await update_mcp_server(
                cur["id"], command=command, args=args, env=env, enabled=enabled,
            )
            updated += 1

    for name, cur in existing.items():
        if name not in desired:
            await delete_mcp_server(cur["id"])
            removed += 1

    return {"added": added, "updated": updated, "removed": removed}
