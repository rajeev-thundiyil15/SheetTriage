"""
auth.py — who is making a request.

AUTH_MODE picks how people sign in:

    off     (default) No accounts. Anyone can upload; each browser only sees its own runs.
    google  Sign in with Google. Needs GOOGLE_CLIENT_ID, and SESSION_SECRET so people stay
            signed in across restarts.
    dev     A sign-in form that takes any email and no password. Only for local
            development and the browser tests. Never set this on the live site.

Every browser also gets a random session cookie, so runs made without signing in still
belong to someone: the browser that made them.
"""
import hashlib
import hmac
import logging
import os
import secrets
import uuid

from fastapi import Request, Response

log = logging.getLogger("sheettriage.auth")

AUTH_MODE = os.getenv("AUTH_MODE", "off").lower()
if AUTH_MODE not in ("off", "google", "dev"):
    raise RuntimeError(f"AUTH_MODE={AUTH_MODE!r} isn't supported. Use 'off', 'google' or 'dev'.")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
if AUTH_MODE == "google" and not GOOGLE_CLIENT_ID:
    raise RuntimeError("AUTH_MODE=google needs GOOGLE_CLIENT_ID.")

SESSION_COOKIE = "st_session"
USER_COOKIE = "st_user"
SIGN_IN_DAYS = 30

# Signs the sign-in cookie so it can't be edited to become someone else. Without a fixed
# SESSION_SECRET a new one is made at each start, which signs everybody out on a redeploy.
_SECRET = os.getenv("SESSION_SECRET", "").encode()
if not _SECRET:
    if AUTH_MODE == "google":
        log.warning("SESSION_SECRET isn't set, so everyone is signed out whenever the server restarts.")
    _SECRET = secrets.token_hex(32).encode()


def accounts_enabled() -> bool:
    return AUTH_MODE != "off"


def _sign(value: str) -> str:
    return hmac.new(_SECRET, value.encode(), hashlib.sha256).hexdigest()


def _secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def session_id(request: Request) -> str:
    """Set by the middleware in app.py before any route runs."""
    return request.state.session_id


def ensure_session(request: Request) -> str | None:
    """Returns a new session id when the browser doesn't have one yet."""
    existing = request.cookies.get(SESSION_COOKIE, "")
    if len(existing) == 32 and all(c in "0123456789abcdef" for c in existing):
        request.state.session_id = existing
        return None
    request.state.session_id = uuid.uuid4().hex
    return request.state.session_id


def set_session_cookie(request: Request, response: Response, value: str) -> None:
    # No expiry date, so the browser forgets it when it closes, and with it the way back to
    # any results made without signing in.
    response.set_cookie(SESSION_COOKIE, value, httponly=True, samesite="lax", secure=_secure(request))


def signed_in_user_id(request: Request) -> str | None:
    if not accounts_enabled():
        return None
    cookie = request.cookies.get(USER_COOKIE, "")
    user_id, _, signature = cookie.rpartition(".")
    if user_id and hmac.compare_digest(signature, _sign(user_id)):
        return user_id
    return None


def sign_in(request: Request, response: Response, user_id: str) -> None:
    response.set_cookie(USER_COOKIE, f"{user_id}.{_sign(user_id)}", max_age=60 * 60 * 24 * SIGN_IN_DAYS,
                        httponly=True, samesite="lax", secure=_secure(request))


def sign_out(response: Response) -> None:
    response.delete_cookie(USER_COOKIE)


# ── Development sign-in ──

def dev_user_id(email: str) -> str:
    return "dev_" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


# ── Google sign-in ──

class SignInError(Exception):
    """The Google sign-in token can't be trusted; the message is safe to show."""


def _google_payload(credential: str) -> dict:
    """Checks the token's signature against Google's keys, its issuer, audience and expiry."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(credential, google_requests.Request(), audience=GOOGLE_CLIENT_ID,
                                        clock_skew_in_seconds=10)


def google_user(credential: str) -> tuple[str, str]:
    """The user id and email from a Google sign-in, or SignInError."""
    if not credential or len(credential) > 4096:
        raise SignInError("The Google sign-in didn't come through. Please try again.")
    try:
        payload = _google_payload(credential)
    except Exception:
        raise SignInError("Google sign-in couldn't be confirmed. Please try again.")
    if payload.get("aud") != GOOGLE_CLIENT_ID or payload.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise SignInError("Google sign-in couldn't be confirmed. Please try again.")
    if not payload.get("email") or payload.get("email_verified") is not True:
        raise SignInError("Your Google account's email address isn't verified, so it can't be used to sign in.")
    # Google's account id never changes, so a new email doesn't make a second account.
    return "google_" + payload["sub"], payload["email"]
