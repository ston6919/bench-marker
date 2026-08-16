"""Render model-generated Remotion compositions to MP4."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "remotion-template"
DATA_DIR = Path(
    os.environ.get("BENCH_MARKER_DATA_DIR", str(BASE_DIR / "data"))
).expanduser()
RENDERS_DIR = DATA_DIR / "renders"
WORK_DIR = DATA_DIR / "work"

RENDER_LOCK = threading.Lock()
RENDER_TIMEOUT_SEC = int(os.environ.get("REMOTION_RENDER_TIMEOUT", "300"))

BUNDLED_BROWSER = (
    TEMPLATE_DIR
    / "node_modules/.remotion/chrome-headless-shell/linux64"
    / "chrome-headless-shell-linux64/chrome-headless-shell"
)

# systemd services often lack ~/.local/bin — resolve absolute paths.
_NODE_CANDIDATES = [
    os.environ.get("NODE_BIN"),
    str(Path.home() / ".local/bin/node"),
    str(Path.home() / ".hermes/node/bin/node"),
    shutil.which("node"),
]
_NPX_CANDIDATES = [
    os.environ.get("NPX_BIN"),
    str(Path.home() / ".local/bin/npx"),
    str(Path.home() / ".hermes/node/bin/npx"),
    shutil.which("npx"),
]


def _first_existing(paths: list[str | None]) -> str | None:
    for p in paths:
        if p and Path(p).exists():
            return p
    return None


NODE_BIN = _first_existing(_NODE_CANDIDATES)
NPX_BIN = _first_existing(_NPX_CANDIDATES)
NODE_BIN_DIR = str(Path(NODE_BIN).parent) if NODE_BIN else ""

DEFAULT_CONSTANTS = """
export const FPS = 30;
export const WIDTH = 960;
export const HEIGHT = 540;
export const DURATION_IN_FRAMES = 90;
""".strip()


def extract_remotion_code(text: str) -> str:
    """Pull TSX source out of model output (fences / prose)."""
    if not text:
        return ""

    cleaned = text.strip()

    # Full-file fence
    fence = re.match(
        r"^```(?:tsx|typescript|ts|jsx|javascript|js)?\s*\n([\s\S]*?)\n?```\s*$",
        cleaned,
        re.IGNORECASE,
    )
    if fence:
        cleaned = fence.group(1).strip()
    else:
        # First fenced block if present
        m = re.search(
            r"```(?:tsx|typescript|ts|jsx|javascript|js)?\s*\n([\s\S]*?)\n?```",
            cleaned,
            re.IGNORECASE,
        )
        if m:
            cleaned = m.group(1).strip()
        else:
            cleaned = re.sub(r"^```(?:tsx|typescript|ts|jsx)?\s*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()

    # Drop leading prose before imports/exports
    lines = cleaned.splitlines()
    start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if (
            s.startswith("import ")
            or s.startswith("export ")
            or s.startswith("const ")
            or s.startswith("function ")
            or s.startswith("/*")
            or s.startswith("//")
            or s.startswith("'use ")
            or s.startswith('"use ')
        ):
            start = i
            break
    cleaned = "\n".join(lines[start:]).strip()
    return cleaned


def ensure_required_exports(code: str) -> str:
    """Guarantee MotionGraphic + composition constants exist."""
    code = (code or "").strip()
    if not code:
        raise ValueError("Model returned empty Remotion code")

    # Normalize alternate component names
    if "export const MotionGraphic" not in code and "export function MotionGraphic" not in code:
        for alt in (
            "export const MyComposition",
            "export const Composition",
            "export const App",
            "export default function",
            "export default",
        ):
            if alt in code:
                break
        # Try rename common patterns
        code2 = code
        code2 = re.sub(
            r"export\s+default\s+function\s+(\w+)",
            r"export const MotionGraphic: React.FC = function \1",
            code2,
            count=1,
        )
        code2 = re.sub(
            r"export\s+default\s+(\w+)\s*;",
            r"export const MotionGraphic = \1;",
            code2,
            count=1,
        )
        code2 = re.sub(
            r"export\s+const\s+(MyComposition|Composition|App|Main)\s*[:=]",
            "export const MotionGraphic:",
            code2,
            count=1,
        )
        code = code2

    if "export const MotionGraphic" not in code and "export function MotionGraphic" not in code:
        # Last resort: wrap entire file as body (unlikely to work but surfaces clearer error)
        raise ValueError(
            "Remotion code must export `MotionGraphic` "
            "(export const MotionGraphic: React.FC = ...)"
        )

    missing = []
    for name in ("FPS", "WIDTH", "HEIGHT", "DURATION_IN_FRAMES"):
        if not re.search(rf"export\s+const\s+{name}\b", code):
            missing.append(name)
    if missing:
        code = DEFAULT_CONSTANTS + "\n\n" + code

    # Ensure React import if JSX present and missing
    if "from \"react\"" not in code and "from 'react'" not in code:
        code = 'import React from "react";\n' + code

    return code


def prepare_work_dir(run_id: int, code: str) -> Path:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    work = WORK_DIR / f"run_{run_id}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / "src").mkdir()

    for name in ("package.json", "tsconfig.json", "remotion.config.ts"):
        shutil.copy2(TEMPLATE_DIR / name, work / name)

    shutil.copy2(TEMPLATE_DIR / "src" / "index.ts", work / "src" / "index.ts")
    shutil.copy2(TEMPLATE_DIR / "src" / "Root.tsx", work / "src" / "Root.tsx")

    nm = work / "node_modules"
    if not nm.exists():
        nm.symlink_to(TEMPLATE_DIR / "node_modules", target_is_directory=True)

    (work / "src" / "MotionGraphic.tsx").write_text(code + "\n", encoding="utf-8")
    return work


def render_run(run_id: int, code: str) -> dict[str, Any]:
    """
    Write code into a per-run workdir and render MotionGraphic → MP4.
    Returns {ok, video_path, log, error}.
    """
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RENDERS_DIR / f"run_{run_id}.mp4"
    log_path = RENDERS_DIR / f"run_{run_id}.log"

    try:
        cleaned = ensure_required_exports(extract_remotion_code(code))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "video_path": None, "error": str(exc), "code": code, "log": ""}

    if not NPX_BIN:
        return {
            "ok": False,
            "video_path": None,
            "error": "npx not found. Install Node or set NPX_BIN.",
            "code": cleaned,
            "log": "",
        }

    with RENDER_LOCK:
        work = prepare_work_dir(run_id, cleaned)
        env = os.environ.copy()
        # Prefer bundled headless shell; do not use broken snap chromium
        if BUNDLED_BROWSER.exists():
            env["REMOTION_BROWSER_EXECUTABLE"] = str(BUNDLED_BROWSER)
        env.pop("PUPPETEER_EXECUTABLE_PATH", None)
        # Ensure node/npx are on PATH for remotion's child processes
        path_parts = [
            NODE_BIN_DIR,
            str(Path.home() / ".local/bin"),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
        existing = env.get("PATH", "")
        env["PATH"] = ":".join([p for p in path_parts if p] + ([existing] if existing else []))
        if NODE_BIN:
            env["NODE_BINARY"] = NODE_BIN

        # Prefer local remotion CLI binary over npx when available
        remotion_cli = TEMPLATE_DIR / "node_modules" / ".bin" / "remotion"
        if remotion_cli.exists():
            cmd = [
                str(remotion_cli),
                "render",
                "src/index.ts",
                "MotionGraphic",
                str(out_path),
                "--concurrency=1",
                "--log=error",
            ]
        else:
            cmd = [
                NPX_BIN,
                "remotion",
                "render",
                "src/index.ts",
                "MotionGraphic",
                str(out_path),
                "--concurrency=1",
                "--log=error",
            ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(work),
                env=env,
                capture_output=True,
                text=True,
                timeout=RENDER_TIMEOUT_SEC,
                check=False,
            )
            log = (proc.stdout or "") + "\n" + (proc.stderr or "")
            log_path.write_text(log[-100_000:], encoding="utf-8")
            if proc.returncode != 0 or not out_path.exists():
                return {
                    "ok": False,
                    "video_path": None,
                    "error": f"Remotion render failed (exit {proc.returncode}): {log[-1500:]}",
                    "code": cleaned,
                    "log": log[-20_000:],
                }
            return {
                "ok": True,
                "video_path": str(out_path),
                "error": None,
                "code": cleaned,
                "log": log[-20_000:],
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "video_path": None,
                "error": f"Remotion render timed out after {RENDER_TIMEOUT_SEC}s",
                "code": cleaned,
                "log": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "video_path": None,
                "error": f"Remotion render error: {exc}",
                "code": cleaned,
                "log": "",
            }
        finally:
            # Keep work dir for debugging last failures; prune old ones
            try:
                _prune_work_dirs(keep=8)
            except Exception:
                pass


def _prune_work_dirs(keep: int = 8) -> None:
    if not WORK_DIR.exists():
        return
    dirs = sorted(
        [p for p in WORK_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def video_path_for_run(run_id: int) -> Path:
    return RENDERS_DIR / f"run_{run_id}.mp4"
