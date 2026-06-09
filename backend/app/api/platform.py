from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.database import add_appointment, appointment_counts, cancel_appointment, list_appointments, list_encounters
from app.core.auth import get_current_user
from app.schemas.chat import ChatRequest
from app.services.guided_triage_service import GuidedTriageService
from app.services.medication_safety_service import MedicationSafetyService
from app.services.medical_business import (
    ConsultationService,
    departments,
    interpret_report,
    report_list,
    schedule_for_department,
)

router = APIRouter(prefix="/api", tags=["platform"])
service = ConsultationService()
guided_triage_service = GuidedTriageService()
medication_safety_service = MedicationSafetyService()


def _sse_response(req: ChatRequest, scene: str):
    async def event_stream():
        try:
            async for event in service.chat_stream(req, scene=scene):
                payload = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except Exception as exc:
            err = json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False)
            yield f"data: {err}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/triage/stream")
async def triage_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    req.user_id = user["user_id"]
    return _sse_response(req, scene="triage")


@router.post("/guided-triage/stream")
async def guided_triage_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    req.user_id = user["user_id"]

    async def event_stream():
        try:
            async for event in guided_triage_service.chat_stream(req):
                payload = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except Exception as exc:
            err = json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False)
            yield f"data: {err}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/consultation/stream")
async def consultation_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    req.user_id = user["user_id"]
    return _sse_response(req, scene="consultation")


@router.post("/medication/stream")
async def medication_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    req.user_id = user["user_id"]
    return _sse_response(req, scene="medication")


@router.post("/medication-safety/agent/stream")
async def medication_safety_agent_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    req.user_id = user["user_id"]

    async def event_stream():
        try:
            async for event in medication_safety_service.chat_stream(req, user):
                payload = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except Exception as exc:
            err = json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False)
            yield f"data: {err}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/records")
async def records(days: int = Query(default=7, ge=1, le=365), user: dict = Depends(get_current_user)):
    return {"records": list_encounters(user_id=user["user_id"], days=days)}


@router.get("/reports")
async def reports():
    return {"reports": report_list()}


@router.get("/reports/{report_id}/interpret")
async def report_interpretation(report_id: str):
    return await interpret_report(report_id)


@router.get("/departments")
async def department_list():
    return {"departments": departments()}


@router.get("/appointments/schedule")
async def appointment_schedule(department: str = "呼吸科", user: dict = Depends(get_current_user)):
    return schedule_for_department(department, appointment_counts(user_id=user["user_id"]))


@router.post("/appointments")
async def create_appointment(payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    if int(payload.get("remaining", 1)) <= 0:
        raise HTTPException(status_code=400, detail="当前号源已约满")
    payload["user_id"] = user["user_id"]
    appointment_id = add_appointment(payload)
    return {"ok": True, "appointment_id": appointment_id, "appointments": list_appointments(user["user_id"])}


@router.get("/appointments")
async def appointments(user: dict = Depends(get_current_user)):
    return {"appointments": list_appointments(user_id=user["user_id"])}


@router.delete("/appointments/{appointment_id}")
async def appointment_cancel(appointment_id: int, user: dict = Depends(get_current_user)):
    ok = cancel_appointment(appointment_id, user_id=user["user_id"])
    return {"ok": ok, "appointments": list_appointments(user_id=user["user_id"])}
