#!/usr/bin/env python3
"""
change_password.py

Change the password of an existing user in the backend SQLite database. Run from
the backend/ directory where books.db lives, or point it at the file explicitly.

Usage:
  python change_password.py USERNAME [NEWPASSWORD] [--db PATH]

Omit NEWPASSWORD to be prompted for it (twice, hidden) so the password never
lands in your shell history. The database path can also come from the
BOOKLIB_DB environment variable.
"""
import getpass
import os
import sqlite3
import sys

from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
DB_PATH = os.environ.get("BOOKLIB_DB", "books.db")


def parse_args(argv):
    args = list(argv)
    db = DB_PATH
    if '--db' in args:
        i = args.index('--db')
        if i + 1 >= len(args):
            usage()
        db = args[i + 1]
        del args[i:i + 2]
    if not args:
        usage()
    username = args[0]
    password = args[1] if len(args) > 1 else None
    return username, password, db


def usage():
    print('Usage: python change_password.py USERNAME [NEWPASSWORD] [--db PATH]')
    sys.exit(2)


def prompt_password():
    first = getpass.getpass('New password: ')
    if not first:
        print('Password cannot be empty.')
        sys.exit(2)
    if first != getpass.getpass('Confirm password: '):
        print('Passwords do not match.')
        sys.exit(2)
    return first


def change_password(username, password, db_path):
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            print("No such user:", username)
            sys.exit(1)
        if password is None:
            password = prompt_password()
        try:
            hashed = pwd_context.hash(password)
        except Exception as e:
            print('Error creating password hash:', e)
            sys.exit(1)
        conn.execute("UPDATE users SET hashed_password=? WHERE id=?", (hashed, row[0]))
        conn.commit()
        print("Password updated for:", username)
    finally:
        conn.close()


if __name__ == '__main__':
    change_password(*parse_args(sys.argv[1:]))
