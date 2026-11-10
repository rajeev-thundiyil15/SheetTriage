"""
auth.py — who is making a request.

AUTH_MODE picks how people sign in:

    off    (default) No accounts. Anyone can upload; each browser only sees its own runs.
    dev    Accounts with a sign-in form that takes any email and no password. Only for
           local development and the browser tests. Never set this on the live site.
    clerk  Planned: Clerk's hosted sign-in, with its session token checked here.

Every browser also gets a random session cookie, so runs made without signing in still
belong to someone: the browser that made them.
"""
import hashlib
import hmac
import os
import secrets
import uuid

from fastapi import Request, Response

AUTH_MODE = os.getenv("AUTH_MODE", "off").lower()
if AUTH_MODE not in ("off", "dev"):
    raise RuntimeError(f"AUTH_MODE={AUTH_MODE!r} isn't supported yet. Use 'off' or 'dev'.")

SESSION_COOKIE = "st_session"
USER_COOKIE = "st_user"

# Signs the dev sign-in cookie so it can't be edited to become someone else.
_SECRET = (os.getenv("SESSION_SECRET") or secrets.token_hex(32)).encode()


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
    response.set_cookie(SESSION_COOKIE, value, max_age=60 * 60 * 24 * 365,
                        httponly=True, samesite="lax", secure=_secure(request))


def signed_in_user_id(request: Request) -> str | None:
    if AUTH_MODE != "dev":
        return None
    cookie = request.cookies.get(USER_COOKIE, "")
    user_id, _, signature = cookie.rpartition(".")
    if user_id and hmac.compare_digest(signature, _sign(user_id)):
        return user_id
    return None


def dev_user_id(email: str) -> str:
    return "dev_" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def sign_in(request: Request, response: Response, user_id: str) -> None:
    response.set_cookie(USER_COOKIE, f"{user_id}.{_sign(user_id)}", max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax", secure=_secure(request))


def sign_out(response: Response) -> None:
    response.delete_cookie(USER_COOKIE)
