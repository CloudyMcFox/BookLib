#!/usr/bin/env python3
"""
create_user.py

Add a user directly to the backend SQLite database. Run from the backend/ directory where books.db lives,
or run inside the backend container.

Usage:
  python create_user.py USERNAME PASSWORD
"""
import sys
import sqlite3
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
DB_PATH = "books.db"

def create_user(username, password):
    # Use full password (argon2 supports long passwords)
    try:
        hashed = pwd_context.hash(password)
    except Exception as e:
        print('Error creating password hash:', e)
        return

    conn = sqlite3.connect(DB_PATH)
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
    if len(sys.argv) < 3:
        print('Usage: python create_user.py USERNAME PASSWORD')
        sys.exit(2)
    create_user(sys.argv[1], sys.argv[2])
