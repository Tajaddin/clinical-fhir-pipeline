"""JSON logs on stdout with request ids bound through contextvars."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", service: str = "cfp") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    # Third-party request logs are noise at INFO; errors still come through.
    for noisy in (
        "httpx",
        "httpcore",
        "opensearch",
        "urllib3",
        "huggingface_hub",
        "sentence_transformers",
        "transformers",
        "filelock",
        "uvicorn.access",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
