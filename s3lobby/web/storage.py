"""One database connection per request.

A database connection, SQLite or MySQL, belongs to the thread that opened it, and Flask may answer
requests on several threads, so each request opens its own and closes it when it ends. Opening is
cheap enough for an admin interface: no pool.
"""

from __future__ import annotations

from collections.abc import Callable

from flask import current_app, flash, g

from s3lobby.database import DatabaseError, LobbyDatabase

DATABASE_ATTRIBUTE = "lobby_database"


def get_database() -> LobbyDatabase:
    """The request's database connection, opened on first use."""
    database = g.get(DATABASE_ATTRIBUTE)
    if database is None:
        database = LobbyDatabase(current_app.config["S3LOBBY_DATABASE"])
        setattr(g, DATABASE_ATTRIBUTE, database)
    return database


def close_database(error: BaseException | None = None) -> None:
    database = g.pop(DATABASE_ATTRIBUTE, None)
    if database is not None:
        database.close()


def apply_change(change: Callable[[], object], success_text: str) -> bool:
    """Run a database change and tell the admin how it went; False when the database refused it,
    for example a name that is taken or a text the game cannot show."""
    try:
        change()
    except DatabaseError as error:
        flash(sentence_from_error(error), "bad")
        return False
    flash(success_text, "good")
    return True


def sentence_from_error(error: DatabaseError) -> str:
    """DatabaseError texts start in lower case for the command line's "error: ..." form."""
    message = str(error)
    return f"{message[:1].upper()}{message[1:]}."
