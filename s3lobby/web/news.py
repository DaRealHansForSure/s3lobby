"""The Home tab news: one text for German games and one for every other language.

The running server reads it from the database at each logon, and the game keeps the text it
fetched for the session. Every text is checked by the database for what the game can show
(check_game_text), the same as from the command line (s3lobby news).
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import Blueprint, redirect, render_template, url_for
from werkzeug.wrappers import Response

from s3lobby.constants import NEWS_AUDIENCE_GERMAN, NEWS_AUDIENCE_OTHER, NEWS_AUDIENCES
from s3lobby.database import News
from s3lobby.web.auth import redirect_visitors_to_logon
from s3lobby.web.storage import apply_change, get_database
from s3lobby.web.texts import form_multiline_text, form_text

blueprint = Blueprint("news", __name__)
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
        return redirect(url_for("news.show_news"))
    title = form_text("title")
    body = form_multiline_text("body")
    apply_change(
        lambda: get_database().set_news(audience, title, body),
        f"News for {NEWS_AUDIENCE_TITLES[audience]} saved. Players see it from their next logon "
        "(the game keeps a text it fetched for the session).",
    )
    return redirect(url_for("news.show_news"))
