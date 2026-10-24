"""Command line: s3lobby serve runs the server, the other subcommands manage its database.

    s3lobby serve --name "Our LAN"
    s3lobby account add Alice
    s3lobby clan add Romans
    s3lobby clan join Romans Alice
    s3lobby news set --audience german --title "Willkommen" --text "Viel Spass!"
    s3lobby announce "Server restart at 22:00"
    s3lobby ladder standings --format json
    s3lobby web --port 8080

Every subcommand takes --database to choose where the data lives: a SQLite file (default:
s3lobby.sqlite3 in the current folder) or mysql://user@host/name for MySQL or MariaDB, with the
password in S3LOBBY_DATABASE_PASSWORD. python -m s3lobby works the same as the installed s3lobby
command.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import sys
from collections.abc import Sequence

from s3lobby import __version__, admin, ladder_admin
from s3lobby.backends import describe_location
from s3lobby.constants import (
    ALL_LANGUAGES,
    BOARD_LANGUAGES,
    DEFAULT_BAN_HOURS,
    DEFAULT_SERVER_PORT,
    DEVELOPMENT_PASSWORD,
    GAME_LANGUAGES,
    NEWS_AUDIENCES,
    SeasonCounts,
)
from s3lobby.database import DatabaseError, LobbyDatabase
from s3lobby.server import ALL_INTERFACES, DEFAULT_DATABASE_PATH, DEFAULT_SERVER_NAME, ServerSettings, serve_forever

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
    """Parse the command line and run the chosen subcommand; returns the process exit code."""
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
    """Run the lobby server (s3lobby serve) until it is stopped; returns the exit code.

    --dev is refused together with --public-host or --upnp, as the shared development password must
    never be usable on a server the internet can reach."""
    configure_logging(options.verbose, options.log_file)
    is_reachable_from_the_internet = bool(options.public_host) or options.upnp
    if options.dev and is_reachable_from_the_internet:
        # A shared, well-known password must never meet a server that is open to the internet.
        log.error("--dev cannot be combined with --public-host or --upnp")
        return EXIT_FAILURE
    settings = ServerSettings(
        bind_host=options.host,
        port=options.port,
        server_name=options.name,
        public_host=options.public_host,
        database_location=options.database,
        use_upnp=options.upnp,
        is_development_mode=options.dev,
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
    if options.dev and options.behind_proxy:
        # A reverse proxy means the interface is reachable from the internet; a shared, well-known
        # password must never meet that.
        log.error("--dev cannot be combined with --behind-proxy")
        return EXIT_FAILURE
    try:
        app = create_app(
            database_location=options.database,
            secret_key=os.environ.get(WEB_SECRET_VARIABLE),
            secure_cookies=options.secure_cookies,
            is_behind_proxy=options.behind_proxy,
            is_development_mode=options.dev,
        )
        # Never the interactive debugger, even with FLASK_DEBUG set: it would run code for anyone.
        app.run(
            host=options.host, port=options.port, threaded=True, debug=False, use_debugger=False, use_reloader=False
        )
    except OSError as error:
        log.error("cannot serve the web interface: %s", error)
        return EXIT_FAILURE
    return EXIT_SUCCESS


def run_admin(options: argparse.Namespace) -> int:
    """Open the database and run one administration subcommand; returns the exit code.

    Errors are printed for the person who typed the command, not logged. database status opens
    the database without applying migrations, so looking changes nothing."""
    try:
        # Only database status sets applies_migrations; every other subcommand brings the schema up to date.
        applies_migrations = getattr(options, "applies_migrations", True)
        database = LobbyDatabase(options.database, applies_migrations=applies_migrations)
    except (OSError, DatabaseError) as error:
        print(f"error: cannot open {describe_location(options.database)}: {error}", file=sys.stderr)
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
    """INFO on the console. is_verbose adds every datagram at DEBUG: into the log file when there is
    one, so the console stays readable while the file keeps the full detail for checking a run
    against the game, and onto the console only when there is no file to take it."""
    detail_level = logging.INFO
    if is_verbose:
        detail_level = logging.DEBUG
    console = logging.StreamHandler()
    console.setLevel(detail_level)
    handlers: list[logging.Handler] = [console]
    if log_file:
        console.setLevel(logging.INFO)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(detail_level)
        handlers.append(file_handler)
    # force replaces the handlers of an earlier call, as main() may run more than once in one process.
    logging.basicConfig(level=detail_level, format=LOG_FORMAT, datefmt=LOG_TIME_FORMAT, handlers=handlers, force=True)
    # asyncio announces its event loop selector at debug level; that is noise in this log.
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for every subcommand; each one shares the --database option."""
    database_option = argparse.ArgumentParser(add_help=False)
    database_option.add_argument(
        "--database",
        default=DEFAULT_DATABASE_PATH,
        help="SQLite file, or mysql://user@host/name, with accounts and lobby content (default: %(default)s)",
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
    add_ladder_commands(commands, database_option)
    add_announce_command(commands, database_option)
    add_web_command(commands, database_option)
    add_database_commands(commands, database_option)
    return parser


def add_serve_command(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the serve subcommand, which runs the lobby server."""
    serve = commands.add_parser("serve", parents=[database_option], help="run the lobby server")
    serve.add_argument("--host", default=ALL_INTERFACES, help="address to listen on (default: all interfaces)")
    serve.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT, help="UDP port (default: %(default)s)")
    serve.add_argument(
        "--name", default=DEFAULT_SERVER_NAME, help="server name shown in the game (default: %(default)s)"
    )
    serve.add_argument(
        "--public-host",
        default="",
        help="host name or IP that players from the internet log on to, e.g. your router's public address",
    )
    serve.add_argument(
        "--upnp",
        action="store_true",
        help="let the router forward the server's port and one port per player in this network (UPnP)",
    )
    serve.add_argument(
        "--dev",
        action="store_true",
        help=f"development mode: every account also logs on with the password {DEVELOPMENT_PASSWORD}, "
        "from this machine and its LAN only; never on a server others can reach",
    )
    serve.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "log every datagram (secrets removed): into the --log-file only when one is given, "
            "the console stays at INFO"
        ),
    )
    serve.add_argument("--log-file", help="also write the log to this file")


def add_account_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the account subcommands: create, access codes, admin rights, bans and kicks."""
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

    ban = actions.add_parser(
        "ban", parents=[database_option], help="bar an account from the lobby for a while, online or not"
    )
    ban.add_argument("name")
    ban.add_argument("--hours", type=float, default=DEFAULT_BAN_HOURS, help="length of the ban (default: %(default)s)")
    ban.add_argument("--reason", default="", help="shown to the player if online, and noted for other admins")
    ban.set_defaults(admin_function=admin.ban_account)

    kick = actions.add_parser(
        "kick", parents=[database_option], help="remove a player from the running lobby; they may log on again"
    )
    kick.add_argument("name")
    kick.add_argument("--reason", default="", help="shown to the player")
    kick.set_defaults(admin_function=admin.kick_account)

    unban = actions.add_parser("unban", parents=[database_option], help="lift a ban")
    unban.add_argument("name")
    unban.set_defaults(admin_function=admin.unban_account)


def add_clan_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the clan subcommands: create, remove, list, membership and the green highlight."""
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

    highlight = actions.add_parser(
        "highlight",
        parents=[database_option],
        help="list the clan's members green in the game's member lists (blue for the clan BB)",
    )
    highlight.add_argument("name")
    highlight.set_defaults(admin_function=admin.highlight_clan)

    unhighlight = actions.add_parser(
        "unhighlight", parents=[database_option], help="list the clan's members in the usual colour"
    )
    unhighlight.add_argument("name")
    unhighlight.set_defaults(admin_function=admin.unhighlight_clan)

    leave = actions.add_parser("leave", parents=[database_option], help="take an account out of its clan")
    leave.add_argument("account")
    leave.set_defaults(admin_function=admin.leave_clan)


def add_news_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the news subcommands for the text on the lobby's Home tab."""
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
    """Add the chat-theme subcommands for the themes listed on the ChatZone tab."""
    chat_theme = commands.add_parser("chat-theme", help="the themes listed on the ChatZone tab")
    actions = chat_theme.add_subparsers(dest="chat_theme_command", metavar="action", required=True)

    add = actions.add_parser("add", parents=[database_option], help="add a chat theme")
    add.add_argument("name")
    add.add_argument("--description", default="", help="shown in the Description column")
    add.add_argument(
        "--language",
        default=ALL_LANGUAGES,
        choices=GAME_LANGUAGES,
        help="show the theme only to games of this language (default: every game)",
    )
    add.set_defaults(admin_function=admin.add_chat_theme)

    remove = actions.add_parser("remove", parents=[database_option], help="remove a chat theme")
    remove.add_argument("name")
    remove.set_defaults(admin_function=admin.remove_chat_theme)

    listing = actions.add_parser("list", parents=[database_option], help="list chat themes")
    listing.set_defaults(admin_function=admin.list_chat_themes)

    # Chat groups the server keeps open in a theme (setting permanent_chat_groups).
    add_group = actions.add_parser(
        "add-group", parents=[database_option], help="keep one more chat group open in a theme"
    )
    add_group.add_argument("theme", help='the theme\'s name, "General" for the built-in one')
    add_group.add_argument("name")
    add_group.set_defaults(admin_function=admin.add_permanent_chat_group)
    remove_group = actions.add_parser(
        "remove-group", parents=[database_option], help="stop keeping a chat group open"
    )
    remove_group.add_argument("group_id", type=int, help="the group's id from chat-theme list")
    remove_group.set_defaults(admin_function=admin.remove_permanent_chat_group)


def add_board_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the board subcommands for the themes and messages on the MessageZone tab."""
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

    # Order and visibility, as the web interface's board page sets them by dragging, Up and Down
    # and Hide and Show.
    for kind in admin.BOARD_ITEM_KINDS:
        move = actions.add_parser(f"move-{kind}", parents=[database_option], help=f"move a {kind} one place up or down")
        move.add_argument("item_id", type=int, metavar=f"{kind}_id")
        move.add_argument("direction", choices=admin.MOVE_DIRECTIONS)
        move.set_defaults(admin_function=admin.move_board_item, board_item_kind=kind)
        for is_hidden, verb in ((True, "hide"), (False, "show")):
            visibility = actions.add_parser(
                f"{verb}-{kind}", parents=[database_option], help=f"{verb} a {kind} in the game"
            )
            visibility.add_argument("item_id", type=int, metavar=f"{kind}_id")
            visibility.set_defaults(
                admin_function=admin.set_board_item_hidden, board_item_kind=kind, is_hidden=is_hidden
            )


def add_ladder_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the ladder subcommands: standings, results, admins' decisions and custom seasons
    (s3lobby/ladder_admin.py). Everything the web interface offers for the ladder is here too."""
    ladder = commands.add_parser("ladder", help="the ladder: standings, results, decisions, seasons")
    actions = ladder.add_subparsers(dest="ladder_command", metavar="action", required=True)
    output_option = argparse.ArgumentParser(add_help=False)
    output_option.add_argument(
        "--format", choices=ladder_admin.OUTPUT_FORMATS, default=ladder_admin.TEXT_FORMAT, help="default: %(default)s"
    )
    season_option = argparse.ArgumentParser(add_help=False)
    season_option.add_argument("--season", help="a custom season's name (default: the infinite season)")
    admin_option = argparse.ArgumentParser(add_help=False)
    admin_option.add_argument("--by", help="the admin account the decision is kept under")

    standings = actions.add_parser(
        "standings", parents=[database_option, output_option, season_option], help="the ranking of a season"
    )
    standings.add_argument(
        "--ladder",
        choices=ladder_admin.STANDINGS_VIEWS,
        default=ladder_admin.LIGA,
        help="liga ranks players, clanliga clans, clans the clans' strength from Liga (default: %(default)s)",
    )
    standings.set_defaults(admin_function=ladder_admin.show_standings)

    results = actions.add_parser(
        "results", parents=[database_option, output_option, season_option], help="ladder results, newest first"
    )
    results.add_argument("--state", choices=[state.value for state in ladder_admin.STORED_STATES])
    results.add_argument("--limit", type=int, help="show at most this many")
    results.set_defaults(admin_function=ladder_admin.show_results)

    statistics = actions.add_parser(
        "stats", parents=[database_option, output_option, season_option], help="races and maps of a season"
    )
    statistics.add_argument(
        "--of",
        choices=ladder_admin.STATISTICS_KINDS,
        default=ladder_admin.RACES_STATISTICS,
        help="what text and CSV show (JSON holds both; default: %(default)s)",
    )
    statistics.set_defaults(admin_function=ladder_admin.show_statistics)

    show = actions.add_parser("show", parents=[database_option], help="what the ladder made of one game")
    show.add_argument("game_id", type=int, help="the game's number, as on the Games page")
    show.set_defaults(admin_function=ladder_admin.show_game_result)

    decide = actions.add_parser(
        "decide", parents=[database_option, admin_option], help="name the winning team of a game"
    )
    decide.add_argument("game_id", type=int)
    decide.add_argument("--winner", type=int, required=True, help="the winning team's number from the reports")
    decide.set_defaults(admin_function=ladder_admin.decide_game)

    void = actions.add_parser(
        "void", parents=[database_option, admin_option], help="take a game's result out of every rating"
    )
    void.add_argument("game_id", type=int)
    void.add_argument("--reason", default="", help="kept with the result")
    void.set_defaults(admin_function=ladder_admin.void_game)

    # Players take part only in the ladders they joined; this is what /ladder join does in the game.
    ladder_choice = argparse.ArgumentParser(add_help=False)
    ladder_choice.add_argument("name", help="the player")
    ladder_choice.add_argument("--ladder", default="", help="liga (default), clanliga or a custom season's name")
    join = actions.add_parser("join", parents=[database_option, ladder_choice], help="make a player a ladder member")
    join.set_defaults(admin_function=ladder_admin.join_ladder_for)
    leave = actions.add_parser("leave", parents=[database_option, ladder_choice], help="take a player out of a ladder")
    leave.set_defaults(admin_function=ladder_admin.leave_ladder_for)
    members = actions.add_parser("members", parents=[database_option, output_option], help="who is in which ladder")
    members.set_defaults(admin_function=ladder_admin.list_members)

    add_ladder_season_commands(actions, database_option, output_option, admin_option)


def add_ladder_season_commands(
    actions: argparse._SubParsersAction,
    database_option: argparse.ArgumentParser,
    output_option: argparse.ArgumentParser,
    admin_option: argparse.ArgumentParser,
) -> None:
    """Add `ladder season` for custom seasons. Dates are YYYY-MM-DD in UTC; an end date includes
    that whole day."""
    season = actions.add_parser("season", help="custom seasons beside the infinite one")
    season_actions = season.add_subparsers(dest="season_command", metavar="action", required=True)
    counts_choices = [counts.value for counts in SeasonCounts]

    listing = season_actions.add_parser("list", parents=[database_option, output_option], help="list custom seasons")
    listing.set_defaults(admin_function=ladder_admin.list_seasons)

    add = season_actions.add_parser("add", parents=[database_option, admin_option], help="create a custom season")
    add.add_argument("name")
    add.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    add.add_argument("--end", help="last day, YYYY-MM-DD (default: until an admin ends it)")
    add.add_argument("--counts", choices=counts_choices, default=SeasonCounts.BOTH.value, help="default: %(default)s")
    add.set_defaults(admin_function=ladder_admin.add_season)

    edit = season_actions.add_parser("edit", parents=[database_option], help="change a custom season")
    edit.add_argument("name")
    edit.add_argument("--name", dest="new_name", help="a new name")
    edit.add_argument("--start", help="first day, YYYY-MM-DD")
    edit.add_argument("--end", help="last day, YYYY-MM-DD")
    edit.add_argument("--no-end", action="store_true", help="run until an admin ends it")
    edit.add_argument("--counts", choices=counts_choices)
    edit.set_defaults(admin_function=ladder_admin.edit_season)

    end = season_actions.add_parser("end", parents=[database_option], help="end a custom season")
    end.add_argument("name")
    end.add_argument("--at", help="last day, YYYY-MM-DD (default: now)")
    end.set_defaults(admin_function=ladder_admin.end_season)

    delete = season_actions.add_parser("delete", parents=[database_option], help="delete a custom season")
    delete.add_argument("name")
    delete.set_defaults(admin_function=ladder_admin.delete_season)


def add_setting_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the setting subcommands for the server settings kept in the database."""
    setting = commands.add_parser("setting", help="server settings stored in the database")
    actions = setting.add_subparsers(dest="setting_command", metavar="action", required=True)

    set_value = actions.add_parser("set", parents=[database_option], help="change a setting")
    set_value.add_argument("key")
    set_value.add_argument("value")
    set_value.set_defaults(admin_function=admin.set_setting)

    listing = actions.add_parser("list", parents=[database_option], help="show every setting")
    listing.set_defaults(admin_function=admin.list_settings)


def add_announce_command(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the announce subcommand, which sends a line to every player in the running lobby."""
    announce = commands.add_parser(
        "announce", parents=[database_option], help="send a line to every player in the running lobby, or to one"
    )
    announce.add_argument("text")
    announce.add_argument("--to", help="one player's name: only they get the line, if online")
    announce.set_defaults(admin_function=admin.queue_announcement)


def add_database_commands(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the database subcommands: schema status, migrating, and copying to another location."""
    database = commands.add_parser("database", help="the database itself: its schema, moving it elsewhere")
    actions = database.add_subparsers(dest="action", metavar="action", required=True)

    status = actions.add_parser(
        "status", parents=[database_option], help="show the applied and pending schema migrations"
    )
    # Looking must not change anything: the pending migrations are shown, not applied.
    status.set_defaults(admin_function=admin.show_database_status, applies_migrations=False)

    migrate = actions.add_parser(
        "migrate", parents=[database_option], help="apply the pending migrations without starting the server"
    )
    migrate.set_defaults(admin_function=admin.migrate_database)

    copy = actions.add_parser(
        "copy",
        parents=[database_option],
        help="copy everything into another, empty database, e.g. from SQLite to MySQL",
        description="Stop the server first. Then point every s3lobby command at the new location.",
    )
    copy.add_argument("--to", required=True, help="the target: a SQLite file or mysql://user@host/name")
    copy.set_defaults(admin_function=admin.copy_database)


def add_web_command(commands: argparse._SubParsersAction, database_option: argparse.ArgumentParser) -> None:
    """Add the web subcommand, which runs the web interface."""
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
    web.add_argument(
        "--dev",
        action="store_true",
        help=f"development mode: every admin also logs on with the password {DEVELOPMENT_PASSWORD}, "
        "from this machine and its LAN only; never where others can reach it",
    )
    web.add_argument("--verbose", action="store_true", help="log every request")


def add_text_source(parser: argparse.ArgumentParser) -> None:
    """Require the text of a news item or board message either as --text or from a --file."""
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="the text itself")
    source.add_argument("--file", help="a UTF-8 text file holding the text")
