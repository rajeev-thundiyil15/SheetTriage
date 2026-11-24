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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

load_dotenv()

import auth  # noqa: E402  (reads AUTH_MODE, so .env has to be loaded first)
import billing  # noqa: E402  (reads the Stripe settings)
import suggestions as fixes  # noqa: E402
import tools  # noqa: E402
from agent import OWN_KEY_SHAPE, check_own_key, derive_report_path, own_key_problem, run_agent  # noqa: E402
from csv_input import normalize_csv  # noqa: E402
from review import build_review  # noqa: E402
from schema import DataSchema  # noqa: E402
from schema_infer import infer_schema  # noqa: E402
from storage import UNFINISHED, Store  # noqa: E402

app = FastAPI(title="SheetTriage")
# The review of a large file is several MB of JSON; compressed, it's a tenth of that.
# Live progress (text/event-stream) is left uncompressed by the middleware.
app.add_middleware(GZipMiddleware, minimum_size=1024)

Path("static").mkdir(exist_ok=True)

# Upload limits keep runs bounded in cost and memory. Override via env vars on a bigger plan.
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "5"))
MAX_UPLOAD_ROWS = int(os.getenv("MAX_UPLOAD_ROWS", "30000"))

# How long results are kept: runs made without signing in (and the demo) are kept briefly,
# account runs for longer.
JOB_FILE_TTL_SECONDS = int(os.getenv("JOB_FILE_TTL_SECONDS", str(60 * 60)))
KEEP_DAYS = {"free": 7, "paid": 30}

# Run limits, so nobody can run up the Claude bill. Everyone not on a paid plan gets the
# same daily number: visitors are counted by their (scrambled) address, signed-in free
# accounts by account, so signing in never means fewer runs. The demo file doesn't count.
FREE_RUNS_PER_DAY = int(os.getenv("FREE_RUNS_PER_DAY", "3"))
PAID_RUNS_PER_MONTH = int(os.getenv("PAID_RUNS_PER_MONTH", "100"))
DAY = 24 * 60 * 60

# Uploads kept per owner, and in total for browsers that haven't signed in, so the disk
# can't be filled by one visitor.
MAX_STORED_UPLOADS = int(os.getenv("MAX_STORED_UPLOADS", "20"))
MAX_ANONYMOUS_UPLOADS = int(os.getenv("MAX_ANONYMOUS_UPLOADS", "200"))

DEMO_FILE = Path("static/demo/event_registrations.csv")

# The Try demo data button is for local work and the browser tests. It's off on Railway,
# which sets these variables on every deploy, unless DEMO_DATA=on says otherwise.
RAILWAY_VARIABLES = ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")
ON_RAILWAY = any(os.getenv(name) for name in RAILWAY_VARIABLES)
DEMO_DATA = os.getenv("DEMO_DATA", "off" if ON_RAILWAY else "on").lower() == "on"

store = Store()

# Runs happen side by side on a small pool of threads; each thread has its own tool state
# (tools._state). Past that, a few more wait their turn, and the rest are asked to try later.
MAX_RUNS_AT_ONCE = int(os.getenv("MAX_RUNS_AT_ONCE", "2"))
MAX_WAITING_RUNS = int(os.getenv("MAX_WAITING_RUNS", "4"))
_runs = ThreadPoolExecutor(max_workers=MAX_RUNS_AT_ONCE, thread_name_prefix="run")
_job_lock = threading.Lock()

BUSY_MESSAGE = "SheetTriage is busy with other people's files right now. Please try again in a few minutes."

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


def _plan(user: dict | None) -> str:
    """free or paid. Paid only while Stripe says the subscription is live (see billing.is_paid)."""
    return "paid" if billing.is_paid(user) else "free"


def _billing_details(user: dict) -> dict:
    """What the account menu and the plans page show about the monthly plan."""
    paid = _plan(user) == "paid"
    return {
        "paid_until": user.get("paid_until") if paid else None,
        "cancel_at_period_end": bool(user.get("cancel_at_period_end")) and paid,
        "past_due": paid and user.get("plan_status") == "past_due",
        "can_manage_billing": bool(user.get("stripe_customer_id")),
    }


def _keep_seconds(user: dict | None, demo: bool) -> float:
    if demo or not user:
        return JOB_FILE_TTL_SECONDS
    return KEEP_DAYS[_plan(user)] * 24 * 60 * 60


def _month_start(months_ahead: int = 0) -> float:
    now = datetime.now(timezone.utc)
    month = now.month - 1 + months_ahead
    return now.replace(year=now.year + month // 12, month=month % 12 + 1, day=1,
                       hour=0, minute=0, second=0, microsecond=0).timestamp()


def _usage(request: Request, user: dict | None) -> dict:
    """How many runs this person has used, their limit, and when the next one frees up."""
    if _plan(user) == "paid":
        used = len(store.run_times(_month_start(), owner=user["id"]))
        limit = PAID_RUNS_PER_MONTH
        return {"runs_used": used, "run_limit": limit, "run_period": "month",
                "next_run_at": _month_start(1) if used >= limit else None}
    since = time.time() - DAY
    if user:
        times = store.run_times(since, owner=user["id"])
    else:
        times = store.run_times(since, client=auth.client_key(request))
    # The window rolls: each run stops counting 24 hours after it started.
    full = len(times) >= FREE_RUNS_PER_DAY
    return {"runs_used": len(times), "run_limit": FREE_RUNS_PER_DAY, "run_period": "day",
            "next_run_at": times[len(times) - FREE_RUNS_PER_DAY] + DAY if full else None}


def _wait_text(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(minutes / 60)
    return f"about {hours} hour{'s' if hours != 1 else ''}"


def _limit_message(usage: dict) -> str:
    if usage["run_period"] == "month":
        return (f"You've used all {usage['run_limit']} runs on your plan this month. "
                "They reset on the 1st.")
    return (f"You've used your {usage['run_limit']} free runs for today. "
            f"You can run another file in {_wait_text(usage['next_run_at'] - time.time())}, "
            "or add your own Anthropic API key for as many runs as you like."
            + (f" The monthly plan ({billing.PRICE_TEXT}) gives up to {PAID_RUNS_PER_MONTH} runs a month."
               if billing.enabled() and auth.accounts_enabled() else ""))


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


# Marks a refusal as being about the visitor's own key, so the page shows it beside the key field.
KEY_PROBLEM = {"X-Problem": "api-key"}


def _run_error_message(exc: Exception, api_key: str | None = None) -> str:
    """What the results page says when a run stops, without the raw API error.

    On a visitor's own key the problem is with their account, so the message says so; and the
    key never reaches the message, even if an unexpected error happened to include it."""
    if api_key:
        if getattr(exc, "status_code", None) is not None:
            return own_key_problem(exc)
        return _run_error_message(exc).replace(api_key, "[your API key]")
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


@app.get("/privacy")
async def privacy_page():
    return FileResponse("static/privacy.html")


@app.get("/terms")
async def terms_page():
    return FileResponse("static/terms.html")


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
        "plan": _plan(user) if user else None,
        "billing": billing.enabled() and auth.accounts_enabled(),
        "price_text": billing.PRICE_TEXT,
        "paid_runs_per_month": PAID_RUNS_PER_MONTH,
        **(_billing_details(user) if user else {}),
        "keep_hours": _keep_seconds(user, demo=False) / 3600,
        "demo_keep_hours": JOB_FILE_TTL_SECONDS / 3600,
        "demo_data": DEMO_DATA,
        **_usage(request, user),
    }


def _require_own_page(request: Request, refusal: str) -> None:
    """Only the site's own pages can send JSON with the person's cookies: another site can't,
    so it can't sign someone in, or start a payment, on their behalf."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "Expected JSON.")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/") and             origin.split("://")[-1] != request.headers.get("host", ""):
        raise HTTPException(403, refusal)


def _site_url(request: Request) -> str:
    """The site's address for Stripe to send people back to: https behind Railway's proxy."""
    if os.getenv("SITE_URL"):
        return os.getenv("SITE_URL").rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    return f"{proto}://{request.headers.get('host', request.url.netloc)}"


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
    _require_own_page(request, "Sign-in has to come from this site.")
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
    # A live subscription is cancelled first, so a deleted account never goes on being charged.
    if user.get("subscription_id") and user.get("plan_status") not in (None, "canceled", "incomplete_expired"):
        try:
            await asyncio.to_thread(billing.cancel_subscription, user["subscription_id"])
        except billing.BillingError as e:
            raise HTTPException(502, str(e))
    store.delete_owner(user["id"])
    response = JSONResponse({"deleted": True})
    auth.sign_out(response)
    return response


# ── The monthly plan (Stripe) ──

@app.get("/pricing")
async def pricing_page():
    return FileResponse("static/pricing.html")


def _billing_user(request: Request) -> dict:
    if not (billing.enabled() and auth.accounts_enabled()):
        raise HTTPException(404, "Not found")
    _require_own_page(request, "Billing has to start from this site.")
    user = _user(request)
    if not user:
        raise HTTPException(401, "Sign in first, so the plan is saved to your account.")
    return user


@app.post("/billing/checkout")
async def start_checkout(request: Request):
    """Sends a signed-in person to Stripe's payment page for the monthly plan."""
    user = _billing_user(request)
    if _plan(user) == "paid":
        raise HTTPException(409, "You're already on the monthly plan. Manage billing changes or cancels it.")
    site = _site_url(request)
    try:
        url = await asyncio.to_thread(billing.create_checkout, user, f"{site}/files?upgraded=1", f"{site}/pricing")
    except billing.BillingError as e:
        raise HTTPException(502, str(e))
    return {"url": url}


@app.post("/billing/portal")
async def open_billing_portal(request: Request):
    """Sends a subscriber to Stripe's page for cancelling, changing the card and seeing invoices."""
    user = _billing_user(request)
    if not user.get("stripe_customer_id"):
        raise HTTPException(400, "There's no billing on this account yet.")
    try:
        url = await asyncio.to_thread(billing.create_portal, user["stripe_customer_id"], f"{_site_url(request)}/pricing")
    except billing.BillingError as e:
        raise HTTPException(502, str(e))
    return {"url": url}


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """Stripe's signed messages about payments and subscriptions: the only thing that changes a plan."""
    if not billing.enabled():
        raise HTTPException(404, "Not found")
    payload = await request.body()
    try:
        event = billing.read_event(payload, request.headers.get("stripe-signature"))
    except Exception:
        raise HTTPException(400, "That message isn't signed by Stripe.")
    if store.stripe_event_seen(event["id"]):
        return {"received": True}
    # If Stripe can't be reached to read the subscription, this fails and Stripe sends it again.
    await asyncio.to_thread(billing.handle_event, store, event)
    store.record_stripe_event(event["id"], event.get("type", ""))
    return {"received": True}


# ── Uploads ──

@app.post("/upload")
async def upload_csv(request: Request, csv_file: UploadFile = File(...)):
    """Check and save a CSV so the schema and results pages can use it by id.

    Signing in is optional: without it, results belong to this browser and are kept for an hour."""
    return _store_upload(request, await csv_file.read(), csv_file.filename or "upload.csv", demo=False)


@app.post("/upload-demo")
async def upload_demo(request: Request):
    """The bundled demo file, for local work and the browser tests."""
    if not DEMO_DATA:
        raise HTTPException(404, "Not found")
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

def _prepare_suggestions(job_store: Store, job_id: str, output_path: Path, review: dict,
                         schema: DataSchema, mode: str, on_log, api_key: str | None = None) -> None:
    """Suggested fixes for the flagged cells, and likely duplicates, for the person to approve.

    Strict mode has already left the flagged rows out of the file, so there's nothing to fix there."""
    if mode == "strict":
        return
    own = {"api_key": api_key} if api_key else {}
    try:
        output = pd.read_csv(output_path, dtype=str, keep_default_na=False)
        found = fixes.build_suggestions(output, review["report"], schema,
                                        rank=lambda cells: fixes.rank_with_ai(cells, on_log, **own))
        job_store.save_suggestions(job_id, found)
        cells = sum(1 for f in found if f["kind"] == "cell" and f["options"])
        copies = sum(1 for f in found if f["kind"] == "duplicate")
        if cells or copies:
            on_log({"type": "suggestions", "fixes": cells, "duplicates": copies})
    except Exception as exc:
        on_log({"type": "warning", "message": f"Suggested fixes couldn't be prepared: {exc}"})


@app.post("/clean")
async def clean(
    request: Request,
    upload_id: str = Form(...),
    schema_json: str = Form(...),
    mode: str = Form(default="clean"),
    api_key: str = Form(default=""),
):
    """api_key is optional: a visitor's own Anthropic key. The run then goes on their account and
    doesn't count toward the free limit. It's held in memory for this run only, never stored."""
    store.delete_expired()

    try:
        schema = DataSchema.model_validate(json.loads(schema_json))
    except Exception as e:
        raise HTTPException(400, f"Invalid schema JSON: {e}")

    upload = _get_upload(request, upload_id)
    user = _user(request)
    api_key = api_key.strip() or None
    if api_key:
        if not OWN_KEY_SHAPE.fullmatch(api_key):
            raise HTTPException(400, "That doesn't look like an Anthropic API key. Keys start with sk-ant-.",
                                headers=KEY_PROBLEM)
        problem = await asyncio.to_thread(check_own_key, api_key)
        if problem:
            raise HTTPException(400, problem, headers=KEY_PROBLEM)
    else:
        usage = _usage(request, user)
        if not upload["demo"] and usage["runs_used"] >= usage["run_limit"]:
            raise HTTPException(429, _limit_message(usage))

    contents = store.upload_contents(upload["id"])
    df = _read_csv(contents)

    # Counting and adding happen under one lock, so two requests can't both take the last place.
    with _job_lock:
        running = store.count_jobs("running")
        waiting = store.count_jobs("queued")
        if running + waiting >= MAX_RUNS_AT_ONCE + MAX_WAITING_RUNS:
            raise HTTPException(409, BUSY_MESSAGE)
        job = store.add_job(_owner(request), upload, mode, schema.model_dump(),
                            keep_seconds=_keep_seconds(user, upload["demo"]), client=auth.client_key(request),
                            own_key=bool(api_key))
    job_id = job["id"]
    output_path = store.output_path(job_id)
    if running + waiting >= MAX_RUNS_AT_ONCE:
        store.add_event(job_id, {"type": "queued", "ahead": running + waiting - MAX_RUNS_AT_ONCE + 1})

    # The run keeps the store it started with, even if the module's is swapped (as tests do).
    job_store = store
    # Only passed when there is one, so the agent (or a test's stand-in) uses the server's key otherwise.
    own = {"api_key": api_key} if api_key else {}

    def on_log(event: dict) -> None:
        # Token counts go on the run's record for the cost report; they aren't shown to people.
        if event.get("type") == "usage":
            job_store.add_usage(job_id, event)
            return
        job_store.add_event(job_id, event)

    def run():
        try:
            job_store.update_job(job_id, status="running")
            run_agent(df, schema, output_path=str(output_path), mode=mode, on_log=on_log, **own)
            flagged = None
            if output_path.exists():
                try:
                    review = build_review(contents, str(output_path), tools.get_error_log())
                    job_store.save_review(job_id, review)
                    flagged = review["flagged_rows"]
                except Exception as exc:
                    on_log({"type": "warning", "message": f"The review couldn't be prepared: {exc}"})
                else:
                    _prepare_suggestions(job_store, job_id, output_path, review, schema, mode, on_log, api_key)
            done = {"type": "done", "download_url": f"/download/{job_id}",
                    "report_url": f"/download/{job_id}?file=report"}
            job_store.finish_job(job_id, done, status="done", flagged_rows=flagged, finished_at=time.time())
        except Exception as exc:
            message = _run_error_message(exc, api_key)
            job_store.finish_job(job_id, {"type": "error", "message": message},
                                 status="error", error=message, finished_at=time.time())

    def run_then_tidy():
        try:
            run()
        finally:
            tools.reset_state()  # the pool reuses threads; don't keep this run's data around

    _runs.submit(run_then_tidy)
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
    elif file == "remaining":
        path, filename = output_path, "still_to_fix.csv"
    else:
        path, filename = output_path, "output.csv"
    if not Path(path).exists():
        raise HTTPException(404, "File not ready yet")
    decisions = store.load_decisions(job_id)
    if file == "remaining":
        review, found = store.load_review(job_id), store.load_suggestions(job_id)
        if review is None:
            raise HTTPException(404, "This run didn't produce a file to review.")
        body = fixes.remaining_csv(Path(path).read_text(encoding="utf-8"), review, found, decisions)
        return Response(body, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="still_to_fix.csv"'})
    if not decisions:
        return FileResponse(path, media_type="text/csv", filename=filename)
    # The person's decisions are applied on the way out; the files from the run stay as they were.
    review, found = store.load_review(job_id), store.load_suggestions(job_id)
    if file == "report":
        body = fixes.decided_report_csv(review, found, decisions)
    else:
        body = fixes.decided_output_csv(Path(path).read_text(encoding="utf-8"), review, found, decisions)
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/review/{job_id}")
async def review(request: Request, job_id: str):
    """The finished output and error report, ready to show on the results page."""
    job = _get_job(request, job_id)
    if job["status"] in UNFINISHED:
        raise HTTPException(409, "The agent is still running.")
    saved = store.load_review(job_id)
    if saved is None:
        raise HTTPException(404, "This run didn't produce a file to review.")
    return fixes.apply_to_review(saved, store.load_suggestions(job_id), store.load_decisions(job_id))


def _decidable(request: Request, job_id: str) -> tuple[list[dict], dict]:
    job = _get_job(request, job_id)
    if job["status"] in UNFINISHED:
        raise HTTPException(409, "The agent is still running.")
    found = store.load_suggestions(job_id)
    if store.load_review(job_id) is None or not found:
        raise HTTPException(404, "This run has nothing to decide.")
    return found, store.load_decisions(job_id)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Expected JSON.")
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected JSON.")
    return body


def _column_rules(request: Request, job_id: str, column: str):
    schema = DataSchema.model_validate(_get_job(request, job_id)["schema"])
    return schema.columns.get(column)


def _record(job_id: str, found: list[dict], decisions: dict) -> dict:
    store.save_decisions(job_id, decisions)
    return fixes.apply_to_review(store.load_review(job_id), found, decisions)


@app.post("/api/jobs/{job_id}/decisions")
async def decide(request: Request, job_id: str):
    """Records one decision: use a suggested fix, type a value, keep the value, remove a copy, or leave it."""
    found, decisions = _decidable(request, job_id)
    body = await _json(request)
    suggestion = next((f for f in found if f["id"] == body.get("id")), None)
    if suggestion is None:
        raise HTTPException(404, "That suggestion doesn't exist.")
    if body.get("action") == "edit" and suggestion["kind"] == "cell":
        rules = _column_rules(request, job_id, suggestion["column"])
        _, problem = fixes.check_value(body.get("value"), rules)
        if problem:
            raise HTTPException(400, problem)
        decision = fixes.check_decision(suggestion, body, rules)
    else:
        decision = fixes.check_decision(suggestion, body)
    if decision is None:
        raise HTTPException(400, "That isn't one of the choices for this suggestion.")
    if decision["action"] == "leave":
        decisions.pop(suggestion["id"], None)
    else:
        decisions[suggestion["id"]] = decision
    return _record(job_id, found, decisions)


@app.post("/api/jobs/{job_id}/decisions/bulk")
async def decide_many(request: Request, job_id: str):
    """The same decision for a group of cells or duplicates, such as every 45 in nights."""
    found, decisions = _decidable(request, job_id)
    body = await _json(request)
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 50_000:
        raise HTTPException(400, "Expected a list of suggestions.")
    by_id = {f["id"]: f for f in found}
    chosen = [by_id.get(i) for i in ids]
    if any(s is None for s in chosen):
        raise HTTPException(404, "One of those suggestions doesn't exist.")
    if body.get("action") == "edit":
        raise HTTPException(400, "Type a value one cell at a time.")
    checked = [(s, fixes.check_decision(s, body)) for s in chosen]
    if any(d is None for _, d in checked):
        raise HTTPException(400, "That isn't one of the choices for all of those suggestions.")
    for s, d in checked:
        if d["action"] == "leave":
            decisions.pop(s["id"], None)
        else:
            decisions[s["id"]] = d
    return _record(job_id, found, decisions)


@app.post("/api/jobs/{job_id}/decisions/rule")
async def change_rule(request: Request, job_id: str):
    """Allows a value, or moves a minimum or maximum, and keeps every flagged cell that change clears."""
    found, decisions = _decidable(request, job_id)
    body = await _json(request)
    column, rule, value = body.get("column"), body.get("rule"), body.get("value")
    if rule not in ("allow", "max", "min") or not isinstance(column, str) or not isinstance(value, str):
        raise HTTPException(400, "That isn't a rule change SheetTriage can make.")
    ids = fixes.rule_change_cells(found, column, rule, value)
    if not ids:
        raise HTTPException(400, "That change wouldn't clear any flagged cells.")
    rules = decisions.setdefault("_rules", [])
    rule_id = f"r{max([int(r['id'][1:]) for r in rules] or [0]) + 1}"
    rules.append({"id": rule_id, "column": column, "rule": rule, "value": value, "cells": len(ids)})
    for i in ids:
        decisions[i] = {"action": "keep", "rule": rule_id}
    return _record(job_id, found, decisions)


@app.post("/api/jobs/{job_id}/decisions/rule/undo")
async def undo_rule(request: Request, job_id: str):
    """Takes back a rule change and the flags it cleared."""
    found, decisions = _decidable(request, job_id)
    rule_id = (await _json(request)).get("id")
    rules = decisions.get("_rules", [])
    if not any(r["id"] == rule_id for r in rules):
        raise HTTPException(404, "That rule change doesn't exist.")
    decisions["_rules"] = [r for r in rules if r["id"] != rule_id]
    for key in [k for k, d in decisions.items() if isinstance(d, dict) and d.get("rule") == rule_id]:
        decisions.pop(key)
    return _record(job_id, found, decisions)


@app.post("/api/jobs/{job_id}/decisions/recommended")
async def accept_recommended(request: Request, job_id: str):
    """Uses every recommended fix that hasn't been decided yet."""
    found, decisions = _decidable(request, job_id)
    for f in found:
        if f["kind"] == "cell" and f["recommended"] is not None and f["id"] not in decisions:
            decisions[f["id"]] = {"action": "accept", "option": f["recommended"]}
    store.save_decisions(job_id, decisions)
    return fixes.apply_to_review(store.load_review(job_id), found, decisions)


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
