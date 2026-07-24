from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class DesktopEngineTests(unittest.TestCase):
    def test_engine_announces_connection_and_stops_through_authenticated_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            ready_file = data_dir / "engine-ready.json"
            environment = os.environ.copy()
            environment["LOGPILOT_DATA_DIR"] = str(data_dir)
            environment["PYTHONPATH"] = str(SRC)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "logpilot.desktop_engine",
                    "--port",
                    "0",
                    "--token",
                    "integration-token",
                    "--ready-file",
                    str(ready_file),
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not ready_file.exists():
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertTrue(ready_file.exists(), process.stderr.read() if process.poll() is not None else "")
                connection = json.loads(ready_file.read_text(encoding="utf-8"))
                self.assertEqual(connection["apiVersion"], 1)

                metadata_request = urllib.request.Request(
                    f"{connection['baseUrl']}/api/meta",
                    headers={"X-LogPilot-Token": "integration-token"},
                )
                with urllib.request.urlopen(metadata_request, timeout=10) as response:
                    metadata = json.loads(response.read().decode("utf-8"))
                self.assertEqual(metadata["name"], "LogPilot Engine")

                shutdown_request = urllib.request.Request(
                    f"{connection['baseUrl']}/api/shutdown",
                    data=b"",
                    headers={"X-LogPilot-Token": "integration-token"},
                    method="POST",
                )
                with urllib.request.urlopen(shutdown_request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertFalse(ready_file.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
