"""Command line: s3lobby serve runs the server, the other subcommands manage its database.

    s3lobby serve --name "Our LAN"
    s3lobby account add Alice
    s3lobby clan add Romans
    s3lobby clan join Romans Alice
    s3lobby news set --audience german --title "Willkommen" --text "Viel Spass!"
    s3lobby announce "Server restart at 22:00"
    s3lobby web --port 8080

Every subcommand takes --database to choose the SQLite file (default: s3lobby.sqlite3 in the
current folder). python -m s3lobby works the same as the installed s3lobby command.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import sys
from collections.abc import Sequence

from s3lobby import __version__, admin
from s3lobby.constants import BOARD_LANGUAGES, DEFAULT_SERVER_PORT, NEWS_AUDIENCES
from s3lobby.database import DatabaseError, LobbyDatabase
from s3lobby.server import ALL_INTERFACES, DEFAULT_DATABASE_PATH, DEFAULT_SERVER_NAME, ServerSettings, serve_forever

DEFAULT_BAN_HOURS = 24
LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s"
LOG_TIME_FORMAT = "%H:%M:%S"
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
# The web interface listens on this machine only unless told otherwise: it is an admin interface,
# and plain HTTP should not leave the machine without a reverse proxy adding HTTPS.
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
WEB_SECRET_VARIABLE = "S3LOBBY_WEB_SECRET"

log = logging.getLogger(__name__)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(arguments)
    if options.command is None:
        parser.print_help()
        return EXIT_FAILURE
    if options.command == "serve":
        return run_server(options)
    if options.command == "web":
        return run_web_interface(options)
    return run_admin(options)


def run_server(options: argparse.Namespace) -> int:
    configure_logging(options.verbose, options.log_file)
    settings = ServerSettings(
        bind_host=options.host,
        port=options.port,
        server_name=options.name,
        public_host=options.public_host,
        database_path=options.database,
    )
    try:
        asyncio.run(serve_forever(settings))
    except KeyboardInterrupt:
        log.info("stopped")
    except (OSError, DatabaseError) as error:
        log.error("cannot serve: %s", error)
        return EXIT_FAILURE
    return EXIT_SUCCESS


def run_web_interface(options: argparse.Namespace) -> int:
    """Serve the web interface with Flask's own server, which is enough for a LAN. For the internet,
    run s3lobby.web.app:create_app_from_environment under a WSGI server behind an HTTPS proxy."""
    if importlib.util.find_spec("flask") is None:
        print("error: the web interface needs Flask; install it with: pip install .[web]", file=sys.stderr)
        return EXIT_FAILURE
    # Imported here so every other command works without Flask installed.
    from s3lobby.web.app import create_app

    configure_logging(options.verbose, log_file=None)
    try:
        app = create_app(
            database_path=options.database,
            secret_key=os.environ.get(WEB_SECRET_VARIABLE),
            secure_cookies=options.secure_cookies,
            is_behind_proxy=options.behind_proxy,
        )
        app.run(host=options.host, port=options.port, threaded=True)
    except OSError as error:
        log.error("cannot serve the web interface: %s", error)
        return EXIT_FAILURE
    return EXIT_SUCCESS


def run_admin(options: argparse.Namespace) -> int:
    try:
        database = LobbyDatabase(options.database)
    except (OSError, DatabaseError) as error:
        print(f"error: cannot open {options.database}: {error}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        admin.run_admin_command(database, options)
    except admin.AdminError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        database.close()
    return EXIT_SUCCESS


def configure_logging(is_verbose: bool, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    level = logging.DEBUG if is_verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_TIME_FORMAT, handlers=handlers)
    # asyncio announces its event loop selector at debug level; that is noise in this log.
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    database_option = argparse.ArgumentParser(add_help=False)
    database_option.add_argument(
        "--database",
        default=DEFAULT_DATABASE_PATH,
        help="SQLite file with accounts and lobby content (default: %(default)s)",
    )

    parser = argparse.ArgumentParser(
        prog="s3lobby",
        description="Lobby server for the online lobby of the original The Settlers III.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    add_serve_command(commands, database_option)
    add_account_commands(commands, database_option)
    add_clan_commands(commands, database_option)
    add_news_commands(commands, database_option)
    add_chat_theme_commands(commands, database_option)
    add_board_commands(commands, database_option)
    add_setting_commands(commands, database_option)
    add_announce_command(commands, database_option)
    add_web_command(commands, database_option)
    return parser


def add_serve_command(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    serve = commands.add_parser("serve", parents=[database_option], help="run the lobby server")
    serve.add_argument("--host", default=ALL_INTERFACES, help="address to listen on (default: all interfaces)")
    serve.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT, help="UDP port (default: %(default)s)")
    serve.add_argument("--name", default=DEFAULT_SERVER_NAME, help="server name shown in the game (default: %(default)s)")
    serve.add_argument(
        "--public-host",
        default="",
        help="host name or IP that players from the internet log on to, e.g. your router's public address",
    )
    serve.add_argument("--verbose", action="store_true", help="log every datagram (secrets removed)")
    serve.add_argument("--log-file", help="also write the log to this file")


def add_account_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    account = commands.add_parser("account", help="manage player accounts and their access codes")
    actions = account.add_subparsers(dest="account_command", metavar="action", required=True)

    add = actions.add_parser("add", parents=[database_option], help="create an account and print its first access code")
    add.add_argument("name", help="player name: letters A to Z and single spaces")
    add.add_argument("--admin", action="store_true", help="may use the admin chat commands")
    add.add_argument("--label", default="", help="note to tell this access code from others, e.g. a PC")
    add.set_defaults(admin_function=admin.add_account)

    code = actions.add_parser("code", parents=[database_option], help="issue another access code")
    code.add_argument("name")
    code.add_argument("--label", default="")
    code.set_defaults(admin_function=admin.issue_access_code)

    revoke = actions.add_parser("revoke-codes", parents=[database_option], help="revoke every access code")
    revoke.add_argument("name")
    revoke.set_defaults(admin_function=admin.revoke_access_codes)

    listing = actions.add_parser("list", parents=[database_option], help="list accounts")
    listing.set_defaults(admin_function=admin.list_accounts)

    remove = actions.add_parser("remove", parents=[database_option], help="delete an account")
    remove.add_argument("name")
    remove.set_defaults(admin_function=admin.remove_account)

    make_admin = actions.add_parser("admin", parents=[database_option], help="grant or remove admin rights")
    make_admin.add_argument("name")
    make_admin.add_argument("--remove", action="store_true", help="take the admin rights away")
    make_admin.set_defaults(admin_function=admin.set_admin)

    ban = actions.add_parser("ban", parents=[database_option], help="bar an account from the lobby for a while")
    ban.add_argument("name")
    ban.add_argument("--hours", type=float, default=DEFAULT_BAN_HOURS, help="length of the ban (default: %(default)s)")
    ban.add_argument("--reason", default="", help="note for other admins")
    ban.set_defaults(admin_function=admin.ban_account)

    unban = actions.add_parser("unban", parents=[database_option], help="lift a ban")
    unban.add_argument("name")
    unban.set_defaults(admin_function=admin.unban_account)


def add_clan_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    clan = commands.add_parser("clan", help="manage clans and their members")
    actions = clan.add_subparsers(dest="clan_command", metavar="action", required=True)

    add = actions.add_parser("add", parents=[database_option], help="create a clan")
    add.add_argument("name")
    add.set_defaults(admin_function=admin.add_clan)

    remove = actions.add_parser("remove", parents=[database_option], help="delete a clan")
    remove.add_argument("name")
    remove.set_defaults(admin_function=admin.remove_clan)

    listing = actions.add_parser("list", parents=[database_option], help="list clans and members")
    listing.set_defaults(admin_function=admin.list_clans)

    join = actions.add_parser("join", parents=[database_option], help="put an account into a clan")
    join.add_argument("clan")
    join.add_argument("account")
    join.set_defaults(admin_function=admin.join_clan)

    leave = actions.add_parser("leave", parents=[database_option], help="take an account out of its clan")
    leave.add_argument("account")
    leave.set_defaults(admin_function=admin.leave_clan)


def add_news_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    news = commands.add_parser("news", help="the news on the lobby's Home tab")
    actions = news.add_subparsers(dest="news_command", metavar="action", required=True)

    set_news = actions.add_parser("set", parents=[database_option], help="set the news for an audience")
    set_news.add_argument("--audience", choices=NEWS_AUDIENCES, required=True, help="german games, or all others")
    set_news.add_argument("--title", required=True, help="shown above the text; must not be empty")
    add_text_source(set_news)
    set_news.set_defaults(admin_function=admin.set_news)

    show = actions.add_parser("show", parents=[database_option], help="show the current news")
    show.set_defaults(admin_function=admin.show_news)


def add_chat_theme_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    chat_theme = commands.add_parser("chat-theme", help="the themes listed on the ChatZone tab")
    actions = chat_theme.add_subparsers(dest="chat_theme_command", metavar="action", required=True)

    add = actions.add_parser("add", parents=[database_option], help="add a chat theme")
    add.add_argument("name")
    add.add_argument("--description", default="", help="shown in the Description column")
    add.set_defaults(admin_function=admin.add_chat_theme)

    remove = actions.add_parser("remove", parents=[database_option], help="remove a chat theme")
    remove.add_argument("name")
    remove.set_defaults(admin_function=admin.remove_chat_theme)

    listing = actions.add_parser("list", parents=[database_option], help="list chat themes")
    listing.set_defaults(admin_function=admin.list_chat_themes)


def add_board_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    board = commands.add_parser("board", help="the message board on the MessageZone tab")
    actions = board.add_subparsers(dest="board_command", metavar="action", required=True)

    add_theme = actions.add_parser("add-theme", parents=[database_option], help="add a theme for one language")
    add_theme.add_argument("language", choices=BOARD_LANGUAGES, help="language of the games that see it")
    add_theme.add_argument("name")
    add_theme.add_argument("--description", default="")
    add_theme.set_defaults(admin_function=admin.add_board_theme)

    remove_theme = actions.add_parser("remove-theme", parents=[database_option], help="remove a theme and its messages")
    remove_theme.add_argument("theme_id", type=int)
    remove_theme.set_defaults(admin_function=admin.remove_board_theme)

    listing = actions.add_parser("list", parents=[database_option], help="list themes and messages")
    listing.set_defaults(admin_function=admin.list_board)

    post = actions.add_parser("post", parents=[database_option], help="post a message to a theme")
    post.add_argument("theme_id", type=int)
    post.add_argument("--title", required=True)
    post.add_argument("--author", default="")
    add_text_source(post)
    post.set_defaults(admin_function=admin.post_board_message)

    remove_message = actions.add_parser("remove-message", parents=[database_option], help="remove a message")
    remove_message.add_argument("message_id", type=int)
    remove_message.set_defaults(admin_function=admin.remove_board_message)


def add_setting_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    setting = commands.add_parser("setting", help="server settings stored in the database")
    actions = setting.add_subparsers(dest="setting_command", metavar="action", required=True)

    set_value = actions.add_parser("set", parents=[database_option], help="change a setting")
    set_value.add_argument("key")
    set_value.add_argument("value")
    set_value.set_defaults(admin_function=admin.set_setting)

    listing = actions.add_parser("list", parents=[database_option], help="show every setting")
    listing.set_defaults(admin_function=admin.list_settings)


def add_announce_command(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    announce = commands.add_parser(
        "announce", parents=[database_option], help="send a line to every player in the running lobby"
    )
    announce.add_argument("text")
    announce.set_defaults(admin_function=admin.queue_announcement)


def add_web_command(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    web = commands.add_parser(
        "web",
        parents=[database_option],
        help="run the web interface: the live lobby, and administration for admins (needs Flask)",
        description=(
            f"Admins log on with their player name and an access code. Set {WEB_SECRET_VARIABLE} to a "
            "long random string to keep them logged on across restarts."
        ),
    )
    web.add_argument("--host", default=DEFAULT_WEB_HOST, help="address to listen on (default: %(default)s)")
    web.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="TCP port (default: %(default)s)")
    web.add_argument("--secure-cookies", action="store_true", help="send the session cookie over HTTPS only")
    web.add_argument(
        "--behind-proxy",
        action="store_true",
        help="take the client address from the reverse proxy's X-Forwarded-For header",
    )
    web.add_argument("--verbose", action="store_true", help="log every request")


def add_text_source(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="the text itself")
    source.add_argument("--file", help="a UTF-8 text file holding the text")
