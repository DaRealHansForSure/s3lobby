"""The MessageZone board: themes per game language with their messages, in the order the game
shows them, hidden or shown.

The running server reads the board from the database whenever a player opens the tab. Texts are
checked by the database for what the game can show (check_game_text), the same as from the
command line (s3lobby board).
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from werkzeug.wrappers import Response

from s3lobby.constants import BOARD_LANGUAGES
from s3lobby.database import BoardMessage, BoardTheme, is_whole_number_text
from s3lobby.web.auth import current_admin, redirect_visitors_to_logon
from s3lobby.web.storage import apply_change, get_database
from s3lobby.web.texts import form_multiline_text, form_text

blueprint = Blueprint("board", __name__)
blueprint.before_request(redirect_visitors_to_logon)

# The board's order arrives as ids separated by commas; a board never holds this many themes or
# messages in one list, so a longer list is refused unread.
ID_SEPARATOR = ","
MAX_ORDERED_IDS = 500
MOVE_UP = "up"
# The value of the hide forms' field that hides; anything else shows.
HIDDEN_VALUE = "true"


@dataclass(frozen=True)
class BoardThemeWithMessages:
    """A board theme with its messages, as the board page lists them."""

    theme: BoardTheme
    messages: list[BoardMessage]


@blueprint.get("/board")
def show_board() -> str:
    """The MessageZone board page: every theme with its messages, and the logged on admin's name as
    the default author."""
    database = get_database()
    themes = [
        BoardThemeWithMessages(theme=theme, messages=database.list_board_messages(theme.theme_id))
        for theme in database.list_board_themes()
    ]
    admin = current_admin()
    return render_template(
        "board.html",
        themes=themes,
        languages=BOARD_LANGUAGES,
        default_author=admin.name if admin is not None else "",
    )


@blueprint.post("/board/themes")
def add_board_theme() -> Response:
    """Add a board theme for the games of one language."""
    language = form_text("language")
    name = form_text("name")
    description = form_text("description")
    # The language is not checked here: the database refuses one outside BOARD_LANGUAGES, for the
    # command line and the web alike.
    apply_change(
        lambda: get_database().add_board_theme(language, name, description),
        f"Board theme {name} added for {language}.",
    )
    return redirect(url_for("board.show_board"))


@blueprint.post("/board/themes/<int:theme_id>/delete")
def delete_board_theme(theme_id: int) -> Response:
    """Remove a board theme together with its messages."""
    apply_change(lambda: get_database().delete_board_theme(theme_id), "Board theme and its messages removed.")
    return redirect(url_for("board.show_board"))


@blueprint.post("/board/messages")
def post_board_message() -> Response:
    """Post a message to the theme the form chose; a missing or malformed theme id is refused."""
    theme_id_text = form_text("theme_id")
    title = form_text("title")
    body = form_multiline_text("body")
    author = form_text("author")
    # Checked before int(), which would raise on a forged value; whether the theme exists is the
    # database's check.
    if not is_whole_number_text(theme_id_text):
        flash("Choose the theme the message belongs to.", "bad")
        return redirect(url_for("board.show_board"))
    apply_change(
        lambda: get_database().post_board_message(int(theme_id_text), title, body, author=author),
        f"Message {title} posted.",
    )
    return redirect(url_for("board.show_board"))


@blueprint.post("/board/themes/order")
def order_board_themes() -> tuple[str, int]:
    """Store the order of one language's themes after a drag on the board page (app.js sends the
    theme ids, top first). Answers 204 without a page: the script already shows the new order."""
    theme_ids = ids_from_form("ids")
    if theme_ids is None:
        abort(HTTPStatus.BAD_REQUEST)
    get_database().set_board_theme_order(theme_ids)
    return "", HTTPStatus.NO_CONTENT


@blueprint.post("/board/themes/<int:theme_id>/messages/order")
def order_board_messages(theme_id: int) -> tuple[str, int]:
    """Store the order of a theme's messages after a drag on the board page; 204 like the themes."""
    message_ids = ids_from_form("ids")
    if message_ids is None:
        abort(HTTPStatus.BAD_REQUEST)
    get_database().set_board_message_order(theme_id, message_ids)
    return "", HTTPStatus.NO_CONTENT


@blueprint.post("/board/themes/<int:theme_id>/move")
def move_board_theme(theme_id: int) -> Response:
    """Move a theme one place up or down among its language's themes: the board without script."""
    is_up = form_text("direction") == MOVE_UP
    apply_change(lambda: get_database().move_board_theme(theme_id, is_up), "Theme moved.")
    return redirect(url_for("board.show_board"))


@blueprint.post("/board/messages/<int:message_id>/move")
def move_board_message(message_id: int) -> Response:
    """Move a message one place up or down in its theme: the board without script."""
    is_up = form_text("direction") == MOVE_UP
    apply_change(lambda: get_database().move_board_message(message_id, is_up), "Message moved.")
    return redirect(url_for("board.show_board"))


@blueprint.post("/board/themes/<int:theme_id>/hidden")
def set_board_theme_hidden(theme_id: int) -> Response:
    """Hide a theme from the game, or show it again (the form's hidden field says which)."""
    is_hidden = form_text("hidden") == HIDDEN_VALUE
    get_database().set_board_theme_hidden(theme_id, is_hidden)
    flash("Theme hidden from the game." if is_hidden else "Theme shown in the game again.", "good")
    return redirect(url_for("board.show_board"))


@blueprint.post("/board/messages/<int:message_id>/hidden")
def set_board_message_hidden(message_id: int) -> Response:
    """Hide a message from the game, or show it again."""
    is_hidden = form_text("hidden") == HIDDEN_VALUE
    get_database().set_board_message_hidden(message_id, is_hidden)
    flash("Message hidden from the game." if is_hidden else "Message shown in the game again.", "good")
    return redirect(url_for("board.show_board"))


@blueprint.post("/board/messages/<int:message_id>/delete")
def delete_board_message(message_id: int) -> Response:
    """Remove a board message."""
    apply_change(lambda: get_database().delete_board_message(message_id), "Message removed.")
    return redirect(url_for("board.show_board"))


def ids_from_form(field_name: str) -> list[int] | None:
    """Ids sent as "3,1,2"; None when anything in them is not an id, or there are too many."""
    text = form_text(field_name)
    if not text:
        return []
    parts = text.split(ID_SEPARATOR)
    if len(parts) > MAX_ORDERED_IDS or not all(is_whole_number_text(part) for part in parts):
        return None
    return [int(part) for part in parts]
