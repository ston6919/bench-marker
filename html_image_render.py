"""Render model-generated square HTML graphics to PNG via headless Chrome."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get("BENCH_MARKER_DATA_DIR", str(BASE_DIR / "data"))
).expanduser()
RENDERS_DIR = DATA_DIR / "renders"
WORK_DIR = DATA_DIR / "work-images"

# Reuse Remotion's chrome-headless-shell (already on this VPS).
BUNDLED_BROWSER = (
    BASE_DIR
    / "remotion-template/node_modules/.remotion/chrome-headless-shell/linux64"
    / "chrome-headless-shell-linux64/chrome-headless-shell"
)

SIZE = int(os.environ.get("SQUARE_IMAGE_SIZE", "1080"))
RENDER_LOCK = threading.Lock()
RENDER_TIMEOUT_SEC = int(os.environ.get("SQUARE_IMAGE_TIMEOUT", "60"))

_BROWSER_CANDIDATES = [
    os.environ.get("REMOTION_BROWSER_EXECUTABLE"),
    os.environ.get("CHROME_BIN"),
    str(BUNDLED_BROWSER),
]


def browser_bin() -> str | None:
    for p in _BROWSER_CANDIDATES:
        if p and Path(p).exists() and "/snap/" not in p:
            return p
    return None


def extract_html(text: str) -> str:
    """Normalize model output into a full HTML document (reuse same patterns as HTML bench)."""
    import db as dbmod

    return dbmod.extract_html_document(text or "")


def force_square_document(html: str, size: int = SIZE) -> str:
    """Inject CSS so the document is exactly size×size with no scrollbars."""
    html = (html or "").strip()
    if not html:
        raise ValueError("Empty HTML from model")

    inject = f"""
<style id="bench-square-force">
  html, body {{
    margin: 0 !important;
    padding: 0 !important;
    width: {size}px !important;
    height: {size}px !important;
    overflow: hidden !important;
    background: #ffffff;
  }}
  body > * {{
    box-sizing: border-box;
  }}
</style>
<meta name="viewport" content="width={size}, height={size}, initial-scale=1" />
""".strip()

    lower = html.lower()
    if "</head>" in lower:
        # case-insensitive replace of first </head>
        idx = lower.index("</head>")
        html = html[:idx] + inject + "\n" + html[idx:]
    elif "<body" in lower:
        idx = lower.index("<body")
        # insert before body
        html = html[:idx] + f"<head>{inject}</head>\n" + html[idx:]
    else:
        html = (
            f"<!DOCTYPE html><html><head><meta charset=\"utf-8\"/>{inject}</head>"
            f"<body>{html}</body></html>"
        )

    if not re.search(r"<!doctype", html, re.I):
        html = "<!DOCTYPE html>\n" + html
    return html


def image_path_for_run(run_id: int) -> Path:
    return RENDERS_DIR / f"run_{run_id}.png"


def render_run(run_id: int, raw_html: str, size: int = SIZE) -> dict[str, Any]:
    """
    Write HTML and screenshot to PNG.
    Returns {ok, image_path, html, error, log}.
    """
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out_path = image_path_for_run(run_id)
    log_path = RENDERS_DIR / f"run_{run_id}.img.log"

    chrome = browser_bin()
    if not chrome:
        return {
            "ok": False,
            "image_path": None,
            "html": raw_html,
            "error": "Headless Chrome not found for image render",
            "log": "",
        }

    try:
        cleaned = force_square_document(extract_html(raw_html), size=size)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "image_path": None,
            "html": raw_html,
            "error": f"HTML normalize failed: {exc}",
            "log": "",
        }

    with RENDER_LOCK:
        work = WORK_DIR / f"run_{run_id}"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        html_file = work / "index.html"
        html_file.write_text(cleaned, encoding="utf-8")
        file_url = html_file.resolve().as_uri()

        # Chrome writes screenshot next to CWD if relative; use absolute path.
        cmd = [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size={size},{size}",
            f"--screenshot={out_path}",
            file_url,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(work),
                capture_output=True,
                text=True,
                timeout=RENDER_TIMEOUT_SEC,
                check=False,
            )
            log = (proc.stdout or "") + "\n" + (proc.stderr or "")
            log_path.write_text(log[-50_000:], encoding="utf-8")

            # Some chrome builds write screenshot.png in cwd instead
            if not out_path.exists():
                alt = work / "screenshot.png"
                if alt.exists():
                    shutil.move(str(alt), str(out_path))

            if not out_path.exists() or out_path.stat().st_size < 100:
                return {
                    "ok": False,
                    "image_path": None,
                    "html": cleaned,
                    "error": f"Screenshot failed (exit {proc.returncode}): {log[-1200:]}",
                    "log": log[-20_000:],
                }
            return {
                "ok": True,
                "image_path": str(out_path),
                "html": cleaned,
                "error": None,
                "log": log[-10_000:],
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "image_path": None,
                "html": cleaned,
                "error": f"Image render timed out after {RENDER_TIMEOUT_SEC}s",
                "log": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "image_path": None,
                "html": cleaned,
                "error": f"Image render error: {exc}",
                "log": "",
            }
        finally:
            # Keep last few work dirs for debugging
            try:
                dirs = sorted(
                    [p for p in WORK_DIR.iterdir() if p.is_dir()],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for old in dirs[8:]:
                    shutil.rmtree(old, ignore_errors=True)
            except Exception:
                pass
