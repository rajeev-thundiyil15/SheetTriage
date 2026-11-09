"""
app.py — FastAPI web server.

Run with:
    uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles \
           --with anthropic --with pandas --with pydantic --with python-dotenv \
           uvicorn app:app --reload
"""
import asyncio
import io
import json
import os
import threading
import time
import uuid
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import tools
from agent import run_agent, derive_report_path
from csv_input import normalize_csv
from review import build_review
from schema import DataSchema
from schema_infer import infer_schema

load_dotenv()

app = FastAPI(title="Data Cleaning Agent")

Path("data").mkdir(exist_ok=True)
Path("static").mkdir(exist_ok=True)

# Upload limits — keep runs bounded on a free-tier instance (cost + memory).
# Override via env vars if you move to a bigger plan.
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "5"))
MAX_UPLOAD_ROWS = int(os.getenv("MAX_UPLOAD_ROWS", "30000"))

# How long a finished job's output files stick around before being swept.
JOB_FILE_TTL_SECONDS = int(os.getenv("JOB_FILE_TTL_SECONDS", str(60 * 60)))

# Uploads wait in memory between the upload page and the run, so keep only a few.
MAX_STORED_UPLOADS = int(os.getenv("MAX_STORED_UPLOADS", "20"))

# upload_id → {contents, filename, rows, columns, created_at}
_uploads: dict[str, dict] = {}

# job_id → {events, output_path, done, error, created_at}
_jobs: dict[str, dict] = {}
_job_lock = threading.Lock()
_active_job: str | None = None


def _read_csv(contents: bytes, **kwargs) -> pd.DataFrame:
    """Only empty cells count as blank, so text like 'n/a' reaches the report as written."""
    return pd.read_csv(io.BytesIO(contents), keep_default_na=False, na_values=[""], **kwargs)


def _enforce_upload_limits(contents: bytes, df: pd.DataFrame) -> None:
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(413, f"File is {size_mb:.1f}MB — limit is {MAX_UPLOAD_MB}MB.")
    if len(df) > MAX_UPLOAD_ROWS:
        raise HTTPException(413, f"File has {len(df):,} rows — limit is {MAX_UPLOAD_ROWS:,} rows.")


def _cleanup_old_jobs() -> None:
    """Delete finished jobs' output files once they're older than the TTL, and
    drop their entries so _jobs doesn't grow unbounded over the process lifetime."""
    now = time.time()
    for uid in [u for u, upload in _uploads.items() if now - upload["created_at"] > JOB_FILE_TTL_SECONDS]:
        _uploads.pop(uid, None)
    with _job_lock:
        stale_ids = [
            jid for jid, job in _jobs.items()
            if job.get("done") and now - job.get("created_at", now) > JOB_FILE_TTL_SECONDS
        ]
    for jid in stale_ids:
        job = _jobs.pop(jid, None)
        if not job:
            continue
        for path in (job["output_path"], derive_report_path(job["output_path"])):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass


@app.get("/")
async def home():
    return FileResponse("static/home.html")


@app.get("/app")
async def tool():
    return FileResponse("static/index.html")


@app.get("/schema")
async def schema_page():
    return FileResponse("static/schema.html")


@app.get("/results")
async def results_page():
    return FileResponse("static/results.html")


def _get_upload(upload_id: str) -> dict:
    upload = _uploads.get(upload_id)
    if not upload:
        raise HTTPException(404, "That upload has expired. Please upload your CSV again.")
    return upload


def _upload_summary(upload_id: str, upload: dict) -> dict:
    return {
        "upload_id": upload_id,
        "filename": upload["filename"],
        "rows": upload["rows"],
        "columns": upload["columns"],
    }


@app.post("/upload")
async def upload_csv(csv_file: UploadFile = File(...)):
    """Check and hold a CSV so the schema and results pages can use it by id."""
    _cleanup_old_jobs()

    raw = await csv_file.read()
    size_mb = len(raw) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(413, f"File is {size_mb:.1f}MB — limit is {MAX_UPLOAD_MB}MB.")
    try:
        contents, _ = normalize_csv(raw)
        df = _read_csv(contents)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Could not read CSV: {e}")

    _enforce_upload_limits(raw, df)

    while len(_uploads) >= MAX_STORED_UPLOADS:
        _uploads.pop(min(_uploads, key=lambda u: _uploads[u]["created_at"]))

    upload_id = str(uuid.uuid4())
    _uploads[upload_id] = {
        "contents": contents,
        "filename": csv_file.filename,
        "rows": len(df),
        "columns": list(df.columns),
        "created_at": time.time(),
    }
    return _upload_summary(upload_id, _uploads[upload_id])


@app.get("/uploads/{upload_id}")
async def upload_info(upload_id: str):
    return _upload_summary(upload_id, _get_upload(upload_id))


@app.post("/infer-schema")
async def infer_schema_endpoint(
    upload_id: str = Form(...),
    rules: str = Form(default=""),
):
    """Scan the CSV and apply any rules: returns a draft schema, questions and decisions."""
    upload = _get_upload(upload_id)

    try:
        draft = infer_schema(_read_csv(upload["contents"], dtype=str), rules)
        # Validate it parses correctly before sending back
        DataSchema.model_validate(draft["schema"])
        return draft
    except Exception as e:
        raise HTTPException(422, f"Could not generate schema: {e}")


@app.post("/clean")
async def clean(
    upload_id: str = Form(...),
    schema_json: str = Form(...),
    mode: str = Form(default="clean"),
):
    global _active_job

    _cleanup_old_jobs()

    with _job_lock:
        if _active_job and not _jobs.get(_active_job, {}).get("done"):
            raise HTTPException(409, "A job is already running — please wait for it to finish.")

    try:
        schema = DataSchema.model_validate(json.loads(schema_json))
    except Exception as e:
        raise HTTPException(400, f"Invalid schema JSON: {e}")

    contents = _get_upload(upload_id)["contents"]
    df = _read_csv(contents)

    job_id = str(uuid.uuid4())
    output_path = f"data/{job_id}_clean.csv"
    events: list[dict] = []

    _jobs[job_id] = {
        "events": events,
        "output_path": output_path,
        "contents": contents,
        "done": False,
        "created_at": time.time(),
    }

    with _job_lock:
        _active_job = job_id

    def run():
        try:
            run_agent(df, schema, output_path=output_path, mode=mode, on_log=events.append)
            _jobs[job_id]["issues"] = tools.get_error_log()
            _jobs[job_id]["done"] = True
            report_url = f"/download/{job_id}?file=report"
            events.append({"type": "done", "download_url": f"/download/{job_id}", "report_url": report_url})
        except Exception as exc:
            _jobs[job_id]["done"] = True
            events.append({"type": "error", "message": str(exc)})

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")

    events: list[dict] = _jobs[job_id]["events"]

    # Every connection replays the run from the start, so refreshing the page loses nothing.
    async def generate():
        sent = 0
        while True:
            while sent < len(events):
                msg = events[sent]
                sent += 1
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error"):
                    return
            yield ": keepalive\n\n"
            await asyncio.sleep(0.3)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/download/{job_id}")
async def download(job_id: str, file: str = "csv"):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    if file == "report":
        path = derive_report_path(_jobs[job_id]["output_path"])
        filename = "error_report.csv"
    else:
        path = _jobs[job_id]["output_path"]
        filename = "output.csv"
    if not Path(path).exists():
        raise HTTPException(404, "File not ready yet")
    return FileResponse(path, media_type="text/csv", filename=filename)


@app.get("/review/{job_id}")
async def review(job_id: str):
    """The finished output and error report, ready to show on the results page."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.get("done"):
        raise HTTPException(409, "The agent is still running.")
    if "review" not in job:
        if not Path(job["output_path"]).exists():
            raise HTTPException(404, "This run didn't produce a file to review.")
        job["review"] = build_review(job["contents"], job["output_path"], job.get("issues", []))
        job["contents"] = None
    return job["review"]


app.mount("/static", StaticFiles(directory="static"), name="static")
