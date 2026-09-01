#!/usr/bin/env python3
"""
create_user.py

Add a user directly to the backend SQLite database. Run from the backend/ directory where books.db lives,
or run inside the backend container.

Usage:
  python create_user.py USERNAME [PASSWORD]
"""
import sys
import sqlite3
import os
import getpass
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
DB_PATH = os.environ.get("BOOKLIB_DB", "books.db")

def create_user(username, password, db_path):
    try:
        hashed = pwd_context.hash(password)
    except Exception as e:
        print('Error creating password hash:', e)
        return

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        hashed_password TEXT NOT NULL
    )""")
    try:
        cur.execute("INSERT INTO users (username, hashed_password) VALUES (?,?)", (username, hashed))
        conn.commit()
        print("User created:", username)
    except sqlite3.IntegrityError:
        print("User already exists:", username)
    finally:
        conn.close()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python create_user.py USERNAME [PASSWORD]')
        sys.exit(2)
    supplied_password = sys.argv[2] if len(sys.argv) > 2 else None
    if supplied_password is None:
        supplied_password = getpass.getpass("Password: ")
        if supplied_password != getpass.getpass("Confirm password: "):
            print("Passwords do not match.")
            sys.exit(2)
    if not supplied_password:
        print("Password cannot be empty.")
        sys.exit(2)
    create_user(sys.argv[1], supplied_password, DB_PATH)
