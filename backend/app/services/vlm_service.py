from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from app.core.config import SETTINGS
from app.core.process_logger import log_step
from app.services.document_processor import ImagePayload

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是医学报告/影像解析助手。把图片中**真实可见的内容**忠实转录为 JSON，禁止推测、禁止确诊、禁止开处方。识别不准的字段填 null 并加入 uncertain_fields。中文输出，只输出 JSON，不要 Markdown。

JSON 结构：
{"doc_type":"lab_report|imaging_report|imaging_film|prescription|other","title":"简短中文标题","report_date":"YYYY-MM-DD或null","patient_info":{"name":null,"gender":null,"age":null},"items":[{"name":"","value":"","unit":null,"ref_range":null,"flag":"偏高|偏低|正常|异常|null"}],"findings":"影像所见/描述","impression":"印象/结论","recommendations":["报告中给出的建议"],"summary":"2-4 句面向患者的关键信息","key_abnormalities":["关键异常项"],"suggested_department":"呼吸科|消化科|内分泌科|内科 等","uncertain_fields":[],"confidence":0.0}
"""

USER_INSTRUCTION = "请按系统提示解析下面这份医疗文档/影像，必须只输出一个合法 JSON 对象，不要解释、不要 Markdown。若为多页同一份报告，请综合整合。"
TEXT_USER_INSTRUCTION = "请按系统提示解析下面这份已抽取文本的医疗文档，必须只输出一个合法 JSON 对象，不要解释、不要 Markdown。"


class VLMService:
    """远程视觉模型解析。

    Gemini 模型优先使用 DMXAPI 的原生 generateContent 接口；其他模型走
    OpenAI 兼容的 chat/completions。网络波动时会短暂重试，并关闭环境代理，
    避免 Windows 代理变量导致的随机 ConnectError/ConnectTimeout。
    """

    def __init__(self):
        cfg = SETTINGS.get("vlm") or SETTINGS.get("llm") or {}
        self.api_key: str = cfg.get("api_key", "")
        self.base_url: str = cfg.get("base_url", "").rstrip("/")
        self.model_name: str = cfg.get("model_name", "")
        self.temperature: float = float(cfg.get("temperature", 0.1))
        self.max_tokens: int = int(cfg.get("max_tokens", 2048))
        self.enabled: bool = bool(self.api_key and self.base_url and self.model_name)

    def _headers(self) -> Dict[str, str]:
        auth_scheme = str((SETTINGS.get("llm") or {}).get("auth_scheme", "auto")).lower()
        if auth_scheme == "bearer":
            authorization = f"Bearer {self.api_key}"
        elif auth_scheme == "raw" or "dmxapi.cn" in self.base_url:
            authorization = self.api_key
        else:
            authorization = f"Bearer {self.api_key}"
        return {"Authorization": authorization, "Content-Type": "application/json"}

    @property
    def is_gemini(self) -> bool:
        return self.model_name.lower().startswith("gemini")

    def _provider_base(self) -> str:
        return re.sub(r"/v1/?$", "", self.base_url.rstrip("/"))

    async def analyze(
        self,
        images: List[ImagePayload],
        doc_type_hint: Optional[str] = None,
        extra_user_text: Optional[str] = None,
        timeout: float = 90.0,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("VLM is disabled or misconfigured")
        if not images:
            raise ValueError("no image to analyze")

        prompt = USER_INSTRUCTION
        if doc_type_hint:
            prompt += f"\n用户提示文档类型：{doc_type_hint}"
        if extra_user_text:
            prompt += f"\n用户附加说明：{extra_user_text}"

        last_error: Optional[Exception] = None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                log_step("model.image_parse.request", model=self.model_name, images=len(images), attempt=attempt + 1)
                raw = await self._chat_completions_analyze(images, prompt, timeout)
                log_step("model.image_parse.response", model=self.model_name, chars=len(raw))
                return _normalise_parsed_report(raw, doc_type_hint)
            except Exception as exc:
                last_error = exc
                logger.warning("VLM analyze attempt %s failed: %s", attempt + 1, exc)
                await asyncio.sleep(0.8 + attempt * 1.2)

        # 默认不按模型家族分流；仅当兼容接口失败时，Gemini 再尝试原生接口兜底。
        if self.is_gemini:
            try:
                log_step("model.image_parse.gemini_native_fallback", model=self.model_name, images=len(images))
                raw = await self._gemini_analyze(images, prompt, timeout)
                return _normalise_parsed_report(raw, doc_type_hint)
            except Exception as exc:
                last_error = exc
                logger.warning("VLM Gemini native fallback failed: %s", exc)

        raise RuntimeError(f"VLM analyze failed: {last_error}")

    async def analyze_text(
        self,
        text: str,
        doc_type_hint: Optional[str] = None,
        extra_user_text: Optional[str] = None,
        timeout: float = 90.0,
    ) -> Dict[str, Any]:
        """解析 PDF/DOCX/XLSX/CSV 抽取出的文本，输出统一报告 JSON。"""
        if not self.enabled:
            raise RuntimeError("VLM is disabled or misconfigured")
        content = (text or "").strip()
        if not content:
            raise ValueError("no text to analyze")

        prompt = TEXT_USER_INSTRUCTION
        if doc_type_hint:
            prompt += f"\n用户提示文档类型：{doc_type_hint}"
        if extra_user_text:
            prompt += f"\n用户附加说明：{extra_user_text}"
        prompt += "\n\n【文档文本】\n" + content[:24000]

        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        url = f"{self.base_url}/chat/completions"
        headers = self._headers()
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                log_step("model.text_parse.request", model=self.model_name, chars=len(content), attempt=attempt + 1)
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout, connect=30.0, read=timeout, write=30.0, pool=10.0),
                    trust_env=False,
                ) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                raw = (data["choices"][0]["message"].get("content") or "").strip()
                log_step("model.text_parse.response", model=self.model_name, chars=len(raw))
                parsed = _normalise_parsed_report(raw, doc_type_hint)
                parsed.setdefault("raw_text", content[:12000])
                return parsed
            except Exception as exc:
                last_error = exc
                logger.warning("text report analyze attempt %s failed: %s", attempt + 1, exc)
                await asyncio.sleep(0.8 + attempt * 1.2)
        raise RuntimeError(f"text report analyze failed: {last_error}")

    async def _gemini_analyze(self, images: List[ImagePayload], prompt: str, timeout: float) -> str:
        model = quote(self.model_name, safe="")
        url = f"{self._provider_base()}/v1beta/models/{model}:generateContent?key={self.api_key}"
        parts: List[Dict[str, Any]] = [{"text": prompt}]
        for img in images:
            parts.append({"inline_data": {"mime_type": img.mime or "image/png", "data": _data_url_body(img.data_url)}})
        payload: Dict[str, Any] = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
            },
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30.0, read=timeout, write=30.0, pool=10.0),
            trust_env=False,
        ) as client:
            resp = await client.post(url, headers={"Content-Type": "application/json; charset=utf-8"}, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return _extract_gemini_text(data)

    async def _chat_completions_analyze(self, images: List[ImagePayload], prompt: str, timeout: float) -> str:
        user_content: List[Dict[str, Any]] = []
        user_content.append({"type": "text", "text": prompt})
        for img in images:
            user_content.append(
                {"type": "image_url", "image_url": {"url": img.data_url}}
            )

        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        url = f"{self.base_url}/chat/completions"
        headers = self._headers()

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30.0, read=timeout, write=30.0, pool=10.0),
            trust_env=False,
        ) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return (data["choices"][0]["message"].get("content") or "").strip()


def _data_url_body(data_url: str) -> str:
    if "," in data_url:
        return data_url.split(",", 1)[1].strip()
    return data_url.strip()


def _extract_gemini_text(data: Dict[str, Any]) -> str:
    parts: List[str] = []
    for candidate in data.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if part.get("text"):
                parts.append(str(part["text"]))
    return "".join(parts).strip()


def _normalise_parsed_report(raw: str, doc_type_hint: Optional[str]) -> Dict[str, Any]:
    parsed = _parse_json(raw)
    if parsed is None:
        parsed = _fallback_structured_report(raw, doc_type_hint)
    parsed.setdefault("items", [])
    parsed.setdefault("recommendations", [])
    parsed.setdefault("uncertain_fields", [])
    parsed.setdefault("key_abnormalities", [])
    parsed.setdefault("confidence", 0.5)
    return parsed


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    for cand in (cleaned, _extract_first_json(cleaned)):
        if not cand:
            continue
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


def _extract_first_json(text: str) -> Optional[str]:
    match = re.search(r"\{[\s\S]*\}", text)
    return match.group(0) if match else None


def _guess_doc_type(text: str, doc_type_hint: Optional[str]) -> str:
    hint = (doc_type_hint or "").lower()
    if hint in {"lab_report", "imaging_report", "imaging_film", "prescription"}:
        return hint
    doc_type_match = re.search(r'"doc_type"\s*:\s*"([^"]+)"', text)
    if doc_type_match:
        value = doc_type_match.group(1)
        if value in {"lab_report", "imaging_report", "imaging_film", "prescription", "other"}:
            return value
    if any(word in text for word in ["白细胞", "红细胞", "血红蛋白", "血小板", "参考范围", "检验"]):
        return "lab_report"
    if any(word in text for word in ["CT", "MRI", "核磁", "超声", "B超", "影像", "所见", "印象"]):
        return "imaging_report"
    if any(word in text for word in ["处方", "用法", "用量", "药品"]):
        return "prescription"
    return "other"


def _fallback_title(doc_type: str, text: str) -> str:
    title_map = {
        "lab_report": "检验报告解析",
        "imaging_report": "影像检查报告解析",
        "imaging_film": "影像片解析",
        "prescription": "处方/用药单解析",
        "other": "医疗文档解析",
    }
    title_match = re.search(r'"title"\s*:\s*"([^"]+)"', text)
    if title_match and title_match.group(1).strip():
        return title_match.group(1).strip()
    for line in text.splitlines()[:8]:
        cleaned = line.strip(" #：:，,。")
        if 4 <= len(cleaned) <= 28 and any(word in cleaned for word in ["报告", "检查", "检验", "处方", "超声", "CT", "核磁"]):
            return cleaned
    return title_map.get(doc_type, "医疗文档解析")


def _fallback_structured_report(raw: str, doc_type_hint: Optional[str]) -> Dict[str, Any]:
    text = (raw or "").strip()
    doc_type = _guess_doc_type(text, doc_type_hint)
    summary = text[:800] if text else "模型未返回可解析的结构化内容，请以原始报告为准。"
    uncertain = ["结构化JSON解析失败，已保留模型原始转录文本"]
    return {
        "doc_type": doc_type,
        "title": _fallback_title(doc_type, text),
        "report_date": None,
        "patient_info": {"name": None, "gender": None, "age": None},
        "items": [],
        "findings": summary if doc_type in {"imaging_report", "imaging_film"} else "",
        "impression": "",
        "recommendations": [],
        "summary": summary,
        "key_abnormalities": [],
        "suggested_department": "",
        "uncertain_fields": uncertain,
        "confidence": 0.35 if text else 0.1,
        "raw": text,
    }


def build_failed_report(error: Exception, doc_type_hint: Optional[str] = None) -> Dict[str, Any]:
    """远程 VLM 不可用时的可入库占位结构，避免上传链路整体失败。"""
    doc_type = _guess_doc_type("", doc_type_hint)
    message = f"远程视觉模型暂时不可用，原始文件已保存，稍后可重新解析。错误类型：{type(error).__name__}"
    return {
        "doc_type": doc_type,
        "title": "报告待解析",
        "report_date": None,
        "patient_info": {"name": None, "gender": None, "age": None},
        "items": [],
        "findings": "",
        "impression": "",
        "recommendations": [],
        "summary": message,
        "key_abnormalities": [],
        "suggested_department": "",
        "uncertain_fields": ["remote_vlm_unavailable"],
        "confidence": 0.0,
        "parse_status": "failed",
        "error": str(error)[:300],
    }


def build_report_summary(parsed: Dict[str, Any]) -> str:
    """生成存入 Chroma user_reports 的语义摘要文本。"""
    parts: List[str] = []
    title = parsed.get("title") or parsed.get("doc_type") or "医疗报告"
    parts.append(f"报告类型/标题：{title}")
    if parsed.get("report_date"):
        parts.append(f"报告日期：{parsed['report_date']}")
    if parsed.get("summary"):
        parts.append(f"摘要：{parsed['summary']}")
    abn = parsed.get("key_abnormalities") or []
    if abn:
        parts.append("关键异常：" + "；".join([str(x) for x in abn]))
    items = parsed.get("items") or []
    if items:
        item_lines = []
        for it in items[:30]:
            name = it.get("name", "")
            value = it.get("value", "")
            unit = it.get("unit") or ""
            flag = it.get("flag") or ""
            ref = it.get("ref_range") or ""
            line = f"{name}: {value}{unit}"
            if ref:
                line += f"（参考{ref}）"
            if flag:
                line += f" [{flag}]"
            item_lines.append(line)
        parts.append("化验/检查项：" + "；".join(item_lines))
    if parsed.get("findings"):
        parts.append(f"影像所见：{parsed['findings']}")
    if parsed.get("impression"):
        parts.append(f"印象：{parsed['impression']}")
    rec = parsed.get("recommendations") or []
    if rec:
        parts.append("建议：" + "；".join([str(x) for x in rec]))
    if parsed.get("suggested_department"):
        parts.append(f"建议科室：{parsed['suggested_department']}")
    return "\n".join(parts)
