from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentSessionCreateRequest(BaseModel):
    title: str | None = None


class AgentMessageResponse(BaseModel):
    id: str
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None
    image_session_id: str | None = None
    created_at: datetime


class AgentSessionResponse(BaseModel):
    id: str
    title: str
    stage: str
    image_session_id: str | None = None
    created_at: datetime
    updated_at: datetime


class AgentSessionDetailResponse(AgentSessionResponse):
    messages: list[AgentMessageResponse] = []


class AgentSessionListResponse(BaseModel):
    items: list[AgentSessionResponse]


class AgentTurnRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class AgentGenerationTaskStatus(BaseModel):
    image_session_id: str | None = None
    task_id: str
    status: str


class AgentTurnResponse(BaseModel):
    session: AgentSessionDetailResponse
    tool_events: list[dict[str, Any]] = []
    pending_generation_tasks: list[AgentGenerationTaskStatus] = []
