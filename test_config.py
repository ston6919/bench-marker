#!/usr/bin/env python3
"""Tests for portable, environment-driven configuration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


class PortableConfigTests(unittest.TestCase):
    def test_data_directory_and_server_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["BENCH_MARKER_DATA_DIR"] = tmp
            env.pop("BENCH_MARKER_DB", None)
            env["HOST"] = "127.0.0.2"
            env["PORT"] = "9876"
            code = """
import json
import db
import html_image_render
import remotion_render
import server
import strudel_render

print(json.dumps({
    "db": str(db.DB_PATH),
    "html": str(html_image_render.RENDERS_DIR),
    "remotion": str(remotion_render.RENDERS_DIR),
    "strudel": str(strudel_render.RENDERS_DIR),
    "server_data": str(server.DATA_DIR),
    "host": server.HOST,
    "port": server.PORT,
}))
"""
            proc = subprocess.run(
                [sys.executable, "-c", code],
                cwd=BASE_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            config = json.loads(proc.stdout)
            expected_data = str(Path(tmp))
            expected_renders = str(Path(tmp) / "renders")
            self.assertEqual(config["db"], str(Path(tmp) / "bench.db"))
            self.assertEqual(config["html"], expected_renders)
            self.assertEqual(config["remotion"], expected_renders)
            self.assertEqual(config["strudel"], expected_renders)
            self.assertEqual(config["server_data"], expected_data)
            self.assertEqual(config["host"], "127.0.0.2")
            self.assertEqual(config["port"], 9876)


if __name__ == "__main__":
    unittest.main()
