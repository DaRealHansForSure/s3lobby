"""Accounts and clans: create accounts, hand out and revoke access codes, ban, grant admin rights.

A new access code is shown once, on the page that answers the form that made it, and never again:
the database keeps only its hash. It is never put in the session or a redirect, where it would end
up in the cookie or the browser history.

Bans, clan changes and admin rights apply from the player's next logon; a player in the lobby
right now keeps the state it logged on with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from werkzeug.wrappers import Response

from s3lobby.commands import kick_reason
from s3lobby.database import Account, Clan, DatabaseError, utc_now
from s3lobby.web.auth import current_admin, redirect_visitors_to_logon
from s3lobby.web.lobby_view import online_player_names
from s3lobby.web.storage import apply_change, get_database, sentence_from_error
from s3lobby.web.texts import form_flag, form_text

blueprint = Blueprint("accounts", __name__)
blueprint.before_request(redirect_visitors_to_logon)

HTTP_NOT_FOUND = 404
DEFAULT_BAN_HOURS = 24
MAX_BAN_HOURS = 10 * 365 * 24
NO_CLAN_VALUE = ""
# The reason is a note for other admins and never reaches the game; the limit only keeps it short.
MAX_BAN_REASON_LENGTH = 200


@dataclass(frozen=True)
class AccountRow:
    account: Account
    clan_name: str
    is_online: bool
    is_banned: bool


# Accounts.


@blueprint.get("/accounts")
def list_accounts() -> str:
    database = get_database()
    online_names = online_player_names()
    now = utc_now()
    rows = []
    for account in database.list_accounts():
        clan = database.find_clan_of_account(account.account_id)
        rows.append(
            AccountRow(
                account=account,
                clan_name=clan.name if clan is not None else "",
                is_online=account.name in online_names,
                is_banned=account.is_banned_at(now),
            )
        )
    return render_template("accounts.html", rows=rows)


@blueprint.post("/accounts")
def create_account() -> str | Response:
    database = get_database()
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
    database = get_database()
    account = require_account(account_id)
    clan = database.find_clan_of_account(account_id)
    return render_template(
        "account.html",
        account=account,
        clan=clan,
        clans=database.list_clans(),
        access_codes=database.list_access_codes(account_id),
        is_online=account.name in online_player_names(),
        is_banned=account.is_banned_at(utc_now()),
        is_own_account=is_own_account(account),
        default_ban_hours=DEFAULT_BAN_HOURS,
        max_ban_hours=MAX_BAN_HOURS,
        max_ban_reason_length=MAX_BAN_REASON_LENGTH,
    )


@blueprint.post("/accounts/<int:account_id>/codes")
def issue_access_code(account_id: int) -> str:
    account = require_account(account_id)
    access_code = get_database().issue_access_code(account_id, label=form_text("label"))
    return render_template("code_issued.html", account=account, access_code=access_code)


@blueprint.post("/accounts/<int:account_id>/codes/<int:code_id>/revoke")
def revoke_access_code(account_id: int, code_id: int) -> Response:
    require_account(account_id)
    if get_database().revoke_access_code(account_id, code_id):
        flash("Access code revoked.", "good")
    else:
        flash("That access code was already revoked.", "warn")
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/codes/revoke-all")
def revoke_all_access_codes(account_id: int) -> Response:
    require_account(account_id)
    revoked_count = get_database().revoke_access_codes(account_id)
    flash(f"Revoked {revoked_count} access code(s).", "good")
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/clan")
def set_clan(account_id: int) -> Response:
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
    account = require_account(account_id)
    if is_own_account(account):
        flash("You cannot ban yourself.", "bad")
        return redirect_to_account(account_id)
    ban_hours = ban_hours_from_form()
    if ban_hours is None:
        flash(f"The ban needs a length from 1 to {MAX_BAN_HOURS} hours.", "bad")
        return redirect_to_account(account_id)
    until = utc_now() + timedelta(hours=ban_hours)
    apply_change(
        lambda: get_database().ban_account(account_id, until, form_text("reason")[:MAX_BAN_REASON_LENGTH]),
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
    apply_change(
        lambda: get_database().queue_kick(account_id, kick_reason("")),
        f"{account.name} is removed from the lobby within a second.",
    )
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/unban")
def unban_account(account_id: int) -> Response:
    account = require_account(account_id)
    apply_change(lambda: get_database().unban_account(account_id), f"{account.name} is no longer banned.")
    return redirect_to_account(account_id)


@blueprint.post("/accounts/<int:account_id>/delete")
def delete_account(account_id: int) -> Response:
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
    database = get_database()
    clans_with_members = [(clan, database.list_clan_member_names(clan.clan_id)) for clan in database.list_clans()]
    return render_template("clans.html", clans_with_members=clans_with_members)


@blueprint.post("/clans")
def create_clan() -> Response:
    name = form_text("name")
    apply_change(lambda: get_database().create_clan(name), f"Clan {name} created.")
    return redirect(url_for("accounts.list_clans"))


@blueprint.post("/clans/<int:clan_id>/delete")
def delete_clan(clan_id: int) -> Response:
    apply_change(lambda: get_database().delete_clan(clan_id), "Clan removed; its members are in no clan now.")
    return redirect(url_for("accounts.list_clans"))


# Helpers.


def require_account(account_id: int) -> Account:
    account = get_database().find_account_by_id(account_id)
    if account is None:
        abort(HTTP_NOT_FOUND)
    return account


def is_own_account(account: Account) -> bool:
    admin = current_admin()
    return admin is not None and admin.account_id == account.account_id


def redirect_to_account(account_id: int) -> Response:
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
    if not hours_text.isdigit():
        return None
    hours = int(hours_text)
    if hours < 1 or hours > MAX_BAN_HOURS:
        return None
    return hours
