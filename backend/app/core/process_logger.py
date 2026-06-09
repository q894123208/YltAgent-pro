from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def setup_process_logging() -> None:
    """配置控制台 + 文件日志，记录用户请求的关键处理步骤。"""
    log_dir = Path(__file__).resolve().parents[3] / "storage" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "").endswith("process.log") for h in root.handlers):
        file_handler = RotatingFileHandler(
            log_dir / "process.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def log_step(step: str, **fields: Any) -> None:
    """统一记录业务流程日志，便于从控制台和 process.log 追踪链路。"""
    details = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logging.getLogger("medical.process").info("%s | %s", step, details)
