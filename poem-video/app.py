"""Poem-to-video: each line of a short poem becomes its own clip, timed to how
long the line takes to read aloud, with the line set on screen.

Settings come in two levels (see workflows/verse-to-film.json):
  film  -- visual style, model, aspect ratio
  line  -- clip length (suggested from reading time, editable)

Each line is generated through Comfy Router (queued with models.submit) at
the model's nearest duration that is at least the requested length, then
trimmed to that exact length while the line is burned in with ffmpeg. The
film is joined in line order once every line has finished; a failed line
blocks the film until it is retried.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import textwrap
import threading
import time
import uuid
from pathlib import Path

import httpx
import imageio_ffmpeg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

from comfy_sdk import Comfy, ComfyError, InsufficientCredits
from comfy_sdk.router_exceptions import ConcurrencyLimitExceeded, RateLimited, RouterError

HERE = Path(__file__).parent
ENV_PATH = HERE.parent / ".env"  # shared with the app next door
load_dotenv(ENV_PATH, override=True)  # .env wins over the shell

WORKFLOW = json.loads((HERE.parent / "workflows" / "verse-to-film.json").read_text())
FILM = WORKFLOW["film"]
READING = WORKFLOW["reading"]
CLIP_LEN = WORKFLOW["clip_length"]
ADAPTERS: dict[str, dict] = WORKFLOW["models"]
OVERLAY = WORKFLOW["overlay"]
MAX_LINES = FILM["max_lines"]

OUT = HERE / "outputs"
STORE = HERE / "poems.json"
OUT.mkdir(exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FONT = OVERLAY["font"]
SIZES = {k: tuple(v) for k, v in OVERLAY["output_sizes"].items()}
FPS = OVERLAY["fps"]

_store_lock = threading.Lock()
_poem_locks: dict[str, threading.Lock] = {}
_client: Comfy | None = None
_catalog: dict = {"ids": None, "at": 0.0, "error": None}


class CreditsError(Exception):
    pass


def client() -> Comfy:
    global _client
    if _client is None:
        if not os.environ.get("COMFY_API_KEY"):
            raise HTTPException(503, f"COMFY_API_KEY is missing. Put it in {ENV_PATH} and restart.")
        _client = Comfy()
    return _client


def dig(obj, path: str):
    """Read a dotted path like 'result.video_url' out of a provider result."""
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(poem: dict) -> dict:
    with _store_lock:
        data = load()
        data[poem["id"]] = poem
        STORE.write_text(json.dumps(data, indent=2))
    return poem


def err_fields(exc: Exception) -> dict:
    return {"error_type": getattr(exc, "error_type", None) or getattr(exc, "code", None) or "error",
            "error": getattr(exc, "detail", None) or str(exc)}


# --- models: adapters the workflow defines AND the workspace catalog lists ---
def catalog_ids() -> set[str] | None:
    """Model ids from models.list(), cached for 10 minutes. None if unreadable."""
    if _catalog["ids"] is not None and time.time() - _catalog["at"] < 600:
        return _catalog["ids"]
    try:
        _catalog["ids"] = {m.id for m in client().models.list(limit=100)}
        _catalog["at"], _catalog["error"] = time.time(), None
    except HTTPException:
        raise
    except Exception as exc:  # keep serving the last good list if we had one
        _catalog["error"] = str(exc)
    return _catalog["ids"]


def available_models() -> list[str]:
    ids = catalog_ids()
    return [m for m in ADAPTERS if ids is not None and m in ids]


def max_length(adapter: dict) -> float:
    d = adapter["duration"]
    return float(max(d["values"]) if d["kind"] == "enum" else d["max"])


def generate_length(adapter: dict, seconds: float) -> int:
    """The duration to ask the model for: never shorter than `seconds`."""
    d = adapter["duration"]
    if d["kind"] == "enum":
        fits = [v for v in sorted(d["values"]) if v >= seconds - 1e-9]
        return fits[0] if fits else max(d["values"])
    step = d.get("step", 1)
    n = math.ceil(seconds / step - 1e-9) * step
    return int(min(max(n, d["min"]), d["max"]))


def suggest_length(line: str) -> float:
    """How long the line takes to read aloud, rounded up to `round_to`."""
    words = len(re.findall(r"[\w'’-]+", line))
    commas = line.count(",")
    stops = len(re.findall(r"[.;:!?]|—|–|--", line))
    raw = (words / READING["words_per_second"] + commas * READING["comma_pause"]
           + stops * READING["stop_pause"] + READING["end_breath"])
    step = READING["round_to"]
    return max(READING["min_seconds"], math.ceil(raw / step - 1e-9) * step)


def clamp_length(adapter: dict, seconds: float) -> float:
    step = CLIP_LEN["step"]
    seconds = math.floor(seconds / step + 0.5 + 1e-9) * step  # halves round up
    return min(max(seconds, CLIP_LEN["min_seconds"]), max_length(adapter))


def split_lines(poem: str) -> list[str]:
    return [ln.strip() for ln in poem.splitlines() if ln.strip()]


# --- text overlay, trim, join ---------------------------------------------
def caption_png(text: str, size: tuple[int, int], path: Path) -> None:
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fs = max(28, int(min(w, h) * OVERLAY["font_size_ratio"]))
    try:
        font = ImageFont.truetype(FONT, fs)
    except OSError:
        font = ImageFont.load_default(size=fs)
    chars = max(14, int(w / (fs * 0.5)))
    wrapped = "\n".join(textwrap.wrap(text, width=chars)) or text
    box = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center", spacing=fs // 3)
    tw, th = box[2] - box[0], box[3] - box[1]
    y = int(h * OVERLAY["baseline_ratio"]) - th
    # soft scrim behind the text for legibility on any footage
    scrim = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rectangle([0, y - fs, w, h], fill=tuple(OVERLAY["scrim_rgba"]))
    img = Image.alpha_composite(scrim, img)
    draw = ImageDraw.Draw(img)
    draw.multiline_text(((w - tw) / 2, y), wrapped, font=font, fill=(255, 255, 255, 255),
                        align="center", spacing=fs // 3, stroke_width=max(1, fs // 18),
                        stroke_fill=(0, 0, 0, 200))
    img.save(path)


def burn_in(src: Path, png: Path, dst: Path, size: tuple[int, int], seconds: float) -> None:
    """Fit to `size`, overlay the caption, and cut to exactly `seconds`.

    tpad holds the last frame for up to 2 s in case the model's clip came back
    a few frames short of what was asked for; -t then trims to the exact length.
    """
    w, h = size
    vf = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
          f"fps={FPS},setsar=1,tpad=stop_mode=clone:stop_duration=2[v];"
          f"[v][1:v]overlay=0:0,format=yuv420p")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-i", str(png),
                    "-filter_complex", vf, "-t", f"{seconds:.3f}", "-an", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "20", "-r", str(FPS), str(dst)], check=True)


def concat(clips: list[Path], dst: Path) -> None:
    """Join clips in the order given, re-encoding so the timing is exact."""
    listing = dst.with_suffix(".txt")
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips))
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-an", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-r", str(FPS), "-pix_fmt", "yuv420p", str(dst)],
                   check=True, cwd=dst.parent)
    listing.unlink()


def video_seconds(path: Path) -> float | None:
    """Duration read from ffmpeg's stream info (no ffprobe needed)."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


# --- per-clip state machine -----------------------------------------------
def build_prompt(line: str, style: str) -> str:
    style = style.strip()
    return (WORKFLOW["prompt_template"]
            .replace("{style}", f"{style}. " if style else "")
            .replace("{line}", line))


def try_submit(poem: dict, clip: dict) -> None:
    adapter = ADAPTERS[poem["model"]]
    body = {"prompt": build_prompt(clip["line"], poem["style"]),
            adapter["aspect"]["field"]: adapter["aspect"]["values"][poem["aspect_ratio"]],
            adapter["duration"]["field"]: clip["generate_seconds"],
            **adapter["fixed_inputs"]}
    try:
        h = client().models.submit(poem["model"], body)
    except InsufficientCredits:
        raise CreditsError()
    except (ConcurrencyLimitExceeded, RateLimited):
        clip["state"] = "WAITING"  # resubmit on the next poll
        return
    except (RouterError, ComfyError) as exc:
        clip.update(state="FAILED", **err_fields(exc))
        return
    clip.update(state="IN_QUEUE", request_id=h.request_id, model=h.model)


def advance(poem: dict, idx: int, clip: dict) -> None:
    if clip["state"] in ("DONE", "FAILED"):
        return
    if clip["state"] == "WAITING":
        try_submit(poem, clip)
        return
    out = ADAPTERS[poem["model"]]["output"]
    handle = client().models.handle(clip["model"], clip["request_id"])
    try:
        upd = handle.status()
    except InsufficientCredits:
        raise CreditsError()
    except (RouterError, ComfyError) as exc:
        clip.update(state="FAILED", **err_fields(exc))
        return
    clip["queue_position"] = upd.queue_position
    if not upd.is_completed:
        clip["state"] = upd.status or clip["state"]
        return
    if upd.error_type:  # COMPLETED but failed / cancelled
        clip.update(state="FAILED", error_type=upd.error_type,
                    error=str(upd.raw.get("detail") or upd.raw.get("error") or ""))
        return
    try:
        result = handle.get(timeout=120)
    except (RouterError, ComfyError) as exc:
        clip.update(state="FAILED", **err_fields(exc))
        return
    pstatus = dig(result, out["status"])
    url = dig(result, out["video_url"])
    if pstatus in out["failed_statuses"] or not url:
        clip.update(state="FAILED",
                    error_type=dig(result, out["error_type"]) or pstatus or "no_output",
                    error=dig(result, out["error_message"]) or "The model finished without a video.")
        return
    # download promptly (24h retention), then burn the line in and trim
    base = f"{poem['id']}_{idx:02d}"
    raw, png, final = OUT / f"{base}_raw.mp4", OUT / f"{base}_text.png", OUT / f"{base}.mp4"
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
            r.raise_for_status()
            with open(raw, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        size = SIZES[poem["aspect_ratio"]]
        caption_png(clip["line"], size, png)
        burn_in(raw, png, final, size, clip["seconds"])
    except (httpx.HTTPError, subprocess.CalledProcessError, OSError) as exc:
        clip.update(state="FAILED", error_type="postprocess_failed", error=str(exc)[:300])
        return
    finally:
        png.unlink(missing_ok=True)
    clip.update(state="DONE", file=f"/outputs/{final.name}", raw_file=f"/outputs/{raw.name}",
                raw_seconds=video_seconds(raw), final_seconds=video_seconds(final))


def maybe_build_film(poem: dict) -> None:
    clips = poem["clips"]
    if poem.get("film") or not clips or any(c["state"] != "DONE" for c in clips):
        return
    ordered = [OUT / Path(c["file"]).name for c in clips]  # line order, always
    film = OUT / f"{poem['id']}_film.mp4"
    concat(ordered, film)
    poem["film"] = f"/outputs/{film.name}"
    poem["film_seconds"] = video_seconds(film)


# --- API -------------------------------------------------------------------
class LineIn(BaseModel):
    line: str = Field(min_length=1, max_length=500)
    seconds: float | None = None  # None = use the suggestion


class PlanIn(BaseModel):
    poem: str = Field("", max_length=4000)
    model: str
    seconds: list[float | None] = []  # per-line overrides, by position


class PoemIn(BaseModel):
    lines: list[LineIn] = Field(min_length=1)
    style: str = Field("", max_length=300)
    model: str
    aspect_ratio: str


app = FastAPI()

CREDITS_MSG = ("Your Comfy workspace is out of credits. Add credits at platform.comfy.org, "
               "then try again.")


def credits_response(poem: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": "insufficient_credits", "detail": CREDITS_MSG, "poem": poem},
                        status_code=402)


def adapter_or_422(model: str) -> dict:
    if model not in ADAPTERS:
        raise HTTPException(422, f"Model {model!r} has no adapter in workflows/verse-to-film.json.")
    if model not in available_models():
        raise HTTPException(422, f"Model {model!r} is not in your workspace's Router catalog.")
    return ADAPTERS[model]


def plan_row(adapter: dict, line: str, override: float | None) -> dict:
    suggested = suggest_length(line)
    seconds = clamp_length(adapter, override if override is not None else suggested)
    return {"line": line, "suggested": clamp_length(adapter, suggested), "seconds": seconds,
            "edited": override is not None,
            "capped": (override if override is not None else suggested) > max_length(adapter),
            "generate_seconds": generate_length(adapter, seconds)}


@app.get("/api/config")
def config():
    key = bool(os.environ.get("COMFY_API_KEY"))
    models = []
    if key:
        for mid in available_models():
            a = ADAPTERS[mid]
            models.append({"id": mid, "label": a["label"], "aspects": list(a["aspect"]["values"]),
                           "max_seconds": max_length(a), "duration": a["duration"]})
    return {"key_present": key, "env_path": str(ENV_PATH), "max_lines": MAX_LINES,
            "default_model": FILM["default_model"], "default_style": FILM["default_style"],
            "clip_length": CLIP_LEN, "models": models, "catalog_error": _catalog["error"]}


@app.post("/api/plan")
def plan(body: PlanIn):
    adapter = adapter_or_422(body.model)
    lines = split_lines(body.poem)
    rows = [plan_row(adapter, ln, body.seconds[i] if i < len(body.seconds) else None)
            for i, ln in enumerate(lines)]
    return {"rows": rows, "too_many": len(lines) > MAX_LINES,
            "total_seconds": sum(r["seconds"] for r in rows[:MAX_LINES]),
            "generate_total": sum(r["generate_seconds"] for r in rows[:MAX_LINES])}


@app.post("/api/poems")
def create(body: PoemIn):
    adapter = adapter_or_422(body.model)
    if body.aspect_ratio not in adapter["aspect"]["values"]:
        raise HTTPException(422, f"{adapter['label']} does not support {body.aspect_ratio}.")
    if len(body.lines) > MAX_LINES:
        raise HTTPException(422, f"Keep it to {MAX_LINES} lines or fewer (got {len(body.lines)}).")
    clips = []
    for ln in body.lines:
        row = plan_row(adapter, ln.line.strip(), ln.seconds)
        clips.append({"line": row["line"], "seconds": row["seconds"],
                      "suggested": row["suggested"], "generate_seconds": row["generate_seconds"],
                      "state": "WAITING"})
    poem = {"id": uuid.uuid4().hex[:10], "created": time.time(), "style": body.style,
            "model": body.model, "model_label": adapter["label"],
            "aspect_ratio": body.aspect_ratio, "clips": clips, "film": None,
            "total_seconds": sum(c["seconds"] for c in clips)}
    try:
        for clip in poem["clips"]:
            try_submit(poem, clip)
    except CreditsError:
        save(poem)  # whatever was already submitted is still tracked
        return credits_response(poem)
    return save(poem)


@app.get("/api/poems")
def list_poems():
    return sorted(load().values(), key=lambda p: p["created"], reverse=True)


def _with_lock(pid: str):
    return _poem_locks.setdefault(pid, threading.Lock())


@app.get("/api/poems/{pid}")
def poll(pid: str):
    with _with_lock(pid):
        poem = load().get(pid)
        if not poem:
            raise HTTPException(404, "unknown poem")
        if "model" not in poem:  # made by an earlier version of the app
            return poem
        try:
            for i, clip in enumerate(poem["clips"]):
                advance(poem, i, clip)
        except CreditsError:
            save(poem)
            return credits_response(poem)
        try:
            maybe_build_film(poem)
        except subprocess.CalledProcessError as exc:
            poem["film_error"] = str(exc)[:300]
        return save(poem)


@app.post("/api/poems/{pid}/clips/{n}/retry")
def retry(pid: str, n: int):
    with _with_lock(pid):
        poem = load().get(pid)
        if not poem:
            raise HTTPException(404, "unknown poem")
        if "model" not in poem:
            raise HTTPException(409, "This poem was made by an earlier version of the app "
                                     "and can't be retried.")
        if not 0 <= n < len(poem["clips"]):
            raise HTTPException(404, "unknown line")
        clip = poem["clips"][n]
        if clip["state"] != "FAILED":
            raise HTTPException(409, "Only a failed line can be retried.")
        for k in ("error", "error_type", "request_id", "queue_position"):
            clip.pop(k, None)
        clip["state"] = "WAITING"
        try:
            try_submit(poem, clip)
        except CreditsError:
            save(poem)
            return credits_response(poem)
        return save(poem)


@app.get("/download/{name}")
def download(name: str):
    path = (OUT / name).resolve()
    if path.parent != OUT.resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4", filename=name)


app.mount("/outputs", StaticFiles(directory=OUT), name="outputs")
app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")
