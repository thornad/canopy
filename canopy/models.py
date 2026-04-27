"""Pydantic models for Canopy API requests and responses."""

from typing import Optional

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: str = "New Chat"
    system_prompt: str = ""
    model: str = ""


# `folder_id` uses a sentinel default ("__unset__") to distinguish "client
# didn't send the field" from "client sent null to unfile the chat" — the
# default `Optional[str] = None` would conflate the two and prevent unfiling.
class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    folder_id: Optional[str] = Field(default="__unset__")


class FolderCreate(BaseModel):
    name: str


class FolderUpdate(BaseModel):
    name: str


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


class MCPServerCreate(BaseModel):
    name: str
    command: str
    args: str = ""  # shell-style string, parsed server-side
    env: str = ""   # KEY=VAL lines, parsed server-side
    enabled: bool = True


class MCPServerUpdate(BaseModel):
    name: Optional[str] = None
    command: Optional[str] = None
    args: Optional[str] = None
    env: Optional[str] = None
    enabled: Optional[bool] = None
