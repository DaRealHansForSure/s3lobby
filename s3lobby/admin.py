"""Administration from the command line: accounts, clans, news, chat themes, board, settings, and
the database itself.

Each function takes the open database and the parsed command line options, does one thing, and
prints what happened. They all run while the server may be running: the server and an
administration command can use the same database at the same time.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from s3lobby.commands import kick_reason
from s3lobby.constants import NEWS_AUDIENCES
from s3lobby.database import (
    BUILT_IN_CHAT_THEMES,
    Account,
    Clan,
    DatabaseError,
    LobbyDatabase,
    PermanentChatGroup,
    Setting,
    is_built_in_chat_theme_name,
    utc_now,
)
from s3lobby.server_info import collect_server_info


# s3lobby info --format: text for people, JSON for tools.
INFO_TEXT_FORMAT = "text"
INFO_JSON_FORMAT = "json"
INFO_FORMATS = (INFO_TEXT_FORMAT, INFO_JSON_FORMAT)

# board move-theme and move-message: the directions, and which kind of board item a command acts on.
MOVE_UP = "up"
MOVE_DIRECTIONS = (MOVE_UP, "down")
BOARD_THEME_KIND = "theme"
BOARD_ITEM_KINDS = (BOARD_THEME_KIND, "message")


class AdminError(Exception):
    """A command that cannot be carried out, with a message for the person who typed it."""


# Accounts.


def add_account(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Create an account (optionally an admin) and print its first access code."""
    account = database.create_account(options.name, is_admin=options.admin)
    access_code = database.issue_access_code(account.account_id, label=options.label)
    print(f"Account {account.name!r} created{' as admin' if account.is_admin else ''}.")
    print_access_code(account, access_code)


def issue_access_code(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Issue another access code for an account, for example for a second PC, and print it."""
    account = require_account(database, options.name)
    access_code = database.issue_access_code(account.account_id, label=options.label)
    print_access_code(account, access_code)


def revoke_access_codes(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Revoke every access code of an account, so it can no longer log on until it gets a new one."""
    account = require_account(database, options.name)
    revoked_count = database.revoke_access_codes(account.account_id)
    print(f"Revoked {revoked_count} access code(s) of {account.name!r}.")


def list_accounts(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print every account with its admin rights, its clan and a ban that is still running."""
    now = utc_now()
    for account in database.list_accounts():
        clan = database.find_clan_of_account(account.account_id)
        notes = []
        if account.is_admin:
            notes.append("admin")
        if clan is not None:
            notes.append(f"clan {clan.name}")
        if account.is_banned_at(now):
            notes.append(f"banned until {account.banned_until:%Y-%m-%d %H:%M} UTC ({account.ban_reason})")
        print(f"{account.name}  {', '.join(notes)}".rstrip())


def remove_account(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Delete an account."""
    account = require_account(database, options.name)
    database.delete_account(account.account_id)
    print(f"Account {account.name!r} removed.")


def set_admin(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Grant admin rights to an account, or take them away with --remove."""
    account = require_account(database, options.name)
    is_admin = not options.remove
    database.set_admin(account.account_id, is_admin)
    print(f"{account.name!r} is {'now' if is_admin else 'no longer'} an admin.")


def ban_account(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Bar an account from the lobby for --hours, with a reason.

    A running server removes the player if they are online; the reason is shown to them and noted
    for other admins."""
    account = require_account(database, options.name)
    until = utc_now() + timedelta(hours=options.hours)
    database.ban_account(account.account_id, until, options.reason)
    print(f"{account.name!r} is banned until {until:%Y-%m-%d %H:%M} UTC; if online, the server removes them.")


def kick_account(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Queue a kick for a player in the running lobby; they may log on again right away.

    The command only writes to the database: the server reads the queue and carries the kick out
    within a second."""
    account = require_account(database, options.name)
    # The full line is stored here, so a kick from the command line reads the same as the chat command's.
    database.queue_kick(account.account_id, kick_reason(options.reason))
    print(f"Queued; a running server removes {account.name!r} from the lobby within a second, if online.")


def unban_account(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Lift an account's ban."""
    account = require_account(database, options.name)
    database.unban_account(account.account_id)
    print(f"{account.name!r} is no longer banned.")


# Clans.


def add_clan(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Create a clan."""
    clan = database.create_clan(options.name)
    print(f"Clan {clan.name!r} created.")


def remove_clan(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Delete a clan; its members are left in no clan."""
    clan = require_clan(database, options.name)
    database.delete_clan(clan.clan_id)
    print(f"Clan {clan.name!r} removed; its members are in no clan now.")


def list_clans(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print every clan with its members and whether it is highlighted."""
    for clan in database.list_clans():
        members = database.list_clan_member_names(clan.clan_id)
        member_text = ", ".join(members) if members else "no members"
        highlight_text = ""
        if clan.is_highlighted:
            highlight_text = " (green in the game)"
        print(f"{clan.name}{highlight_text}: {member_text}")


def highlight_clan(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Have the game list the clan's members green, from their next logon."""
    clan = require_clan(database, options.name)
    database.set_clan_highlighted(clan.clan_id, True)
    print(f"Members of clan {clan.name!r} who show the clan are listed green in the game from their next logon.")


def unhighlight_clan(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """List the clan's members in the usual colour again, from their next logon."""
    clan = require_clan(database, options.name)
    database.set_clan_highlighted(clan.clan_id, False)
    print(f"Members of clan {clan.name!r} are listed in the usual colour from their next logon.")


def join_clan(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Put an account into a clan, taking it out of the one it was in."""
    clan = require_clan(database, options.clan)
    account = require_account(database, options.account)
    database.set_clan_of_account(account.account_id, clan.clan_id)
    print(f"{account.name!r} is now in clan {clan.name!r}.")


def leave_clan(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Take an account out of its clan."""
    account = require_account(database, options.account)
    database.set_clan_of_account(account.account_id, None)
    print(f"{account.name!r} is in no clan now.")


# Home tab news.


def set_news(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Set the Home tab news for one audience (german games, or all others)."""
    body = read_text_option(options)
    database.set_news(options.audience, options.title, body)
    print(f"News for the {options.audience} audience set. Players see it from their next logon.")


def show_news(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print the Home tab news of both audiences."""
    for audience in NEWS_AUDIENCES:
        news = database.find_news(audience)
        if news is None:
            print(f"[{audience}] no news")
            continue
        print(f"[{audience}] {news.title}\n{news.body}\n")


# ChatZone themes.


def add_chat_theme(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Add a theme to the list on the ChatZone tab."""
    theme = database.add_chat_theme(options.name, options.description, options.language)
    print(f"Chat theme {theme.name!r} added.")


def remove_chat_theme(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Remove the chat theme with this name; the first one when several share it."""
    themes = [theme for theme in database.list_chat_themes() if theme.name == options.name]
    if not themes:
        raise AdminError(f"there is no chat theme named {options.name!r}")
    database.delete_chat_theme(themes[0].theme_id)
    print(f"Chat theme {options.name!r} removed.")


def list_chat_themes(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print every chat theme with its description and, for a theme of one language, that
    language; the built-in ones first. Under each, its permanent chat groups with their ids."""
    # General first, with its permanent groups indented under it.
    groups = database.list_permanent_chat_groups()
    for built_in in BUILT_IN_CHAT_THEMES:
        print(f"{built_in.name}  {built_in.description}  (built in)")
        print_permanent_groups(groups, None)
    # Then the admins' themes, each with its language if it has one, and its groups.
    for theme in database.list_chat_themes():
        language_note = f"  ({theme.language} games only)" if theme.language else ""
        print(f"{theme.name}  {theme.description}{language_note}".rstrip())
        print_permanent_groups(groups, theme.theme_id)


def print_permanent_groups(groups: list[PermanentChatGroup], chat_theme_id: int | None) -> None:
    """Print the permanent chat groups of one theme, indented under it."""
    for group in groups:
        if group.chat_theme_id == chat_theme_id:
            print(f"    group {group.group_row_id}: {group.name}")


def add_permanent_chat_group(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Keep one more chat group open in a theme, named by the theme's name ("General" for the
    built-in one)."""
    chat_theme_id = require_chat_theme_id(database, options.theme)
    group = database.add_permanent_chat_group(chat_theme_id, options.name)
    print(f"Permanent chat group {group.group_row_id} {group.name!r} added.")


def remove_permanent_chat_group(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Stop keeping a chat group open, by its id from chat-theme list."""
    database.delete_permanent_chat_group(options.group_id)
    print(f"Permanent chat group {options.group_id} removed; it closes once nobody is in it.")


def require_chat_theme_id(database: LobbyDatabase, name: str) -> int | None:
    """The database id of the theme with this name, None for the built-in General; raises
    AdminError for an unknown theme."""
    if is_built_in_chat_theme_name(name):
        return None
    for theme in database.list_chat_themes():
        if theme.name.casefold() == name.strip().casefold():
            return theme.theme_id
    raise AdminError(f"there is no chat theme named {name!r}")


# MessageZone board.


def add_board_theme(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Add a MessageZone board theme, seen by the games of one language."""
    theme = database.add_board_theme(options.language, options.name, options.description)
    print(f"Board theme {theme.theme_id} {theme.name!r} added for {theme.language}.")


def remove_board_theme(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Remove a board theme, by its id, together with its messages."""
    database.delete_board_theme(options.theme_id)
    print(f"Board theme {options.theme_id} and its messages removed.")


def list_board(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print every board theme with the messages posted to it, in the board's order, hidden ones
    marked."""
    # One line per theme with its marks, its messages indented under it in the board's order.
    for theme in database.list_board_themes():
        theme_marks = hidden_mark(theme.is_hidden)
        if theme.is_server_written:
            theme_marks += " (written by the server)"
        line = f"{theme.theme_id}  [{theme.language}] {theme.name}"
        if theme.description:
            line += f"  {theme.description}"
        print(line + theme_marks)
        # Each message with its id, for move-message, hide-message and remove-message.
        for message in database.list_board_messages(theme.theme_id):
            print(
                f"    message {message.message_id}: {message.title} ({message.author}, "
                f"{message.posted_at:%Y-%m-%d}){hidden_mark(message.is_hidden)}"
            )


def hidden_mark(is_hidden: bool) -> str:
    """The note after a hidden board theme or message in the listing."""
    if is_hidden:
        return " (hidden)"
    return ""


def move_board_item(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Move a board theme or message one place up or down, as Up and Down on the web do."""
    # The same move as Up and Down on the web, for a theme or a message.
    is_up = options.direction == MOVE_UP
    if options.board_item_kind == BOARD_THEME_KIND:
        database.move_board_theme(options.item_id, is_up)
    else:
        database.move_board_message(options.item_id, is_up)
    print(f"Board {options.board_item_kind} {options.item_id} moved {options.direction}.")


def set_board_item_hidden(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Hide a board theme or message from the game, or show it again. The server's own theme and
    messages can be hidden like any other, though not removed."""
    # The same switch as Hide and Show on the web, for a theme or a message.
    if options.board_item_kind == BOARD_THEME_KIND:
        database.set_board_theme_hidden(options.item_id, options.is_hidden)
    else:
        database.set_board_message_hidden(options.item_id, options.is_hidden)
    state = "hidden" if options.is_hidden else "shown"
    print(f"Board {options.board_item_kind} {options.item_id} {state} in the game.")


def post_board_message(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Post a message to a board theme, by the theme's id."""
    body = read_text_option(options)
    message = database.post_board_message(options.theme_id, options.title, body, author=options.author)
    print(f"Message {message.message_id} posted.")


def remove_board_message(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Remove a board message, by its id."""
    database.delete_board_message(options.message_id)
    print(f"Message {options.message_id} removed.")


# Announcements.


def queue_announcement(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Queue a line for every player in the running lobby, or with --to for one player only.

    The server polls the queue and sends it within a second; lines queued while no server runs
    are dropped at its next start. A line for one player reaches them only if they are online then."""
    if not options.to:
        database.queue_announcement(options.text)
        print("Queued; a running server sends it to every player in the lobby within a second.")
        return
    account = require_account(database, options.to)
    database.queue_announcement(options.text, account_id=account.account_id)
    print(f"Queued; a running server sends it to {account.name} within a second, if online.")


# Settings.


def set_setting(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Change a server setting, refusing a key that is not a known setting."""
    database.set_setting(require_setting(options.key), options.value)
    print(f"{options.key} = {options.value}")


def list_settings(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print every setting with its current value."""
    for setting in Setting:
        print(f"{setting.key} = {database.get_setting(setting)}")


# The database itself.


def show_info(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print the installation at a glance, as the web interface's Info page shows it: text for
    people, JSON (--format json) for tools."""
    info = collect_server_info(database)
    if options.format == INFO_JSON_FORMAT:
        print(json.dumps(asdict(info), default=json_time, indent=2, ensure_ascii=False))
        return
    # The software, the database and the server.
    state = "running" if info.is_running else "not running"
    lines = [
        f"s3lobby {info.version}, Python {info.python_version}, {info.system}",
        f"Database: {info.database} ({info.dialect}), schema version {info.schema_version}, "
        f"{info.pending_migration_count} migration(s) pending",
        f"Server: {info.server_name or '-'} {state}",
    ]
    # The lobby's numbers.
    lines += [
        f"Players: {info.players_online} online, {info.players_last_hour} in the last hour, "
        f"{info.players_last_day} in the last day",
        f"Games: {info.games_open} open, {info.games_last_hour} in the last hour, {info.games_last_day} in the "
        f"last day, {info.game_count} in all, {info.report_count} report(s)",
        f"Accounts: {info.account_count} ({info.admin_count} admin(s), {info.banned_count} banned), "
        f"{info.clan_count} clan(s)",
        f"Content: {info.chat_theme_count} chat theme(s), {info.permanent_group_count} permanent group(s), "
        f"{info.board_theme_count} board theme(s) with {info.board_message_count} message(s)",
        f"Ladder: {info.counted_result_count} counted, {info.disputed_result_count} disputed, "
        f"{info.void_result_count} void",
    ]
    lines += [f"  {members.ladder}: {len(members.names)} member(s)" for members in info.ladder_members]
    # The settings worth knowing at a glance.
    lines += [f"{key} = {'on' if is_on else 'off'}" for key, is_on in info.settings]
    print("\n".join(lines))


def json_time(value: object) -> str:
    """What json cannot write itself: times as ISO 8601."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    raise TypeError(f"{type(value).__name__} is not JSON")


def show_database_status(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Opened without migrating: shows what the next start of the server would apply."""
    status = database.read_migration_status()
    print(f"{database.description} ({database.dialect}), schema version {database.schema_version}")
    for applied in status.applied:
        print(f"  applied  {applied.version:04d} {applied.name}  ({applied.applied_at})")
    for pending in status.pending:
        print(f"  pending  {pending.version:04d} {pending.name}")
    if not status.pending:
        print("Up to date.")


def migrate_database(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Opening the database has applied the migrations; this reports which."""
    for migration in database.applied_migrations:
        print(f"Applied {migration.version:04d} {migration.name}.")
    print(f"{database.description} is at schema version {database.schema_version}.")


def copy_database(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Copy everything into another, empty database (--to) and print the rows per table.

    Meant for moving, for example from SQLite to MySQL, while the server is stopped."""
    # Opened with migrations applied, so the target has the same schema as the source before copying.
    target = LobbyDatabase(options.to)
    try:
        copied_rows = database.copy_to(target)
    finally:
        target.close()
    for table, row_count in copied_rows.items():
        print(f"{table}: {row_count}")
    print(f"Copied {database.description} to {target.description}.")


# Helpers.


def print_access_code(account: Account, access_code: str) -> None:
    """Print a new access code and how to hand it over. It is shown only this once: the database
    keeps nothing but its hash."""
    print(f"Access code for {account.name!r}: {access_code}")
    print("Give it to the player: player name and this code as password in the game's logon. It is")
    print("shown only now; the server keeps nothing but a hash of it.")


def require_account(database: LobbyDatabase, name: str) -> Account:
    """The account with this name; raises AdminError when there is none."""
    account = database.find_account(name)
    if account is None:
        raise AdminError(f"there is no account named {name!r}")
    return account


def require_clan(database: LobbyDatabase, name: str) -> Clan:
    """The clan with this name; raises AdminError when there is none."""
    clan = database.find_clan(name)
    if clan is None:
        raise AdminError(f"there is no clan named {name!r}")
    return clan


def require_setting(key: str) -> Setting:
    """The setting with this key; raises AdminError listing the known keys when there is none."""
    for setting in Setting:
        if setting.key == key:
            return setting
    known_keys = ", ".join(setting.key for setting in Setting)
    raise AdminError(f"unknown setting {key!r}; known settings: {known_keys}")


def read_text_option(options: argparse.Namespace) -> str:
    """The text of a news item or message, from --text or from the file named by --file."""
    if options.file:
        # Editors end a file with a newline; it would otherwise become part of the stored text.
        return Path(options.file).read_text(encoding="utf-8").strip()
    return options.text


def run_admin_command(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Run the function the subcommand chose, turning expected failures into AdminError."""
    try:
        options.admin_function(database, options)
    # The database refuses what breaks its rules (a taken name, an unknown id, a text the game
    # cannot show); for the person at the command line that is a message, not a traceback.
    except DatabaseError as error:
        raise AdminError(str(error)) from error

