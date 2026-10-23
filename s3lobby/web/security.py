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

from flask import Response, abort, current_app, request, session

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
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
# Pages show player addresses and, once, new access codes: no browser or proxy may keep a copy.
NO_STORE = "no-store"
STATIC_PATH_PREFIX = "/static/"
# With HTTPS, browsers keep to it for a year, also when someone types http://.
STRICT_TRANSPORT_SECURITY = "max-age=31536000"


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
    # Compared as bytes: compare_digest refuses str with characters beyond ASCII, which a forged
    # form could send to turn a refusal into a server error.
    is_token_right = expected_token is not None and hmac.compare_digest(
        offered_token.encode("utf-8"), expected_token.encode("utf-8")
    )
    if not is_token_right:
        abort(HTTP_BAD_REQUEST, description="The form has expired. Load the page again and resend it.")


def add_security_headers(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if not request.path.startswith(STATIC_PATH_PREFIX):
        response.headers["Cache-Control"] = NO_STORE
    if current_app.config.get("S3LOBBY_HTTPS_ONLY"):
        response.headers.setdefault("Strict-Transport-Security", STRICT_TRANSPORT_SECURITY)
    return response


class LogonFailureLimiter:
    """Allows as many failed web logons per address and per name as the lobby allows in the game:
    LOGON_FAILURE_BURST at once, refilled slowly. Only failures count, so admins who type their
    code right are never held up, not even when others failed with their name. Flask may answer on
    several threads, hence the lock."""

    def __init__(self) -> None:
        self._failures_by_address = RateLimiter(LOGON_FAILURES_PER_SECOND, LOGON_FAILURE_BURST)
        self._failures_by_name = RateLimiter(LOGON_FAILURES_PER_SECOND, LOGON_FAILURE_BURST)
        self._lock = threading.Lock()

    def is_address_blocked(self, address: Hashable) -> bool:
        with self._lock:
            return self._failures_by_address.is_exhausted(address, time.monotonic())

    def is_name_blocked(self, name: str) -> bool:
        with self._lock:
            return self._failures_by_name.is_exhausted(name.casefold(), time.monotonic())

    def record_failure(self, address: Hashable, name: str) -> None:
        now_seconds = time.monotonic()
        with self._lock:
            self._failures_by_address.record_event(address, now_seconds)
            self._failures_by_name.record_event(name.casefold(), now_seconds)
            self._failures_by_address.forget_quiet_keys(now_seconds)
            self._failures_by_name.forget_quiet_keys(now_seconds)
