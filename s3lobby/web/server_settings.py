"""Server settings and announcements.

Settings are read by the running server when it needs them (at each logon, each visit to the Home
tab), so a change applies from then on. An announcement is queued in the database; the running
server sends it to every player in the lobby within a second (ANNOUNCEMENT_POLL_INTERVAL_SECONDS).
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from s3lobby.constants import MIN_CHOSEN_PASSWORD_LENGTH
from s3lobby.database import TRUE_SETTING_VALUES, DatabaseError, LobbyDatabase, Setting, check_setting_value
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
    # The section of the page the setting is shown in (SETTING_GROUPS).
    group: str = ""


# The sections of the settings page, in order.
SETTING_GROUPS = (
    "Logons",
    "Chat and Home tab",
    "Member lists and board",
    "Network",
    "Ladder",
    "Ladder rating",
    "Ladder texts",
)

# The two settings pages: the server's own, and the ladder's in the Ladder menu.
GENERAL_PAGE = "general"
LADDER_PAGE = "ladder"
SETTINGS_PAGES = {
    GENERAL_PAGE: ("Logons", "Chat and Home tab", "Member lists and board", "Network"),
    LADDER_PAGE: ("Ladder", "Ladder rating", "Ladder texts"),
}
SETTINGS_PAGE_TITLES = {GENERAL_PAGE: "Settings", LADDER_PAGE: "Ladder settings"}
SETTINGS_PAGE_ENDPOINTS = {
    GENERAL_PAGE: "server_settings.show_settings",
    LADDER_PAGE: "server_settings.show_ladder_settings",
}

SETTING_FIELDS = (
    SettingField(
        Setting.MAX_PLAYERS,
        "Player limit",
        "Logons beyond this many players get the game's \"server full\" message. 0 for no limit.",
        group="Logons",
    ),
    SettingField(
        Setting.UNIQUE_SERIAL_NUMBERS,
        "One logon per serial number",
        "Refuse a second player with the same serial number, as Blue Byte's server did. Copies in one"
        " household often share a serial, so this is off by default.",
        is_checkbox=True,
        group="Logons",
    ),
    SettingField(
        Setting.ALLOW_CREATE_PLAYER,
        "Players may create accounts in the game",
        "The game's \"Create Player\" button makes an account with the name and password typed there "
        f"(at least {MIN_CHOSEN_PASSWORD_LENGTH} characters, any case). Off by default: admins make the"
        " accounts. Every refusal, a taken name included, shows the game's \"refuses your combination\""
        " text, so nobody learns from it which names exist.",
        is_checkbox=True,
        group="Logons",
    ),
    SettingField(
        Setting.PERMANENT_CHAT_GROUPS,
        "Keep permanent chat groups open",
        "Each chat theme gets the chat groups listed on the Chat themes page, marked [Server], always "
        "open and ready to chat in without opening one first. Not yet tried with the real game.",
        is_checkbox=True,
        group="Chat and Home tab",
    ),
    SettingField(
        Setting.WELCOME_MESSAGE,
        "Welcome line",
        "Shown in the chat as a [Server] line right after each logon. Empty for none.",
        group="Chat and Home tab",
    ),
    SettingField(
        Setting.BANNER_URL,
        "Banner URL",
        "ftp://HOST/PATH of the advert banner on the Home tab, no port or user: the game fetches it "
        "by anonymous active FTP on port 21. Empty for no banner.",
        group="Chat and Home tab",
    ),
    SettingField(
        Setting.BANNER_ID,
        "Banner id",
        "Must not be 0, which the game ignores. The game downloads the banner again only when this or"
        " the sub id changes, never for a new address alone: raise it after replacing the picture.",
        group="Chat and Home tab",
    ),
    SettingField(
        Setting.BANNER_SUB_ID,
        "Banner sub id",
        "Second half of the banner's version.",
        group="Chat and Home tab",
    ),
    SettingField(
        Setting.MARK_ADMINS,
        "Mark admins in the member lists",
        "Draw admins' rows in the room member lists in green (blue for an admin who shows the clan "
        "BB). The message is the one highlighted clans use, which a run with the game confirmed.",
        is_checkbox=True,
        group="Member lists and board",
    ),
    SettingField(
        Setting.CHECK_REACHABILITY,
        "Check whether players can be reached",
        "After each logon from the internet, test from a second port whether other players can write "
        "to the player first. The lobby page shows the result to admins, /net to the player. Not yet "
        "tried with the real game.",
        is_checkbox=True,
        group="Network",
    ),
    SettingField(
        Setting.LADDER_ENABLED,
        "Tell the game a result was stored",
        "Answer a valid ladder report with \"Game status stored at the Lobby server.\" instead of \"not "
        "valid for ladder\", and stop telling players that ladder games are not ranked. The ladder "
        "ranks games either way. Not yet tried with the real game.",
        is_checkbox=True,
        group="Ladder",
    ),
    SettingField(
        Setting.LADDER_MINIMUM_MINUTES,
        "Minimum game time",
        "Minutes on the game's clock. Shorter matches, and players leaving earlier, count for nobody.",
        group="Ladder",
    ),
    SettingField(
        Setting.LADDER_REPORT_TIMEOUT_MINUTES,
        "Report timeout",
        "Minutes after a game closed in which its players' end reports may still arrive.",
        group="Ladder",
    ),
    SettingField(
        Setting.LADDER_PAIR_DAILY_LIMIT,
        "Results per pair and day",
        "Counted results between the same two players or clans in one day; more are void (win "
        "trading).",
        group="Ladder",
    ),
    SettingField(
        Setting.LADDER_START_RATING,
        "Start rating",
        "Everyone's rating before the first result.",
        group="Ladder rating",
    ),
    SettingField(
        Setting.LADDER_K,
        "K",
        "How far one result moves an established rating.",
        group="Ladder rating",
    ),
    SettingField(
        Setting.LADDER_K_PROVISIONAL,
        "K while provisional",
        "How far one result moves a new player's rating.",
        group="Ladder rating",
    ),
    SettingField(
        Setting.LADDER_PROVISIONAL_GAMES,
        "Provisional results",
        "A rating is provisional until it rests on this many results.",
        group="Ladder rating",
    ),
    SettingField(
        Setting.LADDER_ACTIVE_DAYS,
        "Active days for clan strength",
        "A member counts for a clan's strength from Liga only with a Liga result this recent.",
        group="Ladder rating",
    ),
    SettingField(
        Setting.LADDER_CLAN_MEMBER_DAYS,
        "Days in a clan for Clanliga",
        "A player counts for a clan in Clanliga only after this many days in it.",
        group="Ladder",
    ),
    SettingField(
        Setting.LADDER_INACTIVE_DAYS,
        "Inactive days",
        "Players without a result for this long leave the infinite season's standings until they "
        "play.",
        group="Ladder rating",
    ),
    SettingField(
        Setting.LADDER_MAX_CUSTOM_SEASONS,
        "Most custom seasons",
        "How many custom seasons may exist at once.",
        group="Ladder",
    ),
    SettingField(
        Setting.LADDER_BOARD_MESSAGES,
        "Board messages kept",
        "The server writes season news and a weekly summary on the board under its own theme (\"Liga\","
        " \"Ladder\"); older messages than this many are deleted.",
        group="Ladder texts",
    ),
    SettingField(
        Setting.LADDER_NEWS_STANDINGS,
        "Standings under the news",
        "Add the top five and the running seasons under the Home tab's news. The game fetches the "
        "news once per logon.",
        is_checkbox=True,
        group="Ladder texts",
    ),
    SettingField(
        Setting.LADDER_LOGON_LINE,
        "A line at logon",
        "Tell players who have a ladder result their rating and place when they log on.",
        is_checkbox=True,
        group="Ladder texts",
    ),
)


@dataclass(frozen=True)
class SettingRow:
    """One setting on the settings page with its current stored value."""

    field: SettingField
    value: str

    @property
    def is_checked(self) -> bool:
        """True when the stored value is one the server reads as true, so the checkbox shows ticked."""
        # Every value the server reads as true: one set to "yes" on the command line must show as
        # ticked, or the next save of this page would turn it off.
        return self.value.strip().lower() in TRUE_SETTING_VALUES


@dataclass(frozen=True)
class SettingSection:
    """One section of the settings page: its text fields side by side, its checkboxes below."""

    name: str
    text_rows: tuple[SettingRow, ...]
    checkbox_rows: tuple[SettingRow, ...]


# Settings.


@blueprint.get("/settings")
def show_settings() -> str:
    """The server's settings, without the ladder's, which have a page of their own."""
    return render_settings_page(GENERAL_PAGE)


@blueprint.get("/ladder/settings")
def show_ladder_settings() -> str:
    """The ladder's settings, on a page of their own in the Ladder menu."""
    return render_settings_page(LADDER_PAGE)


def render_settings_page(page: str) -> str:
    """One settings page: the sections of SETTINGS_PAGES[page], each setting with its value."""
    database = get_database()
    groups = SETTINGS_PAGES[page]
    rows = [
        SettingRow(field=field, value=database.get_setting(field.setting))
        for field in SETTING_FIELDS
        if field.group in groups
    ]
    sections = [
        SettingSection(
            name=group,
            text_rows=tuple(row for row in rows if row.field.group == group and not row.field.is_checkbox),
            checkbox_rows=tuple(row for row in rows if row.field.group == group and row.field.is_checkbox),
        )
        for group in groups
    ]
    return render_template("settings.html", sections=sections, page=page, title=SETTINGS_PAGE_TITLES[page])


@blueprint.post("/settings")
def save_settings() -> Response:
    """Save the settings of the page that sent the form, and only those: a checkbox missing from the
    form means off, so a page must never touch another page's settings. Every value is checked
    first and saved only when all pass, so a typo in one field never leaves the settings half
    changed."""
    page = form_text("page")
    if page not in SETTINGS_PAGES:
        page = GENERAL_PAGE
    back = redirect(url_for(SETTINGS_PAGE_ENDPOINTS[page]))
    database = get_database()
    fields = [field for field in SETTING_FIELDS if field.group in SETTINGS_PAGES[page]]
    new_values = {field.setting: setting_value_from_form(field, database) for field in fields}
    has_error = False
    for setting, value in new_values.items():
        try:
            check_setting_value(setting, value)
        except DatabaseError as error:
            flash(sentence_from_error(error), "bad")
            has_error = True
    if has_error:
        flash("Nothing was saved.", "warn")
        return back

    apply_change(lambda: get_database().set_settings(new_values), "Settings saved.")
    return back


def setting_value_from_form(field: SettingField, database: LobbyDatabase) -> str:
    """The value the form sent for a setting; a checkbox becomes the stored on or off value. A text
    field the form does not send keeps its stored value, so a page loaded before a setting existed
    cannot wipe it."""
    # A browser leaves an unticked checkbox out of the form, so "off" has to be written explicitly.
    if field.is_checkbox:
        if form_flag(field.setting.key):
            return CHECKED_SETTING_VALUE
        return UNCHECKED_SETTING_VALUE
    if field.setting.key not in request.form:
        return database.get_setting(field.setting)
    return form_text(field.setting.key)


# Announcements.


@blueprint.get("/announcements")
def show_announcements() -> str:
    """The announcements page: the recent announcements and whether a server is running to send one."""
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
