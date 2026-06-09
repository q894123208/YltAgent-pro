from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PatientContext(BaseModel):
    age: Optional[int] = None
    gender: Optional[str] = None
    chronic_diseases: Optional[str] = None
    allergy_history: Optional[str] = None
    medication_history: Optional[str] = None


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: Optional[str] = None
    user_id: str = "demo_user"
    patient_context: PatientContext = Field(default_factory=PatientContext)
    enable_deep_search: bool = True
    attached_doc_ids: List[str] = Field(default_factory=list)


class Evidence(BaseModel):
    source: str
    title: str
    score: float
    content: str


class AgentTrace(BaseModel):
    agent: str
    action: str
    detail: str
    duration_ms: int = 0


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    risk_level: str
    suggestions: List[str]
    recommended_department: str = "全科"
    thinking_steps: List[str] = Field(default_factory=list)
    disclaimer: str
    evidence: List[Evidence]
    agent_trace: List[AgentTrace]
    metrics: Dict[str, Any]
