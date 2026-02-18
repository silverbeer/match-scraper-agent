"""Structured logging configuration for match-scraper-agent."""

from __future__ import annotations

import logging

import structlog

LOG_LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def configure_logging(*, json_output: bool = False, log_level: str = "info") -> None:
    """Configure structlog for match-scraper-agent.

    Args:
        json_output: If True, output JSON lines. If False, pretty console output.
        log_level: Minimum log level (debug, info, warning, error).
    """
    level = LOG_LEVEL_MAP.get(log_level.lower(), logging.INFO)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if json_output:
        # JSONRenderer needs format_exc_info to serialize tracebacks
        processors.append(structlog.processors.format_exc_info)
        processors.append(structlog.processors.JSONRenderer())
    else:
        # ConsoleRenderer handles exc_info natively — adding format_exc_info
        # before it causes a duplicate-rendering warning
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
