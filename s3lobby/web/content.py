"""What players read in the lobby: the Home tab news, the ChatZone themes, the MessageZone board.

The running server reads these from the database whenever a player opens the tab, so a change
shows from the next visit to the tab. Every text is checked by the database for what the game can
show (check_game_text), the same as from the command line.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import Blueprint, flash, redirect, render_template, url_for
from werkzeug.wrappers import Response

from s3lobby.constants import BOARD_LANGUAGES, NEWS_AUDIENCE_GERMAN, NEWS_AUDIENCE_OTHER, NEWS_AUDIENCES
from s3lobby.database import BoardMessage, BoardTheme, News
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
    audience: str
    audience_title: str
    news: News | None


@dataclass(frozen=True)
class BoardThemeWithMessages:
    theme: BoardTheme
    messages: list[BoardMessage]


# Home tab news.


@blueprint.get("/news")
def show_news() -> str:
    database = get_database()
    forms = [
        NewsForm(audience=audience, audience_title=NEWS_AUDIENCE_TITLES[audience], news=database.find_news(audience))
        for audience in NEWS_AUDIENCES
    ]
    return render_template("news.html", forms=forms)


@blueprint.post("/news/<audience>")
def set_news(audience: str) -> Response:
    if audience not in NEWS_AUDIENCES:
        return redirect(url_for("content.show_news"))
    title = form_text("title")
    body = form_multiline_text("body")
    apply_change(
        lambda: get_database().set_news(audience, title, body),
        f"News for {NEWS_AUDIENCE_TITLES[audience]} saved. Players see it the next time they open the Home tab.",
    )
    return redirect(url_for("content.show_news"))


# ChatZone themes.


@blueprint.get("/chat-themes")
def list_chat_themes() -> str:
    return render_template("chat_themes.html", themes=get_database().list_chat_themes())


@blueprint.post("/chat-themes")
def add_chat_theme() -> Response:
    name = form_text("name")
    description = form_text("description")
    apply_change(lambda: get_database().add_chat_theme(name, description), f"Chat theme {name} added.")
    return redirect(url_for("content.list_chat_themes"))


@blueprint.post("/chat-themes/<int:theme_id>/delete")
def delete_chat_theme(theme_id: int) -> Response:
    apply_change(lambda: get_database().delete_chat_theme(theme_id), "Chat theme removed.")
    return redirect(url_for("content.list_chat_themes"))


# MessageZone board.


@blueprint.get("/board")
def show_board() -> str:
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
    language = form_text("language")
    name = form_text("name")
    description = form_text("description")
    apply_change(
        lambda: get_database().add_board_theme(language, name, description),
        f"Board theme {name} added for {language}.",
    )
    return redirect(url_for("content.show_board"))


@blueprint.post("/board/themes/<int:theme_id>/delete")
def delete_board_theme(theme_id: int) -> Response:
    apply_change(lambda: get_database().delete_board_theme(theme_id), "Board theme and its messages removed.")
    return redirect(url_for("content.show_board"))


@blueprint.post("/board/messages")
def post_board_message() -> Response:
    theme_id_text = form_text("theme_id")
    title = form_text("title")
    body = form_multiline_text("body")
    author = form_text("author")
    if not theme_id_text.isdigit():
        flash("Choose the theme the message belongs to.", "bad")
        return redirect(url_for("content.show_board"))
    apply_change(
        lambda: get_database().post_board_message(int(theme_id_text), title, body, author=author),
        f"Message {title} posted.",
    )
    return redirect(url_for("content.show_board"))


@blueprint.post("/board/messages/<int:message_id>/delete")
def delete_board_message(message_id: int) -> Response:
    apply_change(lambda: get_database().delete_board_message(message_id), "Message removed.")
    return redirect(url_for("content.show_board"))
