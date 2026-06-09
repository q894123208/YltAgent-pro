"""扫描 data/knowledge_base/ 下的所有支持的文件并入库到 Chroma。

支持类型：
  .md / .markdown / .txt   → 文本切片
  .pdf                     → 自动判定文本型/扫描型
  .png .jpg .jpeg .webp .bmp → VLM 描述后入库

用法（在 backend 目录）：
    python scripts/build_kb.py            # 增量同步
    python scripts/build_kb.py --reset    # 清空 kb collection 后重建
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import SETTINGS  # noqa: E402
from app.services.chroma_rag_service import get_chroma_service  # noqa: E402
from app.services.kb_ingestor import ALL_SUPPORTED, ingest_file  # noqa: E402


async def run(reset: bool) -> None:
    svc = get_chroma_service()
    if not svc.available:
        print("[ERR] Chroma 不可用，请先 pip install chromadb")
        return

    if reset:
        try:
            svc.client.delete_collection(svc.kb_name)
        except Exception:
            pass
        svc.kb_collection = svc.client.get_or_create_collection(
            name=svc.kb_name, metadata={"hnsw:space": "cosine"}
        )
        print(f"[INFO] 已重置 collection: {svc.kb_name}")

    kb_dirs = [Path(SETTINGS["rag"]["knowledge_dir"])]
    triage_kb_dir = SETTINGS.get("rag", {}).get("triage_kb_dir")
    if triage_kb_dir:
        kb_dirs.append(Path(triage_kb_dir))
    files = sorted(
        [
            p
            for kb_dir in kb_dirs
            if kb_dir.exists()
            for p in kb_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in ALL_SUPPORTED
        ]
    )
    print(f"[INFO] 发现 {len(files)} 个文件，开始处理 ...")

    total = 0
    by_type: dict[str, int] = {}
    for path in files:
        try:
            info = await ingest_file(svc, path)
            total += info["chunks"]
            by_type[info["type"]] = by_type.get(info["type"], 0) + 1
            print(f"  + [{info['type']:>10}] {path.name}  -> {info['chunks']} chunks")
        except Exception as exc:
            print(f"  ! {path.name} 失败: {exc}")

    stats = svc.stats()
    print(f"\n[OK] 总写入 {total} chunks；当前 collection 数量：{stats.get('kb_count')}")
    print(f"[OK] 按类型统计：{by_type}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="清空 kb collection 后重建")
    args = parser.parse_args()
    asyncio.run(run(args.reset))


if __name__ == "__main__":
    main()
