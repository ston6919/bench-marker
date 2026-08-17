#!/usr/bin/env python3
"""Bench Marker — compare OpenRouter models on your own business tasks."""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without overriding the process environment."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


EXTRA_ENV_FILE = os.environ.get("BENCH_MARKER_ENV_FILE", "").strip()
if EXTRA_ENV_FILE:
    load_env_file(Path(EXTRA_ENV_FILE).expanduser())
load_env_file(BASE_DIR / ".env")

import db  # noqa: E402  (configuration must load before local modules)
import html_image_render  # noqa: E402
import remotion_render  # noqa: E402
import strudel_render  # noqa: E402

STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(
    os.environ.get("BENCH_MARKER_DATA_DIR", str(BASE_DIR / "data"))
).expanduser()
SETTINGS_FILE = DATA_DIR / "settings.json"
RENDERS_DIR = DATA_DIR / "renders"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8796"))
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_AUTH_URL = "https://openrouter.ai/api/v1/auth/key"
OPENROUTER_HTTP_REFERER = os.environ.get(
    "OPENROUTER_HTTP_REFERER", f"http://localhost:{PORT}/"
)
OPENROUTER_APP_TITLE = os.environ.get("OPENROUTER_APP_TITLE", "Bench Marker")

RUN_LOCK = threading.Lock()
SETTINGS_LOCK = threading.Lock()
CANCEL_LOCK = threading.Lock()
ACTIVE_RUNS: dict[int, dict[str, Any]] = {}
OR_MODELS_CACHE: dict[str, Any] = {"fetched_at": 0.0, "models": []}
OR_MODELS_CACHE_TTL = 300.0  # seconds
IN_FLIGHT = ("pending", "running", "rendering")


class RunCancelled(Exception):
    """User stopped an in-flight run."""


def begin_active_run(run_id: int) -> threading.Event:
    ev = threading.Event()
    with CANCEL_LOCK:
        ACTIVE_RUNS[int(run_id)] = {"event": ev, "response": None}
    return ev


def attach_active_response(run_id: int, resp: Any) -> None:
    with CANCEL_LOCK:
        slot = ACTIVE_RUNS.get(int(run_id))
        if not slot:
            return
        slot["response"] = resp
        if slot["event"].is_set():
            try:
                resp.close()
            except Exception:
                pass


def end_active_run(run_id: int) -> None:
    with CANCEL_LOCK:
        ACTIVE_RUNS.pop(int(run_id), None)


def is_run_cancelled(run_id: int) -> bool:
    with CANCEL_LOCK:
        slot = ACTIVE_RUNS.get(int(run_id))
        if slot and slot["event"].is_set():
            return True
    run = db.get_run(int(run_id))
    return bool(run and (run.get("status") or "") == "cancelled")


def cancel_run(run_id: int, reason: str = "Stopped by user") -> dict[str, Any] | None:
    """Request stop: close the live HTTP stream and mark the run cancelled."""
    run = db.get_run(int(run_id))
    if not run:
        return None
    status = run.get("status") or ""
    with CANCEL_LOCK:
        slot = ACTIVE_RUNS.get(int(run_id))
        if slot:
            slot["event"].set()
            resp = slot.get("response")
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
    if status in IN_FLIGHT or slot:
        updated = db.update_run(
            int(run_id),
            status="cancelled",
            error=(reason or "Stopped by user")[:2000],
            completed_at=db.utc_now(),
        )
        return updated or run
    return run


def cost_limit_usd() -> float | None:
    """Configured spend cap. None / 0 means no limit."""
    with SETTINGS_LOCK:
        raw = load_settings().get("cost_limit_usd")
    val = _safe_float(raw)
    if val is None or val <= 0:
        return None
    return float(val)


def estimate_tokens_from_text(text: str) -> int:
    """Conservative char→token estimate so we trip a cap a little early."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.5))


def estimate_running_cost_usd(
    openrouter_id: str,
    prompt_text: str,
    completion_text: str,
    usage: dict[str, Any] | None,
    raw: dict[str, Any] | None,
    price_map: dict[str, dict[str, float | None]],
) -> float | None:
    """Prefer provider-reported cost; otherwise catalogue price × tokens."""
    official = extract_cost_usd(raw) if raw else None
    if official is None and usage:
        official = extract_cost_usd({"usage": usage})
    if official is not None:
        return float(official)
    usage = usage or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if prompt_tokens is None:
        prompt_tokens = estimate_tokens_from_text(prompt_text)
    if completion_tokens is None:
        completion_tokens = estimate_tokens_from_text(completion_text)
    return estimate_cost_from_tokens(
        int(prompt_tokens or 0),
        int(completion_tokens or 0),
        openrouter_id,
        price_map,
    )


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(SETTINGS_FILE)
    try:
        SETTINGS_FILE.chmod(0o600)
    except OSError:
        pass


def openrouter_key() -> str:
    """Prefer key saved from the UI; fall back to env files."""
    with SETTINGS_LOCK:
        saved = (load_settings().get("openrouter_api_key") or "").strip()
    if saved:
        return saved
    return (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("OPENROUTER_KEY")
        or ""
    ).strip()


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    # NaN / Inf become invalid JSON for browsers (JSON.parse rejects them).
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return n


def mask_key(key: str) -> str:
    """Human-readable mask; avoid unicode ellipsis which can render oddly."""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 12:
        return "****" + key[-4:]
    return f"{key[:7]}...{key[-4:]}"


def settings_public() -> dict[str, Any]:
    key = openrouter_key()
    with SETTINGS_LOCK:
        source = "ui" if (load_settings().get("openrouter_api_key") or "").strip() else (
            "env" if key else "none"
        )
    return {
        "openrouter_configured": bool(key),
        "openrouter_key_source": source,
        "openrouter_key_masked": mask_key(key) if key else "",
        "cost_limit_usd": cost_limit_usd(),
    }


def json_response(
    handler: SimpleHTTPRequestHandler,
    status: int,
    payload: Any,
) -> None:
    # allow_nan=False so we never emit NaN/Infinity (invalid in browser JSON.parse).
    body = json.dumps(payload, default=str, allow_nan=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: SimpleHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def openrouter_request(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 120.0,
    run_id: int | None = None,
) -> dict[str, Any]:
    key = openrouter_key()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. Add it to the environment, .env, "
            "or the Settings screen."
        )
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_HTTP_REFERER,
        "X-Title": OPENROUTER_APP_TITLE,
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if run_id is not None:
                attach_active_response(run_id, resp)
                if is_run_cancelled(run_id):
                    raise RunCancelled()
            return json.loads(resp.read().decode("utf-8"))
    except RunCancelled:
        raise
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {detail[:800]}") from e


def _delta_text(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    # Some providers put the first token on message.content
    message = choices[0].get("message") or {}
    msg_content = message.get("content")
    if isinstance(msg_content, str):
        return msg_content
    return ""


def _delta_reasoning(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    for key in ("reasoning", "reasoning_content"):
        val = delta.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def openrouter_stream(
    payload: dict[str, Any],
    timeout: float = 90.0,
    run_id: int | None = None,
):
    """Yield dicts: {delta}, {heartbeat}, {reasoning}, {usage}, {error} as SSE arrives.

    `timeout` is idle time between lines, not total generation time.
    """
    key = openrouter_key()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. Add it to the environment, .env, "
            "or the Settings screen."
        )
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "HTTP-Referer": OPENROUTER_HTTP_REFERER,
        "X-Title": OPENROUTER_APP_TITLE,
    }
    body = dict(payload)
    body["stream"] = True
    req = urllib.request.Request(
        OPENROUTER_CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {detail[:800]}") from e

    if run_id is not None:
        attach_active_response(run_id, resp)
        if is_run_cancelled(run_id):
            try:
                resp.close()
            except Exception:
                pass
            yield {"cancelled": True}
            return

    try:
        while True:
            if run_id is not None and is_run_cancelled(run_id):
                yield {"cancelled": True}
                return
            try:
                raw_line = resp.readline()
            except TimeoutError as exc:
                yield {
                    "error": (
                        "Stream timed out waiting for the next token "
                        f"({int(timeout)}s idle)."
                    )
                }
                return
            except Exception as exc:  # noqa: BLE001
                if "timed out" in str(exc).lower():
                    yield {
                        "error": (
                            "Stream timed out waiting for the next token "
                            f"({int(timeout)}s idle)."
                        )
                    }
                    return
                raise
            if not raw_line:
                if run_id is not None and is_run_cancelled(run_id):
                    yield {"cancelled": True}
                    return
                break
            line = raw_line.decode("utf-8", errors="replace").strip("\r\n")
            if not line:
                continue
            if line.startswith(":"):
                yield {"heartbeat": True}
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            if parsed.get("error"):
                err = parsed["error"]
                if isinstance(err, dict):
                    msg = str(err.get("message") or err)
                else:
                    msg = str(err)
                yield {"error": msg[:2000], "raw": parsed}
                return
            event: dict[str, Any] = {}
            delta = _delta_text(parsed)
            reasoning = _delta_reasoning(parsed)
            if delta:
                event["delta"] = delta
            if reasoning:
                event["reasoning"] = reasoning
            if parsed.get("usage"):
                event["usage"] = parsed["usage"]
                event["raw"] = parsed
            if event:
                yield event
    finally:
        try:
            resp.close()
        except Exception:
            pass


def extract_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content or "")


def extract_cost_usd(response: dict[str, Any] | None) -> float | None:
    """Pull USD cost from an OpenRouter chat completion payload."""
    if not response or not isinstance(response, dict):
        return None
    usage = response.get("usage") or {}
    for key in ("cost", "total_cost", "native_cost"):
        val = usage.get(key)
        if val is not None:
            parsed = _safe_float(val)
            if parsed is not None:
                return parsed
    details = usage.get("cost_details") or {}
    for key in (
        "upstream_inference_cost",
        "total_cost",
        "upstream_inference_prompt_cost",  # incomplete alone; try combined first
    ):
        if key == "upstream_inference_prompt_cost":
            pin = _safe_float(details.get("upstream_inference_prompt_cost"))
            pout = _safe_float(details.get("upstream_inference_completions_cost"))
            if pin is not None or pout is not None:
                return (pin or 0.0) + (pout or 0.0)
            continue
        parsed = _safe_float(details.get(key))
        if parsed is not None:
            return parsed
    # Rare top-level fields
    for key in ("total_cost", "cost"):
        parsed = _safe_float(response.get(key))
        if parsed is not None:
            return parsed
    return None


def estimate_cost_from_tokens(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    openrouter_id: str,
    price_by_id: dict[str, dict[str, float | None]],
) -> float | None:
    """Estimate USD from token counts × catalogue pricing (per-token rates)."""
    if not openrouter_id:
        return None
    pricing = price_by_id.get(openrouter_id) or price_by_id.get(
        openrouter_id.lstrip("~")
    )
    if not pricing:
        # try without :suffix variants already exact
        return None
    pin = pricing.get("prompt")  # USD per token
    pout = pricing.get("completion")
    if pin is None and pout is None:
        return None
    total = 0.0
    if prompt_tokens is not None and pin is not None:
        total += int(prompt_tokens) * float(pin)
    if completion_tokens is not None and pout is not None:
        total += int(completion_tokens) * float(pout)
    if prompt_tokens is None and completion_tokens is None:
        return None
    return total


def get_openrouter_price_map(force_refresh: bool = False) -> dict[str, dict[str, float | None]]:
    """id -> {prompt, completion} in USD per token."""
    now = time.time()
    cached = OR_MODELS_CACHE.get("models") or []
    fetched_at = float(OR_MODELS_CACHE.get("fetched_at") or 0)
    if force_refresh or not cached or (now - fetched_at) >= OR_MODELS_CACHE_TTL:
        try:
            data = openrouter_request(OPENROUTER_MODELS_URL, method="GET", timeout=30)
            models = data.get("data") or []
            slim = []
            for m in models:
                pricing = m.get("pricing") or {}
                prompt_per_token = _safe_float(pricing.get("prompt"))
                completion_per_token = _safe_float(pricing.get("completion"))
                prompt_per_m = (
                    prompt_per_token * 1_000_000 if prompt_per_token is not None else None
                )
                completion_per_m = (
                    completion_per_token * 1_000_000
                    if completion_per_token is not None
                    else None
                )
                slim.append(
                    {
                        "id": m.get("id"),
                        "name": m.get("name") or m.get("id"),
                        "context_length": m.get("context_length"),
                        "created": m.get("created"),
                        "pricing": {
                            "prompt": prompt_per_token,
                            "completion": completion_per_token,
                            "prompt_per_million": prompt_per_m,
                            "completion_per_million": completion_per_m,
                        },
                        "is_free": bool(
                            (prompt_per_m or 0) == 0 and (completion_per_m or 0) == 0
                        ),
                    }
                )
            slim.sort(key=lambda x: ((x.get("name") or "").lower(), x.get("id") or ""))
            OR_MODELS_CACHE["models"] = slim
            OR_MODELS_CACHE["fetched_at"] = now
            cached = slim
        except Exception:
            pass
    out: dict[str, dict[str, float | None]] = {}
    for m in cached:
        mid = m.get("id")
        if not mid:
            continue
        p = m.get("pricing") or {}
        out[str(mid)] = {
            "prompt": p.get("prompt"),
            "completion": p.get("completion"),
        }
    return out


def backfill_run_costs() -> dict[str, Any]:
    """Fill cost_usd on completed runs missing it (from raw_response or token estimate)."""
    runs = db.list_runs(limit=10_000)
    price_map = get_openrouter_price_map()
    updated = 0
    from_raw = 0
    from_estimate = 0
    skipped = 0
    details: list[dict[str, Any]] = []

    for run in runs:
        if run.get("status") != "completed":
            skipped += 1
            continue
        if run.get("cost_usd") is not None:
            skipped += 1
            continue

        cost = None
        source = None
        raw = run.get("raw_response") or ""
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
                cost = extract_cost_usd(payload)
                if cost is not None:
                    source = "raw_response"
                    from_raw += 1
            except json.JSONDecodeError:
                pass

        if cost is None:
            cost = estimate_cost_from_tokens(
                run.get("tokens_prompt"),
                run.get("tokens_completion"),
                str(run.get("model_openrouter_id") or ""),
                price_map,
            )
            if cost is not None:
                source = "token_estimate"
                from_estimate += 1

        if cost is None:
            skipped += 1
            details.append({"id": run["id"], "status": "unresolved"})
            continue

        db.update_run(int(run["id"]), cost_usd=float(cost))
        updated += 1
        details.append(
            {"id": run["id"], "cost_usd": float(cost), "source": source}
        )

    return {
        "updated": updated,
        "from_raw_response": from_raw,
        "from_token_estimate": from_estimate,
        "skipped": skipped,
        "details": details[:100],
    }


def _run_messages(run: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    system_prompt = (run.get("system_prompt_snapshot") or "").strip()
    user_prompt = (run.get("user_prompt_snapshot") or "").strip()
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt or "(empty prompt)"})
    return messages


def _cost_from_usage(
    usage: dict[str, Any],
    raw: dict[str, Any] | None,
    openrouter_id: str,
) -> float | None:
    cost = extract_cost_usd(raw) if raw else None
    if cost is None:
        cost = extract_cost_usd({"usage": usage})
    if cost is None:
        try:
            price_map = get_openrouter_price_map()
            cost = estimate_cost_from_tokens(
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                openrouter_id,
                price_map,
            )
        except Exception:
            cost = None
    return cost


def execute_draw_run(
    run_id: int,
    run: dict[str, Any],
    model: dict[str, Any],
) -> None:
    """Stream SVG tokens and persist partial output so the UI can paint live."""
    messages = _run_messages(run)
    payload = {
        "model": model["openrouter_id"],
        "messages": messages,
    }
    prompt_text = "\n".join(m.get("content") or "" for m in messages)
    started = time.perf_counter()
    buffer = ""
    last_write = 0.0
    last_len = 0
    usage: dict[str, Any] = {}
    raw_last: dict[str, Any] | None = None
    stream_error = ""
    heartbeats = 0
    reasoning_chars = 0
    reasoning_buf = ""
    reasoning_all = ""
    connected = False
    first_token_at: float | None = None
    est_cost: float | None = None
    cap = cost_limit_usd()
    kind = (db.get_task(int(run["task_id"])) or {}).get("kind") or "draw"
    try:
        price_map = get_openrouter_price_map()
    except Exception:
        price_map = {}

    def current_est_cost() -> float | None:
        return estimate_running_cost_usd(
            str(model.get("openrouter_id") or ""),
            prompt_text,
            buffer + reasoning_buf,
            usage,
            raw_last,
            price_map,
        )

    def live_pen_commands() -> list[dict[str, Any]]:
        if kind != "pen":
            return []
        return db.extract_pen_commands_from_sources(
            buffer, reasoning_all, partial=True
        )

    def progress_payload() -> str:
        cmds = live_pen_commands()
        phase = "connecting"
        if buffer or cmds:
            phase = "drawing"
        elif connected:
            phase = "thinking"
        payload_obj: dict[str, Any] = {
            "stream": True,
            "phase": phase,
            "heartbeats": heartbeats,
            "reasoning_chars": reasoning_chars,
            "reasoning_preview": reasoning_buf[-3000:],
            "reasoning_full": reasoning_all[-80_000:] if kind == "pen" else "",
            "output_chars": len(buffer),
            "est_cost_usd": est_cost,
            "cost_limit_usd": cap,
            "error": stream_error,
            "raw": raw_last,
        }
        if kind == "pen":
            payload_obj["pen_commands"] = cmds
        return json.dumps(payload_obj, default=str)[:200_000]

    def flush(force: bool = False) -> None:
        nonlocal last_write, last_len
        now = time.monotonic()
        grew = len(buffer) - last_len
        if not force and (now - last_write) < 0.15 and grew < 80:
            return
        db.update_run(
            run_id,
            output_text=buffer[: db.DRAW_OUTPUT_CAP],
            raw_response=progress_payload(),
            cost_usd=est_cost,
            error="",
        )
        last_write = now
        last_len = len(buffer)

    def over_cost_cap() -> str | None:
        nonlocal est_cost
        est_cost = current_est_cost()
        if cap is None or est_cost is None:
            return None
        if est_cost < cap:
            return None
        return (
            f"Stopped: estimated cost ${est_cost:.4f} reached the "
            f"${cap:.4f} cap"
        )

    print(f"[bench-marker] {kind} run {run_id} streaming {model.get('openrouter_id')}")
    begin_active_run(run_id)
    cancelled = False
    try:
        try:
            for event in openrouter_stream(payload, run_id=run_id):
                if event.get("cancelled") or is_run_cancelled(run_id):
                    cancelled = True
                    break
                connected = True
                if event.get("error"):
                    stream_error = str(event["error"])
                    break
                if event.get("heartbeat"):
                    heartbeats += 1
                    flush(force=heartbeats == 1 or heartbeats % 4 == 0)
                    continue
                if event.get("reasoning"):
                    chunk = str(event["reasoning"])
                    reasoning_chars += len(chunk)
                    reasoning_all = (reasoning_all + chunk)[: db.DRAW_OUTPUT_CAP]
                    reasoning_buf = reasoning_all[-8000:]
                    flush()
                    hit = over_cost_cap()
                    if hit:
                        print(f"[bench-marker] run {run_id} {hit}")
                        cancel_run(run_id, reason=hit)
                        cancelled = True
                        break
                if event.get("delta"):
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                        print(
                            f"[bench-marker] draw run {run_id} first content token "
                            f"after {int((first_token_at - started) * 1000)}ms"
                        )
                    buffer += str(event["delta"])
                    if len(buffer) > db.DRAW_OUTPUT_CAP:
                        buffer = buffer[: db.DRAW_OUTPUT_CAP]
                    flush()
                    hit = over_cost_cap()
                    if hit:
                        print(f"[bench-marker] run {run_id} {hit}")
                        cancel_run(run_id, reason=hit)
                        cancelled = True
                        break
                if event.get("usage"):
                    usage = event["usage"] if isinstance(event["usage"], dict) else {}
                    hit = over_cost_cap()
                    if hit:
                        print(f"[bench-marker] run {run_id} {hit}")
                        cancel_run(run_id, reason=hit)
                        cancelled = True
                        break
                if event.get("raw"):
                    raw_last = event["raw"]
        except Exception as stream_exc:  # noqa: BLE001
            if is_run_cancelled(run_id):
                cancelled = True
            # Only fall back if we never got a live stream (provider rejected SSE).
            elif connected or buffer:
                raise
            print(
                f"[bench-marker] draw run {run_id} stream failed before connect; "
                f"falling back to blocking: {stream_exc}"
            )
            response = openrouter_request(
                OPENROUTER_CHAT_URL, method="POST", payload=payload
            )
            buffer = extract_text(response)
            usage = response.get("usage") or {}
            raw_last = response
            if not buffer:
                raise stream_exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if cancelled or is_run_cancelled(run_id):
            existing = db.get_run(run_id) or {}
            reason = (existing.get("error") or "").strip() or "Stopped by user"
            db.update_run(
                run_id,
                status="cancelled",
                output_text=buffer[: db.DRAW_OUTPUT_CAP],
                error=reason[:2000],
                latency_ms=latency_ms,
                cost_usd=est_cost,
                raw_response=progress_payload(),
                completed_at=db.utc_now(),
            )
            return

        cost = _cost_from_usage(
            usage, raw_last, str(model.get("openrouter_id") or "")
        )
        if kind == "draw":
            final_out = db.extract_preview_svg(buffer) or buffer[: db.DRAW_OUTPUT_CAP]
        elif kind == "pen":
            cmds = live_pen_commands()
            formatted = db.format_pen_commands(cmds)
            final_out = formatted or buffer[: db.DRAW_OUTPUT_CAP]
        else:
            final_out = buffer[: db.DRAW_OUTPUT_CAP]
        raw_dump = progress_payload()

        if stream_error and not final_out.strip():
            db.update_run(
                run_id,
                status="failed",
                output_text=buffer[: db.DRAW_OUTPUT_CAP],
                error=stream_error[:2000],
                latency_ms=latency_ms,
                tokens_prompt=usage.get("prompt_tokens"),
                tokens_completion=usage.get("completion_tokens"),
                cost_usd=cost,
                raw_response=raw_dump,
                completed_at=db.utc_now(),
            )
            return

        db.update_run(
            run_id,
            status="completed",
            output_text=final_out,
            error=stream_error[:2000] if stream_error else "",
            latency_ms=latency_ms,
            tokens_prompt=usage.get("prompt_tokens"),
            tokens_completion=usage.get("completion_tokens"),
            cost_usd=cost,
            raw_response=raw_dump,
            completed_at=db.utc_now(),
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - started) * 1000)
        if is_run_cancelled(run_id):
            existing = db.get_run(run_id) or {}
            reason = (existing.get("error") or "").strip() or "Stopped by user"
            db.update_run(
                run_id,
                status="cancelled",
                output_text=buffer[: db.DRAW_OUTPUT_CAP],
                error=reason[:2000],
                latency_ms=latency_ms,
                cost_usd=est_cost,
                completed_at=db.utc_now(),
            )
            return
        db.update_run(
            run_id,
            status="failed",
            output_text=buffer[: db.DRAW_OUTPUT_CAP],
            error=str(exc)[:2000],
            latency_ms=latency_ms,
            completed_at=db.utc_now(),
            raw_response=traceback.format_exc()[:20_000],
        )
    finally:
        end_active_run(run_id)


def execute_run(run_id: int) -> None:
    """Background worker: call OpenRouter for a pending run."""
    run = db.get_run(run_id)
    if not run:
        return
    model = db.get_model(int(run["model_id"]))
    task = db.get_task(int(run["task_id"]))
    if not model:
        db.update_run(
            run_id,
            status="failed",
            error="Model not found",
            completed_at=db.utc_now(),
        )
        return

    if (run.get("status") or "") == "cancelled" or is_run_cancelled(run_id):
        db.update_run(
            run_id,
            status="cancelled",
            error="Stopped by user",
            completed_at=db.utc_now(),
        )
        return

    db.update_run(run_id, status="running")
    kind = (task or {}).get("kind") or "text"
    if kind in {"draw", "pen"}:
        execute_draw_run(run_id, run, model)
        return

    begin_active_run(run_id)

    messages = _run_messages(run)
    payload = {
        "model": model["openrouter_id"],
        "messages": messages,
    }

    started = time.perf_counter()
    try:
        response = openrouter_request(
            OPENROUTER_CHAT_URL, method="POST", payload=payload, run_id=run_id
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = response.get("usage") or {}
        cost = extract_cost_usd(response)
        if cost is None:
            # Fall back to catalogue pricing × tokens if OpenRouter omits cost
            try:
                price_map = get_openrouter_price_map()
                cost = estimate_cost_from_tokens(
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    str(model.get("openrouter_id") or ""),
                    price_map,
                )
            except Exception:
                cost = None

        if is_run_cancelled(run_id):
            raise RunCancelled()
        output = extract_text(response)
        kind = (task or {}).get("kind") or "text"
        if kind in ("html", "game"):
            output = db.extract_html_document(output)

        if kind == "remotion":
            # Persist model code first, then render under a lock
            db.update_run(
                run_id,
                status="rendering",
                output_text=output,
                latency_ms=latency_ms,
                tokens_prompt=usage.get("prompt_tokens"),
                tokens_completion=usage.get("completion_tokens"),
                cost_usd=cost,
                raw_response=json.dumps(response)[:200_000],
                error="",
            )
            render_started = time.perf_counter()
            result = remotion_render.render_run(run_id, output)
            render_ms = int((time.perf_counter() - render_started) * 1000)
            total_ms = latency_ms + render_ms
            if result.get("ok"):
                db.update_run(
                    run_id,
                    status="completed",
                    output_text=result.get("code") or output,
                    latency_ms=total_ms,
                    completed_at=db.utc_now(),
                    error="",
                )
            else:
                db.update_run(
                    run_id,
                    status="failed",
                    output_text=result.get("code") or output,
                    latency_ms=total_ms,
                    completed_at=db.utc_now(),
                    error=str(result.get("error") or "Remotion render failed")[:2000],
                )
            return

        if kind == "strudel":
            # Persist model Strudel code, then offline-render to MP3
            db.update_run(
                run_id,
                status="rendering",
                output_text=output,
                latency_ms=latency_ms,
                tokens_prompt=usage.get("prompt_tokens"),
                tokens_completion=usage.get("completion_tokens"),
                cost_usd=cost,
                raw_response=json.dumps(response)[:200_000],
                error="",
            )
            render_started = time.perf_counter()
            result = strudel_render.render_run(run_id, output)
            render_ms = int((time.perf_counter() - render_started) * 1000)
            total_ms = latency_ms + render_ms
            if result.get("ok"):
                db.update_run(
                    run_id,
                    status="completed",
                    output_text=result.get("code") or output,
                    latency_ms=total_ms,
                    completed_at=db.utc_now(),
                    error="",
                )
            else:
                db.update_run(
                    run_id,
                    status="failed",
                    output_text=result.get("code") or output,
                    latency_ms=total_ms,
                    completed_at=db.utc_now(),
                    error=str(result.get("error") or "Strudel render failed")[:2000],
                )
            return

        if kind == "image":
            output = db.extract_html_document(output)
            db.update_run(
                run_id,
                status="rendering",
                output_text=output,
                latency_ms=latency_ms,
                tokens_prompt=usage.get("prompt_tokens"),
                tokens_completion=usage.get("completion_tokens"),
                cost_usd=cost,
                raw_response=json.dumps(response)[:200_000],
                error="",
            )
            render_started = time.perf_counter()
            result = html_image_render.render_run(run_id, output)
            render_ms = int((time.perf_counter() - render_started) * 1000)
            total_ms = latency_ms + render_ms
            if result.get("ok"):
                db.update_run(
                    run_id,
                    status="completed",
                    output_text=result.get("html") or output,
                    latency_ms=total_ms,
                    completed_at=db.utc_now(),
                    error="",
                )
            else:
                db.update_run(
                    run_id,
                    status="failed",
                    output_text=result.get("html") or output,
                    latency_ms=total_ms,
                    completed_at=db.utc_now(),
                    error=str(result.get("error") or "Image render failed")[:2000],
                )
            return

        db.update_run(
            run_id,
            status="completed",
            output_text=output,
            latency_ms=latency_ms,
            tokens_prompt=usage.get("prompt_tokens"),
            tokens_completion=usage.get("completion_tokens"),
            cost_usd=cost,
            raw_response=json.dumps(response)[:200_000],
            completed_at=db.utc_now(),
            error="",
        )
    except (RunCancelled, Exception) as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - started) * 1000)
        if isinstance(exc, RunCancelled) or is_run_cancelled(run_id):
            db.update_run(
                run_id,
                status="cancelled",
                error="Stopped by user",
                latency_ms=latency_ms,
                completed_at=db.utc_now(),
            )
            return
        db.update_run(
            run_id,
            status="failed",
            error=str(exc)[:2000],
            latency_ms=latency_ms,
            completed_at=db.utc_now(),
            raw_response=traceback.format_exc()[:20_000],
        )
    finally:
        end_active_run(run_id)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[bench-marker] {self.address_string()} {fmt % args}")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path == "/api/health":
                pub = settings_public()
                return json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "bench-marker",
                        **pub,
                    },
                )
            if path == "/api/settings":
                return json_response(self, 200, {"settings": settings_public()})
            if path == "/api/tasks":
                include = qs.get("include_archived", ["0"])[0] in ("1", "true", "yes")
                return json_response(self, 200, {"tasks": db.list_tasks(include)})
            if path.startswith("/api/tasks/"):
                task_id = int(path.split("/")[-1])
                task = db.get_task(task_id)
                if not task:
                    return json_response(self, 404, {"error": "task not found"})
                return json_response(self, 200, {"task": task})
            if path == "/api/models":
                return json_response(self, 200, {"models": db.list_models()})
            if path == "/api/openrouter/models":
                force = qs.get("refresh", ["0"])[0] in ("1", "true", "yes") or qs.get(
                    "t"
                )
                return self._list_openrouter_models(force_refresh=bool(force))
            if path == "/api/runs":
                task_id = int(qs["task_id"][0]) if qs.get("task_id") else None
                model_id = int(qs["model_id"][0]) if qs.get("model_id") else None
                limit = int(qs.get("limit", ["100"])[0])
                runs = db.list_runs(task_id=task_id, model_id=model_id, limit=limit)
                enriched = []
                for run in runs:
                    item = dict(run)
                    rid = int(item["id"])
                    # List payloads are polled often. Drop fields the list UI does not
                    # need; full details remain on GET /api/runs/:id (Source tab).
                    item.pop("raw_response", None)
                    kind = (item.get("task_kind") or "").lower()
                    out = item.get("output_text") or ""
                    item["has_output"] = bool(str(out).strip())
                    # HTML/game previews use /preview; remotion/strudel/image use media
                    # URLs. Omit large source blobs from the list to keep JSON small
                    # and browser-parseable under polling.
                    if kind in ("html", "game", "remotion", "strudel", "image"):
                        item["output_text"] = ""
                    video_file = remotion_render.video_path_for_run(rid)
                    image_file = html_image_render.image_path_for_run(rid)
                    audio_file = strudel_render.audio_path_for_run(rid)
                    item["has_video"] = video_file.exists()
                    item["video_url"] = (
                        f"/api/runs/{rid}/video" if video_file.exists() else None
                    )
                    item["has_image"] = image_file.exists()
                    item["image_url"] = (
                        f"/api/runs/{rid}/image" if image_file.exists() else None
                    )
                    item["has_audio"] = audio_file.exists()
                    item["audio_url"] = (
                        f"/api/runs/{rid}/audio" if audio_file.exists() else None
                    )
                    db.attach_draw_preview(item)
                    enriched.append(item)
                return json_response(self, 200, {"runs": enriched})
            if path.startswith("/api/runs/") and path.endswith("/preview"):
                run_id = int(path.split("/")[-2])
                return self._serve_run_preview(run_id)
            if path.startswith("/api/runs/") and path.endswith("/video"):
                run_id = int(path.split("/")[-2])
                return self._serve_run_video(run_id)
            if path.startswith("/api/runs/") and path.endswith("/image"):
                run_id = int(path.split("/")[-2])
                return self._serve_run_image(run_id)
            if path.startswith("/api/runs/") and path.endswith("/audio"):
                run_id = int(path.split("/")[-2])
                return self._serve_run_audio(run_id)
            if path.startswith("/api/runs/"):
                run_id = int(path.split("/")[-1])
                run = db.get_run(run_id)
                if not run:
                    return json_response(self, 404, {"error": "run not found"})
                # Attach media availability for remotion / image / strudel runs
                video_file = remotion_render.video_path_for_run(run_id)
                image_file = html_image_render.image_path_for_run(run_id)
                audio_file = strudel_render.audio_path_for_run(run_id)
                run = dict(run)
                run["has_video"] = video_file.exists()
                run["video_url"] = f"/api/runs/{run_id}/video" if video_file.exists() else None
                run["has_image"] = image_file.exists()
                run["image_url"] = f"/api/runs/{run_id}/image" if image_file.exists() else None
                run["has_audio"] = audio_file.exists()
                run["audio_url"] = f"/api/runs/{run_id}/audio" if audio_file.exists() else None
                db.attach_draw_preview(run)
                return json_response(self, 200, {"run": run})
            if path == "/api/export":
                return json_response(self, 200, db.export_snapshot())
            if path == "/api/instructions":
                return json_response(
                    self, 200, {"instructions": db.list_instructions()}
                )
            if path.startswith("/api/instructions/"):
                inst_id = int(path.split("/")[-1])
                inst = db.get_instruction(inst_id)
                if not inst:
                    return json_response(self, 404, {"error": "instruction not found"})
                return json_response(self, 200, {"instruction": inst})
            # static files
            return super().do_GET()
        except ValueError as exc:
            return json_response(self, 400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return json_response(self, 500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = read_json(self)
            if path == "/api/settings":
                return self._save_settings(body)
            if path == "/api/settings/test":
                return self._test_openrouter()
            if path == "/api/tasks":
                name = (body.get("name") or "").strip()
                if not name:
                    return json_response(self, 400, {"error": "name is required"})
                task = db.create_task(
                    name=name,
                    description=body.get("description") or "",
                    system_prompt=body.get("system_prompt") or "",
                    user_prompt=body.get("user_prompt") or "",
                    scoring_notes=body.get("scoring_notes") or "",
                )
                return json_response(self, 201, {"task": task})
            if path == "/api/models":
                openrouter_id = (body.get("openrouter_id") or "").strip()
                if not openrouter_id:
                    return json_response(self, 400, {"error": "openrouter_id is required"})
                model = db.create_model(
                    openrouter_id=openrouter_id,
                    display_name=body.get("display_name"),
                    notes=body.get("notes") or "",
                    is_favorite=bool(body.get("is_favorite")),
                )
                return json_response(self, 201, {"model": model})
            if path == "/api/instructions":
                name = (body.get("name") or "").strip()
                if not name:
                    return json_response(self, 400, {"error": "name is required"})
                try:
                    inst = db.create_instruction(
                        name=name,
                        body=body.get("body") or "",
                        notes=body.get("notes") or "",
                    )
                except Exception as exc:  # noqa: BLE001
                    return json_response(self, 400, {"error": str(exc)})
                return json_response(self, 201, {"instruction": inst})
            if path == "/api/runs":
                return self._create_and_start_runs(body)
            if path == "/api/runs/backfill-costs":
                result = backfill_run_costs()
                return json_response(self, 200, {"ok": True, **result})
            if path.startswith("/api/runs/") and path.endswith("/score"):
                run_id = int(path.split("/")[-2])
                if "score" not in body:
                    return json_response(self, 400, {"error": "score is required"})
                run = db.score_run(
                    run_id,
                    float(body["score"]),
                    score_notes=body.get("score_notes") or "",
                )
                if not run:
                    return json_response(self, 404, {"error": "run not found"})
                return json_response(self, 200, {"run": run})
            if path.startswith("/api/runs/") and path.endswith("/stop"):
                run_id = int(path.split("/")[-2])
                run = cancel_run(run_id)
                if not run:
                    return json_response(self, 404, {"error": "run not found"})
                if (run.get("status") or "") != "cancelled":
                    return json_response(
                        self,
                        409,
                        {"error": "run is not in progress", "run": run},
                    )
                print(f"[bench-marker] stop requested for run {run_id}")
                return json_response(self, 200, {"run": run, "stopped": True})
            if path.startswith("/api/runs/") and path.endswith("/rerun"):
                run_id = int(path.split("/")[-2])
                old = db.get_run(run_id)
                if not old:
                    return json_response(self, 404, {"error": "run not found"})
                import secrets

                new_run = db.create_run(
                    task_id=int(old["task_id"]),
                    model_id=int(old["model_id"]),
                    system_prompt_snapshot=old.get("system_prompt_snapshot") or "",
                    user_prompt_snapshot=old.get("user_prompt_snapshot") or "",
                    batch_id=secrets.token_hex(8),
                )
                threading.Thread(
                    target=execute_run, args=(int(new_run["id"]),), daemon=True
                ).start()
                return json_response(self, 201, {"run": new_run})
            return json_response(self, 404, {"error": "not found"})
        except ValueError as exc:
            return json_response(self, 400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return json_response(self, 500, {"error": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802
        return self.do_PATCH()

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = read_json(self)
            if path.startswith("/api/tasks/"):
                task_id = int(path.split("/")[-1])
                task = db.update_task(task_id, **body)
                if not task:
                    return json_response(self, 404, {"error": "task not found"})
                return json_response(self, 200, {"task": task})
            if path.startswith("/api/models/"):
                model_id = int(path.split("/")[-1])
                model = db.update_model(model_id, **body)
                if not model:
                    return json_response(self, 404, {"error": "model not found"})
                return json_response(self, 200, {"model": model})
            if path.startswith("/api/instructions/"):
                inst_id = int(path.split("/")[-1])
                try:
                    inst = db.update_instruction(inst_id, **body)
                except ValueError as exc:
                    return json_response(self, 400, {"error": str(exc)})
                if not inst:
                    return json_response(self, 404, {"error": "instruction not found"})
                return json_response(self, 200, {"instruction": inst})
            return json_response(self, 404, {"error": "not found"})
        except ValueError as exc:
            return json_response(self, 400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return json_response(self, 500, {"error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path.startswith("/api/tasks/"):
                task_id = int(path.split("/")[-1])
                ok = db.delete_task(task_id)
                return json_response(
                    self, 200 if ok else 404, {"ok": ok} if ok else {"error": "not found"}
                )
            if path.startswith("/api/models/"):
                model_id = int(path.split("/")[-1])
                ok = db.delete_model(model_id)
                return json_response(
                    self, 200 if ok else 404, {"ok": ok} if ok else {"error": "not found"}
                )
            if path.startswith("/api/instructions/"):
                inst_id = int(path.split("/")[-1])
                ok = db.delete_instruction(inst_id)
                return json_response(
                    self, 200 if ok else 404, {"ok": ok} if ok else {"error": "not found"}
                )
            if path.startswith("/api/runs/"):
                # soft-delete not implemented; allow hard delete
                run_id = int(path.split("/")[-1])
                conn = db.get_conn()
                try:
                    cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
                    conn.commit()
                    ok = cur.rowcount > 0
                finally:
                    conn.close()
                # Remove rendered media + logs if present
                try:
                    for p in (
                        remotion_render.video_path_for_run(run_id),
                        html_image_render.image_path_for_run(run_id),
                        RENDERS_DIR / f"run_{run_id}.log",
                        RENDERS_DIR / f"run_{run_id}.img.log",
                    ):
                        if p.exists():
                            p.unlink()
                except OSError:
                    pass
                return json_response(
                    self, 200 if ok else 404, {"ok": ok} if ok else {"error": "not found"}
                )
            return json_response(self, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return json_response(self, 500, {"error": str(exc)})

    def _serve_index(self) -> None:
        index = STATIC_DIR / "index.html"
        data = index.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _list_openrouter_models(self, force_refresh: bool = False) -> None:
        try:
            now = time.time()
            cached = OR_MODELS_CACHE.get("models") or []
            fetched_at = float(OR_MODELS_CACHE.get("fetched_at") or 0)
            if (
                not force_refresh
                and cached
                and (now - fetched_at) < OR_MODELS_CACHE_TTL
            ):
                return json_response(
                    self,
                    200,
                    {
                        "models": cached,
                        "count": len(cached),
                        "cached": True,
                        "fetched_at": fetched_at,
                    },
                )

            data = openrouter_request(OPENROUTER_MODELS_URL, method="GET", timeout=30)
            models = data.get("data") or []
            slim = []
            for m in models:
                pricing = m.get("pricing") or {}
                prompt_per_token = _safe_float(pricing.get("prompt"))
                completion_per_token = _safe_float(pricing.get("completion"))
                # OpenRouter returns USD per token; convert to USD per 1M tokens
                prompt_per_m = (
                    prompt_per_token * 1_000_000 if prompt_per_token is not None else None
                )
                completion_per_m = (
                    completion_per_token * 1_000_000
                    if completion_per_token is not None
                    else None
                )
                slim.append(
                    {
                        "id": m.get("id"),
                        "name": m.get("name") or m.get("id"),
                        "context_length": m.get("context_length"),
                        "created": m.get("created"),
                        "pricing": {
                            "prompt": prompt_per_token,
                            "completion": completion_per_token,
                            "prompt_per_million": prompt_per_m,
                            "completion_per_million": completion_per_m,
                        },
                        # Sort helpers for the UI
                        "is_free": bool(
                            (prompt_per_m or 0) == 0 and (completion_per_m or 0) == 0
                        ),
                    }
                )
            slim.sort(key=lambda x: ((x.get("name") or "").lower(), x.get("id") or ""))
            OR_MODELS_CACHE["models"] = slim
            OR_MODELS_CACHE["fetched_at"] = now
            return json_response(
                self,
                200,
                {
                    "models": slim,
                    "count": len(slim),
                    "cached": False,
                    "fetched_at": now,
                },
            )
        except Exception as exc:  # noqa: BLE001
            if OR_MODELS_CACHE.get("models"):
                return json_response(
                    self,
                    200,
                    {
                        "models": OR_MODELS_CACHE["models"],
                        "count": len(OR_MODELS_CACHE["models"]),
                        "cached": True,
                        "stale": True,
                        "error": str(exc),
                    },
                )
            return json_response(self, 502, {"error": str(exc), "models": []})

    def _save_settings(self, body: dict[str, Any]) -> None:
        # Only accept known fields. Empty string clears the UI-saved key / cap.
        touched: list[str] = []
        with SETTINGS_LOCK:
            settings = load_settings()
            if "openrouter_api_key" in body:
                raw = body.get("openrouter_api_key")
                if raw is None:
                    return json_response(
                        self, 400, {"error": "openrouter_api_key is required"}
                    )
                key = str(raw).strip()
                if key:
                    settings["openrouter_api_key"] = key
                else:
                    settings.pop("openrouter_api_key", None)
                touched.append("key")
            if "cost_limit_usd" in body:
                raw_lim = body.get("cost_limit_usd")
                if raw_lim is None or raw_lim == "":
                    settings.pop("cost_limit_usd", None)
                else:
                    parsed = _safe_float(raw_lim)
                    if parsed is None or parsed < 0:
                        return json_response(
                            self, 400, {"error": "cost_limit_usd must be a number ≥ 0"}
                        )
                    if parsed == 0:
                        settings.pop("cost_limit_usd", None)
                    else:
                        settings["cost_limit_usd"] = float(parsed)
                touched.append("cost_limit")
            if not touched:
                return json_response(
                    self,
                    400,
                    {"error": "provide openrouter_api_key and/or cost_limit_usd"},
                )
            save_settings(settings)
        messages = []
        if "key" in touched:
            messages.append(
                "OpenRouter key saved"
                if (load_settings().get("openrouter_api_key") or "").strip()
                else "UI key cleared (env fallback if set)"
            )
        if "cost_limit" in touched:
            cap = cost_limit_usd()
            messages.append(
                f"Cost cap set to ${cap:.4f}" if cap else "Cost cap cleared"
            )
        return json_response(
            self,
            200,
            {
                "ok": True,
                "settings": settings_public(),
                "message": " · ".join(messages),
            },
        )

    def _test_openrouter(self) -> None:
        key = openrouter_key()
        if not key:
            return json_response(
                self, 400, {"ok": False, "error": "No OpenRouter API key configured"}
            )
        try:
            # /models is public and can succeed with a bad key — use auth/key instead
            data = openrouter_request(OPENROUTER_AUTH_URL, method="GET", timeout=20)
            info = data.get("data") or data
            label = (
                info.get("label")
                or info.get("name")
                or info.get("limit_remaining")
                or "authenticated"
            )
            return json_response(
                self,
                200,
                {
                    "ok": True,
                    "message": f"OpenRouter key is valid ({label})",
                    "auth": info,
                    "settings": settings_public(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "401" in msg or "User not found" in msg:
                msg = (
                    "OpenRouter rejected this API key (401 User not found). "
                    "Create a new key at https://openrouter.ai/keys and paste it in Settings."
                )
            return json_response(
                self,
                502,
                {
                    "ok": False,
                    "error": msg[:500],
                    "settings": settings_public(),
                },
            )

    def _serve_run_preview(self, run_id: int) -> None:
        run = db.get_run(run_id)
        if not run:
            return json_response(self, 404, {"error": "run not found"})
        task = db.get_task(int(run["task_id"]))
        html = run.get("output_text") or ""
        kind = (task or {}).get("kind") or ""
        if kind == "html":
            html = db.extract_html_document(html)
        elif kind == "game":
            html = db.wrap_game_preview(db.extract_html_document(html))
        elif kind == "draw":
            svg = db.extract_preview_svg(html)
            html = db.wrap_svg_preview(svg)
        elif kind == "pen":
            cmds = db.extract_pen_commands(html, partial=False)
            html = db.wrap_pen_preview(cmds)
        if not html.strip():
            html = (
                "<!DOCTYPE html><html><body style='font-family:sans-serif;"
                "padding:24px;color:#666'>No HTML output yet.</body></html>"
            )
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # Sandboxed preview: block top-navigation abuse; allow same-doc scripts/styles
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _serve_run_video(self, run_id: int) -> None:
        path = remotion_render.video_path_for_run(run_id)
        if not path.exists():
            return json_response(self, 404, {"error": "video not found"})

        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            try:
                requested = range_header[6:].split(",", 1)[0]
                first, last = requested.split("-", 1)
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    suffix_length = int(last)
                    start = max(size - suffix_length, 0)
                if start < 0 or start > end or start >= size:
                    raise ValueError
            except (TypeError, ValueError):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if range_header.startswith("bytes=") else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("Accept-Ranges", "bytes")
        if range_header.startswith("bytes="):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as video:
            video.seek(start)
            remaining = length
            while remaining:
                chunk = video.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_run_image(self, run_id: int) -> None:
        path = html_image_render.image_path_for_run(run_id)
        if not path.exists():
            return json_response(self, 404, {"error": "image not found"})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_run_audio(self, run_id: int) -> None:
        path = strudel_render.audio_path_for_run(run_id)
        if not path.exists():
            return json_response(self, 404, {"error": "audio not found"})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header(
            "Content-Disposition",
            f'inline; filename="run_{run_id}.mp3"',
        )
        self.end_headers()
        self.wfile.write(data)

    def _create_and_start_runs(self, body: dict[str, Any]) -> None:
        task_id = body.get("task_id")
        if task_id is None:
            return json_response(self, 400, {"error": "task_id is required"})
        task = db.get_task(int(task_id))
        if not task:
            return json_response(self, 404, {"error": "task not found"})

        model_ids: list[int] = []
        if body.get("model_id") is not None:
            model_ids.append(int(body["model_id"]))
        for mid in body.get("model_ids") or []:
            model_ids.append(int(mid))
        # allow openrouter_ids directly
        for oid in body.get("openrouter_ids") or []:
            model = db.create_model(str(oid))
            model_ids.append(int(model["id"]))
        if body.get("openrouter_id"):
            model = db.create_model(str(body["openrouter_id"]))
            model_ids.append(int(model["id"]))

        # dedupe preserve order
        seen: set[int] = set()
        unique_ids: list[int] = []
        for mid in model_ids:
            if mid not in seen:
                seen.add(mid)
                unique_ids.append(mid)
        if not unique_ids:
            return json_response(
                self, 400, {"error": "provide model_id, model_ids, or openrouter_id(s)"}
            )

        import secrets

        # Add model(s) to an existing test: reuse exact prompts + batch id
        reuse_from_run_id = body.get("reuse_from_run_id") or body.get("from_run_id")
        requested_batch_id = (body.get("batch_id") or "").strip()
        if reuse_from_run_id is not None:
            source = db.get_run(int(reuse_from_run_id))
            if not source:
                return json_response(self, 404, {"error": "source run not found"})
            if int(source["task_id"]) != int(task_id):
                return json_response(
                    self, 400, {"error": "source run belongs to a different task"}
                )
            system_prompt = source.get("system_prompt_snapshot") or ""
            user_prompt = source.get("user_prompt_snapshot") or ""
            if not (user_prompt or "").strip() and not (system_prompt or "").strip():
                return json_response(
                    self, 400, {"error": "source run has no prompt snapshot to reuse"}
                )
            batch_id = (
                (source.get("batch_id") or "").strip()
                or requested_batch_id
                or secrets.token_hex(8)
            )
            # Promote legacy concurrent groups so new models land in the same section
            if not (source.get("batch_id") or "").strip():
                db.assign_batch_id_to_run_group(int(source["id"]), batch_id)
            created = []
            for mid in unique_ids:
                if not db.get_model(mid):
                    return json_response(self, 404, {"error": f"model {mid} not found"})
                run = db.create_run(
                    task_id=int(task_id),
                    model_id=mid,
                    system_prompt_snapshot=str(system_prompt),
                    user_prompt_snapshot=str(user_prompt),
                    batch_id=batch_id,
                )
                created.append(run)
                threading.Thread(
                    target=execute_run, args=(int(run["id"]),), daemon=True
                ).start()
            return json_response(
                self,
                201,
                {
                    "runs": created,
                    "count": len(created),
                    "batch_id": batch_id,
                    "reused_from_run_id": int(source["id"]),
                },
            )

        system_prompt = body.get("system_prompt")
        user_prompt = body.get("user_prompt")
        if system_prompt is None:
            system_prompt = task.get("system_prompt") or ""
        if user_prompt is None:
            user_prompt = task.get("user_prompt") or ""

        kind = (task.get("kind") or "text").lower()
        if kind == "html":
            brief = (user_prompt or "").strip()
            if not brief:
                return json_response(
                    self,
                    400,
                    {"error": "Provide a page brief / prompt for the HTML benchmark"},
                )
            # Always re-apply format rules so every model returns renderable HTML
            user_prompt = db.build_html_user_prompt(brief)
            if not (system_prompt or "").strip():
                system_prompt = db.HTML_SYSTEM_PROMPT
        elif kind == "game":
            game_name = (
                body.get("game")
                or body.get("game_name")
                or "Tetris"
            )
            game_name = str(game_name).strip() or "Tetris"
            allowed = {"Tetris", "Pac-Man", "Flappy Bird"}
            if game_name not in allowed:
                # Accept loose casing / separators
                lowered = game_name.lower().replace("_", " ").replace("-", " ")
                alias = {
                    "tetris": "Tetris",
                    "pac man": "Pac-Man",
                    "pacman": "Pac-Man",
                    "flappy bird": "Flappy Bird",
                    "flappybird": "Flappy Bird",
                }.get(lowered)
                if not alias:
                    return json_response(
                        self,
                        400,
                        {"error": "game must be one of: Tetris, Pac-Man, Flappy Bird"},
                    )
                game_name = alias
            brief = (user_prompt or "").strip()
            user_prompt = db.build_game_user_prompt(game_name, brief)
            if not (system_prompt or "").strip():
                system_prompt = db.GAME_SYSTEM_PROMPT
        elif kind == "remotion":
            brief = (user_prompt or "").strip()
            if not brief:
                return json_response(
                    self,
                    400,
                    {"error": "Provide a motion brief / prompt for the Remotion benchmark"},
                )
            user_prompt = db.build_remotion_user_prompt(brief)
            if not (system_prompt or "").strip():
                system_prompt = db.REMOTION_SYSTEM_PROMPT
        elif kind == "text":
            # Free-form: instruction (system) + prompt (user). Both come from UI.
            # Optional instruction_id loads a saved skill if body fields omitted.
            if body.get("instruction_id") is not None and not (system_prompt or "").strip():
                inst = db.get_instruction(int(body["instruction_id"]))
                if not inst:
                    return json_response(self, 404, {"error": "instruction not found"})
                system_prompt = inst.get("body") or ""
            user_prompt = (user_prompt or "").strip()
            system_prompt = (system_prompt or "").strip()
            if not user_prompt:
                return json_response(
                    self,
                    400,
                    {"error": "Provide a user prompt for the text benchmark"},
                )
            if not system_prompt:
                return json_response(
                    self,
                    400,
                    {"error": "Provide or select an instruction/skill for the text benchmark"},
                )
        elif kind == "image":
            brief = (user_prompt or "").strip()
            if not brief:
                return json_response(
                    self,
                    400,
                    {"error": "Provide a graphic brief / prompt for the square image benchmark"},
                )
            user_prompt = db.build_square_image_user_prompt(brief)
            if not (system_prompt or "").strip():
                system_prompt = db.SQUARE_IMAGE_SYSTEM_PROMPT
        elif kind == "draw":
            brief = (user_prompt or "").strip()
            if not brief:
                return json_response(
                    self,
                    400,
                    {"error": "Provide a subject / prompt for the live SVG draw benchmark"},
                )
            user_prompt = db.build_draw_user_prompt(brief)
            if not (system_prompt or "").strip():
                system_prompt = db.DRAW_SYSTEM_PROMPT
        elif kind == "pen":
            brief = (user_prompt or "").strip()
            if not brief:
                return json_response(
                    self,
                    400,
                    {"error": "Provide a subject / prompt for the live pen drawing benchmark"},
                )
            user_prompt = db.build_pen_user_prompt(brief)
            if not (system_prompt or "").strip():
                system_prompt = db.PEN_SYSTEM_PROMPT
        elif kind == "strudel":
            brief = (user_prompt or "").strip()
            if not brief:
                return json_response(
                    self,
                    400,
                    {"error": "Provide a song brief / prompt for the Strudel MP3 benchmark"},
                )
            user_prompt = db.build_strudel_user_prompt(brief)
            if not (system_prompt or "").strip():
                system_prompt = db.STRUDEL_SYSTEM_PROMPT

        batch_id = requested_batch_id or secrets.token_hex(8)
        created = []
        for mid in unique_ids:
            if not db.get_model(mid):
                return json_response(self, 404, {"error": f"model {mid} not found"})
            run = db.create_run(
                task_id=int(task_id),
                model_id=mid,
                system_prompt_snapshot=str(system_prompt),
                user_prompt_snapshot=str(user_prompt),
                batch_id=batch_id,
            )
            created.append(run)
            threading.Thread(
                target=execute_run, args=(int(run["id"]),), daemon=True
            ).start()

        return json_response(
            self,
            201,
            {"runs": created, "count": len(created), "batch_id": batch_id},
        )


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    """Allow quick restarts without TIME_WAIT bind failures."""

    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    db.init_db()
    db.seed_defaults()
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    try:
        bf = backfill_run_costs()
        if bf.get("updated"):
            print(
                f"Backfilled costs on {bf['updated']} run(s) "
                f"(raw={bf['from_raw_response']}, estimate={bf['from_token_estimate']})"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"Cost backfill skipped: {exc}")

    # Bind first. Failed start attempts must NOT mark live runs as interrupted —
    # that was killing tests whenever another process held the port.
    try:
        server = ReusableThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(
            f"Bench Marker failed to bind {HOST}:{PORT}: {exc}\n"
            "Another process is likely using this port. "
            "In-flight runs were left untouched."
        )
        raise SystemExit(1) from exc

    orphaned = db.fail_orphaned_runs()
    if orphaned:
        print(f"Marked {orphaned} in-flight run(s) as failed after restart")

    print(f"Bench Marker listening on http://{HOST}:{PORT}/")
    print(f"OpenRouter configured: {bool(openrouter_key())}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
