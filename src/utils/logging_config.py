"""
Structured JSON logging for the BetterWiser briefing agent.
Each run gets its own log file at runs/{run_id}/run.log.
All modules call get_logger(__name__, run_id) at module level.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def __init__(self, run_id: Optional[str] = None) -> None:
        super().__init__()
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.run_id:
            obj["run_id"] = self.run_id
        if record.exc_info:
            obj["exc_info"] = self.formatException(record.exc_info)
        # Attach any extra fields passed via extra={}
        for key, value in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            ):
                try:
                    json.dumps(value)  # only include JSON-serialisable extras
                    obj[key] = value
                except (TypeError, ValueError):
                    pass
        return json.dumps(obj, ensure_ascii=False)


def setup_logging(run_id: str, runs_dir: str = "runs", log_level: str = "INFO") -> None:
    """
    Configure root logger with:
    - A file handler writing JSON to runs/{run_id}/run.log
    - A stderr handler (also JSON) for console visibility

    Call once from orchestrator at pipeline start.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers (avoid duplicates on re-runs in tests)
    root.handlers.clear()

    formatter = JSONFormatter(run_id=run_id)

    # File handler
    log_dir = Path(runs_dir) / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    # Stderr handler
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(level)
    root.addHandler(stderr_handler)

    root.info("Logging initialised", extra={"log_dir": str(log_dir)})


def get_logger(name: str, run_id: Optional[str] = None) -> logging.Logger:
    """
    Return a named logger. If run_id is provided, a JSONFormatter with the
    run_id is attached as an extra field on all messages from this logger.
    Typically called at module level:

        logger = get_logger(__name__, run_id)
    """
    logger = logging.getLogger(name)
    if run_id and not logger.handlers:
        # Add a temporary stderr handler so pre-setup logging still works
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JSONFormatter(run_id=run_id))
        logger.addHandler(handler)
    return logger


# Module-level default logger (no run_id until setup_logging is called)
logger = logging.getLogger(__name__)
