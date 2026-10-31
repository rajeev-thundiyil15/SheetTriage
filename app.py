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
import queue
import threading
import time
import uuid
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent import run_agent, derive_report_path
from schema import DataSchema
from schema_infer import infer_schema

load_dotenv()

app = FastAPI(title="Data Cleaning Agent")

Path("data").mkdir(exist_ok=True)
Path("static").mkdir(exist_ok=True)

# Upload limits — keep runs bounded on a free-tier instance (cost + memory).
# Override via env vars if you move to a bigger plan.
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "5"))
MAX_UPLOAD_ROWS = int(os.getenv("MAX_UPLOAD_ROWS", "5000"))

# How long a finished job's output files stick around before being swept.
JOB_FILE_TTL_SECONDS = int(os.getenv("JOB_FILE_TTL_SECONDS", str(60 * 60)))

# job_id → {queue, output_path, done, error, created_at}
_jobs: dict[str, dict] = {}
_job_lock = threading.Lock()
_active_job: str | None = None


def _enforce_upload_limits(contents: bytes, df: pd.DataFrame) -> None:
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(413, f"File is {size_mb:.1f}MB — limit is {MAX_UPLOAD_MB}MB.")
    if len(df) > MAX_UPLOAD_ROWS:
        raise HTTPException(413, f"File has {len(df)} rows — limit is {MAX_UPLOAD_ROWS} rows.")


def _cleanup_old_jobs() -> None:
    """Delete finished jobs' output files once they're older than the TTL, and
    drop their entries so _jobs doesn't grow unbounded over the process lifetime."""
    now = time.time()
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


@app.get("/results")
async def results_page():
    return FileResponse("static/results.html")


@app.post("/infer-schema")
async def infer_schema_endpoint(
    csv_file: UploadFile = File(...),
    rules: str = Form(...),
):
    """Convert natural language rules + CSV headers into a DataSchema JSON object."""
    try:
        contents = await csv_file.read()
        df = pd.read_csv(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(400, f"Could not read CSV: {e}")

    _enforce_upload_limits(contents, df)

    try:
        schema_dict = infer_schema(
            column_names=list(df.columns),
            rules=rules,
            sample_rows=df.head(3).to_dict(orient="records"),
        )
        # Validate it parses correctly before sending back
        DataSchema.model_validate(schema_dict)
        return schema_dict
    except Exception as e:
        raise HTTPException(422, f"Could not generate schema: {e}")


@app.post("/clean")
async def clean(
    csv_file: UploadFile = File(...),
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

    try:
        contents = await csv_file.read()
        df = pd.read_csv(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(400, f"Could not read CSV: {e}")

    _enforce_upload_limits(contents, df)

    job_id = str(uuid.uuid4())
    output_path = f"data/{job_id}_clean.csv"
    log_queue: queue.Queue = queue.Queue()

    _jobs[job_id] = {
        "queue": log_queue,
        "output_path": output_path,
        "done": False,
        "created_at": time.time(),
    }

    with _job_lock:
        _active_job = job_id

    def run():
        try:
            run_agent(df, schema, output_path=output_path, mode=mode, on_log=log_queue.put)
            _jobs[job_id]["done"] = True
            report_url = f"/download/{job_id}?file=report"
            log_queue.put({"type": "done", "download_url": f"/download/{job_id}", "report_url": report_url})
        except Exception as exc:
            _jobs[job_id]["done"] = True
            log_queue.put({"type": "error", "message": str(exc)})

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")

    job = _jobs[job_id]
    q: queue.Queue = job["queue"]

    async def generate():
        loop = asyncio.get_event_loop()
        while True:
            try:
                msg = await loop.run_in_executor(None, lambda: q.get(timeout=0.3))
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                if job["done"]:
                    break
                yield ": keepalive\n\n"

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


app.mount("/static", StaticFiles(directory="static"), name="static")
