"""The Flask application: configuration, blueprints, and the hooks every request passes."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from s3lobby.constants import (
    DEVELOPMENT_PASSWORD,
    MAX_ANNOUNCEMENT_LENGTH,
    MAX_GROUP_TEXT_LENGTH,
    MAX_TEXT_MESSAGE_BYTES,
)
from s3lobby.database import PLAYER_NAME_PATTERN, TRUE_SETTING_VALUES
from s3lobby.server import DEFAULT_DATABASE_PATH
from s3lobby.web import accounts, auth, content, games, ladder_pages, lobby_view, server_settings
from s3lobby.web.auth import install_logon_failure_limiter
from s3lobby.web.security import add_security_headers, check_csrf_token
from s3lobby.web.storage import close_database
from s3lobby.web.texts import format_moment, format_relative_moment

# Environment variables read by create_app_from_environment, for running under a WSGI server.
DATABASE_LOCATION_VARIABLE = "S3LOBBY_DATABASE"
SECRET_KEY_VARIABLE = "S3LOBBY_WEB_SECRET"
SECURE_COOKIES_VARIABLE = "S3LOBBY_WEB_SECURE_COOKIES"
BEHIND_PROXY_VARIABLE = "S3LOBBY_WEB_BEHIND_PROXY"

# The largest form this interface has is a news text of MAX_TEXT_MESSAGE_BYTES; anything far
# beyond that is not a form from these pages.
MAX_REQUEST_BYTES = 64 * 1024
SESSION_LIFETIME = timedelta(hours=12)
SECRET_KEY_BYTES = 32
SECURE_SESSION_COOKIE_NAME = "__Host-s3lobby"
# Behind a reverse proxy, the one proxy in front sets X-Forwarded-For and X-Forwarded-Proto.
TRUSTED_PROXY_COUNT = 1

log = logging.getLogger(__name__)


def create_app(
    database_location: str,
    secret_key: str | None = None,
    secure_cookies: bool = False,
    is_behind_proxy: bool = False,
    is_development_mode: bool = False,
) -> Flask:
    """The web application for one database file.

    secret_key signs the session cookie. Without one a random key is made, so every restart logs
    every admin out. secure_cookies sends the cookie over HTTPS only; turn it on whenever the
    interface is reached through HTTPS. is_behind_proxy takes the client address from the reverse
    proxy's X-Forwarded-For header; without it every client would seem to be the proxy, and the
    limit on failed logons would lock everyone out at once. Only set it when a proxy is in front:
    otherwise any client could claim any address. is_development_mode also lets admins log on with
    DEVELOPMENT_PASSWORD from this machine and its LAN, as `s3lobby serve --dev` does for the game.
    """
    app = Flask(__name__)
    if not secret_key:
        log.warning("no %s set: admins are logged out whenever the web interface restarts", SECRET_KEY_VARIABLE)
        secret_key = secrets.token_hex(SECRET_KEY_BYTES)
    app.config.update(
        S3LOBBY_DATABASE=str(database_location),
        SECRET_KEY=secret_key,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies,
        S3LOBBY_HTTPS_ONLY=secure_cookies,
        S3LOBBY_DEVELOPMENT_MODE=is_development_mode,
    )
    if is_development_mode:
        log.warning(
            "DEVELOPMENT MODE: every admin also logs on with the password %r from this machine and its "
            "LAN. Never run this where others can reach it.",
            DEVELOPMENT_PASSWORD,
        )
    if secure_cookies:
        # The __Host- prefix makes browsers refuse the cookie unless it is secure, host-only and
        # for path /, so a sibling subdomain cannot plant or overwrite it.
        app.config["SESSION_COOKIE_NAME"] = SECURE_SESSION_COOKIE_NAME
    app.jinja_env.filters["moment"] = format_moment
    app.jinja_env.filters["relative_moment"] = format_relative_moment

    # x_proto as well, so behind an HTTPS proxy Flask knows the request came over HTTPS and builds its
    # redirects with https.
    if is_behind_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXY_COUNT, x_proto=TRUSTED_PROXY_COUNT)
    install_logon_failure_limiter(app)

    app.before_request(check_csrf_token)
    app.after_request(add_security_headers)
    app.teardown_appcontext(close_database)
    app.context_processor(auth.template_context)
    app.context_processor(text_limits)

    for blueprint in (
        lobby_view.blueprint,
        auth.blueprint,
        accounts.blueprint,
        content.blueprint,
        server_settings.blueprint,
        games.blueprint,
        ladder_pages.public_blueprint,
        ladder_pages.admin_blueprint,
    ):
        app.register_blueprint(blueprint)
    return app


def text_limits() -> dict[str, object]:
    """The limits the database checks, for the forms' maxlength and pattern attributes. The
    browser's check only spares the admin a round trip; the database checks again."""
    return {
        "max_name_length": MAX_GROUP_TEXT_LENGTH,
        "max_text_length": MAX_TEXT_MESSAGE_BYTES,
        "max_announcement_length": MAX_ANNOUNCEMENT_LENGTH,
        "player_name_pattern": PLAYER_NAME_PATTERN.pattern,
    }


def create_app_from_environment() -> Flask:
    """The application configured from environment variables, for WSGI servers such as gunicorn:

        S3LOBBY_DATABASE=/srv/lobby.sqlite3 S3LOBBY_WEB_SECRET=... \\
            gunicorn "s3lobby.web.app:create_app_from_environment()"

    The secret is required here: a WSGI server runs several worker processes, and a random key per
    worker would log admins out whenever a request reached another worker.
    """
    secret_key = os.environ.get(SECRET_KEY_VARIABLE)
    if not secret_key:
        raise RuntimeError(f"set {SECRET_KEY_VARIABLE} to a long random string")
    return create_app(
        database_location=os.environ.get(DATABASE_LOCATION_VARIABLE, DEFAULT_DATABASE_PATH),
        secret_key=secret_key,
        secure_cookies=is_variable_set(SECURE_COOKIES_VARIABLE),
        is_behind_proxy=is_variable_set(BEHIND_PROXY_VARIABLE),
    )


def is_variable_set(name: str) -> bool:
    """True when the environment variable holds a value that counts as true (1, true, yes, on)."""
    return os.environ.get(name, "").strip().lower() in TRUE_SETTING_VALUES
