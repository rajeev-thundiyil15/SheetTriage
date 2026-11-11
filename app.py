"""
app.py — FastAPI web server.

Run with:
    uv run --no-project --with-requirements requirements.txt uvicorn app:app --reload
"""
import asyncio
import io
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import auth  # noqa: E402  (reads AUTH_MODE, so .env has to be loaded first)
import tools  # noqa: E402
from agent import run_agent, derive_report_path  # noqa: E402
from csv_input import normalize_csv  # noqa: E402
from review import build_review  # noqa: E402
from schema import DataSchema  # noqa: E402
from schema_infer import infer_schema  # noqa: E402
from storage import UNFINISHED, Store  # noqa: E402

app = FastAPI(title="SheetTriage")

Path("static").mkdir(exist_ok=True)

# Upload limits keep runs bounded in cost and memory. Override via env vars on a bigger plan.
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "5"))
MAX_UPLOAD_ROWS = int(os.getenv("MAX_UPLOAD_ROWS", "30000"))

# How long results are kept: runs made without signing in (and the demo) are kept briefly,
# account runs for longer.
JOB_FILE_TTL_SECONDS = int(os.getenv("JOB_FILE_TTL_SECONDS", str(60 * 60)))
KEEP_DAYS = {"free": 7, "paid": 30}

# Runs a month, per plan. The demo file doesn't count.
RUNS_PER_MONTH = {
    "free": int(os.getenv("FREE_RUNS_PER_MONTH", "5")),
    "paid": int(os.getenv("PAID_RUNS_PER_MONTH", "100")),
}

# Uploads kept per owner, and in total for browsers that haven't signed in, so the disk
# can't be filled by one visitor.
MAX_STORED_UPLOADS = int(os.getenv("MAX_STORED_UPLOADS", "20"))
MAX_ANONYMOUS_UPLOADS = int(os.getenv("MAX_ANONYMOUS_UPLOADS", "200"))

DEMO_FILE = Path("static/demo/event_registrations.csv")

store = Store()

# Tool state is shared by the whole process (tools._state), so only one run at a time.
_job_lock = threading.Lock()
_active_job: str | None = None

NOT_FOUND_UPLOAD = "That upload has expired. Please upload your CSV again."
NOT_FOUND_JOB = "Job not found"


# ── Who is asking ──

@app.middleware("http")
async def session_cookie(request: Request, call_next):
    new_session = auth.ensure_session(request)
    response = await call_next(request)
    if new_session:
        auth.set_session_cookie(request, response, new_session)
    return response


def _user(request: Request) -> dict | None:
    user_id = auth.signed_in_user_id(request)
    return store.get_user(user_id) if user_id else None


def _owner(request: Request) -> str:
    user = _user(request)
    return user["id"] if user else "session:" + auth.session_id(request)


def _keep_seconds(user: dict | None, demo: bool) -> float:
    if demo or not user:
        return JOB_FILE_TTL_SECONDS
    return KEEP_DAYS.get(user["plan"], KEEP_DAYS["free"]) * 24 * 60 * 60


def _month_start() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _usage(user: dict | None) -> dict:
    if not user:
        return {"runs_this_month": None, "run_limit": None}
    return {
        "runs_this_month": store.runs_since(user["id"], _month_start()),
        "run_limit": RUNS_PER_MONTH.get(user["plan"], RUNS_PER_MONTH["free"]),
    }


# ── Helpers ──

def _read_csv(contents: bytes, **kwargs) -> pd.DataFrame:
    """Only empty cells count as blank, so text like 'n/a' reaches the report as written."""
    return pd.read_csv(io.BytesIO(contents), keep_default_na=False, na_values=[""], **kwargs)


def _enforce_upload_limits(contents: bytes, df: pd.DataFrame) -> None:
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(413, f"File is {size_mb:.1f}MB — limit is {MAX_UPLOAD_MB}MB.")
    if len(df) > MAX_UPLOAD_ROWS:
        raise HTTPException(413, f"File has {len(df):,} rows — limit is {MAX_UPLOAD_ROWS:,} rows.")


def _run_error_message(exc: Exception) -> str:
    """What the results page says when a run stops, without the raw API error."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status in (529, 503) or "Overloaded" in name:
        return "Claude is overloaded right now, so the run stopped. Please try again in a few minutes."
    if status == 429:
        return "The service has reached its usage limit for now, so the run stopped. Please try again later."
    if status in (401, 403):
        return "The server's connection to Claude isn't set up correctly, so the run couldn't start."
    if status == 400 and "credit" in str(exc).lower():
        return "The service has reached its spending limit, so the run stopped. Please try again later."
    if "Connection" in name or "Timeout" in name:
        return "The server couldn't reach Claude, so the run stopped. Please try again in a few minutes."
    return f"The run stopped because of an unexpected error: {exc}"


def _make_room_for_upload(owner: str) -> None:
    """Drops the oldest uploads past the per-owner cap, and past the cap for all signed-out browsers."""
    mine = store.uploads_of(owner)
    for upload in mine[: max(0, len(mine) - MAX_STORED_UPLOADS + 1)]:
        store.delete_upload(upload["id"])
    if owner.startswith("session:"):
        anonymous = store.count_uploads("session:")
        for upload in anonymous[: max(0, len(anonymous) - MAX_ANONYMOUS_UPLOADS + 1)]:
            store.delete_upload(upload["id"])


def _get_upload(request: Request, upload_id: str) -> dict:
    upload = store.get_upload(upload_id, _owner(request))
    if not upload:
        raise HTTPException(404, NOT_FOUND_UPLOAD)
    return upload


def _get_job(request: Request, job_id: str) -> dict:
    job = store.get_job(job_id, _owner(request))
    if not job:
        raise HTTPException(404, NOT_FOUND_JOB)
    return job


def _upload_summary(upload: dict) -> dict:
    return {
        "upload_id": upload["id"],
        "filename": upload["filename"],
        "rows": upload["rows"],
        "columns": upload["columns"],
    }


def _job_summary(job: dict) -> dict:
    return {
        "job_id": job["id"],
        "filename": job["filename"],
        "rows": job["rows"],
        "mode": job["mode"],
        "status": job["status"],
        "flagged_rows": job["flagged_rows"],
        "demo": job["demo"],
        "created_at": job["created_at"],
        "expires_at": job["expires_at"],
    }


def _store_upload(request: Request, raw: bytes, filename: str, demo: bool) -> dict:
    store.delete_expired()
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

    owner = _owner(request)
    _make_room_for_upload(owner)
    upload = store.add_upload(owner, filename, contents, rows=len(df), columns=list(df.columns),
                              keep_seconds=_keep_seconds(_user(request), demo), demo=demo)
    return _upload_summary(upload)


# ── Pages ──

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


@app.get("/files")
async def files_page():
    return FileResponse("static/files.html")


@app.get("/sign-in")
async def sign_in_page():
    if not auth.accounts_enabled():
        return RedirectResponse("/app")
    return FileResponse("static/sign-in.html")


# ── Account ──

@app.get("/me")
async def me(request: Request):
    """What the pages need to show the account menu, limits and how long results are kept."""
    user = _user(request)
    return {
        "accounts": auth.accounts_enabled(),
        "auth_mode": auth.AUTH_MODE,
        "google_client_id": auth.GOOGLE_CLIENT_ID if auth.AUTH_MODE == "google" else None,
        "signed_in": user is not None,
        "email": user["email"] if user else None,
        "plan": user["plan"] if user else None,
        "keep_hours": _keep_seconds(user, demo=False) / 3600,
        "demo_keep_hours": JOB_FILE_TTL_SECONDS / 3600,
        **_usage(user),
    }


def _safe_next(next: str) -> str:
    return next if next.startswith("/") and not next.startswith("//") else "/files"


def _finish_sign_in(request: Request, response: Response, user_id: str, email: str) -> None:
    store.upsert_user(user_id, email)
    # Runs this browser made before signing in move to the account.
    store.move_owner("session:" + auth.session_id(request), user_id)
    auth.sign_in(request, response, user_id)


@app.post("/auth/google")
async def google_sign_in(request: Request):
    """Signs in with the token Google's button hands the sign-in page."""
    if auth.AUTH_MODE != "google":
        raise HTTPException(404, "Not found")
    # Only the site's own pages can send JSON here: another site can't, so it can't sign
    # someone into an account that isn't theirs.
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "Expected JSON.")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/") and             origin.split("://")[-1] != request.headers.get("host", ""):
        raise HTTPException(403, "Sign-in has to come from this site.")
    try:
        body = await request.json()
        user_id, email = auth.google_user(str(body.get("credential", "")))
    except auth.SignInError as e:
        raise HTTPException(401, str(e))
    except Exception:
        raise HTTPException(400, "The Google sign-in didn't come through. Please try again.")
    response = JSONResponse({"next": _safe_next(str(body.get("next", "/files")))})
    _finish_sign_in(request, response, user_id, email)
    return response


@app.post("/auth/dev-sign-in")
async def dev_sign_in(request: Request, email: str = Form(...), next: str = Form(default="/files")):
    if auth.AUTH_MODE != "dev":
        raise HTTPException(404, "Not found")
    email = email.strip()
    if "@" not in email or len(email) > 254:
        raise HTTPException(400, "Please enter an email address.")
    response = RedirectResponse(_safe_next(next), status_code=303)
    _finish_sign_in(request, response, auth.dev_user_id(email), email)
    return response


@app.post("/auth/sign-out")
async def sign_out():
    response = RedirectResponse("/app", status_code=303)
    auth.sign_out(response)
    return response


@app.delete("/account")
async def delete_account(request: Request):
    """Deletes the account with every upload, run and file it has."""
    user = _user(request)
    if not user:
        raise HTTPException(401, "You're not signed in.")
    store.delete_owner(user["id"])
    response = JSONResponse({"deleted": True})
    auth.sign_out(response)
    return response


# ── Uploads ──

@app.post("/upload")
async def upload_csv(request: Request, csv_file: UploadFile = File(...)):
    """Check and save a CSV so the schema and results pages can use it by id.

    Signing in is optional: without it, results belong to this browser and are kept for an hour."""
    return _store_upload(request, await csv_file.read(), csv_file.filename or "upload.csv", demo=False)


@app.post("/upload-demo")
async def upload_demo(request: Request):
    """The bundled demo file, which anyone can run without signing in."""
    return _store_upload(request, DEMO_FILE.read_bytes(), DEMO_FILE.name, demo=True)


@app.get("/uploads/{upload_id}")
async def upload_info(request: Request, upload_id: str):
    return _upload_summary(_get_upload(request, upload_id))


@app.post("/infer-schema")
async def infer_schema_endpoint(
    request: Request,
    upload_id: str = Form(...),
    rules: str = Form(default=""),
):
    """Scan the CSV and apply any rules: returns a draft schema, questions and decisions."""
    upload = _get_upload(request, upload_id)

    try:
        draft = infer_schema(_read_csv(store.upload_contents(upload["id"]), dtype=str), rules)
        # Validate it parses correctly before sending back
        DataSchema.model_validate(draft["schema"])
        return draft
    except Exception as e:
        raise HTTPException(422, f"Could not generate schema: {e}")


# ── Runs ──

@app.post("/clean")
async def clean(
    request: Request,
    upload_id: str = Form(...),
    schema_json: str = Form(...),
    mode: str = Form(default="clean"),
):
    global _active_job

    store.delete_expired()

    with _job_lock:
        active = store.get_job(_active_job, None) if _active_job else None
        if active and active["status"] in UNFINISHED:
            raise HTTPException(409, "A job is already running — please wait for it to finish.")

    try:
        schema = DataSchema.model_validate(json.loads(schema_json))
    except Exception as e:
        raise HTTPException(400, f"Invalid schema JSON: {e}")

    upload = _get_upload(request, upload_id)
    user = _user(request)
    usage = _usage(user)
    if not upload["demo"] and usage["run_limit"] is not None and usage["runs_this_month"] >= usage["run_limit"]:
        raise HTTPException(
            429, f"You've used all {usage['run_limit']} runs on your plan this month. "
                 "Runs reset on the 1st. The demo data still works.")

    contents = store.upload_contents(upload["id"])
    df = _read_csv(contents)

    job = store.add_job(_owner(request), upload, mode, schema.model_dump(),
                        keep_seconds=_keep_seconds(user, upload["demo"]))
    job_id = job["id"]
    output_path = store.output_path(job_id)

    with _job_lock:
        _active_job = job_id

    # The run keeps the store it started with, even if the module's is swapped (as tests do).
    job_store = store

    def on_log(event: dict) -> None:
        job_store.add_event(job_id, event)

    def run():
        try:
            run_agent(df, schema, output_path=str(output_path), mode=mode, on_log=on_log)
            flagged = None
            if output_path.exists():
                try:
                    review = build_review(contents, str(output_path), tools.get_error_log())
                    job_store.save_review(job_id, review)
                    flagged = review["flagged_rows"]
                except Exception as exc:
                    on_log({"type": "warning", "message": f"The review couldn't be prepared: {exc}"})
            done = {"type": "done", "download_url": f"/download/{job_id}",
                    "report_url": f"/download/{job_id}?file=report"}
            job_store.finish_job(job_id, done, status="done", flagged_rows=flagged, finished_at=time.time())
        except Exception as exc:
            message = _run_error_message(exc)
            job_store.finish_job(job_id, {"type": "error", "message": message},
                                 status="error", error=message, finished_at=time.time())

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(request: Request, job_id: str):
    _get_job(request, job_id)

    # Every connection replays the run from the start, so refreshing the page loses nothing.
    async def generate():
        sent = 0
        while True:
            new = store.events(job_id, after=sent)
            for seq, msg in new:
                sent = seq
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error"):
                    return
            if not new and store.get_job(job_id, None) is None:
                return  # deleted while someone was watching
            yield ": keepalive\n\n"
            await asyncio.sleep(0.3)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/download/{job_id}")
async def download(request: Request, job_id: str, file: str = "csv"):
    _get_job(request, job_id)
    output_path = str(store.output_path(job_id))
    if file == "report":
        path, filename = derive_report_path(output_path), "error_report.csv"
    else:
        path, filename = output_path, "output.csv"
    if not Path(path).exists():
        raise HTTPException(404, "File not ready yet")
    return FileResponse(path, media_type="text/csv", filename=filename)


@app.get("/review/{job_id}")
async def review(request: Request, job_id: str):
    """The finished output and error report, ready to show on the results page."""
    job = _get_job(request, job_id)
    if job["status"] in UNFINISHED:
        raise HTTPException(409, "The agent is still running.")
    saved = store.load_review(job_id)
    if saved is None:
        raise HTTPException(404, "This run didn't produce a file to review.")
    return saved


@app.get("/api/jobs")
async def list_jobs(request: Request):
    """The runs on the My files page: the account's, or this browser's when signed out."""
    store.delete_expired()
    return [_job_summary(job) for job in store.jobs_of(_owner(request))]


@app.get("/api/jobs/{job_id}")
async def job_info(request: Request, job_id: str):
    return _job_summary(_get_job(request, job_id))


@app.delete("/api/jobs/{job_id}")
async def delete_job(request: Request, job_id: str):
    job = _get_job(request, job_id)
    if job["status"] in UNFINISHED:
        raise HTTPException(409, "This run is still going. You can delete it once it finishes.")
    store.delete_job(job_id)
    return {"deleted": job_id}


app.mount("/static", StaticFiles(directory="static"), name="static")
