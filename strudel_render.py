"""Render model-generated Strudel code to MP3 via headless Chromium."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "strudel-template"
DATA_DIR = Path(
    os.environ.get("BENCH_MARKER_DATA_DIR", str(BASE_DIR / "data"))
).expanduser()
RENDERS_DIR = DATA_DIR / "renders"
WORK_DIR = DATA_DIR / "work-strudel"

RENDER_LOCK = threading.Lock()
RENDER_TIMEOUT_SEC = int(os.environ.get("STRUDEL_RENDER_TIMEOUT", "240"))
DEFAULT_DURATION_SEC = float(os.environ.get("STRUDEL_RENDER_DURATION", "16"))
DEFAULT_SAMPLE_RATE = int(os.environ.get("STRUDEL_SAMPLE_RATE", "44100"))

BUNDLED_BROWSER = (
    BASE_DIR
    / "remotion-template/node_modules/.remotion/chrome-headless-shell/linux64"
    / "chrome-headless-shell-linux64/chrome-headless-shell"
)

_NODE_CANDIDATES = [
    os.environ.get("NODE_BIN"),
    str(Path.home() / ".local/bin/node"),
    str(Path.home() / ".hermes/node/bin/node"),
    shutil.which("node"),
]
_FFMPEG_CANDIDATES = [
    os.environ.get("FFMPEG_BIN"),
    shutil.which("ffmpeg"),
    "/usr/bin/ffmpeg",
]


def _first_existing(paths: list[str | None | Path]) -> str | None:
    for p in paths:
        if p and Path(p).exists():
            return str(p)
    return None


NODE_BIN = _first_existing(_NODE_CANDIDATES)
FFMPEG_BIN = _first_existing(_FFMPEG_CANDIDATES)
NODE_BIN_DIR = str(Path(NODE_BIN).parent) if NODE_BIN else ""


def extract_strudel_code(text: str) -> str:
    """Pull Strudel JS source out of model output (fences / prose)."""
    if not text:
        return ""

    cleaned = text.strip()

    fence = re.match(
        r"^```(?:javascript|js|strudel|tidal)?\s*\n([\s\S]*?)\n?```\s*$",
        cleaned,
        re.IGNORECASE,
    )
    if fence:
        cleaned = fence.group(1).strip()
    else:
        m = re.search(
            r"```(?:javascript|js|strudel|tidal)?\s*\n([\s\S]*?)\n?```",
            cleaned,
            re.IGNORECASE,
        )
        if m:
            cleaned = m.group(1).strip()
        else:
            cleaned = re.sub(r"^```(?:javascript|js|strudel)?\s*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()

    # Drop leading prose before code-looking lines
    lines = cleaned.splitlines()
    start = 0
    found = False
    code_prefixes = (
        "$:",
        "setcpm",
        "setcps",
        "setCpm",
        "setCps",
        "samples(",
        "note(",
        "n(",
        "s(",
        "stack(",
        "//",
        "/*",
        "const ",
        "let ",
        "var ",
        "await ",
        "sound(",
        "chord(",
        "transpose(",
    )
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith(code_prefixes) or s.startswith("export ") or s.startswith("import "):
            start = i
            found = True
            break
        # Bare mini-notation / pattern-ish lines (e.g. $: already handled)
        if s.startswith("$") or re.match(r"^(note|n|s|stack|cat|seq|sound)\s*\(", s):
            start = i
            found = True
            break
    if found:
        cleaned = "\n".join(lines[start:]).strip()
    return cleaned


def audio_path_for_run(run_id: int) -> Path:
    return RENDERS_DIR / f"run_{run_id}.mp3"


def wav_path_for_run(run_id: int) -> Path:
    return RENDERS_DIR / f"run_{run_id}.wav"


def ensure_bundle() -> None:
    """Build public/bundle.js if missing."""
    bundle = TEMPLATE_DIR / "public" / "bundle.js"
    if bundle.exists() and bundle.stat().st_size > 1000:
        return
    if not NODE_BIN:
        raise RuntimeError("node not found — cannot build Strudel bundle")
    build_js = TEMPLATE_DIR / "build.mjs"
    if not build_js.exists():
        raise RuntimeError(f"Missing {build_js}")
    proc = subprocess.run(
        [NODE_BIN, str(build_js)],
        cwd=str(TEMPLATE_DIR),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0 or not bundle.exists():
        raise RuntimeError(
            f"Strudel bundle build failed (exit {proc.returncode}): "
            f"{(proc.stdout or '') + (proc.stderr or '')}"[-1500:]
        )


def render_run(
    run_id: int,
    raw_code: str,
    *,
    duration_sec: float | None = None,
    cycles: int | None = None,
    sample_rate: int | None = None,
) -> dict[str, Any]:
    """
    Extract Strudel code, render offline audio → MP3.
    Returns {ok, audio_path, code, error, log, meta}.
    """
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out_mp3 = audio_path_for_run(run_id)
    out_wav = wav_path_for_run(run_id)
    log_path = RENDERS_DIR / f"run_{run_id}.strudel.log"

    duration_sec = float(
        duration_sec if duration_sec is not None else DEFAULT_DURATION_SEC
    )
    sample_rate = int(sample_rate if sample_rate is not None else DEFAULT_SAMPLE_RATE)

    cleaned = extract_strudel_code(raw_code)
    if not cleaned.strip():
        return {
            "ok": False,
            "audio_path": None,
            "code": raw_code or "",
            "error": "Model returned empty Strudel code",
            "log": "",
            "meta": {},
        }

    if not NODE_BIN:
        return {
            "ok": False,
            "audio_path": None,
            "code": cleaned,
            "error": "node not found. Install Node or set NODE_BIN.",
            "log": "",
            "meta": {},
        }

    if not FFMPEG_BIN:
        return {
            "ok": False,
            "audio_path": None,
            "code": cleaned,
            "error": "ffmpeg not found — required to encode MP3",
            "log": "",
            "meta": {},
        }

    try:
        ensure_bundle()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "audio_path": None,
            "code": cleaned,
            "error": str(exc),
            "log": "",
            "meta": {},
        }

    render_js = TEMPLATE_DIR / "render.mjs"
    if not render_js.exists():
        return {
            "ok": False,
            "audio_path": None,
            "code": cleaned,
            "error": f"Missing {render_js}",
            "log": "",
            "meta": {},
        }

    with RENDER_LOCK:
        work = WORK_DIR / f"run_{run_id}"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        code_file = work / "song.js"
        code_file.write_text(cleaned + "\n", encoding="utf-8")

        # Clean previous artefacts for this run
        for p in (out_mp3, out_wav):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

        env = os.environ.copy()
        path_parts = [
            NODE_BIN_DIR,
            str(Path.home() / ".local/bin"),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
        existing = env.get("PATH", "")
        env["PATH"] = ":".join([p for p in path_parts if p] + ([existing] if existing else []))
        if BUNDLED_BROWSER.exists():
            env.setdefault("CHROME_BIN", str(BUNDLED_BROWSER))
            env.setdefault("REMOTION_BROWSER_EXECUTABLE", str(BUNDLED_BROWSER))

        cmd = [
            NODE_BIN,
            str(render_js),
            "--code",
            str(code_file),
            "--out",
            str(out_wav),
            "--duration",
            str(duration_sec),
            "--sample-rate",
            str(sample_rate),
            "--timeout-ms",
            str(max(30_000, RENDER_TIMEOUT_SEC * 1000 - 10_000)),
        ]
        if cycles is not None:
            cmd.extend(["--cycles", str(int(cycles))])
        if BUNDLED_BROWSER.exists():
            cmd.extend(["--chrome", str(BUNDLED_BROWSER)])

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(TEMPLATE_DIR),
                env=env,
                capture_output=True,
                text=True,
                timeout=RENDER_TIMEOUT_SEC,
                check=False,
            )
            combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            log_path.write_text(combined[-100_000:], encoding="utf-8")

            # render.mjs prints a single JSON object on stdout (last non-empty line)
            payload: dict[str, Any] = {}
            for line in reversed((proc.stdout or "").splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        payload = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue

            if not payload.get("ok") or not out_wav.exists() or out_wav.stat().st_size < 100:
                err = (
                    (payload.get("error") if isinstance(payload, dict) else None)
                    or f"Strudel render failed (exit {proc.returncode})"
                )
                detail = payload.get("log") if isinstance(payload, dict) else ""
                return {
                    "ok": False,
                    "audio_path": None,
                    "code": cleaned,
                    "error": str(err)[:2000],
                    "log": (str(detail) + "\n" + combined)[-20_000:],
                    "meta": (payload.get("meta") if isinstance(payload, dict) else {}) or {},
                }

            # WAV → MP3
            ff = subprocess.run(
                [
                    FFMPEG_BIN,
                    "-y",
                    "-i",
                    str(out_wav),
                    "-codec:a",
                    "libmp3lame",
                    "-qscale:a",
                    "2",
                    str(out_mp3),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            ff_log = (ff.stdout or "") + "\n" + (ff.stderr or "")
            log_path.write_text((combined + "\n\n--- ffmpeg ---\n" + ff_log)[-100_000:], encoding="utf-8")

            if ff.returncode != 0 or not out_mp3.exists() or out_mp3.stat().st_size < 100:
                return {
                    "ok": False,
                    "audio_path": None,
                    "code": cleaned,
                    "error": f"ffmpeg MP3 encode failed (exit {ff.returncode}): {ff_log[-1200:]}",
                    "log": (combined + "\n" + ff_log)[-20_000:],
                    "meta": payload.get("meta") or {},
                }

            # Drop bulky intermediate WAV to save disk
            try:
                out_wav.unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                if out_wav.exists():
                    out_wav.unlink()
            except OSError:
                pass

            return {
                "ok": True,
                "audio_path": str(out_mp3),
                "code": cleaned,
                "error": None,
                "log": combined[-10_000:],
                "meta": payload.get("meta") or {},
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "audio_path": None,
                "code": cleaned,
                "error": f"Strudel render timed out after {RENDER_TIMEOUT_SEC}s",
                "log": "",
                "meta": {},
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "audio_path": None,
                "code": cleaned,
                "error": f"Strudel render error: {exc}",
                "log": "",
                "meta": {},
            }
        finally:
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
