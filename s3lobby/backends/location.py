"""Where the data lives, as one string: a SQLite file, or mysql://user@host/name for MySQL."""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from s3lobby.backends.base import BackendError

MYSQL_SCHEME = "mysql"
MYSQL_LOCATION_PREFIX = f"{MYSQL_SCHEME}://"
DEFAULT_MYSQL_PORT = 3306
# The password may stay out of the location, which shows up in process lists and shell history.
MYSQL_PASSWORD_VARIABLE = "S3LOBBY_DATABASE_PASSWORD"
# Encryption of a MySQL connection, from the location's query: tls=verify or tls=verify-ca, and
# ca=PATH for the authority's certificate (MysqlLocation explains the modes).
TLS_OPTION = "tls"
CA_FILE_OPTION = "ca"
TLS_VERIFY = "verify"
TLS_VERIFY_CA = "verify-ca"
TLS_MODES = frozenset({"", TLS_VERIFY, TLS_VERIFY_CA})


@dataclass(frozen=True)
class MysqlLocation:
    """Where a MySQL database is. tls is "" for an unencrypted connection, TLS_VERIFY to check the
    server's certificate and host name against the system's authorities (or ca_file), or
    TLS_VERIFY_CA to check the certificate against ca_file only, for a server certificate made for
    no particular host name, such as the one MySQL makes for itself."""

    host: str
    port: int
    user: str
    password: str
    database_name: str
    tls: str = ""
    ca_file: str = ""

    def describe(self) -> str:
        """The location without its password, for logs and error messages."""
        described = f"{MYSQL_LOCATION_PREFIX}{self.user}@{self.host}:{self.port}/{self.database_name}"
        if self.tls:
            return f"{described}?tls={self.tls}"
        return described

    def tls_context(self) -> ssl.SSLContext | None:
        """The TLS settings for the connection, or None for an unencrypted one."""
        if not self.tls:
            return None
        context = ssl.create_default_context(cafile=self.ca_file or None)
        if self.tls == TLS_VERIFY_CA:
            context.check_hostname = False
            # Newer Pythons check certificates strictly and refuse the authority MySQL makes for
            # itself (it lacks an Authority Key Identifier). The chain to ca_file is still checked.
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return context


def describe_location(location: str) -> str:
    """The location as it may be shown: a MySQL password is left out."""
    if not location.startswith(MYSQL_LOCATION_PREFIX):
        return location
    try:
        return parse_mysql_location(location).describe()
    except BackendError:
        return MYSQL_LOCATION_PREFIX


def parse_mysql_location(location: str) -> MysqlLocation:
    """Read mysql://user:password@host:port/name?tls=verify&ca=/path/ca.pem. The password may be
    left out of the location and given in S3LOBBY_DATABASE_PASSWORD instead; special characters in
    it are percent-encoded. Without tls the connection is not encrypted (MysqlLocation explains the
    modes)."""
    parts = urlsplit(location)
    try:
        port = parts.port or DEFAULT_MYSQL_PORT
    except ValueError as error:
        raise BackendError(f"the port in the MySQL location is not a number: {error}") from error
    database_name = unquote(parts.path.lstrip("/"))
    if not parts.hostname or not parts.username or not database_name:
        raise BackendError("a MySQL location needs a user, a host and a database: mysql://user@host/name")
    password = parts.password
    if password is None:
        password = os.environ.get(MYSQL_PASSWORD_VARIABLE, "")
    options = parse_qs(parts.query)
    tls = options.get(TLS_OPTION, [""])[-1]
    ca_file = options.get(CA_FILE_OPTION, [""])[-1]
    if tls not in TLS_MODES:
        raise BackendError(f"tls must be {TLS_VERIFY} or {TLS_VERIFY_CA}, not {tls!r}")
    if tls == TLS_VERIFY_CA and not ca_file:
        raise BackendError(f"tls={TLS_VERIFY_CA} needs the authority's certificate: {CA_FILE_OPTION}=/path/ca.pem")
    return MysqlLocation(
        host=parts.hostname,
        port=port,
        user=unquote(parts.username),
        password=unquote(password),
        database_name=database_name,
        tls=tls,
        ca_file=ca_file,
    )
