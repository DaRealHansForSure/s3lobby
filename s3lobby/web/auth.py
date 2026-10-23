"""Admin logon: account name and access code, the same pair the game's logon dialog takes.

Only accounts with admin rights get in. The session remembers the account and the access code it
logged on with, and every request checks both again: an admin whose rights are taken away, who is
banned, or whose code is revoked is logged out at the next click, not when the cookie runs out.
"""

from __future__ import annotations

import threading
import urllib.parse

from flask import Blueprint, Flask, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.wrappers import Response

from s3lobby.constants import MAX_GROUP_TEXT_LENGTH
from s3lobby.database import Account, hash_access_code, is_valid_player_name, utc_now
from s3lobby.web.security import LogonFailureLimiter, csrf_token
from s3lobby.web.storage import get_database
from s3lobby.web.texts import form_text

blueprint = Blueprint("auth", __name__)

# The cookie holds only the token of a web session row (LobbyDatabase.start_web_session).
SESSION_TOKEN_KEY = "web_session"
# Each logon check costs scrypt hashes of about 16 MB each; more than this many at once are turned
# away, so a flood of logon requests cannot exhaust memory on threads Flask starts without limit.
MAX_CONCURRENT_LOGON_CHECKS = 2
ADMIN_ATTRIBUTE = "admin_account"
LIMITER_EXTENSION = "s3lobby_logon_failures"
LOGON_CHECKS_EXTENSION = "s3lobby_logon_checks"
# Longer than any name or code the game's dialog takes; longer input is cut before it is hashed.
MAX_LOGON_FIELD_LENGTH = MAX_GROUP_TEXT_LENGTH
HTTP_TOO_MANY_REQUESTS = 429
HTTP_UNAUTHORIZED = 401
SAFE_REDIRECT_PREFIX = "/"
PROTOCOL_RELATIVE_PREFIX = "//"
# Browsers read a backslash in a URL as a slash, so a path starting with a slash and a backslash is
# protocol relative too.
BACKSLASH = "\\"
FIRST_PRINTABLE = " "
DELETE = "\x7f"
REFUSED_LOGON_TEXT = "Unknown name or access code, or the account has no admin rights."


def current_admin() -> Account | None:
    """The logged on admin, checked against the database once per request; None for a visitor."""
    if ADMIN_ATTRIBUTE in g:
        return g.get(ADMIN_ATTRIBUTE)
    admin = load_admin_from_session()
    setattr(g, ADMIN_ATTRIBUTE, admin)
    return admin


def load_admin_from_session() -> Account | None:
    """The admin of the session's live web session. The session ends as soon as it runs out, is
    logged out, or its account loses admin rights, is banned, or its access code is revoked."""
    token = session.get(SESSION_TOKEN_KEY)
    if not isinstance(token, str):
        return None
    database = get_database()
    web_session = database.find_web_session(token)
    if web_session is None:
        session.clear()
        return None
    account = database.find_account_by_id(web_session.account_id)
    is_still_allowed = (
        account is not None
        and account.is_admin
        and not account.is_banned_at(utc_now())
        and database.is_access_code_active(web_session.account_id, web_session.access_code_id)
    )
    if not is_still_allowed:
        session.clear()
        return None
    return account


def redirect_visitors_to_logon() -> Response | None:
    """before_request hook of every blueprint for admins only: visitors go to the logon page and
    come back here afterwards."""
    if current_admin() is None:
        return redirect(url_for("auth.log_on", next=request.path))
    return None


def template_context() -> dict[str, object]:
    """Available in every template: the logged on admin and the form token."""
    return {"admin": current_admin(), "csrf_token": csrf_token}


@blueprint.route("/logon", methods=["GET", "POST"])
def log_on() -> str | Response | tuple[str, int]:
    next_path = safe_next_path(request.values.get("next", ""))
    if request.method == "GET":
        return render_template("logon.html", next_path=next_path, name="")

    name = form_text("name")[:MAX_LOGON_FIELD_LENGTH]
    access_code = form_text("access_code")[:MAX_LOGON_FIELD_LENGTH]
    client_address = request.remote_addr or ""
    limiter = logon_failure_limiter()
    if limiter.is_address_blocked(client_address):
        flash("Too many failed attempts. Try again in a few minutes.", "bad")
        return render_template("logon.html", next_path=next_path, name=name), HTTP_TOO_MANY_REQUESTS
    if not logon_checks().acquire(blocking=False):
        flash("The server is busy. Try again in a moment.", "bad")
        return render_template("logon.html", next_path=next_path, name=name), HTTP_TOO_MANY_REQUESTS
    try:
        account, access_code_id = check_logon(name, access_code)
    finally:
        logon_checks().release()
    if access_code_id is None:
        limiter.record_failure(client_address, name)
        # Failures for a name count from every address, against guessing spread over many
        # addresses; the name's own correct code still gets through, so nobody can lock an admin
        # out by failing on purpose with their (public) name.
        if limiter.is_name_blocked(name):
            flash("Too many failed attempts. Try again in a few minutes.", "bad")
            return render_template("logon.html", next_path=next_path, name=name), HTTP_TOO_MANY_REQUESTS
    is_admitted = (
        account is not None
        and access_code_id is not None
        and account.is_admin
        and not account.is_banned_at(utc_now())
    )
    if not is_admitted:
        flash(REFUSED_LOGON_TEXT, "bad")
        return render_template("logon.html", next_path=next_path, name=name), HTTP_UNAUTHORIZED

    # A fresh session, so nothing from before the logon (such as a planted session) carries over.
    session.clear()
    session.permanent = True
    session[SESSION_TOKEN_KEY] = get_database().start_web_session(account.account_id, access_code_id)
    flash(f"Logged on as {account.name}.", "good")
    return redirect(next_path or url_for("lobby_view.live_lobby"))


@blueprint.post("/logout")
def log_out() -> Response:
    """End the web session in the database too: a copy of the cookie stops working with it."""
    token = session.get(SESSION_TOKEN_KEY)
    if isinstance(token, str):
        get_database().end_web_session(token)
    session.clear()
    flash("Logged out.", "good")
    return redirect(url_for("lobby_view.live_lobby"))


def check_logon(name: str, access_code: str) -> tuple[Account | None, int | None]:
    """The account and the id of its matching access code; the id is None when they do not match.

    An unknown name still costs one scrypt hash, as a wrong code does for an account with one code,
    so the time an answer takes does not tell whether an account exists.
    """
    database = get_database()
    account = None
    if is_valid_player_name(name):
        account = database.find_account(name)
    if account is None:
        hash_access_code(access_code)
        return None, None
    return account, database.match_access_code(account.account_id, access_code)


def install_logon_failure_limiter(app: Flask) -> None:
    """One limiter per application, made before the first request so threads never race to make
    it, and so tests with their own application start with a clean slate."""
    app.extensions[LIMITER_EXTENSION] = LogonFailureLimiter()
    app.extensions[LOGON_CHECKS_EXTENSION] = threading.BoundedSemaphore(MAX_CONCURRENT_LOGON_CHECKS)


def logon_checks() -> threading.BoundedSemaphore:
    return current_app.extensions[LOGON_CHECKS_EXTENSION]


def logon_failure_limiter() -> LogonFailureLimiter:
    return current_app.extensions[LIMITER_EXTENSION]


def safe_next_path(path: str) -> str:
    """Only a path on this site: "//evil.example" or "https://..." would send the admin elsewhere.
    Browsers drop tabs and line breaks from a URL and read a backslash as a slash, so "/<tab>/evil.example"
    is "//evil.example" to them: any such character makes the path unsafe, and so does anything
    that still parses with a scheme or a host."""
    has_ignored_character = any(character < FIRST_PRINTABLE or character == DELETE for character in path)
    if has_ignored_character or BACKSLASH in path:
        return ""
    is_local_path = path.startswith(SAFE_REDIRECT_PREFIX) and not path.startswith(PROTOCOL_RELATIVE_PREFIX)
    if not is_local_path:
        return ""
    parts = urllib.parse.urlsplit(path)
    if parts.scheme or parts.netloc:
        return ""
    return path
