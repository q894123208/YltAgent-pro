from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass
class Chunk:
    text: str
    section: str = ""
    index: int = 0


def split_markdown_sections(text: str) -> List[tuple[str, str]]:
    """按二/三级标题切节；返回 [(section_title, body), ...]。"""
    if not text.strip():
        return []
    pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return [("", text.strip())]
    sections: List[tuple[str, str]] = []
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        title = m.group(2).strip()
        body = text[start:end].strip()
        if body:
            sections.append((title, body))
    return sections


def chunk_text(
    text: str,
    chunk_size: int = 400,
    overlap: int = 60,
    section: str = "",
) -> List[Chunk]:
    """对长文本做字符级滑窗切分，对中文友好。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [Chunk(text=text, section=section, index=0)]

    chunks: List[Chunk] = []
    step = max(chunk_size - overlap, 50)
    idx = 0
    for start in range(0, len(text), step):
        piece = text[start : start + chunk_size]
        if not piece.strip():
            continue
        chunks.append(Chunk(text=piece.strip(), section=section, index=idx))
        idx += 1
        if start + chunk_size >= len(text):
            break
    return chunks


def chunk_markdown(text: str, chunk_size: int = 400, overlap: int = 60) -> List[Chunk]:
    """先按标题切段，再对每段做滑窗。每个 chunk 前面会带上 section 标题。"""
    chunks: List[Chunk] = []
    sections = split_markdown_sections(text)
    global_index = 0
    for title, body in sections:
        sub = chunk_text(body, chunk_size=chunk_size, overlap=overlap, section=title)
        for c in sub:
            prefixed = f"【{title}】\n{c.text}" if title else c.text
            chunks.append(Chunk(text=prefixed, section=title, index=global_index))
            global_index += 1
    return chunks
