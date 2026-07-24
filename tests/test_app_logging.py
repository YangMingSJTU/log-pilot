from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from logpilot.app_logging import configure_logging, log_directory, shutdown_logging
from logpilot.storage import DATA_DIR_ENV


class AppLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_tmp = tempfile.TemporaryDirectory()
        self.previous_data_dir = os.environ.get(DATA_DIR_ENV)
        os.environ[DATA_DIR_ENV] = self.data_tmp.name
        shutdown_logging()

    def tearDown(self) -> None:
        shutdown_logging()
        if self.previous_data_dir is None:
            os.environ.pop(DATA_DIR_ENV, None)
        else:
            os.environ[DATA_DIR_ENV] = self.previous_data_dir
        self.data_tmp.cleanup()

    def test_configure_logging_writes_rotating_file_in_user_data_directory(self) -> None:
        path = configure_logging("test-component", level=logging.DEBUG)
        logging.getLogger("logpilot.test").info("diagnostic_event value=%s", 42)
        for handler in logging.getLogger("logpilot").handlers:
            handler.flush()

        self.assertEqual(path, log_directory() / "logpilot-test-component.log")
        self.assertTrue(path.is_file())
        content = path.read_text(encoding="utf-8")
        self.assertIn("diagnostic_event value=42", content)
        self.assertIn("logpilot.test", content)
        handlers = [
            handler
            for handler in logging.getLogger("logpilot").handlers
            if isinstance(handler, RotatingFileHandler)
        ]
        self.assertEqual(len(handlers), 1)
        self.assertGreater(handlers[0].maxBytes, 0)
        self.assertGreater(handlers[0].backupCount, 0)

    def test_repeated_configuration_does_not_duplicate_handlers(self) -> None:
        first = configure_logging("desktop-engine")
        second = configure_logging("desktop-engine")

        self.assertEqual(first, second)
        handlers = [
            handler
            for handler in logging.getLogger("logpilot").handlers
            if getattr(handler, "_logpilot_handler", False)
        ]
        self.assertEqual(len(handlers), 1)

    def test_log_file_rotates_at_the_configured_size(self) -> None:
        with patch("logpilot.app_logging.LOG_MAX_BYTES", 256), patch(
            "logpilot.app_logging.LOG_BACKUP_COUNT", 2
        ):
            path = configure_logging("rotation", level=logging.DEBUG)
            logger = logging.getLogger("logpilot.rotation")
            for index in range(10):
                logger.info("rotation_event index=%s payload=%s", index, "x" * 80)
            for handler in logging.getLogger("logpilot").handlers:
                handler.flush()

        self.assertTrue(path.is_file())
        self.assertTrue(Path(f"{path}.1").is_file())


if __name__ == "__main__":
    unittest.main()
