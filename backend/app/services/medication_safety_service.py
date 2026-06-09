from __future__ import annotations

import uuid
from typing import Any, AsyncIterator, Dict, List

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.messages import AIMessage, HumanMessage

from app.core.auth import public_user
from app.core.database import add_encounter, add_message, list_messages, upsert_session
from app.core.process_logger import log_step
from app.langchain.llm import get_chat_model
from app.langchain.prompts import MEDICATION_SAFETY_AGENT_PROMPT
from app.langchain.tools import build_medication_safety_tools, finalize_medication_answer
from app.schemas.chat import ChatRequest
from app.core.database import get_user_by_id
from app.services.medical_business import DISCLAIMER


MAX_AGENT_ITERATIONS = 10
TOOL_LABELS = {
    "search_medication_knowledge": "RAGAgent",
    "get_patient_medication_profile": "ProfileAgent",
    "check_medication_interaction_rules": "SafetyAgent",
    "web_search_medication_info": "ResearchAgent",
}


class MedicationSafetyService:
    """用药安全 Agent：LangChain Tool-calling Agent + SSE 事件流。"""

    def _build_executor(self, user: Dict[str, Any], patient: Dict[str, Any]) -> AgentExecutor:
        llm = get_chat_model(temperature=0.2, max_tokens=1600)
        tools = build_medication_safety_tools(public_user(user), patient)
        agent = create_tool_calling_agent(llm, tools, MEDICATION_SAFETY_AGENT_PROMPT)
        return AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=False,
            max_iterations=MAX_AGENT_ITERATIONS,
            early_stopping_method="generate",
            handle_parsing_errors=True,
            return_intermediate_steps=True,
        )

    @staticmethod
    def _history_to_messages(history: List[Dict[str, Any]]) -> List[Any]:
        pairs: List[Any] = []
        for row in history[-10:]:
            role = row.get("role")
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                pairs.append(HumanMessage(content=content))
            elif role == "assistant":
                pairs.append(AIMessage(content=content))
        return pairs

    async def chat_stream(self, req: ChatRequest, user: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        session_id = req.session_id or str(uuid.uuid4())
        scene = "medication_safety"
        patient = req.patient_context.model_dump()
        log_step("medication_safety.begin", user_id=req.user_id, session_id=session_id)

        upsert_session(session_id, req.message[:30] or "用药安全咨询", user_id=req.user_id, scene=scene)
        msg_meta = {
            "scene": scene,
            "patient_context": patient,
            "engine": "langchain",
        }
        user_message_id = add_message(session_id, "user", req.message, msg_meta)
        yield {"type": "session", "session_id": session_id, "user_message_id": user_message_id}

        db_user = get_user_by_id(req.user_id) or user
        executor = self._build_executor(db_user, patient)
        history = list_messages(session_id, limit=12)
        chat_history = self._history_to_messages(history[:-1] if history else [])

        yield {
            "type": "trace",
            "agent": "MedicationAgent",
            "action": "start",
            "detail": "LangChain Tool Agent 开始分析用药安全问题",
        }

        trace: List[Dict[str, Any]] = [
            {
                "agent": "MedicationAgent",
                "action": "start",
                "detail": "LangChain Tool Agent 开始分析用药安全问题",
            }
        ]
        evidence_items: List[Dict[str, Any]] = []
        answer = ""
        intermediate_steps: List[Any] = []

        try:
            result = await executor.ainvoke(
                {
                    "input": req.message,
                    "chat_history": chat_history,
                }
            )
            answer = str(result.get("output") or "").strip()
            intermediate_steps = list(result.get("intermediate_steps") or [])
        except Exception as exc:
            log_step("medication_safety.agent_failed", error=type(exc).__name__)
            yield {
                "type": "trace",
                "agent": "MedicationAgent",
                "action": "fallback",
                "detail": f"Agent 执行失败，切换简化回答：{type(exc).__name__}",
            }
            answer = (
                "暂时无法完成完整的用药安全分析。请补充具体药名、剂量、用药时长，"
                "并咨询药学门诊或主治医生。"
            )

        for step in intermediate_steps:
            if not isinstance(step, tuple) or len(step) != 2:
                continue
            action, observation = step
            tool_name = getattr(action, "tool", "") or ""
            agent = TOOL_LABELS.get(tool_name, "ToolAgent")
            detail = f"调用 {tool_name}：{str(getattr(action, 'tool_input', ''))[:120]}"
            trace.append({"agent": agent, "action": "tool_call", "detail": detail})
            yield {"type": "trace", "agent": agent, "action": "tool_call", "detail": detail}
            if tool_name == "search_medication_knowledge" and observation:
                evidence_items.append(
                    {
                        "source": "医学知识库",
                        "title": "用药安全检索结果",
                        "score": 1.0,
                        "content": str(observation)[:800],
                    }
                )
            if tool_name == "web_search_medication_info" and observation:
                evidence_items.append(
                    {
                        "source": "联网检索",
                        "title": "用药安全联网资料",
                        "score": 0.9,
                        "content": str(observation)[:800],
                    }
                )

        if evidence_items:
            yield {"type": "evidence", "items": evidence_items[:6]}

        answer = finalize_medication_answer(answer)
        if not answer:
            answer = "暂未生成有效回答，请补充药名、剂量和用药场景后重试。"

        chunk_size = 32
        for i in range(0, len(answer), chunk_size):
            yield {"type": "chunk", "delta": answer[i : i + chunk_size]}

        risk_level = "中风险" if any(k in req.message for k in ("一起吃", "同时", "联合", "相互作用")) else "低风险"
        if any(k in req.message for k in ("胸痛", "呼吸困难", "过敏", "休克", "出血")):
            risk_level = "高风险"

        add_message(
            session_id,
            "assistant",
            answer,
            {
                "scene": scene,
                "engine": "langchain",
                "risk_level": risk_level,
                "department": "药学门诊",
                "trace": trace,
            },
        )
        add_encounter(
            session_id=session_id,
            user_id=req.user_id,
            scene=scene,
            chief_complaint=req.message,
            risk_level=risk_level,
            department="药学门诊",
            summary=answer,
            metadata={"engine": "langchain", "tool_calls": len(intermediate_steps)},
        )

        thinking_steps = [f"{t['agent']}: {t['detail']}" for t in trace]
        yield {
            "type": "done",
            "session_id": session_id,
            "phase": "completed",
            "risk_level": risk_level,
            "recommended_department": "药学门诊",
            "suggestions": ["如需调整处方或确认剂量，请咨询药学门诊或主治医生。"],
            "thinking_steps": thinking_steps,
            "evidence": evidence_items[:6],
            "agent_trace": trace,
            "metrics": {
                "engine": "langchain",
                "tool_calls": len(intermediate_steps),
                "agent_count": len({t["agent"] for t in trace}),
                "evidence_count": len(evidence_items),
            },
            "disclaimer": DISCLAIMER,
        }
        log_step("medication_safety.done", user_id=req.user_id, session_id=session_id, tools=len(intermediate_steps))
