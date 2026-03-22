"""Tests for Canopy database layer."""

import tempfile
from pathlib import Path

import pytest

from canopy import database as db


@pytest.fixture(autouse=True)
async def setup_db():
    """Create a fresh in-memory-like DB for each test."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        await db.init_db(db_path)
        yield
        await db.close_db()


# --- Settings ---


class TestSettings:
    async def test_default_settings(self):
        settings = await db.get_settings()
        assert settings["omlx_url"] == "http://localhost:8000"
        assert settings["omlx_api_key"] == ""
        assert settings["theme"] == "light"
        assert settings["system_prompt"] == ""

    async def test_update_settings(self):
        await db.update_settings({"omlx_url": "http://localhost:9000", "theme": "dark"})
        settings = await db.get_settings()
        assert settings["omlx_url"] == "http://localhost:9000"
        assert settings["theme"] == "dark"
        # Unchanged
        assert settings["omlx_api_key"] == ""


# --- Conversations ---


class TestConversations:
    async def test_create_and_list(self):
        conv = await db.create_conversation(title="Test Chat", model="gpt-4")
        assert conv["id"]
        assert conv["title"] == "Test Chat"
        assert conv["model"] == "gpt-4"

        convs = await db.list_conversations()
        assert len(convs) == 1
        assert convs[0]["id"] == conv["id"]

    async def test_get_conversation(self):
        conv = await db.create_conversation(title="Full Chat")
        result = await db.get_conversation(conv["id"])
        assert result is not None
        assert result["title"] == "Full Chat"
        assert result["messages"] == []
        assert result["documents"] == []

    async def test_get_nonexistent(self):
        result = await db.get_conversation("nonexistent")
        assert result is None

    async def test_update_conversation(self):
        conv = await db.create_conversation(title="Old Title")
        ok = await db.update_conversation(conv["id"], title="New Title")
        assert ok is True
        result = await db.get_conversation(conv["id"])
        assert result["title"] == "New Title"

    async def test_delete_conversation(self):
        conv = await db.create_conversation(title="To Delete")
        ok = await db.delete_conversation(conv["id"])
        assert ok is True
        assert await db.get_conversation(conv["id"]) is None

    async def test_delete_nonexistent(self):
        ok = await db.delete_conversation("nonexistent")
        assert ok is False

    async def test_list_ordered_by_updated(self):
        c1 = await db.create_conversation(title="First")
        c2 = await db.create_conversation(title="Second")
        # Update c1 to make it more recent
        await db.update_conversation(c1["id"], title="First Updated")
        convs = await db.list_conversations()
        assert convs[0]["id"] == c1["id"]  # most recently updated
        assert convs[1]["id"] == c2["id"]


# --- Messages ---


class TestMessages:
    async def test_add_message(self):
        conv = await db.create_conversation()
        msg = await db.add_message(conv["id"], "user", "Hello")
        assert msg["id"]
        assert msg["role"] == "user"
        assert msg["content"] == "Hello"
        assert msg["parent_id"] is None

    async def test_linear_chain(self):
        conv = await db.create_conversation()
        m1 = await db.add_message(conv["id"], "user", "Hi")
        m2 = await db.add_message(conv["id"], "assistant", "Hello!", parent_id=m1["id"])
        m3 = await db.add_message(conv["id"], "user", "How are you?", parent_id=m2["id"])

        # Verify path from m3 to root
        path = await db.get_message_path(m3["id"])
        assert len(path) == 3
        assert path[0]["content"] == "Hi"
        assert path[1]["content"] == "Hello!"
        assert path[2]["content"] == "How are you?"

    async def test_branching(self):
        conv = await db.create_conversation()
        m1 = await db.add_message(conv["id"], "user", "Hi")
        m2 = await db.add_message(conv["id"], "assistant", "Hello!", parent_id=m1["id"])

        # Branch: two different follow-ups to m2
        m3a = await db.add_message(conv["id"], "user", "Branch A", parent_id=m2["id"])
        m3b = await db.add_message(conv["id"], "user", "Branch B", parent_id=m2["id"])

        # Both branches share the same path up to m2
        path_a = await db.get_message_path(m3a["id"])
        path_b = await db.get_message_path(m3b["id"])
        assert len(path_a) == 3
        assert len(path_b) == 3
        assert path_a[0]["id"] == path_b[0]["id"]  # m1
        assert path_a[1]["id"] == path_b[1]["id"]  # m2
        assert path_a[2]["content"] == "Branch A"
        assert path_b[2]["content"] == "Branch B"

    async def test_get_children(self):
        conv = await db.create_conversation()
        m1 = await db.add_message(conv["id"], "user", "Root")
        m2a = await db.add_message(conv["id"], "assistant", "Response A", parent_id=m1["id"])
        m2b = await db.add_message(conv["id"], "assistant", "Response B", parent_id=m1["id"])

        children = await db.get_children(m1["id"])
        assert len(children) == 2
        assert children[0]["content"] == "Response A"
        assert children[1]["content"] == "Response B"

    async def test_get_root_messages(self):
        conv = await db.create_conversation()
        m1 = await db.add_message(conv["id"], "user", "First root")
        m2 = await db.add_message(conv["id"], "assistant", "Reply", parent_id=m1["id"])
        # Edit creates a new root-level user message (sibling of m1)
        m3 = await db.add_message(conv["id"], "user", "Edited root")

        roots = await db.get_root_messages(conv["id"])
        assert len(roots) == 2
        assert roots[0]["content"] == "First root"
        assert roots[1]["content"] == "Edited root"

    async def test_conversation_includes_messages(self):
        conv = await db.create_conversation()
        await db.add_message(conv["id"], "user", "Hello")
        await db.add_message(conv["id"], "assistant", "Hi there")

        result = await db.get_conversation(conv["id"])
        assert len(result["messages"]) == 2

    async def test_cascade_delete(self):
        conv = await db.create_conversation()
        await db.add_message(conv["id"], "user", "Hello")
        await db.delete_conversation(conv["id"])

        # Messages should be gone too (CASCADE)
        result = await db.get_conversation(conv["id"])
        assert result is None


# --- Documents ---


class TestDocuments:
    async def test_add_document(self):
        conv = await db.create_conversation()
        doc = await db.add_document(conv["id"], "test.md", "# Hello", token_estimate=2)
        assert doc["id"]
        assert doc["filename"] == "test.md"
        assert doc["token_estimate"] == 2

    async def test_conversation_includes_documents(self):
        conv = await db.create_conversation()
        await db.add_document(conv["id"], "file.pdf", "PDF content", token_estimate=100)
        result = await db.get_conversation(conv["id"])
        assert len(result["documents"]) == 1
        assert result["documents"][0]["filename"] == "file.pdf"

    async def test_get_document(self):
        conv = await db.create_conversation()
        doc = await db.add_document(conv["id"], "doc.txt", "Some text")
        result = await db.get_document(doc["id"])
        assert result is not None
        assert result["content"] == "Some text"


# --- Active Path ---


class TestActivePath:
    async def test_active_path_linear(self):
        from canopy.server import _compute_active_path

        messages = [
            {"id": "1", "parent_id": None, "role": "user", "content": "Hi", "created_at": 1.0},
            {"id": "2", "parent_id": "1", "role": "assistant", "content": "Hello", "created_at": 2.0},
            {"id": "3", "parent_id": "2", "role": "user", "content": "Bye", "created_at": 3.0},
        ]
        path = _compute_active_path(messages)
        assert [m["id"] for m in path] == ["1", "2", "3"]

    async def test_active_path_branching(self):
        from canopy.server import _compute_active_path

        messages = [
            {"id": "1", "parent_id": None, "role": "user", "content": "Hi", "created_at": 1.0},
            {"id": "2a", "parent_id": "1", "role": "assistant", "content": "A", "created_at": 2.0},
            {"id": "2b", "parent_id": "1", "role": "assistant", "content": "B", "created_at": 3.0},
            {"id": "3b", "parent_id": "2b", "role": "user", "content": "Follow B", "created_at": 4.0},
        ]
        # Most recent child of root "1" is "2b", most recent child of "2b" is "3b"
        path = _compute_active_path(messages)
        assert [m["id"] for m in path] == ["1", "2b", "3b"]

    async def test_active_path_empty(self):
        from canopy.server import _compute_active_path

        assert _compute_active_path([]) == []
