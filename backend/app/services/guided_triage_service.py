from __future__ import annotations

import uuid
from typing import Any, AsyncIterator, Dict, List

from app.agents.guided_triage_graph import get_guided_triage_graph
from app.core.database import add_encounter, add_message, list_messages, upsert_session
from app.core.process_logger import log_step
from app.schemas.chat import ChatRequest
from app.services.medical_business import DISCLAIMER


class GuidedTriageService:
    """分步导诊台：基于 LangGraph 状态机的多轮结构化追问导诊。"""

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[Dict[str, Any]]:
        session_id = req.session_id or str(uuid.uuid4())
        scene = "guided_triage"
        log_step("guided_triage.begin", user_id=req.user_id, session_id=session_id)
        upsert_session(session_id, req.message[:30] or "分步导诊", user_id=req.user_id, scene=scene)

        msg_meta = {
            "scene": scene,
            "patient_context": req.patient_context.model_dump(),
            "engine": "langgraph",
        }
        user_message_id = add_message(session_id, "user", req.message, msg_meta)
        yield {"type": "session", "session_id": session_id, "user_message_id": user_message_id}

        history = list_messages(session_id, limit=20)
        graph = get_guided_triage_graph()
        config = {"configurable": {"thread_id": session_id}}

        input_state = {
            "latest_message": req.message,
            "patient": req.patient_context.model_dump(),
            "history": history,
            "user_id": req.user_id,
        }

        result = await graph.ainvoke(input_state, config=config)
        trace = result.get("trace") or []
        for item in trace:
            yield {
                "type": "trace",
                "agent": item.get("agent", "Agent"),
                "action": item.get("action", ""),
                "detail": item.get("detail", ""),
            }

        evidence = result.get("evidence") or []
        if evidence:
            yield {"type": "evidence", "items": evidence}

        phase = result.get("phase") or "collecting"
        answer = str(result.get("answer") or result.get("follow_up") or "").strip()
        if not answer:
            answer = "请继续补充您的症状信息，以便更准确导诊。"

        # 追问阶段整段输出；结论阶段模拟流式体验
        if phase == "completed":
            chunk_size = 28
            for i in range(0, len(answer), chunk_size):
                yield {"type": "chunk", "delta": answer[i : i + chunk_size]}
        else:
            yield {"type": "chunk", "delta": answer}
            yield {
                "type": "phase",
                "phase": phase,
                "completeness": result.get("completeness", 0),
                "questions_asked": result.get("questions_asked", 0),
            }

        risk_level = result.get("risk_level") or ""
        department = result.get("recommended_department") or ""
        assistant_meta = {
            "scene": scene,
            "phase": phase,
            "engine": "langgraph",
            "completeness": result.get("completeness", 0),
            "questions_asked": result.get("questions_asked", 0),
            "trace": trace,
        }
        if phase == "completed":
            assistant_meta["risk_level"] = risk_level
            assistant_meta["department"] = department

        add_message(session_id, "assistant", answer, assistant_meta)

        if phase == "completed":
            add_encounter(
                session_id=session_id,
                user_id=req.user_id,
                scene=scene,
                chief_complaint=req.message,
                risk_level=risk_level,
                department=department,
                summary=answer,
                metadata={
                    "engine": "langgraph",
                    "evidence_count": len(evidence),
                    "questions_asked": result.get("questions_asked", 0),
                },
            )

        thinking_steps = [f"{t.get('agent')}: {t.get('detail')}" for t in trace]
        log_step(
            "guided_triage.done",
            user_id=req.user_id,
            session_id=session_id,
            phase=phase,
            risk=risk_level or "-",
        )
        yield {
            "type": "done",
            "session_id": session_id,
            "phase": phase,
            "risk_level": risk_level,
            "recommended_department": department,
            "suggestions": [],
            "thinking_steps": thinking_steps,
            "evidence": evidence,
            "agent_trace": trace,
            "metrics": {
                "engine": "langgraph",
                "phase": phase,
                "completeness": result.get("completeness", 0),
                "questions_asked": result.get("questions_asked", 0),
                "agent_count": len({t.get("agent") for t in trace}),
                "evidence_count": len(evidence),
            },
            "disclaimer": DISCLAIMER,
        }
