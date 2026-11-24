"""The monthly plan through Stripe: checkout, webhook messages, the plan they set, and cancelling.

Stripe's servers are never called: its calls are replaced, and webhook messages are signed here
with a test secret, so the real signature check in the Stripe library runs on every one."""
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import billing
from storage import Store

SECRET = "whsec_test_secret"
NEXT_MONTH = time.time() + 30 * 24 * 3600


@pytest.fixture
def stripe(monkeypatch, tmp_path):
    """Billing on, with Stripe's side played by this fixture. Returns what was asked of 'Stripe'."""
    store = Store(tmp_path)
    monkeypatch.setattr(web, "store", store)
    monkeypatch.setattr(auth, "AUTH_MODE", "dev")
    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", "price_test")

    fake = {"checkouts": [], "portals": [], "cancelled": [], "subscriptions": {}, "reads": 0, "store": store}

    def create_checkout(user, success_url, cancel_url):
        fake["checkouts"].append({"user": dict(user), "success_url": success_url, "cancel_url": cancel_url})
        return "https://checkout.stripe.test/c/pay"

    def create_portal(customer_id, return_url):
        fake["portals"].append((customer_id, return_url))
        return "https://billing.stripe.test/p/session"

    def get_subscription(subscription_id):
        fake["reads"] += 1
        return fake["subscriptions"][subscription_id]

    def cancel_subscription(subscription_id):
        fake["cancelled"].append(subscription_id)

    monkeypatch.setattr(billing, "create_checkout", create_checkout)
    monkeypatch.setattr(billing, "create_portal", create_portal)
    monkeypatch.setattr(billing, "get_subscription", get_subscription)
    monkeypatch.setattr(billing, "cancel_subscription", cancel_subscription)
    return fake


def signed_in(email="ana@example.com"):
    client = TestClient(web.app)
    assert client.post("/auth/dev-sign-in", data={"email": email}, follow_redirects=False).status_code == 303
    return client


def user_id(email="ana@example.com"):
    return auth.dev_user_id(email)


def subscription(sub_id="sub_1", status="active", customer="cus_1", user=None, period_end=NEXT_MONTH,
                 cancel_at_period_end=False):
    return {"id": sub_id, "object": "subscription", "status": status, "customer": customer,
            "cancel_at_period_end": cancel_at_period_end,
            "metadata": {"user_id": user} if user else {},
            "items": {"data": [{"id": "si_1", "current_period_end": int(period_end)}]}}


def send(client, event_id, kind, obj, secret=SECRET):
    """Posts a webhook message signed the way Stripe signs them."""
    payload = json.dumps({"id": event_id, "object": "event", "type": kind, "data": {"object": obj}})
    stamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{stamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/webhook/stripe", content=payload,
                       headers={"stripe-signature": f"t={stamp},v1={signature}", "content-type": "application/json"})


def checkout_completed(user, customer="cus_1", sub_id="sub_1"):
    return {"id": "cs_1", "object": "checkout.session", "mode": "subscription", "client_reference_id": user,
            "customer": customer, "subscription": sub_id}


def json_post(client, path, **headers):
    return client.post(path, json={}, headers=headers)


def subscribe(stripe, client, email="ana@example.com", **sub):
    uid = user_id(email)
    stripe["subscriptions"]["sub_1"] = subscription(user=uid, **sub)
    assert send(client, f"evt_{time.time_ns()}", "checkout.session.completed", checkout_completed(uid)).status_code == 200


# ── Starting a payment ──

def test_upgrading_sends_a_signed_in_person_to_stripe_with_their_account_attached(stripe):
    client = signed_in()

    response = json_post(client, "/billing/checkout")

    assert response.status_code == 200
    assert response.json()["url"] == "https://checkout.stripe.test/c/pay"
    [checkout] = stripe["checkouts"]
    assert checkout["user"]["id"] == user_id()
    assert checkout["success_url"].endswith("/files?upgraded=1")
    assert checkout["cancel_url"].endswith("/pricing")


def test_the_checkout_parameters_tie_the_subscription_to_the_account(monkeypatch):
    sent = {}

    class Sessions:
        def create(self, params):
            sent.clear()
            sent.update(params)
            return type("Session", (), {"url": "https://checkout.stripe.test/x"})()

    client = type("C", (), {"v1": type("V1", (), {"checkout": type("Co", (), {"sessions": Sessions()})()})()})()
    monkeypatch.setattr(billing, "_client", lambda: client)
    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", "price_test")

    billing.create_checkout({"id": "u1", "email": "ana@example.com", "stripe_customer_id": None}, "s", "c")
    first = dict(sent)
    billing.create_checkout({"id": "u1", "email": "ana@example.com", "stripe_customer_id": "cus_9"}, "s", "c")

    assert first["mode"] == "subscription"
    assert first["line_items"] == [{"price": "price_test", "quantity": 1}]
    assert first["client_reference_id"] == "u1"
    assert first["subscription_data"] == {"metadata": {"user_id": "u1"}}
    assert first["customer_email"] == "ana@example.com" and "customer" not in first
    assert sent["customer"] == "cus_9" and "customer_email" not in sent  # coming back: same Stripe customer


def test_upgrading_needs_an_account(stripe):
    response = json_post(TestClient(web.app), "/billing/checkout")

    assert response.status_code == 401
    assert stripe["checkouts"] == []


def test_another_site_cant_start_a_payment(stripe):
    client = signed_in()

    from_elsewhere = json_post(client, "/billing/checkout", origin="https://evil.example")
    as_a_form = client.post("/billing/checkout", data={"x": "1"})

    assert from_elsewhere.status_code == 403
    assert as_a_form.status_code == 415
    assert stripe["checkouts"] == []


def test_billing_is_off_until_stripe_is_set_up(stripe, monkeypatch):
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", "")
    client = signed_in()

    assert json_post(client, "/billing/checkout").status_code == 404
    assert client.post("/webhook/stripe", content=b"{}").status_code == 404
    assert client.get("/me").json()["billing"] is False


def test_someone_already_paying_isnt_sent_to_pay_again(stripe):
    client = signed_in()
    subscribe(stripe, client)

    response = json_post(client, "/billing/checkout")

    assert response.status_code == 409
    assert stripe["checkouts"] == []


# ── Stripe's messages set the plan ──

def test_a_finished_checkout_puts_the_account_on_the_monthly_plan(stripe):
    client = signed_in()
    subscribe(stripe, client)

    user = stripe["store"].get_user(user_id())
    me = client.get("/me").json()
    assert (user["plan"], user["plan_status"], user["stripe_customer_id"], user["subscription_id"]) == \
        ("paid", "active", "cus_1", "sub_1")
    assert user["paid_until"] == int(NEXT_MONTH)
    assert (me["plan"], me["run_limit"], me["run_period"]) == ("paid", web.PAID_RUNS_PER_MONTH, "month")
    assert me["keep_hours"] == web.KEEP_DAYS["paid"] * 24
    assert me["can_manage_billing"] is True


def test_coming_back_from_the_payment_page_doesnt_change_the_plan_by_itself(stripe):
    client = signed_in()

    client.get("/files?upgraded=1")

    assert client.get("/me").json()["plan"] == "free"


def test_a_message_not_signed_by_stripe_is_refused(stripe):
    client = signed_in()
    uid = user_id()
    stripe["subscriptions"]["sub_1"] = subscription(user=uid)

    forged = send(client, "evt_forged", "checkout.session.completed", checkout_completed(uid), secret="whsec_wrong")
    unsigned = client.post("/webhook/stripe", content=b'{"id": "evt_x", "type": "checkout.session.completed"}')

    assert forged.status_code == 400
    assert unsigned.status_code == 400
    assert client.get("/me").json()["plan"] == "free"


def test_the_same_message_twice_is_only_handled_once(stripe):
    client = signed_in()
    uid = user_id()
    stripe["subscriptions"]["sub_1"] = subscription(user=uid)

    send(client, "evt_same", "checkout.session.completed", checkout_completed(uid))
    again = send(client, "evt_same", "checkout.session.completed", checkout_completed(uid))

    assert again.status_code == 200
    assert stripe["reads"] == 1


def test_the_subscription_is_read_fresh_so_messages_out_of_order_cant_leave_a_wrong_plan(stripe):
    client = signed_in()
    subscribe(stripe, client)
    # Stripe says the subscription is now cancelled, then an older "created" message arrives late.
    stripe["subscriptions"]["sub_1"] = subscription(user=user_id(), status="canceled")

    send(client, "evt_late", "customer.subscription.created", subscription(user=user_id(), status="active"))

    assert client.get("/me").json()["plan"] == "free"


def test_a_renewal_moves_the_paid_until_date_on(stripe):
    client = signed_in()
    subscribe(stripe, client)
    later = NEXT_MONTH + 30 * 24 * 3600
    stripe["subscriptions"]["sub_1"] = subscription(user=user_id(), period_end=later)

    send(client, "evt_renew", "customer.subscription.updated", {"id": "sub_1", "metadata": {}})

    assert stripe["store"].get_user(user_id())["paid_until"] == int(later)


def test_cancelling_keeps_the_plan_until_the_paid_month_ends(stripe):
    client = signed_in()
    subscribe(stripe, client)
    stripe["subscriptions"]["sub_1"] = subscription(user=user_id(), cancel_at_period_end=True)

    send(client, "evt_cancel", "customer.subscription.updated", {"id": "sub_1"})
    me = client.get("/me").json()

    assert me["plan"] == "paid"
    assert me["cancel_at_period_end"] is True
    assert me["paid_until"] == int(NEXT_MONTH)


def test_when_the_subscription_ends_the_account_goes_back_to_free_and_keeps_its_customer(stripe):
    client = signed_in()
    subscribe(stripe, client)
    stripe["subscriptions"]["sub_1"] = subscription(user=user_id(), status="canceled")

    send(client, "evt_end", "customer.subscription.deleted", {"id": "sub_1"})
    me = client.get("/me").json()

    assert (me["plan"], me["run_limit"], me["run_period"]) == ("free", web.FREE_RUNS_PER_DAY, "day")
    assert stripe["store"].get_user(user_id())["stripe_customer_id"] == "cus_1"
    assert me["can_manage_billing"] is True  # past invoices are still there


@pytest.mark.parametrize("status, plan", [
    ("active", "paid"), ("trialing", "paid"), ("past_due", "paid"),
    ("unpaid", "free"), ("canceled", "free"), ("incomplete", "free"), ("incomplete_expired", "free"), ("paused", "free"),
])
def test_which_stripe_statuses_count_as_paid(status, plan):
    assert billing.subscription_state(subscription(status=status))["plan"] == plan


def test_a_failed_card_keeps_the_plan_while_stripe_retries_and_says_so(stripe):
    client = signed_in()
    subscribe(stripe, client, status="past_due")

    me = client.get("/me").json()

    assert me["plan"] == "paid"
    assert me["past_due"] is True


def test_a_subscription_message_without_the_account_id_is_matched_by_customer(stripe):
    client = signed_in()
    subscribe(stripe, client)
    stripe["subscriptions"]["sub_1"] = subscription(user=None, status="canceled")

    send(client, "evt_by_customer", "customer.subscription.deleted", {"id": "sub_1", "metadata": {}})

    assert client.get("/me").json()["plan"] == "free"


def test_an_old_subscription_ending_doesnt_undo_a_newer_one(stripe):
    client = signed_in()
    subscribe(stripe, client)
    uid = user_id()
    stripe["subscriptions"]["sub_2"] = subscription(sub_id="sub_2", user=uid)
    send(client, "evt_new", "customer.subscription.created", {"id": "sub_2"})
    stripe["subscriptions"]["sub_1"] = subscription(user=uid, status="canceled")

    send(client, "evt_old_ends", "customer.subscription.deleted", {"id": "sub_1"})

    user = stripe["store"].get_user(uid)
    assert (user["plan"], user["subscription_id"]) == ("paid", "sub_2")


def test_a_lost_cancellation_still_lapses_a_few_days_after_the_paid_month(stripe, monkeypatch):
    client = signed_in()
    tomorrow = time.time() + 24 * 3600
    subscribe(stripe, client, period_end=tomorrow)
    assert client.get("/me").json()["plan"] == "paid"

    monkeypatch.setattr(time, "time", lambda: int(tomorrow) + billing.GRACE_SECONDS + 60)

    assert client.get("/me").json()["plan"] == "free"


def test_a_message_stripe_cant_be_asked_about_fails_so_stripe_sends_it_again(stripe):
    client = signed_in()
    uid = user_id()  # no subscription stored: reading it fails, as if Stripe were unreachable

    with pytest.raises(KeyError):
        send(client, "evt_unreachable", "checkout.session.completed", checkout_completed(uid))

    assert not stripe["store"].stripe_event_seen("evt_unreachable")


def test_other_kinds_of_message_are_acknowledged_and_ignored(stripe):
    client = signed_in()

    response = send(client, "evt_other", "invoice.finalized", {"id": "in_1"})

    assert response.status_code == 200
    assert stripe["reads"] == 0


# ── Managing and cancelling ──

def test_manage_billing_opens_stripes_page_for_the_accounts_own_customer(stripe):
    client = signed_in()
    subscribe(stripe, client)

    response = json_post(client, "/billing/portal")

    assert response.json()["url"] == "https://billing.stripe.test/p/session"
    assert stripe["portals"][0][0] == "cus_1"
    assert stripe["portals"][0][1].endswith("/pricing")


def test_manage_billing_needs_a_stripe_customer(stripe):
    assert json_post(signed_in(), "/billing/portal").status_code == 400


def test_deleting_the_account_cancels_the_subscription_first(stripe):
    client = signed_in()
    subscribe(stripe, client)

    response = client.delete("/account")

    assert response.status_code == 200
    assert stripe["cancelled"] == ["sub_1"]
    assert stripe["store"].get_user(user_id()) is None


def test_if_the_subscription_cant_be_cancelled_the_account_is_kept(stripe, monkeypatch):
    client = signed_in()
    subscribe(stripe, client)

    def refuse(subscription_id):
        raise billing.BillingError("Your subscription couldn't be cancelled, so your account hasn't been deleted.")

    monkeypatch.setattr(billing, "cancel_subscription", refuse)

    response = client.delete("/account")

    assert response.status_code == 502
    assert "hasn't been deleted" in response.json()["detail"]
    assert stripe["store"].get_user(user_id())["plan"] == "paid"


def test_a_free_account_with_no_subscription_is_deleted_without_asking_stripe(stripe):
    client = signed_in()

    assert client.delete("/account").status_code == 200
    assert stripe["cancelled"] == []


def test_the_daily_limit_message_mentions_the_monthly_plan_when_billing_is_on(stripe, monkeypatch):
    usage = {"run_period": "day", "run_limit": 3, "next_run_at": time.time() + 3600}

    with_billing = web._limit_message(usage)
    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", "")

    assert "The monthly plan ($3 a month) gives up to 100 runs a month." in with_billing
    assert "monthly plan" not in web._limit_message(usage)


def test_the_plans_page_loads(stripe):
    response = TestClient(web.app).get("/pricing")

    assert response.status_code == 200
    assert "Monthly" in response.text


def test_an_older_database_gets_the_billing_columns(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "sheettriage.db")
    db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free', "
               "stripe_customer_id TEXT, created_at REAL NOT NULL)")
    db.execute("INSERT INTO users VALUES ('u1', 'a@example.com', 'free', NULL, 0)")
    db.commit()
    db.close()

    store = Store(tmp_path)
    store.set_billing("u1", plan="paid", subscription_id="sub_1", plan_status="active", paid_until=1.0)

    assert store.get_user("u1")["subscription_id"] == "sub_1"
