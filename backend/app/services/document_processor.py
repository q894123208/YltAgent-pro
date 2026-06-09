from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
PDF_EXTS = {".pdf"}


@dataclass
class ImagePayload:
    """送入 VLM 的单张图片。"""
    data_url: str          # data:image/png;base64,xxxx
    page_index: int        # 0 起；非 PDF 固定为 0
    mime: str = "image/png"
    width: int = 0
    height: int = 0


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS | PDF_EXTS


def encode_image_file(path: Path, max_side: int = 1280) -> ImagePayload:
    """读取图片文件、可选缩放、转为 data URL。"""
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, max_side / float(max(w, h)))
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return ImagePayload(
            data_url=f"data:image/png;base64,{b64}",
            page_index=0,
            mime="image/png",
            width=img.size[0],
            height=img.size[1],
        )


def render_pdf_to_images(path: Path, dpi: int = 150, max_side: int = 1280, max_pages: int = 6) -> List[ImagePayload]:
    """扫描型 PDF 直接渲染为图片页，交给远程 VLM 解析。"""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少依赖 PyMuPDF，请 pip install pymupdf") from exc

    from PIL import Image

    images: List[ImagePayload] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(str(path)) as doc:
        for page_idx, page in enumerate(doc):
            if page_idx >= max_pages:
                break
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            w, h = img.size
            scale = min(1.0, max_side / float(max(w, h)))
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            images.append(
                ImagePayload(
                    data_url=f"data:image/png;base64,{b64}",
                    page_index=page_idx,
                    mime="image/png",
                    width=img.size[0],
                    height=img.size[1],
                )
            )
    return images


def file_to_images(path: Path) -> List[ImagePayload]:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTS:
        return [encode_image_file(path)]
    if suffix in PDF_EXTS:
        return render_pdf_to_images(path)
    raise ValueError(f"不支持的文件类型: {suffix}")
