"""Protection for the web interface: CSRF tokens, response headers, and a limit on failed logons.

Every form that changes something is a POST carrying the session's CSRF token, so another site
cannot make an admin's browser post to this interface. The session cookie is HttpOnly and
SameSite=Lax as well (see create_app), but the token does not depend on browsers honouring that.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Hashable

from flask import Response, abort, request, session

from s3lobby.constants import LOGON_FAILURE_BURST, LOGON_FAILURES_PER_SECOND
from s3lobby.guards import RateLimiter

CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_TOKEN_BYTES = 32
METHODS_THAT_CHANGE_STATE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
HTTP_BAD_REQUEST = 400

# Pages load nothing from elsewhere and run no inline script or style, so the policy can be strict:
# an injected script tag would not run even if a text slipped past the template escaping.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    )
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


def csrf_token() -> str:
    """The session's CSRF token for a form, made on first use."""
    token = session.get(CSRF_SESSION_KEY)
    if token is None:
        token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
        session[CSRF_SESSION_KEY] = token
    return token


def check_csrf_token() -> None:
    """Refuse a request that changes state unless it carries the session's token."""
    if request.method not in METHODS_THAT_CHANGE_STATE:
        return
    expected_token = session.get(CSRF_SESSION_KEY)
    offered_token = request.form.get(CSRF_FORM_FIELD, "")
    if expected_token is None or not hmac.compare_digest(offered_token, expected_token):
        abort(HTTP_BAD_REQUEST, description="The form has expired. Load the page again and resend it.")


def add_security_headers(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


class LogonFailureLimiter:
    """Allows as many failed web logons per address and per name as the lobby allows in the game:
    LOGON_FAILURE_BURST at once, refilled slowly. Only failures count, so admins who type their
    code right are never held up. Flask may answer on several threads, hence the lock."""

    def __init__(self) -> None:
        self._failures_by_address = RateLimiter(LOGON_FAILURES_PER_SECOND, LOGON_FAILURE_BURST)
        self._failures_by_name = RateLimiter(LOGON_FAILURES_PER_SECOND, LOGON_FAILURE_BURST)
        self._lock = threading.Lock()

    def is_blocked(self, address: Hashable, name: str) -> bool:
        now_seconds = time.monotonic()
        with self._lock:
            is_address_blocked = self._failures_by_address.is_exhausted(address, now_seconds)
            is_name_blocked = self._failures_by_name.is_exhausted(name.casefold(), now_seconds)
        return is_address_blocked or is_name_blocked

    def record_failure(self, address: Hashable, name: str) -> None:
        now_seconds = time.monotonic()
        with self._lock:
            self._failures_by_address.record_event(address, now_seconds)
            self._failures_by_name.record_event(name.casefold(), now_seconds)
            self._failures_by_address.forget_quiet_keys(now_seconds)
            self._failures_by_name.forget_quiet_keys(now_seconds)
