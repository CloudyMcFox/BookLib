#!/usr/bin/env python3
"""Seed an empty BookLib database with non-personal App Review sample data."""

import argparse
import sys

from app import main


SAMPLE_BOOKS = [
    {
        "title": "Pride and Prejudice",
        "author": "Jane Austen",
        "isbn": "9780141439518",
        "tags": ["Classics", "Fiction", "Romance"],
        "location": (1, 1),
    },
    {
        "title": "Frankenstein",
        "author": "Mary Shelley",
        "isbn": "9780141439471",
        "tags": ["Classics", "Gothic", "Science Fiction"],
        "location": (2, 1),
    },
    {
        "title": "Twenty Thousand Leagues Under the Seas",
        "author": "Jules Verne",
        "isbn": "9780140390978",
        "tags": ["Adventure", "Classics", "Science Fiction"],
        "location": (3, 1),
    },
    {
        "title": "Dune",
        "author": "Frank Herbert",
        "isbn": "9780441172719",
        "series": "Dune",
        "series_index": 1,
        "tags": ["Fiction", "Science Fiction"],
        "location": (1, 2),
        "borrower_name": "Sample Borrower",
        "checked_out_at": "2026-09-01T12:00:00Z",
    },
    {
        "title": "The Left Hand of Darkness",
        "author": "Ursula K. Le Guin",
        "isbn": "9780441478125",
        "tags": ["Fiction", "Science Fiction"],
        "location": (2, 2),
    },
    {
        "title": "Kindred",
        "author": "Octavia E. Butler",
        "isbn": "9780807083697",
        "tags": ["Fiction", "Historical Fiction", "Science Fiction"],
        "location": (3, 2),
    },
    {
        "title": "A Wizard of Earthsea",
        "author": "Ursula K. Le Guin",
        "isbn": "9780547773742",
        "series": "Earthsea",
        "series_index": 1,
        "tags": ["Fantasy", "Fiction"],
        "location": (1, 3),
    },
    {
        "title": "The Tombs of Atuan",
        "author": "Ursula K. Le Guin",
        "isbn": "9780689845369",
        "series": "Earthsea",
        "series_index": 2,
        "tags": ["Fantasy", "Fiction"],
        "location": (2, 3),
    },
]


def seed(enrich: bool) -> None:
    if main.conn.execute("SELECT 1 FROM books LIMIT 1").fetchone():
        print("Refusing to seed a database that already contains books.")
        sys.exit(1)

    shelf = main.conn.execute(
        "SELECT id FROM shelves ORDER BY sort_order, id LIMIT 1"
    ).fetchone()
    if not shelf:
        raise RuntimeError("BookLib did not create its initial shelf")
    shelf_id = shelf["id"]
    main.conn.execute(
        "UPDATE shelves SET name=?, columns=?, rows=? WHERE id=?",
        ("App Review Library", 6, 8, shelf_id),
    )

    inserted = []
    for sample in SAMPLE_BOOKS:
        column, row = sample["location"]
        cursor = main.conn.execute(
            """INSERT INTO books
               (title, author, isbn, notes, series, series_index, created_at,
                shelf_id, shelf_column, shelf_row, borrower_name, checked_out_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sample["title"],
                sample["author"],
                sample["isbn"],
                "Sample data for App Review",
                sample.get("series"),
                sample.get("series_index"),
                main.now_iso(),
                shelf_id,
                column,
                row,
                sample.get("borrower_name"),
                sample.get("checked_out_at"),
            ),
        )
        book_id = cursor.lastrowid
        main.set_book_tags(book_id, sample["tags"])
        inserted.append((book_id, sample))
    main.conn.commit()

    if enrich:
        for index, (book_id, sample) in enumerate(inserted, start=1):
            print(f"Fetching cover and metadata {index}/{len(inserted)}: {sample['title']}")
            main._enrich_new_book(
                book_id,
                sample["title"],
                sample["author"],
                sample["isbn"],
                None,
                None,
                None,
                needs_series=not bool(sample.get("series")),
                needs_description=True,
                needs_tags=False,
            )

    print(f"Seeded {len(inserted)} sample books in {main.DB_PATH}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Fetch covers, descriptions, and missing series from public catalogues.",
    )
    seed(parser.parse_args().enrich)
