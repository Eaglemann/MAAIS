import logging

from maais.core.logging import configure_logging


def test_http_transport_success_logs_are_suppressed() -> None:
    configure_logging(log_level="INFO", is_production=True)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
