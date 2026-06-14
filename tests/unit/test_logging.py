import logging

import pytest

from lib.logging_utils import setup_logger


@pytest.fixture(autouse=True)
def reset_logging():
    import lib.logging_utils
    lib.logging_utils._configured = False
    logging.root.handlers.clear()
    logging.root.setLevel(logging.WARNING)
    yield
    lib.logging_utils._configured = False
    logging.root.handlers.clear()


class TestSetupLogger:
    def test_returns_logger_with_given_name(self):
        log = setup_logger("my_module")
        assert log.name == "my_module"

    def test_second_call_preserves_level(self):
        log1 = setup_logger("a", level=logging.DEBUG)
        log2 = setup_logger("b")
        assert logging.root.level == logging.DEBUG
        assert log2.level == logging.DEBUG

    def test_subsequent_call_can_change_level(self):
        setup_logger("a", level=logging.DEBUG)
        setup_logger("b", level=logging.INFO)
        assert logging.root.level == logging.INFO
