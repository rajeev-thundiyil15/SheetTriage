"""The real app with a scripted agent, so browser tests are fast, free and repeatable.

Only the Claude loop is replaced. Uploads, schema building, validation, saving and the
review all run the real code. Started by playwright.config.ts; never used in production.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(os.getenv("PORT", "8010"))

# Each test server saves to its own folder, emptied at start so every test run begins clean.
DATA_DIR = ROOT / "e2e" / f".data-{PORT}"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["DATA_DIR"] = str(DATA_DIR)

import app as web  # noqa: E402
import suggestions  # noqa: E402
import tools  # noqa: E402
from agent import derive_report_path  # noqa: E402

STEP_SECONDS = 0.1

# A column with one of these names in a test file changes how the scripted run behaves.
FAIL_COLUMN = "simulate_api_overloaded"  # the run stops the way an overloaded Claude API stops it
SLOW_COLUMN = "simulate_slow_run"  # each step takes SLOW_STEP_SECONDS, so a second run can be tried
SLOW_STEP_SECONDS = 2.5  # 3 columns: about 8 s, long enough for a second tab to upload and press Run


class OverloadedError(Exception):
    """Stands in for the SDK's error: the app only reads the status code, as it does for the real one."""
    status_code = 529


def _overloaded_error():
    return OverloadedError("Error code: 529 - {'type': 'error', 'error': {'type': 'overloaded_error'}}")


def fake_run_agent(df, schema, output_path, mode, on_log, api_key=None):
    """Flags every schema violation, then saves, sending the events a real run sends."""
    tools.set_dataframe(df)
    columns = list(schema.columns)
    on_log({"type": "start"})

    step_seconds = SLOW_STEP_SECONDS if SLOW_COLUMN in df.columns else STEP_SECONDS
    flagged = 0
    for step, column in enumerate(columns, start=1):
        time.sleep(step_seconds)
        if FAIL_COLUMN in df.columns and step == 2:
            raise _overloaded_error()
        on_log({"type": "iteration", "n": step})
        on_log({"type": "usage", "model": "scripted", "input_tokens": 1000, "output_tokens": 100,
                "cache_write_tokens": 0, "cache_read_tokens": 0})
        on_log({"type": "tool_call", "name": "mark_remaining_violations", "input_summary": f"Checking {column}"})
        flagged += json.loads(tools.mark_remaining_violations(column, schema))["marked"]
        status = {name: "clean" if i < step else "pending" for i, name in enumerate(columns)}
        on_log({"type": "progress", "columns": status, "fixed": 0, "flagged": flagged})

    saved = tools.save_with_report(
        output_path,
        derive_report_path(output_path),
        exclude_error_rows=mode == "strict",
    )
    on_log({"type": "summary", **json.loads(saved)})
    on_log({"type": "finish"})


web.run_agent = fake_run_agent


def fake_rank(cells, on_log=None, api_key=None):
    """Recommends the first option the code found for each cell, as Claude would but without a call."""
    return {c["id"]: (0, f"Scripted: '{c['options'][0]['value']}' is the most likely fix.") for c in cells}


suggestions.rank_with_ai = fake_rank

# A visitor's own API key: keys with "refused" in them fail the check, the way Anthropic refuses
# a wrong key; any other well-formed key is accepted. No call is made either way.
REFUSED_KEY_MESSAGE = "Anthropic didn't accept that API key. Check you copied all of it, or create a new one."


def fake_check_own_key(api_key):
    return REFUSED_KEY_MESSAGE if "refused" in api_key else None


web.check_own_key = fake_check_own_key


# ── A stand-in for Stripe, on the server with sign-in turned on ──
# Stripe's payment page becomes /test/stripe/pay, which "pays" and sends the app the same
# checkout.session.completed message Stripe would, two seconds later, the way Stripe's message
# can arrive after the person is back on the site. Stripe's billing page becomes
# /test/stripe/billing, which cancels the subscription the way someone would there.
if os.getenv("AUTH_MODE") == "dev":
    import threading
    from urllib.parse import quote

    from fastapi.responses import RedirectResponse

    import billing

    billing.STRIPE_SECRET_KEY = "sk_test_scripted"
    billing.STRIPE_WEBHOOK_SECRET = "whsec_scripted"
    billing.STRIPE_PRICE_ID = "price_scripted"
    subscriptions = {}
    PAYMENT_MESSAGE_DELAY = 2.0

    def fake_subscription(user_id, status="active"):
        return {"id": f"sub_{user_id}", "status": status, "customer": f"cus_{user_id}",
                "cancel_at_period_end": False, "metadata": {"user_id": user_id},
                "items": {"data": [{"current_period_end": int(time.time()) + 30 * 24 * 3600}]}}

    billing.create_checkout = lambda user, success_url, cancel_url: \
        f"/test/stripe/pay?user={quote(user['id'])}&next={quote(success_url)}"
    billing.create_portal = lambda customer_id, return_url: \
        f"/test/stripe/billing?customer={quote(customer_id)}&next={quote(return_url)}"
    billing.get_subscription = lambda subscription_id: subscriptions[subscription_id]
    billing.cancel_subscription = lambda subscription_id: subscriptions.pop(subscription_id, None)

    @web.app.get("/test/stripe/pay")
    def fake_payment_page(user: str, next: str):
        subscriptions[f"sub_{user}"] = fake_subscription(user)
        event = {"id": f"evt_pay_{user}", "type": "checkout.session.completed", "data": {"object": {
            "mode": "subscription", "client_reference_id": user, "customer": f"cus_{user}",
            "subscription": f"sub_{user}"}}}
        threading.Timer(PAYMENT_MESSAGE_DELAY, billing.handle_event, args=(web.store, event)).start()
        return RedirectResponse(next, status_code=303)

    @web.app.get("/test/stripe/billing")
    def fake_billing_page(customer: str, next: str):
        user_id = customer.removeprefix("cus_")
        subscriptions[f"sub_{user_id}"] = fake_subscription(user_id, status="canceled")
        billing.handle_event(web.store, {"id": f"evt_end_{user_id}", "type": "customer.subscription.deleted",
                                         "data": {"object": {"id": f"sub_{user_id}"}}})
        return RedirectResponse(next, status_code=303)

if __name__ == "__main__":
    uvicorn.run(web.app, host="127.0.0.1", port=PORT, log_level="warning")
