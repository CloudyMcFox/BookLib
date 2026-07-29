#!/usr/bin/env python3
"""
list_users.py

List the accounts in the backend SQLite database. Run from the backend/ directory
where books.db lives, or point it at the file explicitly.

Usage:
  python list_users.py [--db PATH]

The database path can also come from the BOOKLIB_DB environment variable.
"""
import os
import sqlite3
import sys

DB_PATH = os.environ.get("BOOKLIB_DB", "books.db")


def parse_args(argv):
    db = DB_PATH
    args = list(argv)
    if '--db' in args:
        i = args.index('--db')
        if i + 1 >= len(args):
            print('Usage: python list_users.py [--db PATH]')
            sys.exit(2)
        db = args[i + 1]
    return db


def list_users(db_path):
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, username FROM users ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        print("No users table in", db_path)
        sys.exit(1)
    finally:
        conn.close()

    if not rows:
        print("No users found.")
        return
    print(f"{'ID':>4}  USERNAME")
    for uid, username in rows:
        print(f"{uid:>4}  {username}")
    print(f"\n{len(rows)} user{'' if len(rows) == 1 else 's'} in {db_path}")


if __name__ == '__main__':
    list_users(parse_args(sys.argv[1:]))
