"""Pydantic models for Canopy API requests and responses."""

from typing import Optional

from pydantic import BaseModel


class ConversationCreate(BaseModel):
    title: str = "New Chat"
    system_prompt: str = ""
    model: str = ""


class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None


class MessageCreate(BaseModel):
    role: str = "user"
    content: str
    parent_id: Optional[str] = None
    model: str = ""


class ChatRequest(BaseModel):
    conversation_id: str
    parent_id: str  # last message in the path — response becomes its child
    model: str


class SettingsUpdate(BaseModel):
    omlx_url: Optional[str] = None
    omlx_api_key: Optional[str] = None
    theme: Optional[str] = None
    system_prompt: Optional[str] = None


class DocumentParseResponse(BaseModel):
    id: str
    filename: str
    content: str
    token_estimate: int
