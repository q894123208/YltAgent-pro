from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import quote
from typing import Any, AsyncIterator, Dict, List

import httpx

from app.core.config import SETTINGS
from app.core.process_logger import log_step

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self):
        self.config = SETTINGS["llm"]
        self.enabled = bool(SETTINGS["features"].get("enable_llm")) and bool(self.config.get("api_key"))
        self.api_type = str(self.config.get("api_type", "chat_completions")).lower()
        if self.api_type == "auto":
            # DMXAPI 对不同模型统一提供 OpenAI 兼容 chat/completions。
            # 不按模型名推断能力，避免 Gemini/GPT/Qwen 等多模态模型被错误分流。
            self.api_type = "chat_completions"

    def _headers(self) -> Dict[str, str]:
        api_key = str(self.config["api_key"])
        auth_scheme = str(self.config.get("auth_scheme", "auto")).lower()
        if auth_scheme == "bearer":
            authorization = f"Bearer {api_key}"
        elif auth_scheme == "raw" or "dmxapi.cn" in str(self.config.get("base_url", "")):
            # DMXAPI 的 chat/completions 示例使用裸 key：Authorization: sk-...
            authorization = api_key
        else:
            authorization = f"Bearer {api_key}"
        return {
            "Authorization": authorization,
            "Content-Type": "application/json",
        }

    def _responses_payload(
        self,
        messages: List[Dict[str, str]],
        stream: bool,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Dict[str, Any]:
        system_parts = [m.get("content", "") for m in messages if m.get("role") == "system" and m.get("content")]
        input_messages = [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in messages
            if m.get("role") != "system"
        ]
        payload: Dict[str, Any] = {
            "model": self.config["model_name"],
            "input": input_messages if input_messages else "",
            "stream": stream,
        }
        if system_parts:
            payload["instructions"] = "\n\n".join(system_parts)
        max_output_tokens = max_tokens or int(self.config.get("max_tokens", 1200))
        if max_output_tokens:
            payload["max_output_tokens"] = max_output_tokens
        if temperature is not None:
            payload["temperature"] = float(temperature)
        elif self.config.get("temperature") is not None:
            payload["temperature"] = float(self.config.get("temperature", 0.3))
        if self.config.get("reasoning_effort"):
            payload["reasoning"] = {"effort": self.config.get("reasoning_effort")}
        return payload

    @staticmethod
    def _extract_responses_text(data: Dict[str, Any]) -> str:
        if data.get("output_text"):
            return str(data["output_text"])
        parts: List[str] = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                if content.get("type") in {"output_text", "text"}:
                    parts.append(content.get("text", ""))
        return "".join(parts)

    async def chat(self, messages: List[Dict[str, str]], timeout: float = 60.0) -> str:
        if not self.enabled:
            raise RuntimeError("LLM is disabled or api_key is empty")
        log_step("model.chat.request", model=self.config.get("model_name"), api_type=self.api_type, messages=len(messages))
        last_error: Exception | None = None
        max_retries = max(1, min(int(self.config.get("max_retries", 1)), 3))
        for attempt in range(max_retries):
            try:
                if self.api_type == "gemini":
                    try:
                        return await self._gemini_once(messages, timeout=timeout)
                    except Exception:
                        logger.warning("Gemini native once failed, trying chat/completions fallback", exc_info=True)
                        return await self._chat_completions_once(messages, timeout=timeout)
                if self.api_type == "responses":
                    try:
                        return await self._responses_once(messages, timeout=timeout)
                    except Exception:
                        logger.warning("Responses once failed, trying chat/completions fallback", exc_info=True)
                        return await self._chat_completions_once(messages, timeout=timeout)
                return await self._chat_completions_once(messages, timeout=timeout)
            except Exception as exc:
                last_error = exc
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(0.8 + attempt * 0.8)
        if last_error:
            raise last_error
        return ""

    async def _responses_once(self, messages: List[Dict[str, str]], timeout: float = 60.0) -> str:
        url = self.config["base_url"].rstrip("/") + "/responses"
        payload = self._responses_payload(messages, stream=False)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30.0, read=timeout, write=20.0, pool=10.0),
            trust_env=False,
        ) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        return self._extract_responses_text(data)

    async def _chat_completions_once(self, messages: List[Dict[str, str]], timeout: float = 60.0) -> str:
        url = self.config["base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config["model_name"],
            "messages": messages,
            "temperature": float(self.config.get("temperature", 0.3)),
            "max_tokens": int(self.config.get("max_tokens", 1200)),
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30.0, read=timeout, write=20.0, pool=10.0),
            trust_env=False,
        ) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"] or ""

    def _gemini_payload(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        system_text = "\n\n".join([m.get("content", "") for m in messages if m.get("role") == "system"])
        contents = []
        for msg in messages:
            if msg.get("role") == "system":
                continue
            role = "model" if msg.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})
        payload: Dict[str, Any] = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
        if system_text:
            payload["system_instruction"] = {"parts": [{"text": system_text}]}
        return payload

    async def _gemini_once(self, messages: List[Dict[str, str]], timeout: float = 60.0) -> str:
        chunks: List[str] = []
        async for delta in self._gemini_stream(messages, timeout=timeout):
            chunks.append(delta)
        return "".join(chunks)

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        timeout: float = 120.0,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """流式输出，每个 chunk 是新增的字符串片段。"""
        if not self.enabled:
            raise RuntimeError("LLM is disabled or api_key is empty")
        log_step("model.chat_stream.begin", model=self.config.get("model_name"), api_type=self.api_type, messages=len(messages))
        if self.api_type == "gemini":
            async for delta in self._stream_with_fallback(
                self._gemini_stream(messages, timeout, max_tokens, temperature),
                self._chat_completions_stream(messages, timeout, max_tokens, temperature),
            ):
                yield delta
            return
        if self.api_type == "responses":
            async for delta in self._stream_with_fallback(
                self._responses_stream(messages, timeout, max_tokens, temperature),
                self._chat_completions_stream(messages, timeout, max_tokens, temperature),
            ):
                yield delta
            return
        async for delta in self._chat_completions_stream(messages, timeout, max_tokens, temperature):
            yield delta

    async def _stream_with_fallback(
        self,
        primary: AsyncIterator[str],
        fallback: AsyncIterator[str],
    ) -> AsyncIterator[str]:
        started = False
        try:
            async for delta in primary:
                started = True
                yield delta
            return
        except Exception:
            if started:
                raise
            logger.warning("Primary LLM stream failed before first chunk, trying fallback stream", exc_info=True)
        async for delta in fallback:
            yield delta

    async def _gemini_stream(
        self,
        messages: List[Dict[str, str]],
        timeout: float,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        model = quote(str(self.config["model_name"]), safe="")
        base = re.sub(r"/v1/?$", "", self.config["base_url"].rstrip("/"))
        url = f"{base}/v1beta/models/{model}:streamGenerateContent?key={self.config['api_key']}&alt=sse"
        payload = self._gemini_payload(messages)
        generation_config: Dict[str, Any] = {}
        if max_tokens:
            generation_config["maxOutputTokens"] = max_tokens
        if temperature is not None:
            generation_config["temperature"] = float(temperature)
        elif self.config.get("temperature") is not None:
            generation_config["temperature"] = float(self.config.get("temperature", 0.3))
        if generation_config:
            payload["generationConfig"] = generation_config
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30.0, read=timeout, write=20.0, pool=10.0),
            trust_env=False,
        ) as client:
            async with client.stream("POST", url, headers={"Content-Type": "application/json; charset=utf-8"}, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = (line or "").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for part in (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []):
                        text = part.get("text") or ""
                        if text:
                            yield text

    async def _responses_stream(
        self,
        messages: List[Dict[str, str]],
        timeout: float,
        max_tokens: int | None,
        temperature: float | None,
    ) -> AsyncIterator[str]:
        url = self.config["base_url"].rstrip("/") + "/responses"
        payload = self._responses_payload(messages, stream=True, max_tokens=max_tokens, temperature=temperature)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30.0, read=timeout, write=20.0, pool=10.0),
            trust_env=False,
        ) as client:
            async with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = (line or "").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta") or ""
                    elif event_type in {"response.refusal.delta", "response.output_text.annotation.added"}:
                        delta = ""
                    else:
                        delta = ""
                    if delta:
                        yield delta

    async def _chat_completions_stream(
        self,
        messages: List[Dict[str, str]],
        timeout: float,
        max_tokens: int | None,
        temperature: float | None,
    ) -> AsyncIterator[str]:
        temp = float(self.config.get("temperature", 0.3)) if temperature is None else float(temperature)
        max_tok = int(self.config.get("max_tokens", 1200)) if max_tokens is None else int(max_tokens)
        url = self.config["base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config["model_name"],
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            "stream": True,
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=30.0, read=None, write=30.0, pool=10.0),
            trust_env=False,
        ) as client:
            async with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                buffer = ""
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_line = line[5:].strip()
                        if data_line == "[DONE]":
                            return
                        try:
                            data = json.loads(data_line)
                        except json.JSONDecodeError:
                            buffer = line + "\n" + buffer
                            break
                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        delta = (choices[0].get("delta") or {}).get("content") or ""
                        if delta:
                            yield delta
