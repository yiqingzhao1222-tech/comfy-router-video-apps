"""Local text-to-video app: Higgsfield Kling 3 Pro through Comfy Router.

Generation is queued with models.submit(); the browser polls /api/jobs/{id},
which does one status() read per poll and, once COMPLETED, collects the
result with get() and downloads the video to ./outputs right away (Router
keeps results for only 24 hours).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from comfy_sdk import COMPLETED, Comfy, ComfyError, InsufficientCredits, MissingApiKey
from comfy_sdk.router_exceptions import RouterError

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True)  # .env wins over the shell

MODEL = "higgsfield/higgsfield-kling-3-pro"
OUTPUTS = ROOT / "outputs"
JOBS_FILE = ROOT / "jobs.json"
OUTPUTS.mkdir(exist_ok=True)

_lock = threading.Lock()
_client: Comfy | None = None


def client() -> Comfy:
    global _client
    if _client is None:
        if not os.environ.get("COMFY_API_KEY"):
            raise HTTPException(
                503, f"COMFY_API_KEY is missing. Put it in {ROOT / '.env'} and restart the server."
            )
        _client = Comfy()
    return _client


# --- tiny job store (survives restarts; handles are rebuilt from ids) ------
def load_jobs() -> dict:
    try:
        return json.loads(JOBS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_job(job: dict) -> dict:
    with _lock:
        jobs = load_jobs()
        jobs[job["id"]] = job
        JOBS_FILE.write_text(json.dumps(jobs, indent=2))
    return job


def credits_error() -> JSONResponse:
    return JSONResponse(
        {
            "error": "insufficient_credits",
            "detail": "Your Comfy workspace is out of credits. Add credits at "
            "platform.comfy.org, then try again.",
        },
        status_code=402,
    )


def router_error(exc: Exception) -> JSONResponse:
    status = getattr(exc, "http_status", None) or 502
    return JSONResponse(
        {
            "error": getattr(exc, "error_type", None) or getattr(exc, "code", None) or "error",
            "detail": getattr(exc, "detail", None) or str(exc),
            "comfy_request_id": getattr(exc, "request_id", None),
        },
        status_code=status if 400 <= status < 600 else 502,
    )


# --- API -------------------------------------------------------------------
class GenerateIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=2500)
    aspect_ratio: str = Field("16:9", pattern=r"^(16:9|9:16|1:1)$")
    duration: int = Field(5, ge=3, le=15)
    sound: str = Field("on", pattern=r"^(on|off)$")
    cfg_scale: float = Field(0.5, ge=0, le=1)


app = FastAPI()


@app.get("/api/health")
def health():
    return {"model": MODEL, "key_present": bool(os.environ.get("COMFY_API_KEY")),
            "env_path": str(ROOT / ".env")}


@app.post("/api/generate")
def generate(body: GenerateIn):
    try:
        handle = client().models.submit(MODEL, body.model_dump())
    except InsufficientCredits:
        return credits_error()
    except (RouterError, ComfyError, MissingApiKey) as exc:
        return router_error(exc)
    job = {
        "id": uuid.uuid4().hex[:12],
        "model": handle.model,
        "request_id": handle.request_id,
        "input": body.model_dump(),
        "state": "IN_QUEUE",
        "queue_position": None,
        "created": time.time(),
    }
    return save_job(job)


@app.get("/api/jobs")
def list_jobs():
    return sorted(load_jobs().values(), key=lambda j: j["created"], reverse=True)


@app.get("/api/jobs/{job_id}")
def poll(job_id: str):
    job = load_jobs().get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job["state"] in ("DONE", "FAILED"):
        return job

    handle = client().models.handle(job["model"], job["request_id"])
    try:
        update = handle.status()  # one poll
    except InsufficientCredits:
        return credits_error()
    except (RouterError, ComfyError) as exc:
        job.update(state="FAILED", error_type=getattr(exc, "error_type", None) or "error",
                   error=getattr(exc, "detail", None) or str(exc))
        return save_job(job)

    job["retry_after"] = update.retry_after
    job["queue_position"] = update.queue_position
    if not update.is_completed:
        job["state"] = update.status or job["state"]
        return save_job(job)

    # COMPLETED: may still be a failure
    if update.error_type:
        job.update(state="FAILED", error_type=update.error_type,
                   error=str(update.raw.get("detail") or update.raw.get("error") or ""))
        return save_job(job)

    try:
        result = handle.get(timeout=120)
    except InsufficientCredits:
        return credits_error()
    except (RouterError, ComfyError) as exc:
        job.update(state="FAILED", error_type=getattr(exc, "error_type", None) or "error",
                   error=getattr(exc, "detail", None) or str(exc))
        return save_job(job)

    job["result"] = result if isinstance(result, dict) else {"binary": True}
    pstatus = result.get("status") if isinstance(result, dict) else None
    url = ((result.get("video") or {}).get("url")) if isinstance(result, dict) else None
    if pstatus in ("nsfw", "failed", "canceled") or not url:
        job.update(state="FAILED", error_type=pstatus or "no_output",
                   error=(result.get("error") if isinstance(result, dict) else None)
                   or "The model finished without a video.")
        return save_job(job)

    # Download promptly: Router keeps results 24h and provider URLs can expire.
    path = OUTPUTS / f"{job_id}.mp4"
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    job.update(state="DONE", file=f"/outputs/{path.name}", remote_url=url, finished=time.time())
    return save_job(job)


@app.get("/download/{name}")
def download(name: str):
    path = (OUTPUTS / name).resolve()
    if path.parent != OUTPUTS.resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4", filename=name)


app.mount("/outputs", StaticFiles(directory=OUTPUTS), name="outputs")
app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
