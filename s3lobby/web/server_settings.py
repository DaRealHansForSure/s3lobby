"""Server settings and announcements.

Settings are read by the running server when it needs them (at each logon, each visit to the Home
tab), so a change applies from then on. An announcement is queued in the database; the running
server sends it to every player in the lobby within a second (ANNOUNCEMENT_POLL_INTERVAL_SECONDS).
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import Blueprint, flash, redirect, render_template, url_for
from werkzeug.wrappers import Response

from s3lobby.database import TRUE_SETTING_VALUES, DatabaseError, Setting, check_setting_value
from s3lobby.web.auth import redirect_visitors_to_logon
from s3lobby.web.lobby_view import read_live_lobby_page
from s3lobby.web.storage import apply_change, get_database, sentence_from_error
from s3lobby.web.texts import form_flag, form_text

blueprint = Blueprint("server_settings", __name__)
blueprint.before_request(redirect_visitors_to_logon)

RECENT_ANNOUNCEMENT_COUNT = 50
CHECKED_SETTING_VALUE = "true"
UNCHECKED_SETTING_VALUE = "false"


@dataclass(frozen=True)
class SettingField:
    """One setting as the form shows it. is_checkbox settings are true or false."""

    setting: Setting
    label: str
    hint: str
    is_checkbox: bool = False


SETTING_FIELDS = (
    SettingField(
        Setting.MAX_PLAYERS,
        "Player limit",
        "Logons beyond this many players get the game's \"server full\" message. 0 for no limit.",
    ),
    SettingField(
        Setting.UNIQUE_SERIAL_NUMBERS,
        "One logon per serial number",
        "Refuse a second player with the same serial number, as Blue Byte's server did. Copies in one "
        "household often share a serial, so this is off by default.",
        is_checkbox=True,
    ),
    SettingField(
        Setting.WELCOME_MESSAGE,
        "Welcome line",
        "Shown in the chat as a [Server] line right after each logon. Empty for none.",
    ),
    SettingField(
        Setting.BANNER_URL,
        "Banner URL",
        "ftp://HOST/PATH of the advert banner on the Home tab, no port or user: the game fetches it "
        "by anonymous active FTP on port 21. Empty for no banner.",
    ),
    SettingField(
        Setting.BANNER_ID,
        "Banner id",
        "Must not be 0, which the game ignores. The game downloads the banner again only when this or "
        "the sub id changes, never for a new address alone: raise it after replacing the picture.",
    ),
    SettingField(Setting.BANNER_SUB_ID, "Banner sub id", "Second half of the banner's version."),
    SettingField(
        Setting.MARK_ADMINS,
        "Mark admins in the member lists",
        "Draw admins' rows in the room member lists in another colour. Not yet tried with the real "
        "game: turn it off again if players lose their connection when an admin enters a room.",
        is_checkbox=True,
    ),
    SettingField(
        Setting.BOARD_CHAT_HINT,
        "Tell players the message board has no chat",
        "Chat typed in the MessageZone reaches nobody, a bug of the game itself. With this on, a player "
        "who opens the MessageZone hears so once per logon as a server line. Not yet tried with the "
        "real game.",
        is_checkbox=True,
    ),
    SettingField(
        Setting.CHECK_REACHABILITY,
        "Check whether players can be reached",
        "After each logon from the internet, test from a second port whether other players can write "
        "to the player first. The lobby page shows the result to admins, /net to the player. Not yet "
        "tried with the real game.",
        is_checkbox=True,
    ),
)


@dataclass(frozen=True)
class SettingRow:
    field: SettingField
    value: str

    @property
    def is_checked(self) -> bool:
        # Every value the server reads as true: one set to "yes" on the command line must show as
        # ticked, or the next save of this page would turn it off.
        return self.value.strip().lower() in TRUE_SETTING_VALUES


# Settings.


@blueprint.get("/settings")
def show_settings() -> str:
    database = get_database()
    rows = [SettingRow(field=field, value=database.get_setting(field.setting)) for field in SETTING_FIELDS]
    return render_template("settings.html", rows=rows)


@blueprint.post("/settings")
def save_settings() -> Response:
    """Check every value first and save only when all pass, so a typo in one field never leaves
    the settings half changed."""
    new_values = {field.setting: setting_value_from_form(field) for field in SETTING_FIELDS}
    has_error = False
    for setting, value in new_values.items():
        try:
            check_setting_value(setting, value)
        except DatabaseError as error:
            flash(sentence_from_error(error), "bad")
            has_error = True
    if has_error:
        flash("Nothing was saved.", "warn")
        return redirect(url_for("server_settings.show_settings"))

    apply_change(lambda: get_database().set_settings(new_values), "Settings saved.")
    return redirect(url_for("server_settings.show_settings"))


def setting_value_from_form(field: SettingField) -> str:
    if field.is_checkbox:
        if form_flag(field.setting.key):
            return CHECKED_SETTING_VALUE
        return UNCHECKED_SETTING_VALUE
    return form_text(field.setting.key)


# Announcements.


@blueprint.get("/announcements")
def show_announcements() -> str:
    return render_template(
        "announcements.html",
        announcements=get_database().list_recent_announcements(RECENT_ANNOUNCEMENT_COUNT),
        is_server_running=read_live_lobby_page().is_running,
    )


@blueprint.post("/announcements")
def queue_announcement() -> Response:
    """Queue a line for the running server. Without one nobody would see it: the server drops lines
    queued while it was stopped when it starts, as they are stale by then."""
    if not read_live_lobby_page().is_running:
        flash("The lobby server is not running, so nobody would see it. Nothing was sent.", "bad")
        return redirect(url_for("server_settings.show_announcements"))
    text = form_text("text")
    success_text = "Queued. The server sends it to every player in the lobby within a second."
    apply_change(lambda: get_database().queue_announcement(text), success_text)
    return redirect(url_for("server_settings.show_announcements"))
