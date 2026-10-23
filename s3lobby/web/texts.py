"""How the pages write times, and how they read text from forms."""

from __future__ import annotations

from datetime import datetime

from flask import request

from s3lobby.database import utc_now

NO_MOMENT = "-"
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE
SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR
# Browsers send line breaks in a textarea as CR LF; the game wants LF only.
BROWSER_LINE_BREAK = "\r\n"
LINE_BREAK = "\n"


def format_moment(moment: datetime | str | None) -> str:
    """"2026-10-23 14:05 UTC"; the server and the web interface both keep UTC."""
    if moment is None:
        return NO_MOMENT
    # The live lobby arrives as JSON, where moments are ISO 8601 text rather than datetimes.
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return f"{moment:%Y-%m-%d %H:%M} UTC"


def format_relative_moment(moment: datetime | str | None) -> str:
    """"just now", "5 min ago", "3 h ago" or "2 days ago", for the live lobby."""
    if moment is None:
        return NO_MOMENT
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    # A moment slightly ahead of this clock (the server wrote it a moment ago) reads "just now",
    # never as a negative age.
    elapsed_seconds = max(0, int((utc_now() - moment).total_seconds()))
    if elapsed_seconds < SECONDS_PER_MINUTE:
        return "just now"
    if elapsed_seconds < SECONDS_PER_HOUR:
        return f"{elapsed_seconds // SECONDS_PER_MINUTE} min ago"
    if elapsed_seconds < SECONDS_PER_DAY:
        return f"{elapsed_seconds // SECONDS_PER_HOUR} h ago"
    return f"{elapsed_seconds // SECONDS_PER_DAY} days ago"


def form_text(field_name: str) -> str:
    """A one line form field, without surrounding spaces; empty when missing."""
    return request.form.get(field_name, "").strip()


def form_multiline_text(field_name: str) -> str:
    """A textarea's text with LF line breaks and no trailing spaces or empty lines at the end."""
    text = request.form.get(field_name, "").replace(BROWSER_LINE_BREAK, LINE_BREAK)
    return text.rstrip()


def form_flag(field_name: str) -> bool:
    """A checkbox: present in the form only when ticked."""
    return field_name in request.form
