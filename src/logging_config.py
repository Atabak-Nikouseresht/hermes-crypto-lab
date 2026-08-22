from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(log_dir: Path, level: str = "INFO") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "data_pipeline.log"
    formatter = logging.Formatter(
        "%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = __import__("time").gmtime

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for handler in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    return log_path
