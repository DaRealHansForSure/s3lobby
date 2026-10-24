"""Accounts and clans: create accounts, hand out and revoke access codes, ban, grant admin rights.

A new access code is shown once, on the page that answers the form that made it, and never again:
the database keeps only its hash. It is never put in the session or a redirect, where it would end
up in the cookie or the browser history.

Clan changes and admin rights apply from the player's next logon; a player in the lobby right now
keeps the state it logged on with. A ban is the exception: it also removes a player who is online.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from s3lobby.commands import kick_reason
from s3lobby.constants import DEFAULT_BAN_HOURS, MAX_BAN_HOURS, MAX_GROUP_TEXT_LENGTH, MIN_BAN_HOURS
from s3lobby.database import MAX_BAN_REASON_LENGTH, Account, Clan, DatabaseError, Setting, is_whole_number_text, utc_now
from s3lobby.groups import game_row_colour
from s3lobby.ladder_book import (
    LadderBook,
    LadderMembershipError,
    join_ladder,
    ladder_labels,
    ladder_members,
    leave_ladder,
)
from s3lobby.web.auth import current_admin, redirect_visitors_to_logon
from s3lobby.web.games import games_of_account
from s3lobby.web.lobby_view import online_player_names
from s3lobby.web.paging import pagination_from_request
from s3lobby.web.storage import apply_change, get_database, sentence_from_error
from s3lobby.web.texts import form_flag, form_text

blueprint = Blueprint("accounts", __name__)
blueprint.before_request(redirect_visitors_to_logon)

NO_CLAN_VALUE = ""


@dataclass(frozen=True)
class AccountRow:
    """One account as the accounts list shows it. row_colour is the colour the game draws the
    player's row in (groups.game_row_colour), empty for the usual colour."""

    account: Account
    clan_name: str
    is_online: bool
    is_banned: bool
    row_colour: str = ""


# Accounts.


@blueprint.get("/accounts")
def list_accounts() -> str:
    """The accounts page: one page of the accounts (?q= searches their names) with each one's clan,
    whether it is online or banned, and its row colour."""
    database = get_database()
    search = request.args.get("q", "").strip()[:MAX_GROUP_TEXT_LENGTH]
    pagination = pagination_from_request(database.count_accounts(search))
    online_names = online_player_names()
    now = utc_now()
    marks_admins = database.get_boolean_setting(Setting.MARK_ADMINS)
    rows = []
    for account in database.list_accounts_page(search, pagination.size, pagination.offset):
        clan = database.find_clan_of_account(account.account_id)
        rows.append(
            AccountRow(
                account=account,
                clan_name=clan.name if clan is not None else "",
                is_online=account.name in online_names,
                is_banned=account.is_banned_at(now),
                row_colour=account_row_colour(account, clan, marks_admins),
            )
        )
    return render_template("accounts.html", rows=rows, search=search, pagination=pagination)


def account_row_colour(account: Account, clan: Clan | None, marks_admins: bool) -> str:
    """The colour the game lists this player in when they show their clan at logon: green for an
    admin (setting mark_admins) or a member of a clan marked green, blue for the clan BB."""
    is_highlighted_clan = clan is not None and clan.is_highlighted
    is_highlighted = is_highlighted_clan or (marks_admins and account.is_admin)
    clan_name = clan.name if clan is not None else ""
    return game_row_colour(is_highlighted, clan_name)


@blueprint.post("/accounts")
def create_account() -> str | Response:
    """Create an account and show its first access code, once, on the page that answers the form.

    A name the database refuses (taken, or not a valid player name) sends the admin back to the list."""
    database = get_database()
    # Only the creation can be refused; the code is issued after it, for an account that now exists.
    try:
        account = database.create_account(form_text("name"), is_admin=form_flag("is_admin"))
    except DatabaseError as error:
        flash(sentence_from_error(error), "bad")
        return redirect(url_for("accounts.list_accounts"))
    flash(f"Account {account.name} created.", "good")
    access_code = database.issue_access_code(account.account_id, label=form_text("label"))
    return render_template("code_issued.html", account=account, access_code=access_code)


@blueprint.get("/accounts/<int:account_id>")
def show_account(account_id: int) -> str:
    """An account's page: clan, access codes, games played, and the forms to change it."""
    database = get_database()
    account = require_account(account_id)
    clan = database.find_clan_of_account(account_id)
    return render_template(
        "account.html",
        account=account,
        clan=clan,
        row_colour=account_row_colour(account, clan, database.get_boolean_setting(Setting.MARK_ADMINS)),
        clans=database.list_clans(),
        access_codes=database.list_access_codes(account_id),
        games=games_of_account(account_id),
        is_online=account.name in online_player_names(),
        is_banned=account.is_banned_at(utc_now()),
        is_own_account=is_own_account(account),
        default_ban_hours=DEFAULT_BAN_HOURS,
        max_ban_hours=MAX_BAN_HOURS,
        max_ban_reason_length=MAX_BAN_REASON_LENGTH,
        ladders=ladder_labels(database, account_id),
        joinable_ladders=[member.ladder for member in ladder_members(database)],
    )


@blueprint.post("/accounts/<int:account_id>/ladders/join")
def join_ladder_for_account(account_id: int) -> Response:
    """Make the player a member of the ladder the form names, as /ladder join does in the game."""
    database = get_database()
    account = require_account(account_id)
    ladder = form_text("ladder")
    try:
        join_ladder(LadderBook(database), database, account_id, ladder)
    except LadderMembershipError as error:
        flash(f"{account.name}: {error}.", "bad")
        return redirect_to_account(account_id)
    flash(f"{account.name} is in {ladder or 'Liga'} now.", "good")
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/ladders/leave")
def leave_ladder_for_account(account_id: int) -> Response:
    """Take the player out of a ladder; their results stay for their opponents."""
    database = get_database()
    account = require_account(account_id)
    ladder = form_text("ladder")
    try:
        leave_ladder(LadderBook(database), database, account_id, ladder)
    except LadderMembershipError as error:
        flash(f"{account.name}: {error}.", "bad")
        return redirect_to_account(account_id)
    flash(f"{account.name} is out of {ladder}; joining again starts at the start rating.", "good")
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/codes")
def issue_access_code(account_id: int) -> str:
    """Issue another access code and show it, once, on the page that answers the form."""
    account = require_account(account_id)
    access_code = get_database().issue_access_code(account_id, label=form_text("label"))
    return render_template("code_issued.html", account=account, access_code=access_code)


@blueprint.post("/accounts/<int:account_id>/codes/<int:code_id>/revoke")
def revoke_access_code(account_id: int, code_id: int) -> Response:
    """Revoke one access code; its web sessions end with it, the account's other codes keep working."""
    require_account(account_id)
    if get_database().revoke_access_code(account_id, code_id):
        flash("Access code revoked.", "good")
    else:
        flash("That access code was already revoked.", "warn")
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/codes/revoke-all")
def revoke_all_access_codes(account_id: int) -> Response:
    """Revoke every access code of an account, so it cannot log on until it gets a new one."""
    require_account(account_id)
    revoked_count = get_database().revoke_access_codes(account_id)
    flash(f"Revoked {revoked_count} access code(s).", "good")
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/clan")
def set_clan(account_id: int) -> Response:
    """Put an account into the clan the form chose, or into none.

    The clan is looked up among the existing ones, so a clan deleted in another tab is reported
    instead of failing."""
    require_account(account_id)
    database = get_database()
    chosen_value = form_text("clan_id")
    clan_id = None
    if chosen_value != NO_CLAN_VALUE:
        clan = find_clan_by_form_value(database.list_clans(), chosen_value)
        if clan is None:
            flash("That clan does not exist any more.", "bad")
            return redirect_to_account(account_id)
        clan_id = clan.clan_id
    apply_change(lambda: database.set_clan_of_account(account_id, clan_id), "Clan changed.")
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/admin")
def set_admin(account_id: int) -> Response:
    """Grant or take away admin rights. An admin cannot take their own away, so the last admin
    cannot lock everybody out by mistake."""
    account = require_account(account_id)
    is_admin = form_flag("is_admin")
    if is_own_account(account) and not is_admin:
        flash("You cannot take away your own admin rights; ask another admin.", "bad")
        return redirect_to_account(account_id)
    success_text = f"{account.name} is {'now' if is_admin else 'no longer'} an admin."
    apply_change(lambda: get_database().set_admin(account_id, is_admin), success_text)
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/ban")
def ban_account(account_id: int) -> Response:
    """Ban an account for the hours the form gives, within MIN_BAN_HOURS and MAX_BAN_HOURS. An
    admin cannot ban themselves. A running server removes the player if they are online."""
    account = require_account(account_id)
    if is_own_account(account):
        flash("You cannot ban yourself.", "bad")
        return redirect_to_account(account_id)
    ban_hours = ban_hours_from_form()
    if ban_hours is None:
        flash(f"The ban needs a length from {MIN_BAN_HOURS} to {MAX_BAN_HOURS} hours.", "bad")
        return redirect_to_account(account_id)
    until = utc_now() + timedelta(hours=ban_hours)
    apply_change(
        lambda: get_database().ban_account(account_id, until, form_text("reason")),
        f"{account.name} is banned for {ban_hours} hour(s).",
    )
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/kick")
def kick_account(account_id: int) -> Response:
    """Remove the player from the running lobby; the server carries it out within a second. The
    player may log on again: a ban is what keeps them out."""
    account = require_account(account_id)
    if is_own_account(account):
        flash("You cannot remove yourself from the lobby.", "bad")
        return redirect_to_account(account_id)
    # The form has no reason field, so the player is told the plain kick line.
    apply_change(
        lambda: get_database().queue_kick(account_id, kick_reason("")),
        f"{account.name} is removed from the lobby within a second.",
    )
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/unban")
def unban_account(account_id: int) -> Response:
    """Lift an account's ban."""
    account = require_account(account_id)
    apply_change(lambda: get_database().unban_account(account_id), f"{account.name} is no longer banned.")
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/delete")
def delete_account(account_id: int) -> Response:
    """Delete an account once the admin typed its name to confirm. An admin cannot delete their own
    account."""
    account = require_account(account_id)
    if is_own_account(account):
        flash("You cannot remove your own account.", "bad")
        return redirect_to_account(account_id)
    if form_text("confirm_name") != account.name:
        flash(f"Type the name {account.name} to confirm the removal.", "bad")
        return redirect_to_account(account_id)
    apply_change(lambda: get_database().delete_account(account_id), f"Account {account.name} removed.")
    return redirect(url_for("accounts.list_accounts"))


# Clans.


@blueprint.get("/clans")
def list_clans() -> str:
    """The clans page: every clan with its members and the colour the game lists them in."""
    database = get_database()
    clans_with_members = []
    for clan in database.list_clans():
        colour = game_row_colour(clan.is_highlighted, clan.name)
        clans_with_members.append((clan, database.list_clan_member_names(clan.clan_id), colour))
    return render_template("clans.html", clans_with_members=clans_with_members)


@blueprint.post("/clans")
def create_clan() -> Response:
    """Create a clan with the name from the form."""
    name = form_text("name")
    apply_change(lambda: get_database().create_clan(name), f"Clan {name} created.")
    return redirect(url_for("accounts.list_clans"))


@blueprint.post("/clans/<int:clan_id>/highlight")
def highlight_clan(clan_id: int) -> Response:
    """Have the game list the clan's members green, or in the usual colour again, from their next logon."""
    is_highlighted = form_flag("is_highlighted")
    if is_highlighted:
        done = "The clan's members are listed green in the game from their next logon."
    else:
        done = "The clan's members are listed in the usual colour from their next logon."
    apply_change(lambda: get_database().set_clan_highlighted(clan_id, is_highlighted), done)
    return redirect(url_for("accounts.list_clans"))


@blueprint.post("/clans/<int:clan_id>/delete")
def delete_clan(clan_id: int) -> Response:
    """Delete a clan; its members are left in no clan."""
    apply_change(lambda: get_database().delete_clan(clan_id), "Clan removed; its members are in no clan now.")
    return redirect(url_for("accounts.list_clans"))


# Helpers.


def require_account(account_id: int) -> Account:
    """The account with this id; answers 404 when there is none."""
    account = get_database().find_account_by_id(account_id)
    if account is None:
        abort(HTTPStatus.NOT_FOUND)
    return account


def is_own_account(account: Account) -> bool:
    """True when the account is the one of the admin who is logged on."""
    admin = current_admin()
    return admin is not None and admin.account_id == account.account_id


def redirect_to_account(account_id: int) -> Response:
    """A redirect to the account's page."""
    return redirect(url_for("accounts.show_account", account_id=account_id))


def find_clan_by_form_value(clans: list[Clan], form_value: str) -> Clan | None:
    """The clan whose id the form sent, compared as text so no form value can fail to parse."""
    for clan in clans:
        if str(clan.clan_id) == form_value:
            return clan
    return None


def ban_hours_from_form() -> int | None:
    """The ban length in whole hours, or None when the field holds no length within the limits."""
    hours_text = form_text("hours")
    if not is_whole_number_text(hours_text):
        return None
    hours = int(hours_text)
    if hours < MIN_BAN_HOURS or hours > MAX_BAN_HOURS:
        return None
    return hours
