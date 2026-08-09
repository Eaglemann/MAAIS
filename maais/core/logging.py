import logging
import sys

import structlog

from maais.observability.events import (
    add_event_schema_version,
    enforce_event_contract,
    normalize_exception,
    remove_processor_metadata,
    render_console_exception,
)
from maais.observability.redaction import redact_event


def configure_logging(log_level: str = "INFO", is_production: bool = False) -> None:
    """Configure structlog for the MAAIS system.

    Development: human-readable console output.
    Production: JSON lines for log aggregation.
    """
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.ExtraAdder(),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        normalize_exception,
        redact_event,
        enforce_event_contract,
        add_event_schema_version,
    ]

    if is_production:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
        formatter_processors = [remove_processor_metadata, renderer]
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=sys.stdout.isatty(),
            exception_formatter=structlog.dev.plain_traceback,
        )
        formatter_processors = [
            remove_processor_metadata,
            render_console_exception,
            renderer,
        ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=formatter_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Successful public-market REST polls are routine and extremely frequent.
    # Keep transport failures visible without flooding a multi-day local run.
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
