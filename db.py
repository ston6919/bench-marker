"""SQLite persistence for Bench Marker."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get("BENCH_MARKER_DATA_DIR", str(BASE_DIR / "data"))
).expanduser()
DB_PATH = Path(
    os.environ.get("BENCH_MARKER_DB", str(DATA_DIR / "bench.db"))
).expanduser()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'text',
                system_prompt TEXT NOT NULL DEFAULT '',
                user_prompt TEXT NOT NULL DEFAULT '',
                scoring_notes TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                openrouter_id TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                is_favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                system_prompt_snapshot TEXT NOT NULL DEFAULT '',
                user_prompt_snapshot TEXT NOT NULL DEFAULT '',
                output_text TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                latency_ms INTEGER,
                tokens_prompt INTEGER,
                tokens_completion INTEGER,
                cost_usd REAL,
                raw_response TEXT NOT NULL DEFAULT '',
                score REAL,
                score_notes TEXT NOT NULL DEFAULT '',
                scored_at TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                pen_color TEXT NOT NULL DEFAULT '#1c1915',
                batch_id TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
            CREATE INDEX IF NOT EXISTS idx_runs_model ON runs(model_id);
            CREATE INDEX IF NOT EXISTS idx_runs_score ON runs(task_id, score);

            CREATE TABLE IF NOT EXISTS instructions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        # Migrations for existing DBs created before `kind`
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "kind" not in cols:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'"
            )
        run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "pen_color" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN pen_color TEXT NOT NULL DEFAULT '#1c1915'")
        if "batch_id" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id)"
            )
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_to_list(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]


# --- Tasks ---


def list_tasks(include_archived: bool = False) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        if include_archived:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY archived ASC, updated_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE archived = 0 ORDER BY updated_at DESC"
            ).fetchall()
        return rows_to_list(rows)
    finally:
        conn.close()


def get_task(task_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        return row_to_dict(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )
    finally:
        conn.close()


def create_task(
    name: str,
    description: str = "",
    kind: str = "text",
    system_prompt: str = "",
    user_prompt: str = "",
    scoring_notes: str = "",
) -> dict[str, Any]:
    now = utc_now()
    kind_norm = (kind or "text").strip().lower() or "text"
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO tasks (
                name, description, kind, system_prompt, user_prompt, scoring_notes,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip(),
                description,
                kind_norm,
                system_prompt,
                user_prompt,
                scoring_notes,
                now,
                now,
            ),
        )
        conn.commit()
        return get_task(int(cur.lastrowid))  # type: ignore[return-value]
    finally:
        conn.close()


def update_task(task_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {
        "name",
        "description",
        "kind",
        "system_prompt",
        "user_prompt",
        "scoring_notes",
        "archived",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_task(task_id)
    if "name" in updates and isinstance(updates["name"], str):
        updates["name"] = updates["name"].strip()
    updates["updated_at"] = utc_now()
    cols = ", ".join(f"{k} = ?" for k in updates)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE tasks SET {cols} WHERE id = ?",
            (*updates.values(), task_id),
        )
        conn.commit()
        return get_task(task_id)
    finally:
        conn.close()


def delete_task(task_id: int) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Models ---


def list_models() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM models ORDER BY is_favorite DESC, display_name ASC"
        ).fetchall()
        return rows_to_list(rows)
    finally:
        conn.close()


def get_model(model_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        return row_to_dict(
            conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        )
    finally:
        conn.close()


def get_model_by_openrouter_id(openrouter_id: str) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM models WHERE openrouter_id = ?", (openrouter_id,)
            ).fetchone()
        )
    finally:
        conn.close()


def create_model(
    openrouter_id: str,
    display_name: str | None = None,
    notes: str = "",
    is_favorite: bool = False,
) -> dict[str, Any]:
    oid = openrouter_id.strip()
    name = (display_name or oid).strip()
    existing = get_model_by_openrouter_id(oid)
    if existing:
        return existing
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO models (openrouter_id, display_name, notes, is_favorite, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (oid, name, notes, 1 if is_favorite else 0, utc_now()),
        )
        conn.commit()
        return get_model(int(cur.lastrowid))  # type: ignore[return-value]
    finally:
        conn.close()


def update_model(model_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {"openrouter_id", "display_name", "notes", "is_favorite"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_model(model_id)
    if "is_favorite" in updates:
        updates["is_favorite"] = 1 if updates["is_favorite"] else 0
    cols = ", ".join(f"{k} = ?" for k in updates)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE models SET {cols} WHERE id = ?",
            (*updates.values(), model_id),
        )
        conn.commit()
        return get_model(model_id)
    finally:
        conn.close()


def delete_model(model_id: int) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM models WHERE id = ?", (model_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Runs ---


def list_runs(
    task_id: int | None = None,
    model_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("r.task_id = ?")
            params.append(task_id)
        if model_id is not None:
            clauses.append("r.model_id = ?")
            params.append(model_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT r.*,
                   t.name AS task_name,
                   t.kind AS task_kind,
                   m.openrouter_id AS model_openrouter_id,
                   m.display_name AS model_display_name
            FROM runs r
            JOIN tasks t ON t.id = r.task_id
            JOIN models m ON m.id = r.model_id
            {where}
            ORDER BY r.created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return rows_to_list(rows)
    finally:
        conn.close()


def get_run(run_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT r.*,
                   t.name AS task_name,
                   t.kind AS task_kind,
                   m.openrouter_id AS model_openrouter_id,
                   m.display_name AS model_display_name
            FROM runs r
            JOIN tasks t ON t.id = r.task_id
            JOIN models m ON m.id = r.model_id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


def assign_batch_id_to_run_group(source_run_id: int, batch_id: str) -> int:
    """Ensure a concurrent test group shares ``batch_id`` (promotes legacy groups)."""
    batch_id = (batch_id or "").strip()
    if not batch_id:
        return 0
    source = get_run(int(source_run_id))
    if not source:
        return 0
    existing = (source.get("batch_id") or "").strip()
    if existing:
        # Already batched — nothing to promote
        return 0
    conn = get_conn()
    try:
        # Match the same concurrent group: task + prompts + same created_at second
        created = str(source.get("created_at") or "")[:19]
        cur = conn.execute(
            """
            UPDATE runs
            SET batch_id = ?
            WHERE task_id = ?
              AND (batch_id IS NULL OR batch_id = '')
              AND IFNULL(system_prompt_snapshot, '') = ?
              AND IFNULL(user_prompt_snapshot, '') = ?
              AND substr(IFNULL(created_at, ''), 1, 19) = ?
            """,
            (
                batch_id,
                int(source["task_id"]),
                source.get("system_prompt_snapshot") or "",
                source.get("user_prompt_snapshot") or "",
                created,
            ),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def create_run(
    task_id: int,
    model_id: int,
    system_prompt_snapshot: str = "",
    user_prompt_snapshot: str = "",
    pen_color: str = "#1c1915",
    batch_id: str = "",
) -> dict[str, Any]:
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO runs (
                task_id, model_id, status,
                system_prompt_snapshot, user_prompt_snapshot,
                created_at, pen_color, batch_id
            ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                model_id,
                system_prompt_snapshot,
                user_prompt_snapshot,
                utc_now(),
                pen_color if re.fullmatch(r"#[0-9a-fA-F]{6}", pen_color or "") else "#1c1915",
                (batch_id or "").strip(),
            ),
        )
        conn.commit()
        return get_run(int(cur.lastrowid))  # type: ignore[return-value]
    finally:
        conn.close()


def update_run(run_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {
        "status",
        "output_text",
        "error",
        "latency_ms",
        "tokens_prompt",
        "tokens_completion",
        "cost_usd",
        "raw_response",
        "score",
        "score_notes",
        "scored_at",
        "completed_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_run(run_id)
    cols = ", ".join(f"{k} = ?" for k in updates)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE runs SET {cols} WHERE id = ?",
            (*updates.values(), run_id),
        )
        conn.commit()
        return get_run(run_id)
    finally:
        conn.close()


def score_run(run_id: int, score: float, score_notes: str = "") -> dict[str, Any] | None:
    if score < 0 or score > 10:
        raise ValueError("score must be between 0 and 10")
    return update_run(
        run_id,
        score=float(score),
        score_notes=score_notes or "",
        scored_at=utc_now(),
    )


HTML_SYSTEM_PROMPT = """You are an expert front-end engineer who builds polished, self-contained HTML pages.

Always produce a complete standalone HTML document that works when opened in a browser with no build step.
Prefer inline CSS in a <style> tag. Use vanilla JS only if needed. Avoid external dependencies unless the brief requires them.
Make the design clean, modern, and responsive."""

HTML_OUTPUT_RULES = """
---
MANDATORY OUTPUT FORMAT
1. Reply with ONLY HTML. No markdown, no explanations, no preamble, no closing notes.
2. Do NOT wrap the HTML in triple backticks or any code fence.
3. The very first non-whitespace characters of your reply must be either <!DOCTYPE html> or <html.
4. Include a full document: doctype, <html>, <head> (with <meta charset="utf-8"> and a <title>), <body>, and matching closing tags.
5. End your reply with </html> and nothing after it.
---
""".strip()


REMOTION_SYSTEM_PROMPT = """You are an expert motion designer who writes Remotion (React) compositions.

You write clean TypeScript/TSX for Remotion 4 using only:
- react
- remotion (AbsoluteFill, Sequence, Series, interpolate, spring, useCurrentFrame, useVideoConfig, Easing, random, etc.)

Constraints for this environment:
- No external assets, images, fonts URLs, or network requests
- No third-party packages beyond react + remotion
- Prefer AbsoluteFill + inline styles
- Keep animations smooth and readable
- Composition size is 960×540 at 30fps, about 3–5 seconds (90–150 frames)"""

REMOTION_OUTPUT_RULES = """
---
MANDATORY OUTPUT FORMAT
1. Reply with ONLY a single TSX module. No markdown fences, no commentary.
2. You MUST export all of:
   - export const FPS = 30;
   - export const WIDTH = 960;
   - export const HEIGHT = 540;
   - export const DURATION_IN_FRAMES = <90-150>;
   - export const MotionGraphic: React.FC = () => { ... };
3. Import from "remotion" and "react" only.
4. Do not call registerRoot or Composition — only define MotionGraphic + constants.
5. No fetch, no img remote URLs, no require of local files.
---
""".strip()


GAME_SYSTEM_PROMPT = """You are an expert JavaScript game developer. Build a complete, playable browser game as one standalone HTML document. Use canvas or DOM, inline CSS and vanilla JavaScript only. Include keyboard and pointer controls, a restart button, visible score/state, and responsive layout. Do not use external assets or network requests."""


def build_game_user_prompt(game: str, brief: str) -> str:
    game = (game or "Tetris").strip()
    brief = (brief or "").strip()
    return build_html_user_prompt(
        f"Create the {game} game. {brief}\n"
        "The game must be immediately playable when the HTML preview opens."
    )


def seed_defaults() -> None:
    """Starter models + built-in benchmarks."""
    defaults = [
        ("openai/gpt-4o-mini", "GPT-4o Mini"),
        ("anthropic/claude-3.5-sonnet", "Claude 3.5 Sonnet"),
        ("google/gemini-2.0-flash-001", "Gemini 2.0 Flash"),
        ("deepseek/deepseek-chat", "DeepSeek Chat"),
        ("openai/gpt-4o", "GPT-4o"),
        ("anthropic/claude-sonnet-4", "Claude Sonnet 4"),
    ]
    for oid, name in defaults:
        if not get_model_by_openrouter_id(oid):
            create_model(oid, name)

    kinds = {t.get("kind") for t in list_tasks(include_archived=True)}

    if "html" not in kinds:
        create_task(
            name="HTML page generation",
            kind="html",
            description=(
                "Paste a brief for a web page or UI. Selected models each generate a full "
                "HTML document. Results are rendered live so you can compare layout, design, "
                "and quality side by side."
            ),
            system_prompt=HTML_SYSTEM_PROMPT,
            user_prompt="",
            scoring_notes=(
                "Score 0–10 on: visual design, layout structure, responsiveness, "
                "how well it matches the brief, polish, and usable HTML (no broken markup)."
            ),
        )

    if "game" not in kinds:
        create_task(
            name="Game maker (Tetris / Pac-Man / Flappy Bird)",
            kind="game",
            description="Build a playable browser game. Choose Tetris, Pac-Man, or Flappy Bird; all workloads stay in this tab.",
            system_prompt=GAME_SYSTEM_PROMPT,
            scoring_notes="Score 0–10 on playability, controls, rules, visual clarity, polish, and recognisability.",
        )

    if "remotion" not in kinds:
        create_task(
            name="Remotion motion graphic",
            kind="remotion",
            description=(
                "Paste a brief for a short motion graphic. Models write Remotion (React) code; "
                "this server renders each composition to MP4 so you can compare the videos."
            ),
            system_prompt=REMOTION_SYSTEM_PROMPT,
            user_prompt="",
            scoring_notes=(
                "Score 0–10 on: motion design quality, clarity, match to brief, timing, "
                "polish, and whether the render looks intentional (not broken)."
            ),
        )

    if "text" not in kinds:
        create_task(
            name="Text + skills",
            kind="text",
            description=(
                "Run free-form text tasks. Pick or save an instruction (skill), enter a prompt, "
                "and compare model outputs side by side."
            ),
            system_prompt="",
            user_prompt="",
            scoring_notes=(
                "Score 0–10 on: instruction following, quality of writing, usefulness, "
                "and consistency with the skill/instruction you selected."
            ),
        )

    if "image" not in kinds:
        create_task(
            name="Square image graphic",
            kind="image",
            description=(
                "Describe a square graphic. Models generate HTML; this server screenshots it "
                "to a 1080×1080 PNG so you can compare the images."
            ),
            system_prompt=SQUARE_IMAGE_SYSTEM_PROMPT,
            user_prompt="",
            scoring_notes=(
                "Score 0–10 on: composition, typography, colour, match to the brief, "
                "polish, and how well it works as a square social/graphic asset."
            ),
        )

    if "draw" not in kinds:
        create_task(
            name="Live SVG draw",
            kind="draw",
            description=(
                "Describe a thing to draw. Models must reply with only SVG. "
                "You watch the picture appear live as tokens stream in."
            ),
            system_prompt=DRAW_SYSTEM_PROMPT,
            user_prompt="",
            scoring_notes=(
                "Score 0–10 on: recognisability, composition, craft, how well it matches "
                "the subject, and whether the SVG looks intentional (not broken)."
            ),
        )

    if "pen" not in kinds:
        create_task(
            name="Live pen drawing",
            kind="pen",
            description=(
                "Describe a thing to sketch. Models output pen coordinates "
                "(down / move / up) and you watch the line travel like a hand on paper."
            ),
            system_prompt=PEN_SYSTEM_PROMPT,
            user_prompt="",
            scoring_notes=(
                "Score 0–10 on: recognisability, line quality, how natural the drawing "
                "order feels, and whether it reads as a hand-drawn sketch of the subject."
            ),
        )
    else:
        for task in list_tasks(include_archived=True):
            if task.get("kind") == "pen" and task.get("name") == "Live pen drawing":
                update_task(int(task["id"]), system_prompt=PEN_SYSTEM_PROMPT)

    if "strudel" not in kinds:
        create_task(
            name="Strudel song → MP3",
            kind="strudel",
            description=(
                "Describe a song. Models write Strudel (live-coding) code; this server "
                "renders each piece to an MP3 so you only hear the music side by side."
            ),
            system_prompt=STRUDEL_SYSTEM_PROMPT,
            user_prompt="",
            scoring_notes=(
                "Score 0–10 on: musicality, how well it matches the brief (genre, mood, "
                "energy), arrangement interest, sound design, and whether the render "
                "feels intentional (not empty, broken, or one-note spam)."
            ),
        )
    else:
        for task in list_tasks(include_archived=True):
            if task.get("kind") == "strudel" and task.get("name") == "Strudel song → MP3":
                update_task(int(task["id"]), system_prompt=STRUDEL_SYSTEM_PROMPT)

def build_html_user_prompt(brief: str) -> str:
    """Wrap the user's brief with format rules models must follow."""
    brief = (brief or "").strip()
    return (
        "Build a complete standalone HTML page from this brief:\n\n"
        f"{brief}\n\n"
        f"{HTML_OUTPUT_RULES}"
    )


def build_remotion_user_prompt(brief: str) -> str:
    """Wrap the user's motion brief with Remotion output rules."""
    brief = (brief or "").strip()
    return (
        "Create a short motion graphic as a Remotion TSX composition for this brief:\n\n"
        f"{brief}\n\n"
        "Aim for a polished 3–5 second piece (90–150 frames at 30fps), 960×540.\n"
        "Use spring/interpolate for motion. Keep text large and legible.\n\n"
        f"{REMOTION_OUTPUT_RULES}"
    )


STRUDEL_SYSTEM_PROMPT = """You are an expert live coder who writes music in Strudel (TidalCycles-in-JS).

You compose short, complete pieces that render cleanly in the Strudel WebAudio engine
(superdough). Your code is evaluated offline and exported to MP3 — there is no human
editing step.

Sound palette available at render time (prefer these):
- Drum samples: s("bd sd hh oh cp rim") and dirt-samples banks (bd, sd, hh, cp, …)
- Synths: .s("sawtooth" | "square" | "triangle" | "sine" | "supersaw" | "piano" | zzfx names)
- Helpers: note(), n(), s(), stack, cat, seq, slow, fast, gain, lpf, hpf, room, delay, jux, every, sometimes, struct, bank, etc.
- Tempo: setcpm(N) sets cycles-per-minute. For ~120 BPM with 4 beats per cycle use setcpm(120/4).

Composition rules:
- Write a self-contained song: drums + bass and/or melody (at least two layers)
- Keep patterns looping musically; use variation (every / sometimes / slow) so 15–20s is interesting
- Avoid external sample URLs, network fetches, Hydra visuals, or speech/shabda
- Prefer moderate gains (0.3–0.9) so the mix does not clip hard
- Prefer musical keys and simple chord motion over random notes"""

STRUDEL_OUTPUT_RULES = """
---
MANDATORY OUTPUT FORMAT
1. Reply with ONLY Strudel JavaScript code. No markdown fences, no commentary, no titles.
2. Start with a tempo line, e.g. setcpm(120/4)
3. Define the piece with one or more `$:` pattern lines, for example:
   setcpm(120/4)
   $: s("bd sd bd [sd cp]").gain(0.8)
   $: note("c2 c2 eb2 g2").s("sawtooth").lpf(800).gain(0.45)
   $: note("c4 eb4 g4 bb4").s("triangle").slow(2).room(0.3).gain(0.35)
4. Do not wrap code in ``` fences.
5. Do not call samples() with remote URLs (banks are preloaded).
6. Do not output explanations before or after the code.
---
""".strip()


def build_strudel_user_prompt(brief: str) -> str:
    """Wrap the user's song brief with Strudel output rules for reliable MP3 renders."""
    brief = (brief or "").strip()
    return (
        "Write a complete Strudel song for this brief:\n\n"
        f"{brief}\n\n"
        "Target a musical loop that still feels good when rendered for about 16 seconds.\n"
        "Include rhythm + at least one pitched part. Make it match the genre/mood in the brief.\n\n"
        f"{STRUDEL_OUTPUT_RULES}"
    )


SQUARE_IMAGE_SYSTEM_PROMPT = """You design polished square graphics as self-contained HTML pages.

The page will be screenshotted at exactly 1080×1080 pixels and used as a static image
(social post, thumbnail, quote card, ad creative, etc.).

Rules:
- One fixed square canvas, no scrolling, no responsive reflow below 1080px
- Self-contained: inline CSS only (no external stylesheets, fonts, images, or scripts that fetch network resources)
- System fonts or pure CSS shapes are fine
- High visual polish: strong hierarchy, good contrast, intentional layout
- Fill the entire 1080×1080 frame"""

SQUARE_IMAGE_OUTPUT_RULES = """
---
MANDATORY OUTPUT FORMAT
1. Reply with ONLY a complete HTML document. No markdown fences, no commentary.
2. First non-whitespace characters must be <!DOCTYPE html> or <html.
3. Include <meta charset="utf-8"> and a <title>.
4. The visible design MUST be exactly 1080px by 1080px:
   - Set html, body { margin:0; width:1080px; height:1080px; overflow:hidden; }
   - Put the graphic inside a root container of 1080×1080 that fills the viewport
5. Use only inline <style> CSS. No external URLs (no Google Fonts, no remote images).
6. No JavaScript required. Decorative pure-CSS is encouraged.
7. End with </html> and nothing after it.
---
""".strip()


def build_square_image_user_prompt(brief: str) -> str:
    """Wrap the user's graphic brief with square-HTML screenshot rules."""
    brief = (brief or "").strip()
    return (
        "Design a single square graphic (1080×1080) as HTML for this brief:\n\n"
        f"{brief}\n\n"
        "Make it look like a finished social/creative asset, not a website page.\n"
        "Text should be large and legible at a glance.\n\n"
        f"{SQUARE_IMAGE_OUTPUT_RULES}"
    )


def extract_html_document(text: str) -> str:
    """Normalize model output into something we can render in an iframe."""
    import re

    if not text:
        return ""

    cleaned = text.strip()

    # Strip common markdown fences (```html ... ``` or ``` ... ```)
    fence = re.match(
        r"^```(?:html|HTML)?\s*\n([\s\S]*?)\n?```\s*$",
        cleaned,
    )
    if fence:
        cleaned = fence.group(1).strip()
    else:
        # Partial fence: leading ```html / trailing ```
        cleaned = re.sub(r"^```(?:html|HTML)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()

    lower = cleaned.lower()
    start = -1
    for marker in ("<!doctype html", "<html"):
        idx = lower.find(marker)
        if idx != -1 and (start == -1 or idx < start):
            start = idx
    end = lower.rfind("</html>")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + len("</html>")]
    elif start != -1:
        cleaned = cleaned[start:]

    # If still not a document, wrap fragment so preview is usable
    if "<html" not in cleaned.lower():
        cleaned = (
            "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\" />\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
            "<title>Model output</title>\n"
            "</head>\n<body>\n"
            f"{cleaned}\n"
            "</body>\n</html>"
        )
    elif not cleaned.lstrip().lower().startswith("<!doctype"):
        cleaned = "<!DOCTYPE html>\n" + cleaned

    return cleaned


DRAW_SYSTEM_PROMPT = """You are an expert illustrator who draws with SVG.

You produce a single self-contained SVG illustration of the subject.
The picture must be recognisable, well composed, and fill a square canvas.

Rules:
- Output SVG only — no HTML page, no markdown, no commentary
- Square canvas: viewBox="0 0 1024 1024"
- xmlns="http://www.w3.org/2000/svg"
- No <image>, no external URLs, no fonts from the network, no scripts
- Prefer path, circle, rect, ellipse, polygon, polyline, line, g, and text
- Use filled shapes and strokes. Colour is encouraged.
- Draw the whole subject, not a tiny icon in the corner"""

DRAW_OUTPUT_RULES = """
---
MANDATORY OUTPUT FORMAT
1. Reply with ONLY an SVG document. No markdown, no explanations, no preamble.
2. Do NOT wrap the SVG in triple backticks or any code fence.
3. The very first non-whitespace characters of your reply must be <svg
4. The root element must include viewBox="0 0 1024 1024" and xmlns="http://www.w3.org/2000/svg".
5. No <image>, no <foreignObject>, no <script>, no external href/src.
6. End your reply with </svg> and nothing after it.
---
""".strip()

DRAW_OUTPUT_CAP = 400_000

_SVG_ALLOWED_TAGS = {
    "svg",
    "g",
    "path",
    "circle",
    "rect",
    "ellipse",
    "polygon",
    "polyline",
    "line",
    "text",
    "tspan",
    "defs",
    "clippath",
    "mask",
    "lineargradient",
    "radialgradient",
    "stop",
    "use",
    "title",
    "desc",
    "style",
    "symbol",
    "marker",
    "pattern",
    "filter",
    "fegaussianblur",
    "feoffset",
    "femerge",
    "femergenode",
    "feblend",
    "fecolormatrix",
    "feflood",
    "fecomposite",
    "fedropshadow",
}

_SVG_FORBIDDEN_TAGS = {
    "script",
    "foreignobject",
    "iframe",
    "object",
    "embed",
    "image",
    "link",
    "meta",
    "video",
    "audio",
    "canvas",
    "html",
    "body",
    "a",
}

_SVG_URL_ATTRS = {"href", "xlink:href", "src"}


def build_draw_user_prompt(brief: str) -> str:
    """Wrap the user's drawing subject with SVG-only output rules."""
    brief = (brief or "").strip()
    return (
        "Draw this subject as a single square SVG illustration:\n\n"
        f"{brief}\n\n"
        "Make it immediately recognisable. Fill the 1024×1024 canvas.\n\n"
        f"{DRAW_OUTPUT_RULES}"
    )


PEN_SYSTEM_PROMPT = """You draw with a pen on paper.

You output a sequence of pen commands so a viewer can watch the line move,
as if a person is holding a pen and sketching by hand.

The paper is a square 1024×1024 canvas. Origin is the top-left. Coordinates are integers.

Draw recognisable, simple line drawings. Use many short TO steps along curves
so the pen travels naturally. Lift the pen between separate strokes.

Do not narrate, plan, or list coordinates in analysis. The only thing you write
is the command stream itself."""

PEN_OUTPUT_RULES = """
---
MANDATORY OUTPUT FORMAT
Your entire reply is the command stream. Nothing else.

Do not think out loud. Do not write "I'll draw…" or explain strokes.
Do not put coordinates in a scratchpad or analysis — only in these commands.

Commands, one per line:
- DOWN x y   put the pen on the paper at (x,y)
- TO x y     drag the pen to (x,y) — this draws a line
- UP         lift the pen off the paper
- END        finished

Rules:
1. First non-empty line must be a DOWN command.
2. Use many TO points (dozens per stroke). Do not jump across the page in one TO.
3. Lift with UP between separate strokes (eyes, then mouth, then outline, etc.).
4. Stay inside 0–1024 on both axes.
5. Do not output SVG, HTML, JSON, markdown, or explanations.
---
""".strip()


def build_pen_user_prompt(brief: str) -> str:
    """Wrap the user's subject with pen-command output rules."""
    brief = (brief or "").strip()
    return (
        "Sketch this subject as a line drawing with a pen on paper:\n\n"
        f"{brief}\n\n"
        "Make it immediately recognisable from a few strokes. "
        "Draw as a person would: outline first, then details.\n\n"
        f"{PEN_OUTPUT_RULES}"
    )


def _clamp_pen_coord(value: Any) -> int | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN
        return None
    return int(max(0, min(1024, round(num))))


def _parse_pen_line(line: str) -> dict[str, Any] | None:
    raw = (line or "").strip().strip(",;")
    if not raw or raw.startswith("#") or raw.startswith("//"):
        return None
    if raw.startswith("```"):
        return None
    lower = raw.lower()
    if lower in {"up", "u", "penup", "pen_up", "lift"}:
        return {"op": "up"}
    if lower in {"end", "done", "finish"}:
        return {"op": "end"}
    if raw[0] in "{[":
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        op = str(obj.get("op") or obj.get("cmd") or "").lower()
        x = _clamp_pen_coord(obj.get("x"))
        y = _clamp_pen_coord(obj.get("y"))
        if op in {"up", "u"}:
            return {"op": "up"}
        if op in {"end", "done"}:
            return {"op": "end"}
        if op in {"down", "d"} and x is not None and y is not None:
            return {"op": "down", "x": x, "y": y}
        if op in {"to", "move", "m"} and x is not None and y is not None:
            return {"op": "to", "x": x, "y": y}
        p = obj.get("p", obj.get("pen", obj.get("down")))
        if x is not None and y is not None and p is not None:
            down = str(p).lower() in {"1", "true", "down", "d"}
            return {"op": "down" if down else "up", "x": x, "y": y} if down else {"op": "up"}
        return None
    parts = re.split(r"[\s,]+", raw)
    if not parts:
        return None
    head = parts[0].lower()
    if head in {"up", "u", "penup", "lift"}:
        return {"op": "up"}
    if head in {"end", "done", "finish"}:
        return {"op": "end"}
    if head in {"down", "d", "to", "move", "m", "pen", "goto"}:
        if len(parts) < 3:
            return None
        x = _clamp_pen_coord(parts[1])
        y = _clamp_pen_coord(parts[2])
        if x is None or y is None:
            return None
        if head in {"down", "d"}:
            return {"op": "down", "x": x, "y": y}
        return {"op": "to", "x": x, "y": y}
    if len(parts) >= 3 and parts[2] in {"0", "1"}:
        x = _clamp_pen_coord(parts[0])
        y = _clamp_pen_coord(parts[1])
        if x is None or y is None:
            return None
        return {"op": "mark", "x": x, "y": y, "down": parts[2] == "1"}
    if len(parts) >= 2 and (
        parts[0][:1].isdigit()
        or (parts[0][:1] == "-" and parts[0][1:2].isdigit())
    ):
        x = _clamp_pen_coord(parts[0])
        y = _clamp_pen_coord(parts[1])
        if x is None or y is None:
            return None
        return {"op": "to", "x": x, "y": y}
    return None


def extract_pen_commands(text: str, *, partial: bool = True) -> list[dict[str, Any]]:
    """Parse complete pen commands from a (possibly streaming) model reply."""
    raw = _svg_strip_fences(text or "")
    if not raw.strip():
        return []
    lines = raw.splitlines()
    if partial and text and not str(text).endswith(("\n", "\r")) and lines:
        lines = lines[:-1]
    out: list[dict[str, Any]] = []
    pen_down = False
    for line in lines:
        parsed = _parse_pen_line(line)
        if not parsed:
            continue
        if parsed.get("op") == "end":
            break
        if parsed.get("op") == "mark":
            if parsed.get("down"):
                op = "to" if pen_down else "down"
                out.append({"op": op, "x": parsed["x"], "y": parsed["y"]})
                pen_down = True
            else:
                if pen_down:
                    out.append({"op": "up"})
                pen_down = False
            continue
        if parsed.get("op") == "up":
            pen_down = False
        elif parsed.get("op") in {"down", "to"}:
            pen_down = True
        out.append(parsed)
    return out[:12_000]


def extract_pen_commands_from_sources(*chunks: str, partial: bool = True) -> list[dict[str, Any]]:
    """Parse commands from content and/or thought text (models often draw in reasoning)."""
    best: list[dict[str, Any]] = []
    seen: list[str] = []
    for chunk in chunks:
        text = (chunk or "").strip()
        if not text or text in seen:
            continue
        seen.append(text)
        cmds = extract_pen_commands(text, partial=partial)
        if len(cmds) > len(best):
            best = cmds
    combined = "\n".join(seen)
    if combined:
        cmds = extract_pen_commands(combined, partial=partial)
        if len(cmds) > len(best):
            best = cmds
    return best


def format_pen_commands(commands: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for cmd in commands or []:
        op = cmd.get("op")
        if op == "up":
            lines.append("UP")
        elif op in {"down", "to"} and cmd.get("x") is not None and cmd.get("y") is not None:
            lines.append(f"{op.upper()} {int(cmd['x'])} {int(cmd['y'])}")
    return ("\n".join(lines) + "\n") if lines else ""


def wrap_pen_preview(commands: list[dict[str, Any]]) -> str:
    """Static SVG snapshot of pen commands (for Open ↗)."""
    parts: list[str] = []
    down = False
    for cmd in commands or []:
        op = cmd.get("op")
        if op == "up":
            down = False
            continue
        x = cmd.get("x")
        y = cmd.get("y")
        if x is None or y is None:
            continue
        if op == "down" or not down:
            parts.append(f"M {int(x)} {int(y)}")
            down = True
        else:
            parts.append(f"L {int(x)} {int(y)}")
    d = " ".join(parts)
    path = (
        f'<path d="{d}" fill="none" stroke="#1a1a1a" stroke-width="3" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        if d
        else ""
    )
    svg = (
        '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">'
        '<rect width="1024" height="1024" fill="#fbf7ef"/>'
        f"{path}</svg>"
    )
    return wrap_svg_preview(svg)


def _svg_end_of_tag(text: str, start: int) -> int:
    """Index of the '>' that closes the tag starting at start, or -1 if incomplete."""
    i = start
    quote = None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ">":
            return i
        i += 1
    return -1


def _svg_strip_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    fence = re.match(
        r"^```(?:svg|xml|html|SVG|XML|HTML)?\s*\n([\s\S]*?)\n?```\s*$",
        cleaned,
    )
    if fence:
        return fence.group(1).strip()
    cleaned = re.sub(r"^```(?:svg|xml|html|SVG|XML|HTML)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
    return cleaned


def _svg_esc_attr(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _svg_clean_open_tag(tag: str) -> str | None:
    """Sanitize one open/empty tag. Return None to drop the whole element."""
    match = re.match(r"<(/)?([a-zA-Z][\w:-]*)(.*?)(/?)>$", tag, re.DOTALL)
    if not match:
        return tag
    closer, raw_name, attrs, slash = match.groups()
    name = raw_name.lower()
    if closer:
        if name in _SVG_FORBIDDEN_TAGS or (
            name not in _SVG_ALLOWED_TAGS and name != "svg"
        ):
            return None
        return f"</{raw_name}>"
    if name in _SVG_FORBIDDEN_TAGS:
        return None
    if name not in _SVG_ALLOWED_TAGS and name != "svg":
        return None
    kept: list[str] = []
    for am in re.finditer(
        r'([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'=<>`]+)))?',
        attrs or "",
    ):
        key = am.group(1)
        val = (
            am.group(2)
            if am.group(2) is not None
            else am.group(3)
            if am.group(3) is not None
            else (am.group(4) or "")
        )
        lk = key.lower()
        if lk.startswith("xmlns:"):
            kept.append(f'{key}="{_svg_esc_attr(val)}"')
            continue
        if lk.startswith("on") or lk in {"srcdoc", "formaction"}:
            continue
        if lk in _SVG_URL_ATTRS or lk.endswith(":href"):
            stripped = val.strip()
            if stripped.startswith("#") or stripped == "":
                kept.append(f'{key}="{_svg_esc_attr(val)}"')
            continue
        if lk == "style" and re.search(
            r"url\s*\(\s*['\"]?\s*(?:https?:|data:|javascript:)", val, re.I
        ):
            continue
        if re.search(r"javascript\s*:", val, re.I):
            continue
        kept.append(f'{key}="{_svg_esc_attr(val)}"')
    inner = (" " + " ".join(kept)) if kept else ""
    return f"<{raw_name}{inner}{slash}>"


def _svg_sanitize_element(element: str) -> str:
    """Sanitize tags/attrs in a complete element. Empty string means drop it."""
    if element.startswith("<!--"):
        return ""
    if not element.lstrip().startswith("<"):
        return element
    out: list[str] = []
    i = 0
    n = len(element)
    while i < n:
        if element.startswith("<!--", i):
            end = element.find("-->", i + 4)
            if end == -1:
                break
            i = end + 3
            continue
        lt = element.find("<", i)
        if lt == -1:
            out.append(element[i:])
            break
        if lt > i:
            out.append(element[i:lt])
        gt = _svg_end_of_tag(element, lt)
        if gt == -1:
            break
        cleaned = _svg_clean_open_tag(element[lt : gt + 1])
        if cleaned is None:
            return ""
        out.append(cleaned)
        i = gt + 1
    return "".join(out)


def _svg_next_piece(text: str, pos: int) -> tuple[str | None, int, str]:
    """
    Next complete top-level piece.
    Returns (piece, new_pos, kind) where kind is 'element', 'text', 'end', or 'incomplete'.
    piece is None when kind is end/incomplete.
    """
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    if pos >= n:
        return None, n, "end"
    if text.startswith("</", pos):
        return None, pos, "end"
    if text.startswith("<!--", pos):
        end = text.find("-->", pos + 4)
        if end == -1:
            return None, pos, "incomplete"
        return text[pos : end + 3], end + 3, "element"
    if text[pos] != "<":
        nxt = text.find("<", pos)
        if nxt == -1:
            return text[pos:], n, "text"
        return text[pos:nxt], nxt, "text"
    gt = _svg_end_of_tag(text, pos)
    if gt == -1:
        return None, pos, "incomplete"
    tag = text[pos : gt + 1]
    match = re.match(r"<(/)?([a-zA-Z][\w:-]*)", tag)
    if not match or match.group(1):
        return None, pos, "end"
    name = match.group(2)
    self_close = tag.rstrip().endswith("/>")
    if self_close:
        return tag, gt + 1, "element"
    depth = 1
    i = gt + 1
    while i < n:
        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            if end == -1:
                return None, pos, "incomplete"
            i = end + 3
            continue
        if text[i] != "<":
            i += 1
            continue
        gt2 = _svg_end_of_tag(text, i)
        if gt2 == -1:
            return None, pos, "incomplete"
        inner = text[i : gt2 + 1]
        tm = re.match(r"<(/)?([a-zA-Z][\w:-]*)", inner)
        if tm and tm.group(2).lower() == name.lower():
            if tm.group(1):
                depth -= 1
                if depth == 0:
                    return text[pos : gt2 + 1], gt2 + 1, "element"
            elif not inner.rstrip().endswith("/>"):
                depth += 1
        i = gt2 + 1
    return None, pos, "incomplete"


def extract_preview_svg(text: str) -> str:
    """
    Turn a (possibly partial) model reply into a safe, closable SVG document.
    Incomplete trailing tags are dropped so the preview can render live.
    """
    cleaned = _svg_strip_fences(text or "")
    if not cleaned:
        return ""
    lower = cleaned.lower()
    start = lower.find("<svg")
    if start == -1:
        return ""
    svg = cleaned[start:]
    gt = _svg_end_of_tag(svg, 0)
    if gt == -1:
        return ""
    open_tag = svg[: gt + 1]
    if open_tag.rstrip().endswith("/>"):
        sanitized_open = _svg_clean_open_tag(open_tag) or ""
        return _svg_ensure_root(sanitized_open) if sanitized_open else ""
    inner = svg[gt + 1 :]
    close_at = inner.lower().rfind("</svg>")
    if close_at != -1:
        inner = inner[:close_at]

    children: list[str] = []
    pos = 0
    while pos < len(inner):
        piece, new_pos, kind = _svg_next_piece(inner, pos)
        if kind == "incomplete":
            break
        if kind == "end":
            break
        if kind == "text":
            pos = new_pos
            continue
        cleaned_el = _svg_sanitize_element(piece or "")
        if cleaned_el.strip():
            children.append(cleaned_el)
        pos = new_pos

    sanitized_open = _svg_clean_open_tag(open_tag)
    if not sanitized_open:
        sanitized_open = '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">'
    sanitized_open = _svg_ensure_root(sanitized_open)
    return sanitized_open + "".join(children) + "</svg>"


def _svg_ensure_root(open_tag: str) -> str:
    lower = open_tag.lower()
    extras: list[str] = []
    if "viewbox=" not in lower:
        extras.append('viewBox="0 0 1024 1024"')
    if "xmlns=" not in lower:
        extras.append('xmlns="http://www.w3.org/2000/svg"')
    if not extras:
        return open_tag
    if open_tag.endswith("/>"):
        return open_tag[:-2] + " " + " ".join(extras) + " />"
    return open_tag[:-1] + " " + " ".join(extras) + ">"


def wrap_svg_preview(svg: str) -> str:
    """Wrap a (possibly empty) SVG in a square HTML document for iframe srcdoc."""
    body = (svg or "").strip()
    if not body:
        body = (
            '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">'
            "</svg>"
        )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8" />'
        "<style>"
        "html,body{margin:0;width:100%;height:100%;background:#fff;overflow:hidden}"
        "svg{display:block;width:100%;height:100%}"
        "</style></head><body>"
        f"{body}"
        "</body></html>"
    )


# Injected into game previews: start paused, expose play/pause, freeze timers/rAF.
_GAME_PAUSE_CONTROLLER = r"""
<style id="bench-game-control-style">
  #bench-game-overlay{
    position:fixed;inset:0;z-index:2147483646;
    display:flex;align-items:center;justify-content:center;
    flex-direction:column;gap:12px;
    background:rgba(15,18,28,.55);
    backdrop-filter:blur(2px);
    color:#fff;font:600 15px/1.4 system-ui,-apple-system,sans-serif;
    letter-spacing:-.01em;text-align:center;
    cursor:pointer;user-select:none;
  }
  #bench-game-overlay[hidden]{display:none!important}
  #bench-game-overlay .bench-game-sub{
    font-weight:500;font-size:13px;opacity:.85;
  }
  #bench-game-overlay .bench-game-play{
    display:inline-flex;align-items:center;gap:8px;
    border:0;border-radius:999px;padding:10px 18px;
    background:#fff;color:#14151a;font:650 14px system-ui,sans-serif;
    box-shadow:0 8px 24px rgba(0,0,0,.25);cursor:pointer;
  }
  #bench-game-overlay .bench-game-play:hover{transform:translateY(-1px)}
  #bench-game-bar{
    position:fixed;top:10px;right:10px;z-index:2147483647;
    display:flex;gap:6px;
  }
  #bench-game-bar button{
    border:0;border-radius:8px;padding:7px 11px;
    font:600 12px system-ui,sans-serif;cursor:pointer;
    background:rgba(20,21,26,.82);color:#fff;
    box-shadow:0 2px 10px rgba(0,0,0,.2);
  }
  #bench-game-bar button:hover{background:rgba(20,21,26,.95)}
  #bench-game-bar button[hidden]{display:none!important}
</style>
<script id="bench-game-control-script">
(function () {
  if (window.__benchGameControl) return;
  var paused = true;
  var rafWaiters = [];
  var rafId = 1;
  var timerId = 1;
  var timerMap = Object.create(null);
  var audioContexts = [];

  var origRAF = window.requestAnimationFrame.bind(window);
  var origCAF = window.cancelAnimationFrame.bind(window);
  var origST = window.setTimeout.bind(window);
  var origSI = window.setInterval.bind(window);
  var origCT = window.clearTimeout.bind(window);
  var origCI = window.clearInterval.bind(window);
  var OrigAC = window.AudioContext || window.webkitAudioContext;

  window.requestAnimationFrame = function (cb) {
    var id = rafId++;
    if (paused) {
      rafWaiters.push({ id: id, cb: cb });
      return id;
    }
    var real = origRAF(function (t) {
      if (paused) {
        rafWaiters.push({ id: id, cb: cb });
        return;
      }
      try { cb(t); } catch (err) { console.error(err); }
    });
    timerMap["r" + id] = real;
    return id;
  };
  window.cancelAnimationFrame = function (id) {
    rafWaiters = rafWaiters.filter(function (w) { return w.id !== id; });
    var real = timerMap["r" + id];
    if (real != null) {
      try { origCAF(real); } catch (e) {}
      delete timerMap["r" + id];
    }
  };

  window.setTimeout = function (fn, ms) {
    var args = Array.prototype.slice.call(arguments, 2);
    var id = timerId++;
    var real = origST(function () {
      delete timerMap["t" + id];
      if (paused) return;
      if (typeof fn === "function") fn.apply(null, args);
      else try { (0, eval)(String(fn)); } catch (e) {}
    }, ms);
    timerMap["t" + id] = real;
    return id;
  };
  window.clearTimeout = function (id) {
    var real = timerMap["t" + id];
    if (real != null) {
      origCT(real);
      delete timerMap["t" + id];
    } else {
      try { origCT(id); } catch (e) {}
    }
  };
  window.setInterval = function (fn, ms) {
    var args = Array.prototype.slice.call(arguments, 2);
    var id = timerId++;
    var real = origSI(function () {
      if (paused) return;
      if (typeof fn === "function") fn.apply(null, args);
      else try { (0, eval)(String(fn)); } catch (e) {}
    }, ms);
    timerMap["i" + id] = real;
    return id;
  };
  window.clearInterval = function (id) {
    var real = timerMap["i" + id];
    if (real != null) {
      origCI(real);
      delete timerMap["i" + id];
    } else {
      try { origCI(id); } catch (e) {}
    }
  };

  if (OrigAC) {
    window.AudioContext = function () {
      var ctx = new OrigAC();
      audioContexts.push(ctx);
      if (paused && ctx.suspend) {
        try { ctx.suspend(); } catch (e) {}
      }
      return ctx;
    };
    window.AudioContext.prototype = OrigAC.prototype;
    if (window.webkitAudioContext) {
      window.webkitAudioContext = window.AudioContext;
    }
  }

  function setPaused(next) {
    paused = !!next;
    var overlay = document.getElementById("bench-game-overlay");
    var playBtn = document.getElementById("bench-game-play-btn");
    var pauseBtn = document.getElementById("bench-game-pause-btn");
    if (overlay) overlay.hidden = !paused;
    if (playBtn) playBtn.hidden = !paused;
    if (pauseBtn) pauseBtn.hidden = paused;
    if (!paused) {
      var queued = rafWaiters.splice(0, rafWaiters.length);
      for (var i = 0; i < queued.length; i++) {
        (function (item) {
          var real = origRAF(function (t) {
            if (paused) {
              rafWaiters.push(item);
              return;
            }
            try { item.cb(t); } catch (err) { console.error(err); }
          });
          timerMap["r" + item.id] = real;
        })(queued[i]);
      }
      audioContexts.forEach(function (ctx) {
        if (ctx && ctx.state === "suspended" && ctx.resume) {
          try { ctx.resume(); } catch (e) {}
        }
      });
    } else {
      audioContexts.forEach(function (ctx) {
        if (ctx && ctx.state === "running" && ctx.suspend) {
          try { ctx.suspend(); } catch (e) {}
        }
      });
    }
    try {
      window.parent.postMessage({ type: "bench-game", action: paused ? "paused" : "playing" }, "*");
    } catch (e) {}
  }

  function ensureUi() {
    if (document.getElementById("bench-game-overlay")) return;
    var overlay = document.createElement("div");
    overlay.id = "bench-game-overlay";
    overlay.innerHTML =
      '<button type="button" class="bench-game-play" id="bench-game-overlay-play">' +
      "<span>▶</span><span>Play game</span></button>" +
      '<div class="bench-game-sub">Paused — press Play to start</div>';
    overlay.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      setPaused(false);
    });
    var bar = document.createElement("div");
    bar.id = "bench-game-bar";
    bar.innerHTML =
      '<button type="button" id="bench-game-play-btn">Play</button>' +
      '<button type="button" id="bench-game-pause-btn" hidden>Pause</button>';
    bar.addEventListener("click", function (e) { e.stopPropagation(); });
    document.documentElement.appendChild(overlay);
    document.documentElement.appendChild(bar);
    document.getElementById("bench-game-play-btn").onclick = function (e) {
      e.preventDefault();
      setPaused(false);
    };
    document.getElementById("bench-game-pause-btn").onclick = function (e) {
      e.preventDefault();
      setPaused(true);
    };
    document.getElementById("bench-game-overlay-play").onclick = function (e) {
      e.preventDefault();
      e.stopPropagation();
      setPaused(false);
    };
  }

  window.addEventListener("message", function (ev) {
    var data = ev && ev.data;
    if (!data || data.type !== "bench-game") return;
    if (data.action === "play") setPaused(false);
    if (data.action === "pause") setPaused(true);
    if (data.action === "toggle") setPaused(!paused);
    if (data.action === "status") {
      try {
        ev.source && ev.source.postMessage(
          { type: "bench-game", action: paused ? "paused" : "playing" },
          "*"
        );
      } catch (e) {}
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensureUi);
  } else {
    ensureUi();
  }
  // If body appears later, still try once more
  origST(ensureUi, 0);

  window.__benchGameControl = {
    play: function () { setPaused(false); },
    pause: function () { setPaused(true); },
    toggle: function () { setPaused(!paused); },
    isPaused: function () { return paused; },
  };
  setPaused(true);
})();
</script>
"""


def wrap_game_preview(html: str) -> str:
    """Inject pause-by-default controls into a model-generated game HTML document."""
    doc = (html or "").strip()
    if not doc:
        return doc
    # Avoid double-injecting on re-serve
    if "bench-game-control-script" in doc or "__benchGameControl" in doc:
        return doc

    inject = _GAME_PAUSE_CONTROLLER
    head_match = re.search(r"<head[^>]*>", doc, flags=re.IGNORECASE)
    if head_match:
        idx = head_match.end()
        return doc[:idx] + inject + doc[idx:]

    html_match = re.search(r"<html[^>]*>", doc, flags=re.IGNORECASE)
    if html_match:
        idx = html_match.end()
        return doc[:idx] + "<head>" + inject + "</head>" + doc[idx:]

    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\" />"
        + inject
        + "</head><body>"
        + doc
        + "</body></html>"
    )


def attach_draw_preview(run: dict[str, Any]) -> dict[str, Any]:
    """Add live preview fields for draw / pen runs (including in-progress streams)."""
    kind = run.get("task_kind") or ""
    if kind == "pen":
        partial = (run.get("status") or "") in {"pending", "running"}
        raw_early = run.get("raw_response") or ""
        meta_early: dict[str, Any] = {}
        if raw_early.startswith("{"):
            try:
                loaded = json.loads(raw_early)
                if isinstance(loaded, dict):
                    meta_early = loaded
            except json.JSONDecodeError:
                meta_early = {}
        stored = meta_early.get("pen_commands")
        if isinstance(stored, list) and stored:
            cmds = stored
        else:
            cmds = extract_pen_commands_from_sources(
                run.get("output_text") or "",
                str(meta_early.get("reasoning_full") or ""),
                str(meta_early.get("reasoning_preview") or ""),
                partial=partial,
            )
        run["pen_commands"] = cmds
        run["has_preview"] = bool(cmds)
    elif kind == "draw":
        svg = extract_preview_svg(run.get("output_text") or "")
        run["preview_html"] = wrap_svg_preview(svg) if svg else ""
        run["has_preview"] = bool(svg)
    else:
        return run
    raw = run.get("raw_response") or ""
    phase = ""
    heartbeats = 0
    reasoning_chars = 0
    est_cost = None
    cost_cap = None
    meta: dict[str, Any] = {}
    if raw.startswith("{"):
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            meta = {}
        if isinstance(meta, dict):
            phase = str(meta.get("phase") or "")
            try:
                heartbeats = int(meta.get("heartbeats") or 0)
            except (TypeError, ValueError):
                heartbeats = 0
            try:
                reasoning_chars = int(meta.get("reasoning_chars") or 0)
            except (TypeError, ValueError):
                reasoning_chars = 0
            est_cost = meta.get("est_cost_usd")
            cost_cap = meta.get("cost_limit_usd")
    if not phase:
        if run.get("output_text"):
            phase = "drawing"
        elif (run.get("status") or "") == "running":
            phase = "connecting"
    run["stream_phase"] = phase
    run["stream_heartbeats"] = heartbeats
    run["stream_reasoning_chars"] = reasoning_chars
    run["est_cost_usd"] = est_cost if est_cost is not None else run.get("cost_usd")
    run["cost_limit_usd"] = cost_cap
    run["reasoning_preview"] = (
        str(meta.get("reasoning_preview") or "") if raw.startswith("{") and isinstance(meta, dict) else ""
    )
    return run


def fail_orphaned_runs(min_age_seconds: int = 2) -> int:
    """Mark in-flight runs as failed after a process restart.

    Only call this after the HTTP server has successfully bound its port.
    Failed bind attempts (e.g. port already in use) must not touch live runs.
    """
    from datetime import datetime, timedelta, timezone

    conn = get_conn()
    try:
        now = utc_now()
        rows = conn.execute(
            """
            SELECT id, created_at FROM runs
            WHERE status IN ('pending', 'running', 'rendering')
            """
        ).fetchall()
        cut = datetime.now(timezone.utc) - timedelta(seconds=max(0, int(min_age_seconds)))
        ids: list[int] = []
        for row in rows:
            rid = int(row["id"])
            created = row["created_at"] or ""
            try:
                ts = str(created).replace("Z", "+00:00")
                if "T" not in ts and " " in ts:
                    ts = ts.replace(" ", "T", 1)
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt <= cut:
                    ids.append(rid)
            except Exception:
                # Unparseable timestamps are treated as orphaned.
                ids.append(rid)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"""
            UPDATE runs
            SET status = 'failed',
                error = 'Interrupted when the server restarted',
                completed_at = ?
            WHERE id IN ({placeholders})
              AND status IN ('pending', 'running', 'rendering')
            """,
            (now, *ids),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


# --- Saved instructions (skills) ---


def list_instructions() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM instructions ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
        return rows_to_list(rows)
    finally:
        conn.close()


def get_instruction(instruction_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM instructions WHERE id = ?", (instruction_id,)
            ).fetchone()
        )
    finally:
        conn.close()


def get_instruction_by_name(name: str) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM instructions WHERE name = ? COLLATE NOCASE",
                (name.strip(),),
            ).fetchone()
        )
    finally:
        conn.close()


def create_instruction(
    name: str,
    body: str = "",
    notes: str = "",
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    now = utc_now()
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO instructions (name, body, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, body or "", notes or "", now, now),
        )
        conn.commit()
        return get_instruction(int(cur.lastrowid))  # type: ignore[return-value]
    finally:
        conn.close()


def update_instruction(instruction_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "body", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_instruction(instruction_id)
    if "name" in updates:
        updates["name"] = str(updates["name"]).strip()
        if not updates["name"]:
            raise ValueError("name cannot be empty")
    updates["updated_at"] = utc_now()
    cols = ", ".join(f"{k} = ?" for k in updates)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE instructions SET {cols} WHERE id = ?",
            (*updates.values(), instruction_id),
        )
        conn.commit()
        return get_instruction(instruction_id)
    finally:
        conn.close()


def delete_instruction(instruction_id: int) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM instructions WHERE id = ?", (instruction_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def export_snapshot() -> dict[str, Any]:
    return {
        "exported_at": utc_now(),
        "tasks": list_tasks(include_archived=True),
        "models": list_models(),
        "instructions": list_instructions(),
        "runs": list_runs(limit=10_000),
    }


def dumps_pretty(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)
