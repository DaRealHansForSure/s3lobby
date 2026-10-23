"""The web interface: the live lobby for everyone, administration for the lobby's admins.

A Flask application on the same SQLite file the server uses. It never talks to the server
directly: it shows the live lobby the server writes to the database (s3lobby/live.py) and writes
accounts, clans, news, themes, settings and announcements, which the server reads from there.

Admins log on with their account name and one of their access codes, the same as in the game.
Only accounts with admin rights get in.

Flask is an optional dependency, installed with `pip install .[web]`; the lobby server itself
needs nothing beyond the standard library. Run it with `s3lobby web`, or under a WSGI server with
s3lobby.web.app:create_app_from_environment. This package file imports nothing, so the rest of
s3lobby (and test discovery) works where Flask is not installed.
"""
