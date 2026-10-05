"""structlog JSON logging with secret redaction and a runtime DEBUG toggle."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import structlog

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|authorization|cookie|api[_-]?key|bearer|credential)", re.I
)
_SECRET_VALUE = re.compile(
    r"(?:\b(?:authorization|cookie|set-cookie)\s*[:=]\s*)?"
    r"(?:\b(?:bearer|basic|digest)\s+[A-Za-z0-9._~+/=-]{8,})"
    r"|\b(?:authorization|cookie|set-cookie)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)

MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
_ROOT = "pagewatch"


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_VALUE.sub(REDACTED, value)
    if isinstance(value, dict):
        return redact(value)
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


def redact(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and _SECRET_KEY.search(key):
            out[key] = REDACTED
        else:
            out[key] = _redact_value(value)
    return out


def _redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    redacted = redact(dict(event_dict))
    event_dict.clear()
    event_dict.update(redacted)
    return event_dict


def configure_logging(log_dir: Path | None, *, debug: bool = False, console: bool = True) -> None:
    """Idempotent: replaces any handlers installed by an earlier call."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor,
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_pagewatch", False):
            root.removeHandler(h)
            h.close()
    handlers: list[logging.Handler] = []
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_dir / "engine.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
            )
        )
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(formatter)
        h._pagewatch = True  # type: ignore[attr-defined]
        root.addHandler(h)
    set_debug(debug)
    # Third-party chatter stays at WARNING even in debug mode.
    for noisy in ("httpx", "httpcore", "asyncio", "multipart", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def set_debug(enabled: bool) -> None:
    logging.getLogger().setLevel(logging.DEBUG if enabled else logging.INFO)


def get_logger(name: str = _ROOT) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
