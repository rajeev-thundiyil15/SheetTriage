"""
billing.py — the $3 monthly plan, through Stripe.

Stripe hosts the payment page and the page for managing a subscription, so card details never
reach this server. What this module keeps on each account:

    stripe_customer_id      the account's Stripe customer, so a person is never billed twice
    subscription_id         their subscription
    plan_status             Stripe's word for it: active, past_due, canceled, ...
    paid_until              when the month they've paid for ends
    cancel_at_period_end    they cancelled, and the plan ends at paid_until

Only Stripe's signed webhook messages change an account's plan, never the browser coming back
from the payment page. Each message is used only as a nudge: the subscription is read fresh
from Stripe, so messages that arrive twice or out of order can't leave a wrong plan behind.

Billing stays off until STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET and STRIPE_PRICE_ID are all set.
"""
from __future__ import annotations

import os
import time

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
PRICE_TEXT = os.getenv("PLAN_PRICE_TEXT", "$3 a month")

# Paid while the subscription is live. past_due is Stripe retrying a failed card: the person keeps
# the plan while it does, and Stripe moves the subscription on (to canceled or unpaid) if it gives up.
PAID_STATUSES = {"active", "trialing", "past_due"}

# If Stripe's last word was lost, the plan still lapses this long after the paid month ends.
# Stripe retries webhook messages for up to 3 days.
GRACE_SECONDS = 3 * 24 * 60 * 60

# The events that can change a plan. Anything else Stripe sends is acknowledged and ignored.
SUBSCRIPTION_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
}


class BillingError(Exception):
    """A Stripe call failed. The message is safe to show."""


def enabled() -> bool:
    return bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET and STRIPE_PRICE_ID)


def is_paid(user: dict | None, now: float | None = None) -> bool:
    """The account is on the monthly plan now."""
    if not user or user.get("plan") != "paid":
        return False
    paid_until = user.get("paid_until")
    return paid_until is None or (now or time.time()) < paid_until + GRACE_SECONDS


# ── Calls to Stripe: small, so the tests can stand in for each one ──

def _client():
    import stripe

    return stripe.StripeClient(STRIPE_SECRET_KEY)


def create_checkout(user: dict, success_url: str, cancel_url: str) -> str:
    """The address of Stripe's payment page for this account."""
    params = {
        "mode": "subscription",
        "line_items": [{"price": STRIPE_PRICE_ID, "quantity": 1}],
        # Ties the payment back to the account, whichever message from Stripe arrives first.
        "client_reference_id": user["id"],
        "metadata": {"user_id": user["id"]},
        "subscription_data": {"metadata": {"user_id": user["id"]}},
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    if user.get("stripe_customer_id"):
        params["customer"] = user["stripe_customer_id"]  # coming back after cancelling: same customer
    else:
        params["customer_email"] = user["email"]
    try:
        return _client().v1.checkout.sessions.create(params=params).url
    except Exception as exc:
        raise BillingError("Stripe's payment page couldn't be opened. Please try again in a minute.") from exc


def create_portal(customer_id: str, return_url: str) -> str:
    """The address of Stripe's page for cancelling, changing the card and seeing invoices."""
    try:
        return _client().v1.billing_portal.sessions.create(
            params={"customer": customer_id, "return_url": return_url}).url
    except Exception as exc:
        raise BillingError("Stripe's billing page couldn't be opened. Please try again in a minute.") from exc


def get_subscription(subscription_id: str) -> dict:
    return _client().v1.subscriptions.retrieve(subscription_id).to_dict()


def cancel_subscription(subscription_id: str) -> None:
    try:
        _client().v1.subscriptions.cancel(subscription_id)
    except Exception as exc:
        raise BillingError(
            "Your subscription couldn't be cancelled, so your account hasn't been deleted. "
            "Please try again, or cancel it first under Manage billing.") from exc


def read_event(payload: bytes, signature: str | None) -> dict:
    """The webhook message, after checking Stripe signed it. Raises if it didn't."""
    return _client().construct_event(payload, signature, STRIPE_WEBHOOK_SECRET).to_dict()


# ── Turning Stripe's messages into the account's plan ──

def subscription_state(subscription: dict) -> dict:
    """The fields saved on the account for this subscription."""
    status = subscription.get("status") or ""
    items = (subscription.get("items") or {}).get("data") or []
    # Stripe keeps the billing period on each item, not on the subscription.
    ends = [item.get("current_period_end") for item in items if item.get("current_period_end")]
    customer = subscription.get("customer")
    return {
        "plan": "paid" if status in PAID_STATUSES else "free",
        "plan_status": status,
        "subscription_id": subscription.get("id"),
        "paid_until": float(max(ends)) if ends else None,
        "cancel_at_period_end": int(bool(subscription.get("cancel_at_period_end"))),
        "stripe_customer_id": customer if isinstance(customer, str) else (customer or {}).get("id"),
    }


def _sync(store, subscription_id: str, user_id: str | None) -> None:
    subscription = get_subscription(subscription_id)
    user_id = user_id or (subscription.get("metadata") or {}).get("user_id")
    user = store.get_user(user_id) if user_id else None
    if not user:
        customer = subscription.get("customer")
        user = store.user_by_customer(customer if isinstance(customer, str) else (customer or {}).get("id"))
    if not user:
        return  # an account that's since been deleted, or a subscription made outside the site
    state = subscription_state(subscription)
    # An old subscription ending mustn't undo a newer one the person has since started.
    if (state["plan"] == "free" and user.get("subscription_id")
            and user["subscription_id"] != state["subscription_id"] and is_paid(user)):
        return
    store.set_billing(user["id"], **state)


def handle_event(store, event: dict) -> None:
    """Applies one checked webhook message. Raises if Stripe can't be reached, so Stripe retries."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        if obj.get("mode") != "subscription" or not obj.get("subscription"):
            return
        user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id")
        if user_id and store.get_user(user_id) and obj.get("customer"):
            store.set_billing(user_id, stripe_customer_id=obj["customer"])
        _sync(store, obj["subscription"], user_id)
    elif kind in SUBSCRIPTION_EVENTS and obj.get("id"):
        _sync(store, obj["id"], (obj.get("metadata") or {}).get("user_id"))
