"""What players read in the lobby: the Home tab news, the ChatZone themes, the MessageZone board.

The running server reads these from the database whenever a player opens the tab, so a change
shows from the next visit to the tab. Every text is checked by the database for what the game can
show (check_game_text), the same as from the command line.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from werkzeug.wrappers import Response

from s3lobby.constants import BOARD_LANGUAGES, GAME_LANGUAGES, NEWS_AUDIENCE_GERMAN, NEWS_AUDIENCE_OTHER, NEWS_AUDIENCES
from s3lobby.database import (
    BUILT_IN_CHAT_THEMES,
    MAX_PERMANENT_CHAT_GROUP_NAME_LENGTH,
    BoardMessage,
    BoardTheme,
    News,
    PermanentChatGroup,
    Setting,
    is_whole_number_text,
)
from s3lobby.web.auth import current_admin, redirect_visitors_to_logon
from s3lobby.web.storage import apply_change, get_database
from s3lobby.web.texts import form_multiline_text, form_text

blueprint = Blueprint("content", __name__)
blueprint.before_request(redirect_visitors_to_logon)

NEWS_AUDIENCE_TITLES = {
    NEWS_AUDIENCE_GERMAN: "German games",
    NEWS_AUDIENCE_OTHER: "All other languages",
}


@dataclass(frozen=True)
class NewsForm:
    """The news form for one audience: its title on the page and the news it holds now, if any."""

    audience: str
    audience_title: str
    news: News | None


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


# Home tab news.


@blueprint.get("/news")
def show_news() -> str:
    """The news page: one form for the Home tab news of each audience."""
    database = get_database()
    forms = [
        NewsForm(audience=audience, audience_title=NEWS_AUDIENCE_TITLES[audience], news=database.find_news(audience))
        for audience in NEWS_AUDIENCES
    ]
    return render_template("news.html", forms=forms)


@blueprint.post("/news/<audience>")
def set_news(audience: str) -> Response:
    """Save the Home tab news for one audience; an unknown audience in the URL changes nothing."""
    if audience not in NEWS_AUDIENCES:
        return redirect(url_for("content.show_news"))
    title = form_text("title")
    body = form_multiline_text("body")
    apply_change(
        lambda: get_database().set_news(audience, title, body),
        f"News for {NEWS_AUDIENCE_TITLES[audience]} saved. Players see it from their next logon "
        "(the game keeps a text it fetched for the session).",
    )
    return redirect(url_for("content.show_news"))


# ChatZone themes.


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
    return redirect(url_for("content.list_chat_themes"))


@blueprint.post("/chat-themes/groups/<int:group_row_id>/delete")
def delete_permanent_chat_group(group_row_id: int) -> Response:
    """Stop keeping a chat group open; the server closes it once nobody is in it."""
    apply_change(
        lambda: get_database().delete_permanent_chat_group(group_row_id),
        "Chat group removed; it closes once nobody is in it.",
    )
    return redirect(url_for("content.list_chat_themes"))


@blueprint.post("/chat-themes")
def add_chat_theme() -> Response:
    """Add a theme to the ChatZone tab's list."""
    name = form_text("name")
    description = form_text("description")
    # Not checked here: the database refuses a language outside GAME_LANGUAGES, for the command
    # line and the web alike.
    language = form_text("language")
    apply_change(lambda: get_database().add_chat_theme(name, description, language), f"Chat theme {name} added.")
    return redirect(url_for("content.list_chat_themes"))


@blueprint.post("/chat-themes/<int:theme_id>/delete")
def delete_chat_theme(theme_id: int) -> Response:
    """Remove a ChatZone theme."""
    apply_change(lambda: get_database().delete_chat_theme(theme_id), "Chat theme removed.")
    return redirect(url_for("content.list_chat_themes"))


# MessageZone board.


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
    return redirect(url_for("content.show_board"))


@blueprint.post("/board/themes/<int:theme_id>/delete")
def delete_board_theme(theme_id: int) -> Response:
    """Remove a board theme together with its messages."""
    apply_change(lambda: get_database().delete_board_theme(theme_id), "Board theme and its messages removed.")
    return redirect(url_for("content.show_board"))


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
        return redirect(url_for("content.show_board"))
    apply_change(
        lambda: get_database().post_board_message(int(theme_id_text), title, body, author=author),
        f"Message {title} posted.",
    )
    return redirect(url_for("content.show_board"))


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
    return redirect(url_for("content.show_board"))


@blueprint.post("/board/messages/<int:message_id>/move")
def move_board_message(message_id: int) -> Response:
    """Move a message one place up or down in its theme: the board without script."""
    is_up = form_text("direction") == MOVE_UP
    apply_change(lambda: get_database().move_board_message(message_id, is_up), "Message moved.")
    return redirect(url_for("content.show_board"))


@blueprint.post("/board/themes/<int:theme_id>/hidden")
def set_board_theme_hidden(theme_id: int) -> Response:
    """Hide a theme from the game, or show it again (the form's hidden field says which)."""
    is_hidden = form_text("hidden") == HIDDEN_VALUE
    get_database().set_board_theme_hidden(theme_id, is_hidden)
    flash("Theme hidden from the game." if is_hidden else "Theme shown in the game again.", "good")
    return redirect(url_for("content.show_board"))


@blueprint.post("/board/messages/<int:message_id>/hidden")
def set_board_message_hidden(message_id: int) -> Response:
    """Hide a message from the game, or show it again."""
    is_hidden = form_text("hidden") == HIDDEN_VALUE
    get_database().set_board_message_hidden(message_id, is_hidden)
    flash("Message hidden from the game." if is_hidden else "Message shown in the game again.", "good")
    return redirect(url_for("content.show_board"))


def ids_from_form(field_name: str) -> list[int] | None:
    """Ids sent as "3,1,2"; None when anything in them is not an id, or there are too many."""
    text = form_text(field_name)
    if not text:
        return []
    parts = text.split(ID_SEPARATOR)
    if len(parts) > MAX_ORDERED_IDS or not all(is_whole_number_text(part) for part in parts):
        return None
    return [int(part) for part in parts]


@blueprint.post("/board/messages/<int:message_id>/delete")
def delete_board_message(message_id: int) -> Response:
    """Remove a board message."""
    apply_change(lambda: get_database().delete_board_message(message_id), "Message removed.")
    return redirect(url_for("content.show_board"))
