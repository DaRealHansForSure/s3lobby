"""The ChatZone themes, and the chat groups the server keeps open in them.

The running server reads both from the database when a player opens the ChatZone tab, so a change
shows from the next visit. Names are checked by the database for what the game can show
(check_game_text), the same as from the command line (s3lobby chat-theme).
"""

from __future__ import annotations

from http import HTTPStatus

from flask import Blueprint, abort, redirect, render_template, url_for
from werkzeug.wrappers import Response

from s3lobby.constants import GAME_LANGUAGES
from s3lobby.database import (
    BUILT_IN_CHAT_THEMES,
    MAX_PERMANENT_CHAT_GROUP_NAME_LENGTH,
    PermanentChatGroup,
    Setting,
    is_whole_number_text,
)
from s3lobby.web.auth import redirect_visitors_to_logon
from s3lobby.web.storage import apply_change, get_database
from s3lobby.web.texts import form_text

blueprint = Blueprint("chat_themes", __name__)
blueprint.before_request(redirect_visitors_to_logon)


@blueprint.get("/chat-themes")
def list_chat_themes() -> str:
    """The ChatZone themes page: the built-in themes, which cannot be changed, and the admins' own."""
    database = get_database()
    groups_by_theme: dict[int | None, list[PermanentChatGroup]] = {}
    for group in database.list_permanent_chat_groups():
        groups_by_theme.setdefault(group.chat_theme_id, []).append(group)
    return render_template(
        "chat_themes.html",
        built_in_themes=BUILT_IN_CHAT_THEMES,
        themes=database.list_chat_themes(),
        languages=GAME_LANGUAGES,
        groups_by_theme=groups_by_theme,
        are_groups_on=database.get_boolean_setting(Setting.PERMANENT_CHAT_GROUPS),
        max_group_name_length=MAX_PERMANENT_CHAT_GROUP_NAME_LENGTH,
    )


@blueprint.post("/chat-themes/groups")
def add_permanent_chat_group() -> Response:
    """Keep one more chat group open in the theme the form names (empty: General)."""
    theme_text = form_text("theme_id")
    name = form_text("name")
    chat_theme_id = None
    if theme_text:
        # Checked before int(), which would raise on a forged value; whether the theme exists is the
        # database's check.
        if not is_whole_number_text(theme_text):
            abort(HTTPStatus.BAD_REQUEST)
        chat_theme_id = int(theme_text)
    apply_change(
        lambda: get_database().add_permanent_chat_group(chat_theme_id, name),
        f"Chat group {name} added; it opens at the next visit to the ChatZone.",
    )
    return redirect(url_for("chat_themes.list_chat_themes"))


@blueprint.post("/chat-themes/groups/<int:group_row_id>/delete")
def delete_permanent_chat_group(group_row_id: int) -> Response:
    """Stop keeping a chat group open; the server closes it once nobody is in it."""
    apply_change(
        lambda: get_database().delete_permanent_chat_group(group_row_id),
        "Chat group removed; it closes once nobody is in it.",
    )
    return redirect(url_for("chat_themes.list_chat_themes"))


@blueprint.post("/chat-themes")
def add_chat_theme() -> Response:
    """Add a theme to the ChatZone tab's list."""
    name = form_text("name")
    description = form_text("description")
    # Not checked here: the database refuses a language outside GAME_LANGUAGES, for the command
    # line and the web alike.
    language = form_text("language")
    apply_change(lambda: get_database().add_chat_theme(name, description, language), f"Chat theme {name} added.")
    return redirect(url_for("chat_themes.list_chat_themes"))


@blueprint.post("/chat-themes/<int:theme_id>/delete")
def delete_chat_theme(theme_id: int) -> Response:
    """Remove a ChatZone theme."""
    apply_change(lambda: get_database().delete_chat_theme(theme_id), "Chat theme removed.")
    return redirect(url_for("chat_themes.list_chat_themes"))
