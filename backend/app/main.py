from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Request
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
import sqlite3
import requests
from typing import Optional, List
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
import csv
import io
import re
from fastapi.middleware.cors import CORSMiddleware

# DB and auth config
import os
DB_PATH = "books.db"
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-to-a-secure-random-string")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", 60 * 8))

# Guest access: anyone can browse and use circulation without an account.
# Guests are not rows in users, they are only a claim in the token, so there is
# no password to leak and nothing to keep in sync. Set
# GUEST_ACCESS_ENABLED=false to turn the door off entirely.
GUEST_ACCESS_ENABLED = os.environ.get("GUEST_ACCESS_ENABLED", "true").strip().lower() not in ("0", "false", "no")
GUEST_USERNAME = "guest"
ROLE_GUEST = "guest"
ROLE_ADMIN = "admin"
# Guest sessions are short: the token is handed out without any proof of
# identity, so it should not stay valid for a working day.
GUEST_TOKEN_EXPIRE_MINUTES = int(os.environ.get("GUEST_TOKEN_EXPIRE_MINUTES", 120))

# Google Books is used as a fallback when OpenLibrary has no genres, cover or
# metadata for a book. An API key is optional but strongly recommended: without
# one Google shares a per-IP anonymous quota that is regularly exhausted, and
# calls simply start returning HTTP 429.
GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
GOOGLE_BOOKS_ENABLED = os.environ.get("GOOGLE_BOOKS_ENABLED", "true").strip().lower() not in ("0", "false", "no")

# Shelf defaults, used only for the shelf seeded on first start. Shelves are
# data, so sizes are edited through the API rather than by redeploying.
DEFAULT_SHELF_COLUMNS = int(os.environ.get("DEFAULT_SHELF_COLUMNS", 6))
DEFAULT_SHELF_ROWS = int(os.environ.get("DEFAULT_SHELF_ROWS", 8))
# Sanity limits so a typo cannot ask the UI to draw a million slots.
MAX_SHELF_DIMENSION = 50

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

app = FastAPI(title="Book Library API")

# Allow cross-origin requests from the frontend. For production, restrict origins to your NAS host.
origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def clean(value: Optional[str]) -> Optional[str]:
    """Strip surrounding whitespace; empty strings become NULL so the unique
    ISBN index does not treat '' as a duplicate value."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def clean_olid(value: Optional[str]) -> Optional[str]:
    """Normalise an OpenLibrary edition id, accepting '/books/OL123M' or a bare
    'ol123m'. Returns None when no valid id is present."""
    v = clean(value)
    if not v:
        return None
    m = re.search(r'OL\d+M', v.upper())
    return m.group(0) if m else None


# The bindings worth having a single spelling for. Anything else a user types is
# kept as typed -- this is a tidying pass, not a whitelist.
KNOWN_FORMATS = [
    "Hardcover",
    "Leatherbound",
    "Paperback",
    "Mass market paperback",
    "Board book",
    "Spiral-bound",
    "Library binding",
    "Ebook",
    "Audiobook",
]

# Matched in order, so the specific wins: "mass market paperback" must be tested
# before "paperback", and "library binding" before the cloth-and-boards
# spellings of hardcover.
_FORMAT_PATTERNS = [
    ("Library binding", (r'library\s*bind',)),
    ("Mass market paperback", (r'mass\s*market', r'\bmmpb\b', r'\bmm\s*pb\b')),
    ("Board book", (r'board\s*book',)),
    ("Spiral-bound", (r'spiral', r'comb\s*bound', r'wire[-\s]*o\b')),
    ("Leatherbound", (r'leather',)),
    ("Audiobook", (r'audio', r'\bcd\b', r'\bmp3\b', r'cassette', r'spoken')),
    ("Ebook", (r'e-?book', r'electronic', r'\bepub\b', r'kindle', r'digital', r'\bpdf\b')),
    ("Paperback", (r'paperback', r'\bpbk', r'soft\s*cover', r'softback', r'\btrade\s*pb\b', r'\bpaper\b')),
    ("Hardcover", (r'hard\s*cover', r'hardback', r'\bhbk', r'\bhc\b', r'\bcloth\b', r'\bboards?\b',
                   r'\bbound\b')),
]


def clean_format(value: Optional[str]) -> Optional[str]:
    """Normalise a binding.

    OpenLibrary records the same binding half a dozen ways -- 'pbk.',
    'Paperback', 'paperback', 'Trade paperback' -- and a field that spells one
    thing several ways cannot be filtered or scanned down a column. Known
    shapes are bucketed into one spelling each; anything unrecognised is kept
    as it was written, since a user typing 'Slipcased' means it."""
    v = clean(value)
    if not v:
        return None
    lowered = v.lower()
    for canonical, patterns in _FORMAT_PATTERNS:
        if any(re.search(pattern, lowered) for pattern in patterns):
            return canonical
    # Unknown, so keep it -- trimmed, and capped so a stray paragraph cannot be
    # parked in the column.
    return v[:60]


def clean_google_id(value: Optional[str]) -> Optional[str]:
    """Normalise a Google Books volume id.

    Accepts a bare id ('otCEEQAAQBAJ') or any of the URLs it appears in:
      https://books.google.com/books?id=otCEEQAAQBAJ
      https://www.google.com/books/edition/Songs_of_the_Dead/otCEEQAAQBAJ
      https://www.googleapis.com/books/v1/volumes/otCEEQAAQBAJ
    Returns None when nothing id-shaped is present."""
    v = clean(value)
    if not v:
        return None
    m = re.search(r'[?&]id=([A-Za-z0-9_-]+)', v)
    if m:
        return m.group(1)
    if '/' in v:
        # Last non-empty path segment, e.g. .../edition/Title/<id>
        segments = [s for s in v.split('?')[0].split('/') if s]
        v = segments[-1] if segments else v
    return v if re.fullmatch(r'[A-Za-z0-9_-]{8,40}', v) else None


# Series names arrive with the volume number attached in half a dozen shapes:
# "The Wheel of Time ; 1" from OpenLibrary, "Discworld, #5" from a title, and
# "Book Three of the Stormlight Archive" from a subtitle. Split rather than
# store the lot, so the number can be sorted on.
MAX_SERIES_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 8000


def clean_series(value: Optional[str]) -> Optional[str]:
    """Tidy a series name: collapse whitespace, drop a trailing 'series' or a
    dangling separator, and cap the length so a stray paragraph cannot be parked
    in the column."""
    v = clean(value)
    if not v:
        return None
    v = re.sub(r'\s+', ' ', v).strip(' ,;:-')
    v = re.sub(r'\s+series$', '', v, flags=re.IGNORECASE).strip(' ,;:-')
    # "the Wheel of Time" and "The Wheel of Time" are one series, and which one
    # is stored depends only on where the sentence it was read out of started.
    v = re.sub(r'^the\s+', 'The ', v, flags=re.IGNORECASE)
    return v[:MAX_SERIES_LENGTH] or None


def clean_series_index(value) -> Optional[float]:
    """A series position as a number. Accepts '3', '3.5' or 'Book 3'; anything
    without a number in it, and anything negative, is no position at all."""
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        m = re.search(r'\d+(?:\.\d+)?', str(value))
        if not m:
            return None
        number = float(m.group(0))
    if number < 0 or number > 10000:
        return None
    # Whole numbers stay whole, so 3.0 is stored and shown as 3.
    return round(number, 2)


def split_series(value: Optional[str]) -> tuple:
    """Split a combined series string into (name, index).

    OpenLibrary packs alternate namings of the same series into one field
    separated by semicolons ("Dune (1); Dune Chronicles"), so each is tried in
    turn and the first that yields a number wins. A string with no number in it
    is a series name and nothing more."""
    v = clean(value)
    if not v:
        return (None, None)
    v = re.sub(r'\s+', ' ', v)
    segments = [v] + [s for s in v.split(';') if s.strip()]
    for segment in segments:
        name, index = _series_from_text(segment)
        if name and index is not None:
            return (name, index)
    # No number anywhere: keep the name, dropping a trailing "(1)" that the
    # patterns above declined only because the name beside it was too short.
    first = re.sub(r'\s*\(\s*\d+(?:\.\d+)?\s*\)\s*$', '', v.split(';')[0])
    return (clean_series(first), None)


def clean_description(value: Optional[str]) -> Optional[str]:
    """Descriptions come as HTML from Google Books and as Markdown-ish text from
    OpenLibrary. Store readable plain text: no tags, no runaway blank lines, and
    a length cap so one book cannot dominate every listing response."""
    v = clean(value)
    if not v:
        return None
    v = re.sub(r'<br\s*/?>|</p\s*>|</div\s*>|</li\s*>', '\n', v, flags=re.IGNORECASE)
    v = re.sub(r'<[^>]+>', '', v)
    v = v.replace('\r\n', '\n').replace('\r', '\n')
    v = (v.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&quot;', '"')
          .replace('&#39;', "'").replace('&lt;', '<').replace('&gt;', '>'))
    # OpenLibrary appends a source line to many work descriptions.
    v = re.split(r'\n\s*-{3,}\s*\n|\[?source\]?\s*:?\s*http', v, flags=re.IGNORECASE)[0]
    # OpenLibrary descriptions are Markdown, which renders as literal asterisks
    # in a plain-text cell. Only emphasis is unwrapped: links and lists read
    # fine as they are.
    v = re.sub(r'\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*', r'\1', v, flags=re.DOTALL)
    v = re.sub(r'\*\*(?=\S)(.+?)(?<=\S)\*\*', r'\1', v, flags=re.DOTALL)
    v = re.sub(r'(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])', r'\1', v)
    v = re.sub(r'[ \t]+', ' ', v)
    v = re.sub(r'\n{3,}', '\n\n', v).strip()
    if len(v) > MAX_DESCRIPTION_LENGTH:
        v = v[:MAX_DESCRIPTION_LENGTH].rstrip() + '…'
    return v or None


TIMESTAMP_FORMATS = ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d')

def clean_timestamp(value: Optional[str]) -> Optional[str]:
    """Normalise a user supplied 'date added' to the stored ISO-8601 UTC format.
    Accepts a plain date or a full timestamp; returns None when nothing given."""
    v = clean(value)
    if not v:
        return None
    v = v.rstrip('Zz').strip()
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(v, fmt).strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail="Date added must be a date like 2024-05-01")


class Book(BaseModel):
    id: Optional[int] = None
    title: str
    author: Optional[str] = None
    isbn: Optional[str] = None
    # OpenLibrary edition id (e.g. OL12345M). Populated when adding from search
    # results; optional for manual entries.
    olid: Optional[str] = None
    # Google Books volume id (e.g. otCEEQAAQBAJ). Lets a lookup go straight to
    # the volume record instead of searching for it first.
    google_id: Optional[str] = None
    # Binding: Hardcover, Paperback and so on. Free text, but written through
    # clean_format so the same binding is not spelled three ways.
    format: Optional[str] = None
    # Series the book belongs to, and its position in it. Stored apart so a
    # series can be sorted in reading order rather than alphabetically by a
    # string that happens to start with a number.
    series: Optional[str] = None
    series_index: Optional[float] = None
    # Publisher blurb / work summary. Long free text, fetched from Google Books
    # or OpenLibrary.
    description: Optional[str] = None
    notes: Optional[str] = None
    # Physical location. All three travel together: a shelf without a slot, or a
    # slot without a shelf, is not a position.
    shelf_id: Optional[int] = None
    shelf_column: Optional[int] = None
    shelf_row: Optional[int] = None
    # Omit on PUT to leave a book's tags untouched; send a list to replace them.
    tags: Optional[List[str]] = None
    # ISO-8601 UTC timestamp of when the book was added (NULL for rows created
    # before this column existed).
    created_at: Optional[str] = None
    # True when a cover image is stored in the database for this book.
    has_cover: bool = False
    # Circulation is deliberately lightweight for a personal library. Both
    # values are null while the book is available.
    borrower_name: Optional[str] = None
    checked_out_at: Optional[str] = None
    # Total physical copies carrying this ISBN, including copies outside the
    # current filtered listing.
    copy_count: int = 1
    # Write-only helper: when supplied on create/update the image at this URL is
    # downloaded and stored as the book cover.
    cover_url: Optional[str] = None


def book_from_row(row, tags: Optional[List[str]] = None) -> Book:
    d = dict(row)
    d['has_cover'] = bool(d.pop('cover_size', 0) or 0)
    d.pop('cover', None)
    d.pop('cover_mime', None)
    book = Book(**d)
    book.tags = tags if tags is not None else get_book_tags(book.id)
    return book

class Token(BaseModel):
    access_token: str
    token_type: str


class Shelf(BaseModel):
    id: Optional[int] = None
    name: str
    columns: int = DEFAULT_SHELF_COLUMNS
    rows: int = DEFAULT_SHELF_ROWS
    sort_order: int = 0
    created_at: Optional[str] = None
    # Read-only: how many books are placed on this shelf.
    book_count: int = 0


class ShelfSlot(BaseModel):
    """One occupied slot, for drawing a shelf with its contents."""
    column: int
    row: int
    book_id: int
    title: str
    author: Optional[str] = None
    has_cover: bool = False


class ShelfLayout(BaseModel):
    shelf: Shelf
    slots: List[ShelfSlot] = []

class UserCreate(BaseModel):
    username: str
    password: str


class CheckoutRequest(BaseModel):
    borrower_name: str

# initialize DB
conn = get_conn()
conn.execute("""CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT,
    isbn TEXT,
    notes TEXT
)
""")
conn.execute("""CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL
)
""")
conn.execute("""CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
)
""")
conn.execute("""CREATE TABLE IF NOT EXISTS book_tags (
    book_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (book_id, tag_id)
)
""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_book_tags_tag ON book_tags(tag_id)")
conn.execute("""CREATE TABLE IF NOT EXISTS shelves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    columns INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
)
""")
conn.commit()

# --- lightweight migration: add cover columns to pre-existing databases ---
_existing_cols = {r['name'] for r in conn.execute("PRAGMA table_info(books)")}
if 'cover' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN cover BLOB")
if 'cover_mime' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN cover_mime TEXT")
if 'created_at' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN created_at TEXT")
if 'olid' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN olid TEXT")
    # Older rows kept the edition id in notes as "OLID:OL12345M"; move it into
    # the dedicated column and drop the marker from the note text.
    for row in conn.execute("SELECT id, notes FROM books WHERE notes LIKE '%OL%M%'").fetchall():
        m = re.search(r'OL\d+M', row['notes'] or '')
        if not m:
            continue
        remaining = re.sub(r'OLID:\s*' + m.group(0), '', row['notes']).strip(' ,;')
        conn.execute("UPDATE books SET olid=?, notes=? WHERE id=?", (m.group(0), remaining or None, row['id']))
if 'google_id' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN google_id TEXT")
# Physical location: which shelf, and which slot on it. All three are set
# together or all are null — a book with a shelf but no slot is meaningless.
if 'shelf_id' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN shelf_id INTEGER")
if 'shelf_column' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN shelf_column INTEGER")
if 'shelf_row' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN shelf_row INTEGER")
# Binding: hardcover, paperback and so on. Free text, but written through
# clean_format so the handful of shapes OpenLibrary uses for the same thing end
# up as one value.
if 'format' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN format TEXT")
# Series name and position, kept apart so "book 2" sorts before "book 10".
if 'series' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN series TEXT")
if 'series_index' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN series_index REAL")
if 'description' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN description TEXT")
if 'borrower_name' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN borrower_name TEXT")
if 'checked_out_at' not in _existing_cols:
    conn.execute("ALTER TABLE books ADD COLUMN checked_out_at TEXT")

# Early versions made ISBN unique. A personal library may own two physical
# copies of the same edition, so rebuild that old table once without the
# constraint. SQLite's automatic unique index cannot be dropped directly.
_unique_isbn = False
for _index in conn.execute("PRAGMA index_list(books)").fetchall():
    if not _index['unique']:
        continue
    _columns = [r['name'] for r in conn.execute(f"PRAGMA index_info('{_index['name']}')").fetchall()]
    if _columns == ['isbn']:
        _unique_isbn = True
        break
if _unique_isbn:
    conn.commit()
    try:
        conn.execute("BEGIN")
        conn.execute("""CREATE TABLE books_without_unique_isbn (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT,
            isbn TEXT,
            notes TEXT,
            cover BLOB,
            cover_mime TEXT,
            created_at TEXT,
            olid TEXT,
            google_id TEXT,
            shelf_id INTEGER,
            shelf_column INTEGER,
            shelf_row INTEGER,
            format TEXT,
            series TEXT,
            series_index REAL,
            description TEXT,
            borrower_name TEXT,
            checked_out_at TEXT
        )""")
        _book_storage_columns = (
            "id, title, author, isbn, notes, cover, cover_mime, created_at, olid, google_id, "
            "shelf_id, shelf_column, shelf_row, format, series, series_index, description, "
            "borrower_name, checked_out_at"
        )
        conn.execute(
            f"INSERT INTO books_without_unique_isbn ({_book_storage_columns}) "
            f"SELECT {_book_storage_columns} FROM books")
        conn.execute("DROP TABLE books")
        conn.execute("ALTER TABLE books_without_unique_isbn RENAME TO books")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
conn.execute("CREATE INDEX IF NOT EXISTS idx_books_shelf ON books(shelf_id)")

# Seed one shelf so the feature works out of the box rather than presenting an
# empty picker on first use.
if not conn.execute("SELECT 1 FROM shelves LIMIT 1").fetchone():
    conn.execute("INSERT INTO shelves (name, columns, rows, sort_order, created_at) VALUES (?,?,?,?,?)",
                 ("Bookshelf", DEFAULT_SHELF_COLUMNS, DEFAULT_SHELF_ROWS, 0,
                  datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')))
conn.commit()

# Never SELECT * from books: the cover BLOB would be loaded for every row.
BOOK_COLUMNS = ("id, title, author, isbn, olid, google_id, notes, format, series, series_index, "
                "description, created_at, shelf_id, shelf_column, shelf_row, borrower_name, checked_out_at, "
                """CASE WHEN isbn IS NULL OR TRIM(isbn)='' THEN 1 ELSE
                   (SELECT COUNT(*) FROM books AS copies
                    WHERE REPLACE(REPLACE(copies.isbn, '-', ''), ' ', '') =
                          REPLACE(REPLACE(books.isbn, '-', ''), ' ', ''))
                   END AS copy_count, """
                "length(cover) AS cover_size")

# Whitelisted ORDER BY clauses, so the sort parameter can never be injected.
# Books added before created_at existed sort by id, which preserves insert order.
SORT_CLAUSES = {
    'title': "title COLLATE NOCASE {dir}, id {dir}",
    'author': "CASE WHEN author IS NULL OR author='' THEN 1 ELSE 0 END, author COLLATE NOCASE {dir}, id {dir}",
    'added': "COALESCE(created_at,'') {dir}, id {dir}",
    # Unplaced books sort last either way, so the list does not open on a block
    # of blanks.
    'location': "CASE WHEN shelf_id IS NULL THEN 1 ELSE 0 END, shelf_id {dir}, shelf_row {dir}, shelf_column {dir}, id {dir}",
    # Within a series, reading order beats alphabetical: the number sorts as a
    # number, and books with no series go last either way.
    'series': ("CASE WHEN series IS NULL OR series='' THEN 1 ELSE 0 END, series COLLATE NOCASE {dir}, "
               "CASE WHEN series_index IS NULL THEN 1 ELSE 0 END, series_index {dir}, "
               "title COLLATE NOCASE {dir}, id {dir}"),
}
DEFAULT_SORT = 'added'


def order_by(sort: Optional[str], direction: Optional[str]) -> str:
    clause = SORT_CLAUSES.get((sort or DEFAULT_SORT).lower(), SORT_CLAUSES[DEFAULT_SORT])
    dir_sql = 'ASC' if (direction or '').lower() == 'asc' else 'DESC'
    return " ORDER BY " + clause.format(dir=dir_sql)


def now_iso() -> str:
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


# --- shelf helpers ---
def shelf_row_to_model(row, book_count: int = 0) -> Shelf:
    return Shelf(id=row['id'], name=row['name'], columns=row['columns'], rows=row['rows'],
                 sort_order=row['sort_order'], created_at=row['created_at'], book_count=book_count)


def get_shelf(shelf_id: int):
    return conn.execute("SELECT * FROM shelves WHERE id=?", (shelf_id,)).fetchone()


def validate_shelf_size(columns: int, rows: int):
    if columns < 1 or rows < 1:
        raise HTTPException(status_code=400, detail="A shelf needs at least one column and one row")
    if columns > MAX_SHELF_DIMENSION or rows > MAX_SHELF_DIMENSION:
        raise HTTPException(status_code=400,
                            detail=f"A shelf can be at most {MAX_SHELF_DIMENSION} columns by {MAX_SHELF_DIMENSION} rows")


def resolve_location(shelf_id: Optional[int], column: Optional[int], row: Optional[int]):
    """Validate a book's location and return it as a (shelf_id, column, row)
    triple, or (None, None, None) when the book is unplaced.

    The three values only mean anything together, so a partial location is
    rejected rather than half-stored."""
    supplied = [v for v in (shelf_id, column, row) if v is not None]
    if not supplied:
        return None, None, None
    if len(supplied) != 3:
        raise HTTPException(status_code=400,
                            detail="A location needs a shelf, a column and a row — or none of the three")

    shelf = get_shelf(shelf_id)
    if not shelf:
        raise HTTPException(status_code=400, detail="No such shelf")
    if not (1 <= column <= shelf['columns']):
        raise HTTPException(status_code=400,
                            detail=f"Column must be between 1 and {shelf['columns']} on “{shelf['name']}”")
    if not (1 <= row <= shelf['rows']):
        raise HTTPException(status_code=400,
                            detail=f"Row must be between 1 and {shelf['rows']} on “{shelf['name']}”")
    return shelf_id, column, row


# --- tag helpers ---
MAX_TAGS_PER_BOOK = 25
MAX_TAG_LENGTH = 45
# How many genres a single OpenLibrary lookup may contribute.
MAX_LOOKUP_TAGS = 8
# OpenLibrary subjects include a lot of library/scan housekeeping, award and
# bestseller-list bookkeeping that is useless as a browsing tag.
SUBJECT_JUNK = re.compile(
    r'^(nyt|award|lc|ddc|bic|bisac|series?)\s*:|\d{4}|accessible book|protected daisy'
    r'|in library|overdrive|large type|new york times|bestseller|reviewed'
    r'|internet archive|open library|lending|wishlist|translations'
    r'|reading level|specimens|manual for civilization|electronic books', re.I)
# BISAC strings end in a filler segment we do not want as a tag.
DROP_SEGMENTS = {'general', 'other', 'miscellaneous'}
# Curated vocabulary used when a book has no BISAC-style subjects: the matched
# subject is replaced by the canonical label so tags stay consistent.
GENRE_VOCAB = [
    ('science fiction', 'Science Fiction'), ('science-fiction', 'Science Fiction'),
    ('fantasy', 'Fantasy'), ('horror', 'Horror'), ('mystery', 'Mystery'),
    ('detective and mystery', 'Mystery'), ('mystery & detective', 'Mystery'),
    ('thriller', 'Thriller'), ('suspense', 'Suspense'), ('romance', 'Romance'),
    ('historical fiction', 'Historical Fiction'), ('adventure', 'Adventure'),
    ('action & adventure', 'Adventure'), ('action and adventure', 'Adventure'),
    ('epic', 'Epic'), ('dragons & mythical creatures', 'Fantasy'), ('romantasy', 'Romantasy'),
    ('western', 'Westerns'), ('dystopian', 'Dystopian'), ('classics', 'Classics'),
    ('short stories', 'Short Stories'), ('poetry', 'Poetry'), ('drama', 'Drama'),
    ('graphic novel', 'Graphic Novels'), ('comics', 'Comics'),
    ('juvenile fiction', 'Juvenile Fiction'), ('juvenile fantasy', 'Fantasy'),
    ('young adult fiction', 'Young Adult'), ('young adult', 'Young Adult'),
    ("children's", "Children's"), ('childrens', "Children's"),
    ('biography', 'Biography'), ('autobiography', 'Biography'), ('memoir', 'Memoir'),
    ('history', 'History'), ('philosophy', 'Philosophy'), ('psychology', 'Psychology'),
    ('science', 'Science'), ('mathematics', 'Mathematics'), ('technology', 'Technology'),
    ('computers', 'Computers'), ('business', 'Business'), ('economics', 'Economics'),
    ('political science', 'Politics'), ('politics', 'Politics'), ('religion', 'Religion'),
    ('self-help', 'Self-Help'), ('cooking', 'Cooking'), ('travel', 'Travel'),
    ('true crime', 'True Crime'), ('art', 'Art'), ('music', 'Music'), ('nature', 'Nature'),
    ('sports', 'Sports'), ('humor', 'Humor'), ('essays', 'Essays'), ('war', 'War'),
    ('fiction', 'Fiction'), ('non-fiction', 'Nonfiction'), ('nonfiction', 'Nonfiction'),
]


def _title_segment(text: str) -> str:
    """'FICTION' -> 'Fiction', but leave 'Mystery & Detective' alone."""
    words = []
    for word in text.split():
        words.append(word.capitalize() if len(word) > 3 and word.isupper() else word)
    return " ".join(words)


def extract_genres(subjects) -> List[str]:
    """Turn raw OpenLibrary subjects into genre tags.

    OpenLibrary usually carries BISAC-style strings such as
    'FICTION / Science Fiction / Hard Science Fiction'; each segment of those is
    a clean genre. Books without them fall back to a curated vocabulary so we do
    not end up tagging things with 'thrushes' or 'award:hugo_award=1966'."""
    out: List[str] = []
    seen = set()

    def push(name: str):
        key = name.lower()
        if not name or len(name) > MAX_TAG_LENGTH or key in seen:
            return
        seen.add(key)
        out.append(name)

    cleaned = [str(s).strip() for s in (subjects or []) if s and not SUBJECT_JUNK.search(str(s))]

    for entry in [s for s in cleaned if ' / ' in s]:
        for segment in (seg.strip() for seg in entry.split('/')):
            if segment and segment.lower() not in DROP_SEGMENTS:
                push(_title_segment(segment))

    if len(out) < 2:
        # Subjects are often comma-style lists ("Fiction, fantasy, epic"), so
        # match every segment rather than the string as a whole — otherwise the
        # leading "Fiction" would shadow the genre that follows it.
        for subject in cleaned:
            for raw_segment in re.split(r'[,;/]', subject):
                segment = raw_segment.strip().lower()
                if not segment or segment in DROP_SEGMENTS:
                    continue
                for needle, label in GENRE_VOCAB:
                    if segment == needle or segment == needle + ' fiction' or segment.startswith(needle + ' '):
                        push(label)
                        break

    return _derive_extra_genres(out[:MAX_LOOKUP_TAGS])


def _derive_extra_genres(genres: List[str]) -> List[str]:
    """Add genres implied by a combination rather than stated outright.
    Applied after merging sources, so a Fantasy from one and a Romance from the
    other still produce Romantasy."""
    result = list(genres)
    lowered = {t.lower() for t in result}
    # OpenLibrary barely uses the 'romantasy' subject, so derive it: a book
    # tagged both Fantasy and Romance is the genre by definition.
    if 'fantasy' in lowered and 'romance' in lowered and 'romantasy' not in lowered:
        result.append('Romantasy')
    return result


def normalize_tag(name: Optional[str]) -> Optional[str]:
    """Collapse whitespace on a user supplied tag. Returns None when unusable."""
    v = clean(name)
    if not v:
        return None
    v = re.sub(r'\s+', ' ', v).strip(' ,;')
    if not v or len(v) > MAX_TAG_LENGTH:
        return None
    return v


def normalize_tags(names) -> List[str]:
    """Normalise a list of tags, dropping duplicates case-insensitively."""
    out: List[str] = []
    seen = set()
    for raw in names or []:
        tag = normalize_tag(raw)
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
        if len(out) >= MAX_TAGS_PER_BOOK:
            break
    return out


def get_book_tags(book_id: Optional[int]) -> List[str]:
    if not book_id:
        return []
    cur = conn.execute("""SELECT t.name FROM tags t
                          JOIN book_tags bt ON bt.tag_id = t.id
                          WHERE bt.book_id = ? ORDER BY t.name COLLATE NOCASE""", (book_id,))
    return [r['name'] for r in cur.fetchall()]


def tags_for_books(book_ids: List[int]) -> dict:
    """Bulk load tags for a page of books, so listing stays a single extra query."""
    if not book_ids:
        return {}
    placeholders = ",".join("?" * len(book_ids))
    cur = conn.execute(f"""SELECT bt.book_id, t.name FROM book_tags bt
                           JOIN tags t ON t.id = bt.tag_id
                           WHERE bt.book_id IN ({placeholders})
                           ORDER BY t.name COLLATE NOCASE""", book_ids)
    grouped: dict = {bid: [] for bid in book_ids}
    for row in cur.fetchall():
        grouped.setdefault(row['book_id'], []).append(row['name'])
    return grouped


def _tag_id(name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    return row['id']


def prune_orphan_tags():
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")


def set_book_tags(book_id: int, names) -> List[str]:
    """Replace a book's tags with the given list."""
    tags = normalize_tags(names)
    conn.execute("DELETE FROM book_tags WHERE book_id = ?", (book_id,))
    for name in tags:
        conn.execute("INSERT OR IGNORE INTO book_tags (book_id, tag_id) VALUES (?,?)", (book_id, _tag_id(name)))
    prune_orphan_tags()
    conn.commit()
    return get_book_tags(book_id)


def add_book_tags(book_id: int, names) -> List[str]:
    """Add tags to a book, keeping any it already has."""
    existing = {t.lower() for t in get_book_tags(book_id)}
    room = MAX_TAGS_PER_BOOK - len(existing)
    if room <= 0:
        return get_book_tags(book_id)
    for name in normalize_tags(names):
        if name.lower() in existing:
            continue
        conn.execute("INSERT OR IGNORE INTO book_tags (book_id, tag_id) VALUES (?,?)", (book_id, _tag_id(name)))
        existing.add(name.lower())
        room -= 1
        if room <= 0:
            break
    conn.commit()
    return get_book_tags(book_id)


# --- auth helpers ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def get_user(username: str):
    cur = conn.execute("SELECT * FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    return dict(row) if row else None

def authenticate_user(username: str, password: str):
    user = get_user(username)
    if not user:
        return False
    if not verify_password(password, user['hashed_password']):
        return False
    return user

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    return _user_from_token(token)


def require_editor(current_user: dict = Depends(get_current_user)):
    """Dependency for catalogue writes; circulation has separate endpoints."""
    if is_guest(current_user):
        raise HTTPException(status_code=403, detail="Guest accounts cannot edit the catalogue")
    return current_user


def is_guest(user: Optional[dict]) -> bool:
    return bool(user) and user.get('role') == ROLE_GUEST


def _user_from_token(token: str):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    # A guest is identified by the role claim rather than by the name, so a real
    # account that happens to be called "guest" keeps its normal rights.
    if payload.get("role") == ROLE_GUEST:
        if not GUEST_ACCESS_ENABLED:
            raise credentials_exception
        return {"id": None, "username": username, "role": ROLE_GUEST}
    user = get_user(username)
    if user is None:
        raise credentials_exception
    return {**user, "role": ROLE_ADMIN}


async def get_current_user_flexible(request: Request, token: Optional[str] = None):
    """Same as get_current_user but also accepts ?token=... so plain <img>
    tags (which cannot send an Authorization header) can load covers."""
    header = request.headers.get('Authorization') or ''
    raw = header[7:].strip() if header.lower().startswith('bearer ') else (token or '')
    if not raw:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    return _user_from_token(raw)


# --- cover helpers ---
COVERS_BASE = "https://covers.openlibrary.org/b"
MAX_COVER_BYTES = 5 * 1024 * 1024
# OpenLibrary returns a 1x1 blank gif when it has no cover, so ignore tiny bodies.
MIN_COVER_BYTES = 1000


def _download_image(url: str) -> Optional[tuple]:
    """Download an image and return (bytes, mime), or None when unusable."""
    try:
        r = requests.get(url, timeout=10)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    content = r.content or b''
    mime = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    if not mime.startswith('image/'):
        return None
    if len(content) < MIN_COVER_BYTES or len(content) > MAX_COVER_BYTES:
        return None
    return content, mime


# --- Google Books fallback ---
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
# Largest first: only smallThumbnail/thumbnail are always present, the rest turn
# up on better catalogued volumes.
GOOGLE_IMAGE_SIZES = ('extraLarge', 'large', 'medium', 'small', 'thumbnail', 'smallThumbnail')
_GOOGLE_CACHE: dict = {}
_GOOGLE_CACHE_TTL = 600
_GOOGLE_CACHE_MAX = 256


def _google_volume_item(isbn: Optional[str]) -> Optional[dict]:
    """Look a book up on Google Books by ISBN and return the whole search item,
    which carries the volume id as well as its volumeInfo.

    Adding a book can ask for genres, a cover and metadata in one request, so
    results are cached briefly to keep that to a single call. A None result is
    cached too, otherwise a book Google does not know about would be retried on
    every lookup."""
    if not GOOGLE_BOOKS_ENABLED:
        return None
    cleaned = re.sub(r'[^0-9Xx]', '', isbn or '')
    if not cleaned:
        return None

    cached = _GOOGLE_CACHE.get(cleaned)
    if cached and (datetime.utcnow() - cached[0]).total_seconds() < _GOOGLE_CACHE_TTL:
        return cached[1]

    params = {'q': f'isbn:{cleaned}', 'maxResults': 1, 'country': 'US'}
    if GOOGLE_BOOKS_API_KEY:
        params['key'] = GOOGLE_BOOKS_API_KEY

    item: Optional[dict] = None
    try:
        r = requests.get(GOOGLE_BOOKS_URL, params=params, timeout=8)
        if r.status_code == 200:
            items = r.json().get('items') or []
            if items:
                item = items[0]
    except (requests.RequestException, ValueError):
        item = None

    if len(_GOOGLE_CACHE) >= _GOOGLE_CACHE_MAX:
        _GOOGLE_CACHE.clear()
    _GOOGLE_CACHE[cleaned] = (datetime.utcnow(), item)
    return item


def _google_volume(isbn: Optional[str]) -> Optional[dict]:
    """The volumeInfo for an ISBN, or None."""
    return (_google_volume_item(isbn) or {}).get('volumeInfo')


def _google_volume_detail(volume_id: Optional[str]) -> Optional[dict]:
    """Fetch a single volume by id and return its volumeInfo.

    This matters for genres: the search endpoint returns an abbreviated record
    whose `categories` are collapsed to one top-level subject ("Fiction"),
    while the per-volume record carries the full BISAC list ("Fiction / Fantasy
    / Urban", ...). Same book, same API, different amount of detail."""
    if not GOOGLE_BOOKS_ENABLED or not volume_id:
        return None

    cache_key = f"volume:{volume_id}"
    cached = _GOOGLE_CACHE.get(cache_key)
    if cached and (datetime.utcnow() - cached[0]).total_seconds() < _GOOGLE_CACHE_TTL:
        return cached[1]

    params = {'country': 'US'}
    if GOOGLE_BOOKS_API_KEY:
        params['key'] = GOOGLE_BOOKS_API_KEY

    info: Optional[dict] = None
    try:
        r = requests.get(f"{GOOGLE_BOOKS_URL}/{volume_id}", params=params, timeout=8)
        if r.status_code == 200:
            info = r.json().get('volumeInfo') or None
    except (requests.RequestException, ValueError):
        info = None

    if len(_GOOGLE_CACHE) >= _GOOGLE_CACHE_MAX:
        _GOOGLE_CACHE.clear()
    _GOOGLE_CACHE[cache_key] = (datetime.utcnow(), info)
    return info


def _google_categories_for(item: Optional[dict]) -> List[str]:
    """Categories for a search item, preferring the fuller per-volume record."""
    if not item:
        return []
    detail = _google_volume_detail(item.get('id'))
    detailed = (detail or {}).get('categories') or []
    if detailed:
        return detailed
    return (item.get('volumeInfo') or {}).get('categories') or []


def _google_item_for(isbn: Optional[str], google_id: Optional[str] = None) -> Optional[dict]:
    """The Google search-item shape for a book, preferring a stored volume id.

    A known id turns two requests into one and removes the guesswork: the ISBN
    search can miss, or match a different edition. Falls back to the ISBN search
    when there is no id, or when the id no longer resolves."""
    volume_id = clean_google_id(google_id)
    if volume_id:
        detail = _google_volume_detail(volume_id)
        if detail:
            return {'id': volume_id, 'volumeInfo': detail}
    return _google_volume_item(isbn)


def _clean_google_image_url(raw: str) -> str:
    """Google hands back http:// links, and its thumbnails carry a page-curl
    effect we do not want burned into a stored cover. `edge=curl` may appear as
    either the first or a later query parameter, so strip it as a parameter
    rather than by string match."""
    url = raw.strip().replace('http://', 'https://')
    url = re.sub(r'[?&]edge=curl\b', lambda m: '?' if m.group(0)[0] == '?' else '', url)
    return url.rstrip('?&')


def _google_cover_urls(isbn: Optional[str], google_id: Optional[str] = None) -> List[str]:
    """Cover URLs from Google Books, largest first."""
    item = _google_item_for(isbn, google_id)
    links = ((item or {}).get('volumeInfo') or {}).get('imageLinks') or {}
    urls: List[str] = []
    for size in GOOGLE_IMAGE_SIZES:
        raw = links.get(size)
        if raw:
            urls.append(_clean_google_image_url(raw))
    return urls


def _google_genres(isbn: Optional[str], title: Optional[str] = None,
                   author: Optional[str] = None, google_id: Optional[str] = None) -> List[str]:
    """Genres from Google Books categories, which are BISAC-style strings such
    as 'Fiction / Science Fiction / Action & Adventure' — exactly the shape
    extract_genres already understands.

    Two things make a plain ISBN search insufficient. Google does not index
    every ISBN, so `isbn:` can miss a book its title search finds happily; and
    the search endpoint collapses `categories` to a single top-level subject, so
    the per-volume record has to be fetched to see the real BISAC list. We start
    from the ISBN when it resolves, fall back to the title and author we already
    hold when it does not, and in both cases top up from sibling volumes."""
    item = _google_item_for(isbn, google_id)
    info = (item or {}).get('volumeInfo') or {}

    genres: List[str] = []
    lookup_title = clean(title)
    lookup_author = clean(author)

    if item:
        genres = extract_genres(_google_categories_for(item))
        if len(genres) >= 3:
            return genres
        lookup_title = (info.get('title') or '').strip() or lookup_title
        found_authors = info.get('authors') or []
        if found_authors:
            lookup_author = found_authors[0]

    if not lookup_title:
        return genres

    # Multiple authors arrive as one string from the books table; Google matches
    # a single name far better than a comma-joined list.
    if lookup_author and ',' in lookup_author:
        lookup_author = lookup_author.split(',')[0].strip()

    seen = {g.lower() for g in genres}
    for extra in _google_sibling_categories(lookup_title, lookup_author):
        if extra.lower() not in seen:
            seen.add(extra.lower())
            genres.append(extra)
    return genres[:MAX_LOOKUP_TAGS]


def _normalized(value: Optional[str]) -> str:
    return re.sub(r'[^a-z0-9]', '', (value or '').lower())


def _google_sibling_volumes(title: str, author: Optional[str]) -> List[dict]:
    """Google volumes that are the same book as `title`/`author`.

    Uses the same quoted query variants as the main search: an unquoted
    `intitle:` binds only to the first word, which made this match nothing at
    all for any multi-word title."""
    if not GOOGLE_BOOKS_ENABLED:
        return []

    wanted = _normalized(title)
    wanted_author = _normalized(author)
    matched: List[dict] = []

    for query in _google_query_variants(title, author, None):
        params = {'q': query, 'maxResults': 20, 'country': 'US'}
        if GOOGLE_BOOKS_API_KEY:
            params['key'] = GOOGLE_BOOKS_API_KEY
        try:
            r = requests.get(GOOGLE_BOOKS_URL, params=params, timeout=8)
            if r.status_code != 200:
                continue
            items = r.json().get('items') or []
        except (requests.RequestException, ValueError):
            continue

        for item in items:
            info = item.get('volumeInfo') or {}
            found_title = _normalized(info.get('title'))
            # Editions differ by subtitle ("...: A Novel"), so match on either
            # title being a prefix of the other rather than on equality.
            if not found_title or not (found_title.startswith(wanted) or wanted.startswith(found_title)):
                continue
            # A shared title is not a shared book; confirm the author too when
            # we know it, so an unrelated namesake cannot donate its genres.
            if wanted_author:
                authors = [_normalized(a) for a in (info.get('authors') or [])]
                if authors and not any(wanted_author in a or a in wanted_author for a in authors):
                    continue
            matched.append(item)
        if matched:
            break
    return matched


def _resolve_google_id(isbn: Optional[str], title: Optional[str],
                       author: Optional[str]) -> Optional[str]:
    """The Google Books volume id for a book, by ISBN and then by title/author.

    Google's `isbn:` index misses often enough -- a regional edition, a reissue
    under a different number -- that the ISBN search alone leaves plenty of
    books without an id even though Google plainly has them. Both the ISBN
    lookup and the per-book lookup use this so a scan resolves the same id the
    Look up button would."""
    volume_id = (_google_volume_item(isbn) or {}).get('id')
    if volume_id:
        return volume_id

    wanted_title = clean(title)
    if not wanted_title:
        return None
    # Search on the first author only: "Gaiman, Pratchett" matches nothing.
    wanted_author = clean(author) or ''
    if ',' in wanted_author:
        wanted_author = wanted_author.split(',')[0].strip()
    siblings = _google_sibling_volumes(wanted_title, wanted_author or None)
    return siblings[0].get('id') if siblings else None


# Per-volume detail costs a request each, so only probe the first few siblings.
MAX_SIBLING_DETAIL_FETCHES = 5


def _google_sibling_categories(title: str, author: Optional[str]) -> List[str]:
    """Categories from other Google volumes of the same book, used to flesh out
    a sparsely catalogued edition."""
    cache_key = f"siblings:{title.lower()}|{(author or '').lower()}"
    cached = _GOOGLE_CACHE.get(cache_key)
    if cached and (datetime.utcnow() - cached[0]).total_seconds() < _GOOGLE_CACHE_TTL:
        return cached[1] or []

    categories: List[str] = []
    for position, item in enumerate(_google_sibling_volumes(title, author)):
        if position < MAX_SIBLING_DETAIL_FETCHES:
            categories.extend(_google_categories_for(item))
        else:
            categories.extend((item.get('volumeInfo') or {}).get('categories') or [])

    found = extract_genres(categories)
    if len(_GOOGLE_CACHE) >= _GOOGLE_CACHE_MAX:
        _GOOGLE_CACHE.clear()
    _GOOGLE_CACHE[cache_key] = (datetime.utcnow(), found)
    return found


def _lookup_olid_by_isbn(isbn: Optional[str]) -> Optional[str]:
    """Ask OpenLibrary which edition an ISBN belongs to."""
    cleaned = re.sub(r'[^0-9Xx]', '', isbn or '')
    if not cleaned:
        return None
    try:
        r = requests.get(f"{OL_BASE}/isbn/{cleaned}.json", timeout=8)
        if r.status_code == 200:
            olid = clean_olid(r.json().get('key'))
            if olid:
                return olid
    except (requests.RequestException, ValueError):
        pass
    # Fallback: the books API exposes the edition key under jscmd=details.
    try:
        r = requests.get(f"{OL_BASE}/api/books?bibkeys=ISBN:{cleaned}&format=json&jscmd=details", timeout=8)
        if r.status_code == 200:
            item = r.json().get(f"ISBN:{cleaned}") or {}
            return clean_olid((item.get('details') or {}).get('key'))
    except (requests.RequestException, ValueError):
        pass
    return None


def _openlibrary_format(olid: Optional[str], isbn: Optional[str]) -> Optional[str]:
    """The binding OpenLibrary records for an edition.

    Google Books is no help here: its only related field is printType, which is
    BOOK or MAGAZINE and says nothing about how the thing is bound. So this is
    OpenLibrary or nothing, by edition id, falling back to resolving the ISBN to
    one.

    Note that the binding belongs to the *edition*, not the work: a book with no
    OLID and no ISBN cannot be answered for, since there is no way to say which
    printing is on the shelf."""
    edition = clean_olid(olid) or _lookup_olid_by_isbn(isbn)
    if edition:
        try:
            r = requests.get(f"{OL_BASE}/books/{edition}.json", timeout=8)
            if r.status_code == 200:
                found = clean_format(r.json().get('physical_format'))
                if found:
                    return found
        except (requests.RequestException, ValueError):
            pass

    cleaned = re.sub(r'[^0-9Xx]', '', isbn or '')
    if not cleaned:
        return None
    try:
        r = requests.get(f"{OL_BASE}/api/books?bibkeys=ISBN:{cleaned}&format=json&jscmd=details", timeout=8)
        if r.status_code == 200:
            details = (r.json().get(f"ISBN:{cleaned}") or {}).get('details') or {}
            return clean_format(details.get('physical_format'))
    except (requests.RequestException, ValueError):
        pass
    return None


def _fetch_genres(olid: Optional[str], isbn: Optional[str],
                  title: Optional[str] = None, author: Optional[str] = None,
                  google_id: Optional[str] = None) -> List[str]:
    """Fetch genre tags for an edition, combining both sources.

    Neither catalogue is complete on its own: OpenLibrary carries rich subjects
    for older titles but often nothing for recent ones, while Google's
    categories can be as thin as a single "Fiction". Merging keeps whatever each
    one knows, with OpenLibrary first because its genres tend to be more
    specific. Title and author let Google still be searched for books whose ISBN
    it has not indexed; a stored volume id skips that search entirely."""
    found = _openlibrary_genres(olid, isbn)
    google = _google_genres(isbn, title, author, google_id)
    if not google:
        return found
    combined = list(found)
    seen = {g.lower() for g in combined}
    for genre in google:
        if genre.lower() not in seen:
            seen.add(genre.lower())
            combined.append(genre)
    return _derive_extra_genres(combined)[:MAX_LOOKUP_TAGS]


def _openlibrary_genres(olid: Optional[str], isbn: Optional[str]) -> List[str]:
    """Genre tags from OpenLibrary subjects: the edition first, then the
    subjects on its parent work."""
    cleaned_isbn = re.sub(r'[^0-9Xx]', '', isbn or '')
    bibkeys = ([f"OLID:{olid}"] if olid else []) + ([f"ISBN:{cleaned_isbn}"] if cleaned_isbn else [])
    for key in bibkeys:
        try:
            r = requests.get(f"{OL_BASE}/api/books?bibkeys={key}&format=json&jscmd=data", timeout=8)
            if r.status_code != 200:
                continue
            item = r.json().get(key) or {}
        except (requests.RequestException, ValueError):
            continue
        subjects = [s.get('name') if isinstance(s, dict) else s for s in (item.get('subjects') or [])]
        found = extract_genres(subjects)
        if found:
            return found

    # The edition record points at a work, which is where subjects usually live.
    edition_url = f"{OL_BASE}/books/{olid}.json" if olid else (f"{OL_BASE}/isbn/{cleaned_isbn}.json" if cleaned_isbn else None)
    if not edition_url:
        return []
    try:
        r = requests.get(edition_url, timeout=8)
        if r.status_code != 200:
            return []
        edition = r.json()
    except (requests.RequestException, ValueError):
        return []
    found = extract_genres(edition.get('subjects') or [])
    if found:
        return found
    for work in (edition.get('works') or []):
        work_key = work.get('key') if isinstance(work, dict) else None
        if not work_key:
            continue
        try:
            r = requests.get(f"{OL_BASE}{work_key}.json", timeout=8)
            if r.status_code != 200:
                continue
            found = extract_genres(r.json().get('subjects') or [])
        except (requests.RequestException, ValueError):
            continue
        if found:
            return found
    return []


def _openlibrary_edition(olid: Optional[str], isbn: Optional[str]) -> Optional[dict]:
    """The raw OpenLibrary edition record, by edition id or ISBN."""
    edition = clean_olid(olid)
    cleaned = re.sub(r'[^0-9Xx]', '', isbn or '')
    urls = ([f"{OL_BASE}/books/{edition}.json"] if edition else []) + \
           ([f"{OL_BASE}/isbn/{cleaned}.json"] if cleaned else [])
    for url in urls:
        try:
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    return data
        except (requests.RequestException, ValueError):
            continue
    return None


def _openlibrary_works(edition: Optional[dict]) -> List[dict]:
    """The work records an edition belongs to. Works are where OpenLibrary keeps
    the description; the edition record rarely has one."""
    works: List[dict] = []
    for ref in ((edition or {}).get('works') or []):
        key = ref.get('key') if isinstance(ref, dict) else None
        if not key:
            continue
        try:
            r = requests.get(f"{OL_BASE}{key}.json", timeout=8)
            if r.status_code == 200 and isinstance(r.json(), dict):
                works.append(r.json())
        except (requests.RequestException, ValueError):
            continue
    return works


def _ol_text(value) -> Optional[str]:
    """OpenLibrary text fields are either a plain string or {'type': ...,
    'value': ...} depending on how old the record is."""
    if isinstance(value, dict):
        return clean(value.get('value'))
    if isinstance(value, str):
        return clean(value)
    return None


# A series is stated in prose far more often than in a structured field. The
# shapes that actually appear on OpenLibrary and Google Books titles and
# subtitles, most specific first:
#   "Book Three of the Stormlight Archive"   "Volume 2 of the Sandman"
#   "The Stormlight Archive, Book 3"         "Discworld, #5"
#   "(The Wheel of Time, #1)"                "The Wheel of Time ; 1"
_ORDINAL_WORDS = {
    'one': 1, 'first': 1, 'two': 2, 'second': 2, 'three': 3, 'third': 3,
    'four': 4, 'fourth': 4, 'five': 5, 'fifth': 5, 'six': 6, 'sixth': 6,
    'seven': 7, 'seventh': 7, 'eight': 8, 'eighth': 8, 'nine': 9, 'ninth': 9,
    'ten': 10, 'tenth': 10, 'eleven': 11, 'eleventh': 11, 'twelve': 12, 'twelfth': 12,
}
_NUMBER_WORD = r'\d+(?:\.\d+)?|' + '|'.join(_ORDINAL_WORDS)
_VOLUME_WORD = r'book|bk\.?|volume|vol\.?|part|pt\.?|no\.?|nr\.?|#'

# "Book Three of the Stormlight Archive" — the number leads, the name follows.
_SERIES_NUMBER_FIRST = re.compile(
    rf'^\s*(?:{_VOLUME_WORD})\s*(?P<num>{_NUMBER_WORD})\b\s+(?:of|in)\s+(?P<name>.+?)\s*$',
    re.IGNORECASE)
# "The Stormlight Archive, Book 3" — the name leads.
_SERIES_NAME_FIRST = re.compile(
    rf'^\s*(?P<name>.+?)\s*[,;:]?\s*(?:{_VOLUME_WORD})\s*(?P<num>{_NUMBER_WORD})\s*$',
    re.IGNORECASE)
# "The Wheel of Time ; 1" — OpenLibrary's own series field.
_SERIES_BARE_NUMBER = re.compile(r'^\s*(?P<name>.+?)\s*[;,]\s*(?P<num>\d+(?:\.\d+)?)\s*$')
# A trailing parenthetical, which is where publishers park the series on a title.
_SERIES_PARENTHETICAL = re.compile(r'[\(\[]\s*(?P<body>[^()\[\]]{2,100}?)\s*[\)\]]\s*$')
# "Dune (1)" — OpenLibrary's series field again, numbered the other way round.
_SERIES_PAREN_NUMBER = re.compile(r'^\s*(?P<name>.+?)\s*[\(\[]\s*(?P<num>\d+(?:\.\d+)?)\s*[\)\]]\s*$')
# Parentheticals and subtitles that are about the printing, not the series.
_NOT_A_SERIES = re.compile(
    r'\b(edition|ed\.|reprint|revised|illustrated|unabridged|abridged|anniversary|'
    r'paperback|hardcover|hardback|audio|audiobook|movie tie|deluxe|boxed set|'
    r'library|translated|complete|collection|omnibus|a novel|novel)\b', re.IGNORECASE)


def _series_number(raw: Optional[str]) -> Optional[float]:
    """A volume number written either way: '3', '3.5' or 'Three'."""
    if raw is None:
        return None
    word = str(raw).strip().lower()
    if word in _ORDINAL_WORDS:
        return float(_ORDINAL_WORDS[word])
    return clean_series_index(word)


def _series_from_text(text: Optional[str]) -> tuple:
    """Read a series and volume number out of one title or subtitle.

    A number is required. Without one there is no telling a series apart from
    an edition note or a tagline, and a wrong series is worse than none: it puts
    an unrelated book in the middle of a reading order."""
    v = clean(text)
    if not v:
        return (None, None)
    v = re.sub(r'\s+', ' ', v)

    bodies = [v]
    m = _SERIES_PARENTHETICAL.search(v)
    if m:
        # The parenthetical is the better candidate, so it is read first.
        bodies.insert(0, m.group('body'))

    for body in bodies:
        if not re.search(r'\d|\b(' + '|'.join(_ORDINAL_WORDS) + r')\b', body, flags=re.IGNORECASE):
            continue
        if _NOT_A_SERIES.search(body):
            continue
        for pattern in (_SERIES_NUMBER_FIRST, _SERIES_NAME_FIRST, _SERIES_BARE_NUMBER, _SERIES_PAREN_NUMBER):
            found = pattern.match(body)
            if not found:
                continue
            name = clean_series(found.group('name'))
            index = _series_number(found.group('num'))
            # "It 2" and "1984, part 2" are a title and a number, not a series;
            # a one-word-or-shorter name is more likely noise than a series.
            if name and len(name) >= 3 and index is not None:
                return (name, index)
    return (None, None)


def _series_from_title(*titles) -> tuple:
    """The first series any of these titles or subtitles states."""
    for title in titles:
        name, index = _series_from_text(title)
        if name:
            return (name, index)
    return (None, None)


def _fetch_series(olid: Optional[str], isbn: Optional[str], title: Optional[str] = None,
                  google_id: Optional[str] = None) -> tuple:
    """Find the series a book belongs to, as (name, index).

    There is no reliable structured field for this on either catalogue.
    OpenLibrary's edition records have a `series` field, which is the one place
    it is stated outright, but it is filled in for a minority of editions;
    Google Books publishes only an order number, never the series name. What
    both do carry is the full title as printed, and publishers put the series in
    the subtitle ("Book Three of the Stormlight Archive") or in a parenthetical
    ("Words of Radiance (The Stormlight Archive, #2)"). So: the explicit field
    first, then the titles and subtitles of the edition, its work, and Google's
    record of it, and the stored title last."""
    edition = _openlibrary_edition(olid, isbn)
    for raw in ((edition or {}).get('series') or []):
        name, index = split_series(raw if isinstance(raw, str) else None)
        if name:
            # An edition that names the series but not the volume can still take
            # its number from the title.
            if index is None:
                index = _series_from_title(edition.get('title'), title)[1]
            return (name, index)

    # OpenLibrary titles and subtitles, then Google's, then ours.
    candidates: List[Optional[str]] = []
    if edition:
        candidates.extend([edition.get('title'), edition.get('subtitle')])
    for work in _openlibrary_works(edition):
        candidates.extend([work.get('title'), work.get('subtitle')])

    info = (_google_item_for(isbn, google_id) or {}).get('volumeInfo') or {}
    candidates.extend([info.get('title'), info.get('subtitle')])
    candidates.append(title)

    name, index = _series_from_title(*candidates)
    return (name, index)


def _fetch_description(olid: Optional[str], isbn: Optional[str], title: Optional[str] = None,
                       author: Optional[str] = None, google_id: Optional[str] = None) -> Optional[str]:
    """Find a description for a book.

    Google Books first: its `description` is the publisher blurb and is present
    for most books in print, while OpenLibrary's is contributed by volunteers
    and is missing far more often than not. OpenLibrary's work description is
    the fallback, and its edition description the last resort."""
    info = (_google_item_for(isbn, google_id) or {}).get('volumeInfo') or {}
    found = clean_description(info.get('description'))
    if found:
        return found

    edition = _openlibrary_edition(olid, isbn)
    for work in _openlibrary_works(edition):
        found = clean_description(_ol_text(work.get('description')))
        if found:
            return found
    found = clean_description(_ol_text((edition or {}).get('description')))
    if found:
        return found

    # No ISBN match on Google, or a match with no blurb: the title search often
    # finds the same book under a different edition, which will have one.
    lookup_title = clean(info.get('title')) or clean(title)
    if not lookup_title:
        return None
    lookup_author = clean((info.get('authors') or [None])[0]) or clean(author)
    if lookup_author and ',' in lookup_author:
        lookup_author = lookup_author.split(',')[0].strip()
    for volume in _google_sibling_volumes(lookup_title, lookup_author):
        found = clean_description((volume.get('volumeInfo') or {}).get('description'))
        if found:
            return found
    return None


def _cover_candidates(isbn: Optional[str], olid: Optional[str], cover_url: Optional[str], only_cover_url: bool = False) -> List[str]:
    urls: List[str] = []
    if cover_url:
        url = cover_url.strip()
        # Ask OpenLibrary for the large variant even if we were handed a thumbnail.
        if 'covers.openlibrary.org' in url:
            url = re.sub(r'-(S|M)\.jpg', '-L.jpg', url)
        urls.append(url)
        # A hand-typed address is a specific request, not a hint: quietly
        # falling back to the OpenLibrary artwork would store a different
        # picture than the one that was asked for and still report success.
        if only_cover_url:
            return [u for u in urls if u]
    if olid:
        urls.append(f"{COVERS_BASE}/olid/{olid}-L.jpg?default=false")
    if isbn:
        cleaned = re.sub(r'[^0-9Xx]', '', isbn)
        if cleaned:
            urls.append(f"{COVERS_BASE}/isbn/{cleaned}-L.jpg?default=false")
    return [u for u in urls if u]


def _store_cover(book_id: int, isbn: Optional[str], olid: Optional[str], cover_url: Optional[str] = None,
                 only_cover_url: bool = False, google_id: Optional[str] = None) -> bool:
    """Best effort: find a cover for the book and save it in the database.
    OpenLibrary is tried first, then Google Books."""
    candidates = _cover_candidates(isbn, olid, cover_url, only_cover_url)
    for url in candidates:
        result = _download_image(url)
        if result:
            content, mime = result
            conn.execute("UPDATE books SET cover=?, cover_mime=? WHERE id=?", (sqlite3.Binary(content), mime, book_id))
            conn.commit()
            return True

    # An explicit address is a specific request; do not quietly substitute
    # Google's artwork for the picture that was asked for.
    if only_cover_url:
        return False

    for url in _google_cover_urls(isbn, google_id):
        result = _download_image(url)
        if result:
            content, mime = result
            conn.execute("UPDATE books SET cover=?, cover_mime=? WHERE id=?", (sqlite3.Binary(content), mime, book_id))
            conn.commit()
            return True
    return False


# --- routes ---
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/token", response_model=Token)
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    access_token = create_access_token(data={"sub": user['username'], "role": ROLE_ADMIN})
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/token/guest", response_model=Token)
def guest_access_token():
    """Hand out a guest token for browsing and circulation."""
    if not GUEST_ACCESS_ENABLED:
        raise HTTPException(status_code=404, detail="Guest access is disabled")
    access_token = create_access_token(data={"sub": GUEST_USERNAME, "role": ROLE_GUEST},
                                       expires_delta=timedelta(minutes=GUEST_TOKEN_EXPIRE_MINUTES))
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/auth/config")
def auth_config():
    """What the login screen needs to know before anyone has signed in."""
    return {"guest_access_enabled": GUEST_ACCESS_ENABLED}


@app.get("/me")
def read_me(current_user: dict = Depends(get_current_user)):
    """Who the caller is and what they are allowed to do."""
    return {
        "username": current_user.get('username'),
        "role": current_user.get('role', ROLE_ADMIN),
        "read_only": is_guest(current_user),
    }


@app.get("/books", response_model=List[Book])
def list_books(q: Optional[str] = None, sort: Optional[str] = None, dir: Optional[str] = None,
               tags: Optional[str] = None, match: Optional[str] = None,
               exclude_tags: Optional[str] = None,
               shelf_id: Optional[int] = None, placed: Optional[bool] = None,
               format: Optional[str] = None, has_format: Optional[bool] = None,
               series: Optional[str] = None, has_series: Optional[bool] = None,
               checked_out: Optional[bool] = None,
               current_user: dict = Depends(get_current_user)):
    """List books. Optional ?q= search over title, author, ISBN, OLID, series
    and notes,
    ?sort=title|author|added|location|series, ?dir=asc|desc,
    ?tags=a,b with ?match=any|all (default any), ?exclude_tags=a,b to omit
    books having any listed tag, ?shelf_id= to limit to one shelf,
    ?placed=false to find books with no location yet, ?format= to limit to one
    binding, ?has_format=false to find the books still missing one, ?series= to
    limit to one series, ?has_series=false for the standalones, and
    ?checked_out=true|false to filter by circulation status."""
    order = order_by(sort, dir)
    where = []
    params: List = []
    if q:
        pattern = f"%{q.strip()}%"
        # Notes included: it is where "signed", "lent to Sam" and "second copy"
        # live, which are exactly the things you go looking for and cannot find
        # by title.
        where.append("(title LIKE ? OR author LIKE ? OR isbn LIKE ? OR olid LIKE ? OR notes LIKE ? OR series LIKE ?)")
        params.extend([pattern] * 6)

    if shelf_id is not None:
        where.append("shelf_id = ?")
        params.append(shelf_id)

    if placed is not None:
        where.append("shelf_id IS NOT NULL" if placed else "shelf_id IS NULL")

    # Compared through clean_format so asking for "pbk." finds the Paperbacks,
    # and NOCASE so a value typed by hand still matches the picker.
    wanted_format = clean_format(format)
    if wanted_format:
        where.append("format = ? COLLATE NOCASE")
        params.append(wanted_format)

    # Separate from ?format= rather than a magic value, the way ?placed= is
    # separate from ?shelf_id=: "no binding recorded" is a different question
    # from "which binding", and a book could plausibly be shelved as "None".
    if has_format is not None:
        where.append("(format IS NOT NULL AND format <> '')" if has_format
                     else "(format IS NULL OR format = '')")

    wanted_series = clean_series(series)
    if wanted_series:
        where.append("series = ? COLLATE NOCASE")
        params.append(wanted_series)

    if has_series is not None:
        where.append("(series IS NOT NULL AND series <> '')" if has_series
                     else "(series IS NULL OR series = '')")

    if checked_out is not None:
        where.append("checked_out_at IS NOT NULL" if checked_out else "checked_out_at IS NULL")

    wanted = normalize_tags((tags or '').split(','))
    if wanted:
        placeholders = ",".join("?" * len(wanted))
        having = " HAVING COUNT(DISTINCT t.id) = ?" if (match or 'any').lower() == 'all' else ""
        where.append(f"""id IN (SELECT bt.book_id FROM book_tags bt
                                JOIN tags t ON t.id = bt.tag_id
                                WHERE t.name IN ({placeholders})
                                GROUP BY bt.book_id{having})""")
        params.extend(wanted)
        if having:
            params.append(len(wanted))

    excluded = normalize_tags((exclude_tags or '').split(','))
    if excluded:
        placeholders = ",".join("?" * len(excluded))
        where.append(f"""NOT EXISTS (SELECT 1 FROM book_tags bt
                                      JOIN tags t ON t.id = bt.tag_id
                                      WHERE bt.book_id = books.id
                                        AND t.name IN ({placeholders}))""")
        params.extend(excluded)

    sql = f"SELECT {BOOK_COLUMNS} FROM books"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql + order, params).fetchall()
    grouped = tags_for_books([r['id'] for r in rows])
    return [book_from_row(r, grouped.get(r['id'], [])) for r in rows]

@app.get("/shelves", response_model=List[Shelf])
def list_shelves(current_user: dict = Depends(get_current_user)):
    counts = {r['shelf_id']: r['n'] for r in conn.execute(
        "SELECT shelf_id, COUNT(*) AS n FROM books WHERE shelf_id IS NOT NULL GROUP BY shelf_id")}
    rows = conn.execute("SELECT * FROM shelves ORDER BY sort_order, id").fetchall()
    return [shelf_row_to_model(r, counts.get(r['id'], 0)) for r in rows]


@app.post("/shelves", response_model=Shelf)
def add_shelf(s: Shelf, current_user: dict = Depends(require_editor)):
    name = clean(s.name)
    if not name:
        raise HTTPException(status_code=400, detail="A shelf needs a name")
    validate_shelf_size(s.columns, s.rows)
    cur = conn.execute("INSERT INTO shelves (name, columns, rows, sort_order, created_at) VALUES (?,?,?,?,?)",
                       (name, s.columns, s.rows, s.sort_order, now_iso()))
    conn.commit()
    return shelf_row_to_model(get_shelf(cur.lastrowid), 0)


@app.put("/shelves/{shelf_id}", response_model=Shelf)
def update_shelf(shelf_id: int, s: Shelf, current_user: dict = Depends(require_editor)):
    """Rename or resize a shelf.

    Shrinking is refused when books are sitting in the slots that would be cut
    off — losing a position silently is worse than making the user move the
    books first."""
    existing = get_shelf(shelf_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Not found")
    name = clean(s.name)
    if not name:
        raise HTTPException(status_code=400, detail="A shelf needs a name")
    validate_shelf_size(s.columns, s.rows)

    orphaned = conn.execute(
        "SELECT COUNT(*) AS n FROM books WHERE shelf_id=? AND (shelf_column > ? OR shelf_row > ?)",
        (shelf_id, s.columns, s.rows)).fetchone()['n']
    if orphaned:
        raise HTTPException(
            status_code=400,
            detail=f"{orphaned} book{'' if orphaned == 1 else 's'} would fall outside the new size. "
                   "Move them first, or choose a larger size.")

    conn.execute("UPDATE shelves SET name=?, columns=?, rows=?, sort_order=? WHERE id=?",
                 (name, s.columns, s.rows, s.sort_order, shelf_id))
    conn.commit()
    count = conn.execute("SELECT COUNT(*) AS n FROM books WHERE shelf_id=?", (shelf_id,)).fetchone()['n']
    return shelf_row_to_model(get_shelf(shelf_id), count)


@app.delete("/shelves/{shelf_id}")
def delete_shelf(shelf_id: int, current_user: dict = Depends(require_editor)):
    """Delete a shelf. Books on it are unplaced, never deleted."""
    if not get_shelf(shelf_id):
        raise HTTPException(status_code=404, detail="Not found")
    unplaced = conn.execute("SELECT COUNT(*) AS n FROM books WHERE shelf_id=?", (shelf_id,)).fetchone()['n']
    conn.execute("UPDATE books SET shelf_id=NULL, shelf_column=NULL, shelf_row=NULL WHERE shelf_id=?", (shelf_id,))
    conn.execute("DELETE FROM shelves WHERE id=?", (shelf_id,))
    conn.commit()
    return {"deleted": 1, "unplaced": unplaced}


@app.get("/shelves/{shelf_id}/layout", response_model=ShelfLayout)
def shelf_layout(shelf_id: int, current_user: dict = Depends(get_current_user)):
    """A shelf plus what is currently in each slot, for drawing the picker."""
    shelf = get_shelf(shelf_id)
    if not shelf:
        raise HTTPException(status_code=404, detail="Not found")
    rows = conn.execute(
        """SELECT id, title, author, shelf_column, shelf_row, length(cover) AS cover_size
           FROM books WHERE shelf_id=? AND shelf_column IS NOT NULL AND shelf_row IS NOT NULL
           ORDER BY shelf_row, shelf_column, title COLLATE NOCASE""", (shelf_id,)).fetchall()
    count = conn.execute("SELECT COUNT(*) AS n FROM books WHERE shelf_id=?", (shelf_id,)).fetchone()['n']
    return ShelfLayout(
        shelf=shelf_row_to_model(shelf, count),
        slots=[ShelfSlot(column=r['shelf_column'], row=r['shelf_row'], book_id=r['id'],
                         title=r['title'], author=r['author'], has_cover=bool(r['cover_size'] or 0))
               for r in rows])


@app.get("/tags")
def list_tags(current_user: dict = Depends(get_current_user)):
    """Every tag in use, with how many books carry it — powers the filter list."""
    cur = conn.execute("""SELECT t.name AS name, COUNT(bt.book_id) AS count
                          FROM tags t JOIN book_tags bt ON bt.tag_id = t.id
                          GROUP BY t.id ORDER BY t.name COLLATE NOCASE""")
    return [{"name": r['name'], "count": r['count']} for r in cur.fetchall()]

@app.delete("/books/{book_id}")
def delete_book(book_id: int, current_user: dict = Depends(require_editor)):
    cur = conn.execute("DELETE FROM books WHERE id=?", (book_id,))
    conn.execute("DELETE FROM book_tags WHERE book_id=?", (book_id,))
    prune_orphan_tags()
    conn.commit()
    return {"deleted": cur.rowcount}

@app.put("/books/{book_id}", response_model=Book)
def update_book(book_id: int, b: Book, current_user: dict = Depends(require_editor)):
    """Update an existing book. All fields in the payload will overwrite stored
    values, except created_at and the shelf location, which are only touched
    when the caller actually sends them."""
    title = clean(b.title)
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    added = clean_timestamp(b.created_at)
    # A client editing the title should not silently unplace the book. The
    # location is three linked fields that most callers omit entirely, so tell
    # "absent" apart from "explicitly null" rather than assuming null means
    # clear. Pydantic v2 exposes this as model_fields_set.
    sent = getattr(b, 'model_fields_set', None) or getattr(b, '__fields_set__', set())
    location_sent = bool({'shelf_id', 'shelf_column', 'shelf_row'} & set(sent))
    try:
        conn.execute("UPDATE books SET title=?, author=?, isbn=?, olid=?, google_id=?, notes=? WHERE id=?",
                     (title, clean(b.author), clean(b.isbn), clean_olid(b.olid), clean_google_id(b.google_id),
                      clean(b.notes), book_id))
        # Only when the caller actually sent it, for the same reason as the
        # location: a client written before this field existed omits it, and
        # editing a title from an older app should not quietly erase the format.
        if 'format' in sent:
            conn.execute("UPDATE books SET format=? WHERE id=?", (clean_format(b.format), book_id))
        # Same reasoning for the series and the description: a client that never
        # loaded them must not blank them by saving a title.
        if 'series' in sent or 'series_index' in sent:
            name = clean_series(b.series)
            index = clean_series_index(b.series_index)
            # "Wheel of Time #3" typed into the name field is still a number and
            # a name; split it rather than storing the number twice or not at all.
            if name and index is None:
                name, index = split_series(name)
            conn.execute("UPDATE books SET series=?, series_index=? WHERE id=?",
                         (name, index if name else None, book_id))
        if 'description' in sent:
            conn.execute("UPDATE books SET description=? WHERE id=?", (clean_description(b.description), book_id))
        if location_sent:
            location = resolve_location(b.shelf_id, b.shelf_column, b.shelf_row)
            conn.execute("UPDATE books SET shelf_id=?, shelf_column=?, shelf_row=? WHERE id=?",
                         (location[0], location[1], location[2], book_id))
        if added:
            conn.execute("UPDATE books SET created_at=? WHERE id=?", (added, book_id))
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if clean(b.cover_url):
        _store_cover(book_id, clean(b.isbn), clean_olid(b.olid), clean(b.cover_url), google_id=clean_google_id(b.google_id))
    if b.tags is not None:
        set_book_tags(book_id, b.tags)
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return book_from_row(row)

@app.post("/books", response_model=Book)
def add_book(b: Book, allow_duplicate: bool = False,
             current_user: dict = Depends(require_editor)):
    title = clean(b.title)
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    b.title, b.author, b.isbn, b.notes = title, clean(b.author), clean(b.isbn), clean(b.notes)
    normalized_isbn = re.sub(r'[-\s]', '', b.isbn or '')
    if normalized_isbn and not allow_duplicate:
        existing = next((
            row for row in conn.execute(
                "SELECT id, title, author, isbn FROM books WHERE isbn IS NOT NULL AND isbn <> ''").fetchall()
            if re.sub(r'[-\s]', '', row['isbn']) == normalized_isbn
        ), None)
        if existing:
            raise HTTPException(status_code=409, detail={
                "code": "duplicate_isbn",
                "message": "A copy with this ISBN is already in the library",
                "book_id": existing['id'],
                "title": existing['title'],
                "author": existing['author'],
            })
    b.olid = clean_olid(b.olid)
    b.google_id = clean_google_id(b.google_id)
    # The client knows the binding when the book was added from a chosen
    # edition; ask OpenLibrary only when it does not, which is the manual and
    # scanned paths.
    b.format = clean_format(b.format) or _openlibrary_format(b.olid, b.isbn)
    # Same for the series: honour what the client already knows, otherwise ask.
    b.series = clean_series(b.series)
    b.series_index = clean_series_index(b.series_index)
    if b.series and b.series_index is None:
        b.series, b.series_index = split_series(b.series)
    if not b.series:
        b.series, b.series_index = _fetch_series(b.olid, b.isbn, b.title, b.google_id)
    b.description = clean_description(b.description) or _fetch_description(b.olid, b.isbn, b.title, b.author, b.google_id)
    # Allow an explicit added date (e.g. when restoring a deleted book via undo).
    b.created_at = clean_timestamp(b.created_at) or now_iso()
    b.shelf_id, b.shelf_column, b.shelf_row = resolve_location(b.shelf_id, b.shelf_column, b.shelf_row)
    try:
        cur = conn.execute("INSERT INTO books (title, author, isbn, olid, google_id, notes, format, series, series_index, description, created_at, shelf_id, shelf_column, shelf_row) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (b.title, b.author, b.isbn, b.olid, b.google_id, b.notes, b.format,
                            b.series, b.series_index, b.description, b.created_at,
                            b.shelf_id, b.shelf_column, b.shelf_row))
        conn.commit()
        b.id = cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Grab a cover while we are adding, so most books never need a manual lookup.
    b.has_cover = _store_cover(b.id, b.isbn, b.olid, clean(b.cover_url), google_id=b.google_id)
    b.cover_url = None
    # Tags: use what the caller supplied, otherwise fetch OpenLibrary subjects.
    supplied = normalize_tags(b.tags)
    b.tags = set_book_tags(b.id, supplied) if supplied else add_book_tags(b.id, _fetch_genres(b.olid, b.isbn, b.title, b.author, b.google_id))
    if b.isbn:
        normalized_isbn = re.sub(r'[-\s]', '', b.isbn)
        b.copy_count = sum(
            1 for row in conn.execute(
                "SELECT isbn FROM books WHERE isbn IS NOT NULL AND isbn <> ''").fetchall()
            if re.sub(r'[-\s]', '', row['isbn']) == normalized_isbn
        )
    return b

@app.get("/books/{book_id}", response_model=Book)
def get_book(book_id: int, current_user: dict = Depends(get_current_user)):
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return book_from_row(row)


@app.post("/books/{book_id}/checkout", response_model=Book)
def checkout_book(book_id: int, checkout: CheckoutRequest,
                  current_user: dict = Depends(get_current_user)):
    """Check out one available book. This is the one write guests may perform."""
    borrower_name = clean(checkout.borrower_name)
    if not borrower_name:
        raise HTTPException(status_code=400, detail="Borrower name is required")
    if len(borrower_name) > 100:
        raise HTTPException(status_code=400, detail="Borrower name must be 100 characters or fewer")

    cur = conn.execute(
        """UPDATE books SET borrower_name=?, checked_out_at=?
           WHERE id=? AND checked_out_at IS NULL""",
        (borrower_name, now_iso(), book_id))
    conn.commit()
    if not cur.rowcount:
        existing = conn.execute("SELECT checked_out_at FROM books WHERE id=?", (book_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Not found")
        raise HTTPException(status_code=409, detail="This book is already checked out")
    row = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,)).fetchone()
    return book_from_row(row)


@app.post("/books/{book_id}/checkin", response_model=Book)
def checkin_book(book_id: int, current_user: dict = Depends(require_editor)):
    """Return a checked-out book. Only administrators may record returns."""
    cur = conn.execute(
        """UPDATE books SET borrower_name=NULL, checked_out_at=NULL
           WHERE id=? AND checked_out_at IS NOT NULL""",
        (book_id,))
    conn.commit()
    if not cur.rowcount:
        existing = conn.execute("SELECT checked_out_at FROM books WHERE id=?", (book_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Not found")
        raise HTTPException(status_code=409, detail="This book is already checked in")
    row = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,)).fetchone()
    return book_from_row(row)


@app.get("/books/{book_id}/cover")
def get_book_cover(book_id: int, current_user: dict = Depends(get_current_user_flexible)):
    """Serve the stored cover image. Accepts ?token=... so <img> can use it."""
    cur = conn.execute("SELECT cover, cover_mime FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row or not row['cover']:
        raise HTTPException(status_code=404, detail="No cover")
    return Response(content=bytes(row['cover']),
                    media_type=row['cover_mime'] or 'image/jpeg',
                    headers={"Cache-Control": "private, max-age=60"})

@app.post("/books/{book_id}/cover/lookup", response_model=Book)
def lookup_book_cover(book_id: int, cover_url: Optional[str] = None, current_user: dict = Depends(require_editor)):
    """Find a cover for an existing book (by explicit URL, stored OLID, or ISBN),
    falling back to Google Books when OpenLibrary has none."""
    cur = conn.execute("SELECT id, isbn, olid, google_id FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    supplied = clean(cover_url)
    if not _store_cover(book_id, row['isbn'], row['olid'], supplied, only_cover_url=bool(supplied), google_id=row['google_id']):
        if supplied:
            raise HTTPException(status_code=400, detail="That address did not return a usable image. It needs to be a direct link to an image file under 5 MB.")
        raise HTTPException(status_code=404, detail="No cover found for this book on OpenLibrary or Google Books")
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone())

@app.post("/books/{book_id}/tags/lookup", response_model=Book)
def lookup_book_tags(book_id: int, replace: bool = False, current_user: dict = Depends(require_editor)):
    """Fetch genre tags from OpenLibrary, falling back to Google Books
    categories. Adds to the book's tags by default; pass replace=true to swap
    them out (used by the Refresh button, so stale or
    noisy tags do not stick around)."""
    cur = conn.execute("SELECT id, isbn, olid, google_id, title, author FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if not clean(row['isbn']) and not clean(row['olid']) and not clean(row['title']):
        raise HTTPException(status_code=400, detail="This book needs an ISBN, OLID or title to look up tags")
    found = _fetch_genres(row['olid'], row['isbn'], row['title'], row['author'], row['google_id'])
    if not found:
        raise HTTPException(status_code=404, detail="No tags found for this book on OpenLibrary or Google Books")
    tags = set_book_tags(book_id, found) if replace else add_book_tags(book_id, found)
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone(), tags)

@app.post("/books/{book_id}/olid/lookup", response_model=Book)
def lookup_book_olid(book_id: int, current_user: dict = Depends(require_editor)):
    """Resolve the OpenLibrary edition id for an existing book from its ISBN."""
    cur = conn.execute("SELECT id, isbn FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if not clean(row['isbn']):
        raise HTTPException(status_code=400, detail="This book has no ISBN to look up")
    olid = _lookup_olid_by_isbn(row['isbn'])
    if not olid:
        raise HTTPException(status_code=404, detail="No OpenLibrary edition found for this ISBN")
    conn.execute("UPDATE books SET olid=? WHERE id=?", (olid, book_id))
    conn.commit()
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone())

@app.post("/books/{book_id}/format/lookup", response_model=Book)
def lookup_book_format(book_id: int, current_user: dict = Depends(require_editor)):
    """Fetch the binding for one book from OpenLibrary.

    Per book rather than across the library: unlike a cover, a wrong answer here
    is a plausible-looking word in a column, and a sweep over every book would
    bury the ones it got wrong among the ones it got right."""
    cur = conn.execute("SELECT id, isbn, olid FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if not clean(row['isbn']) and not clean_olid(row['olid']):
        raise HTTPException(status_code=400,
                            detail="This book needs an ISBN or an OLID before its format can be looked up")

    found = _openlibrary_format(row['olid'], row['isbn'])
    if not found:
        raise HTTPException(status_code=404, detail="OpenLibrary does not record a format for this edition")

    conn.execute("UPDATE books SET format=? WHERE id=?", (found, book_id))
    conn.commit()
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone())

@app.post("/books/{book_id}/series/lookup", response_model=Book)
def lookup_book_series(book_id: int, current_user: dict = Depends(require_editor)):
    """Fetch the series and volume number for one book.

    OpenLibrary states the series outright on its edition records; where it does
    not, the series is read out of the parenthesised suffix publishers put on
    the title. Either way this only ever writes what a catalogue says, so a
    series typed by hand is replaced only when a lookup actually finds one."""
    cur = conn.execute("SELECT id, isbn, olid, google_id, title FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if not clean(row['isbn']) and not clean_olid(row['olid']) and not clean(row['title']):
        raise HTTPException(status_code=400, detail="This book needs an ISBN, OLID or title to look up its series")

    name, index = _fetch_series(row['olid'], row['isbn'], row['title'], row['google_id'])
    if not name:
        raise HTTPException(status_code=404, detail="No series found for this book on OpenLibrary or Google Books")

    conn.execute("UPDATE books SET series=?, series_index=? WHERE id=?", (name, index, book_id))
    conn.commit()
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone())


@app.post("/books/{book_id}/description/lookup", response_model=Book)
def lookup_book_description(book_id: int, current_user: dict = Depends(require_editor)):
    """Fetch the publisher blurb for one book from Google Books, falling back to
    the OpenLibrary work description."""
    cur = conn.execute("SELECT id, isbn, olid, google_id, title, author FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if not clean(row['isbn']) and not clean_olid(row['olid']) and not clean(row['title']):
        raise HTTPException(status_code=400, detail="This book needs an ISBN, OLID or title to look up a description")

    found = _fetch_description(row['olid'], row['isbn'], row['title'], row['author'], row['google_id'])
    if not found:
        raise HTTPException(status_code=404, detail="No description found for this book on Google Books or OpenLibrary")

    conn.execute("UPDATE books SET description=? WHERE id=?", (found, book_id))
    conn.commit()
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone())


@app.get("/series")
def list_series(current_user: dict = Depends(get_current_user)):
    """Every series in the library with how many books are in it, so the Manage
    tab can offer them as a filter."""
    return [{"name": r['series'], "count": r['n']} for r in conn.execute(
        """SELECT series, COUNT(*) AS n FROM books
           WHERE series IS NOT NULL AND series <> ''
           GROUP BY series COLLATE NOCASE
           ORDER BY series COLLATE NOCASE""")]


@app.get("/formats")
def list_formats(current_user: dict = Depends(get_current_user)):
    """The bindings offered in the pickers, and whatever else is already in use.

    Clients suggest rather than restrict, so a value typed by hand on one of
    them shows up in the list on the others instead of looking like a mistake."""
    in_use = [r['format'] for r in conn.execute(
        "SELECT DISTINCT format FROM books WHERE format IS NOT NULL AND format<>'' ORDER BY format COLLATE NOCASE")]
    extra = [f for f in in_use if f not in KNOWN_FORMATS]
    return {"known": KNOWN_FORMATS, "in_use": in_use, "other": extra}

@app.post("/books/{book_id}/google/lookup", response_model=Book)
def lookup_book_google_id(book_id: int, current_user: dict = Depends(require_editor)):
    """Resolve the Google Books volume id for an existing book.

    Storing it lets later tag and cover lookups go straight to the volume record
    instead of searching for it first, and pins the book to one exact edition."""
    cur = conn.execute("SELECT id, isbn, title, author FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    volume_id = _resolve_google_id(row['isbn'], row['title'], row['author'])
    if not volume_id:
        raise HTTPException(status_code=404, detail="No Google Books volume found for this book")

    conn.execute("UPDATE books SET google_id=? WHERE id=?", (volume_id, book_id))
    conn.commit()
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone())

@app.post("/books/{book_id}/cover", response_model=Book)
async def upload_book_cover(book_id: int, file: UploadFile = File(...), current_user: dict = Depends(require_editor)):
    """Upload a cover image manually."""
    cur = conn.execute("SELECT id FROM books WHERE id=?", (book_id,))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Not found")
    content = await file.read()
    mime = (file.content_type or '').split(';')[0].strip().lower()
    if not mime.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    if not content or len(content) > MAX_COVER_BYTES:
        raise HTTPException(status_code=400, detail="Image must be between 1 byte and 5 MB")
    conn.execute("UPDATE books SET cover=?, cover_mime=? WHERE id=?", (sqlite3.Binary(content), mime, book_id))
    conn.commit()
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone())

@app.delete("/books/{book_id}/cover", response_model=Book)
def delete_book_cover(book_id: int, current_user: dict = Depends(require_editor)):
    conn.execute("UPDATE books SET cover=NULL, cover_mime=NULL WHERE id=?", (book_id,))
    conn.commit()
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return book_from_row(row)

class BookLocation(BaseModel):
    """Body for setting a book's position. All null unplaces the book."""
    shelf_id: Optional[int] = None
    shelf_column: Optional[int] = None
    shelf_row: Optional[int] = None


@app.put("/books/{book_id}/location", response_model=Book)
def set_book_location(book_id: int, loc: BookLocation, current_user: dict = Depends(require_editor)):
    """Set or clear where a book lives.

    Separate from PUT /books/{id} so placing a book from the shelf picker cannot
    clobber fields the picker never loaded."""
    if not conn.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Not found")
    shelf_id, column, row = resolve_location(loc.shelf_id, loc.shelf_column, loc.shelf_row)
    conn.execute("UPDATE books SET shelf_id=?, shelf_column=?, shelf_row=? WHERE id=?",
                 (shelf_id, column, row, book_id))
    conn.commit()
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone())


@app.post("/books/import")
async def import_books(file: UploadFile = File(...), current_user: dict = Depends(require_editor)):
    """Accepts a CSV file with headers: title,author,isbn,olid,google_id,tags,notes,series,series_index,description
    (tags separated by ';' or '|', since ',' is the column separator)"""
    content = (await file.read()).decode('utf-8')
    reader = csv.DictReader(io.StringIO(content))
    inserted = 0
    added_at = now_iso()
    for row in reader:
        title = clean(row.get('title') or row.get('Title'))
        if not title:
            continue
        author = clean(row.get('author') or row.get('Author'))
        isbn = clean(row.get('isbn') or row.get('ISBN'))
        olid = clean_olid(row.get('olid') or row.get('OLID'))
        google_id = clean_google_id(row.get('google_id') or row.get('Google ID') or row.get('googleid'))
        notes = clean(row.get('notes') or row.get('Notes'))
        series, series_index = split_series(clean(row.get('series') or row.get('Series')))
        explicit_index = clean_series_index(row.get('series_index') or row.get('Series Index'))
        if explicit_index is not None:
            series_index = explicit_index
        description = clean_description(row.get('description') or row.get('Description'))
        tags = normalize_tags(re.split(r'[;|]', row.get('tags') or row.get('Tags') or ''))
        try:
            cur = conn.execute("INSERT INTO books (title, author, isbn, olid, google_id, notes, series, series_index, description, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (title, author, isbn, olid, google_id, notes, series,
                                series_index if series else None, description, added_at))
            if tags and cur.lastrowid and cur.rowcount:
                set_book_tags(cur.lastrowid, tags)
            inserted += 1
        except Exception:
            continue
    conn.commit()
    return {"inserted": inserted}

OL_BASE = "https://openlibrary.org"
MAX_EDITIONS_PER_WORK = 200
EDITIONS_PAGE_SIZE = 100
BIBKEYS_CHUNK_SIZE = 25


def _is_english(langs) -> Optional[bool]:
    """Return True if English, False if a non-English language is declared,
    or None when the edition declares no language at all."""
    if not langs:
        return None
    for ln in langs:
        if isinstance(ln, str):
            if 'eng' in ln:
                return True
        elif isinstance(ln, dict) and ln.get('key', '').endswith('/eng'):
            return True
    return False


def _collect_isbns(obj) -> List[str]:
    """Pull every ISBN we can find off an edition record (either shape).
    ISBN-13 first so it is the value shown and stored by default."""
    found: List[str] = []
    if not obj:
        return found
    for field in ('isbn_13', 'isbn_10', 'isbn'):
        vals = obj.get(field)
        if vals:
            found.extend(vals if isinstance(vals, list) else [vals])
    ids = obj.get('identifiers') or {}
    for field in ('isbn_13', 'isbn_10', 'isbn'):
        vals = ids.get(field)
        if vals:
            found.extend(vals if isinstance(vals, list) else [vals])
    return found


def _fetch_work_editions(work_key: str) -> List[dict]:
    """Fetch every edition of a work, following pagination so works with more
    than one page of editions are not silently truncated."""
    entries: List[dict] = []
    offset = 0
    while len(entries) < MAX_EDITIONS_PER_WORK:
        url = f"{OL_BASE}{work_key}/editions.json?limit={EDITIONS_PAGE_SIZE}&offset={offset}"
        try:
            r = requests.get(url, timeout=8)
        except requests.RequestException:
            break
        if r.status_code != 200:
            break
        page = r.json().get('entries', [])
        if not page:
            break
        entries.extend(page)
        if len(page) < EDITIONS_PAGE_SIZE:
            break
        offset += EDITIONS_PAGE_SIZE
    return entries[:MAX_EDITIONS_PER_WORK]


def _fetch_edition_details(olids: List[str]) -> dict:
    """Look up display metadata for OLIDs, chunked so the request URL stays sane."""
    details: dict = {}
    for i in range(0, len(olids), BIBKEYS_CHUNK_SIZE):
        chunk = olids[i:i + BIBKEYS_CHUNK_SIZE]
        bibkeys = ",".join(f"OLID:{k}" for k in chunk)
        url = f"{OL_BASE}/api/books?bibkeys={bibkeys}&format=json&jscmd=data"
        try:
            r = requests.get(url, timeout=10)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            try:
                details.update(r.json())
            except ValueError:
                continue
    return details


def _edition_title(entry: dict, detail: dict, fallback: Optional[str]) -> str:
    """Prefer a title that includes the subtitle, since that is what
    distinguishes special editions (e.g. leather bound anniversary printings)."""
    for src in (entry, detail):
        if not src:
            continue
        full = src.get('full_title')
        if full and full.strip().lower() != 'untitled':
            return full
        title = src.get('title')
        if title and title.strip().lower() != 'untitled':
            subtitle = src.get('subtitle')
            return f"{title}: {subtitle}" if subtitle else title
    return fallback or ''


def _google_query_variants(title: Optional[str], author: Optional[str], q: Optional[str]) -> List[str]:
    """Build Google Books queries, most precise first.

    `intitle:` and `inauthor:` bind only to the text immediately following them,
    so an unquoted multi-word title silently degrades: `intitle:Songs of the
    Dead` searches titles for "Songs" and treats the rest as loose keywords.
    Quoting keeps the phrase together. The plain free-text variant is what
    books.google.com effectively runs, and is kept as a fallback because an
    exact-phrase title match fails on subtitles and punctuation differences."""
    title = (title or '').strip()
    author = (author or '').strip()
    q = (q or '').strip()

    if q:
        return [q]

    variants: List[str] = []
    fielded: List[str] = []
    if title:
        fielded.append(f'intitle:"{title}"')
    if author:
        fielded.append(f'inauthor:"{author}"')
    if fielded:
        variants.append(' '.join(fielded))

    # Loosen the author first: a title is the stronger signal, and author
    # spellings differ between catalogues more often than titles do.
    if title and author:
        variants.append(f'intitle:"{title}"')

    plain = ' '.join(x for x in (title, author) if x)
    if plain:
        variants.append(plain)
    return variants


def _google_search(title: Optional[str], author: Optional[str], q: Optional[str]) -> List[dict]:
    """Search Google Books and shape the results like the OpenLibrary ones, so
    the frontend can render them with the same edition picker.

    Google returns individual volumes rather than works with editions, so each
    volume becomes a single-edition result."""
    if not GOOGLE_BOOKS_ENABLED:
        return []

    items: List[dict] = []
    for query in _google_query_variants(title, author, q):
        params = {'q': query, 'maxResults': 10, 'country': 'US'}
        if GOOGLE_BOOKS_API_KEY:
            params['key'] = GOOGLE_BOOKS_API_KEY
        try:
            r = requests.get(GOOGLE_BOOKS_URL, params=params, timeout=8)
            if r.status_code != 200:
                continue
            items = r.json().get('items') or []
        except (requests.RequestException, ValueError):
            continue
        if items:
            break

    results = []
    for item in items:
        info = item.get('volumeInfo') or {}
        isbns = [i.get('identifier') for i in (info.get('industryIdentifiers') or [])
                 if i.get('type', '').startswith('ISBN') and i.get('identifier')]
        # Prefer ISBN-13 so the stored value matches the OpenLibrary path.
        isbns.sort(key=lambda x: len(x or ''), reverse=True)

        links = info.get('imageLinks') or {}
        cover = None
        for size in GOOGLE_IMAGE_SIZES:
            if links.get(size):
                cover = _clean_google_image_url(links[size])
                break

        full_title = info.get('title') or ''
        if info.get('subtitle'):
            full_title = f"{full_title}: {info['subtitle']}"

        published = info.get('publishedDate') or ''
        year = None
        if published[:4].isdigit():
            year = int(published[:4])

        results.append({
            'title': info.get('title'),
            'authors': info.get('authors') or [],
            'publish_year': year,
            'edition_keys': [],
            'isbns': isbns,
            'work_key': f"google:{item.get('id')}",
            'google_id': item.get('id'),
            'source': 'google',
            'editions': [{
                'olid': None,
                'google_id': item.get('id'),
                'title': full_title,
                'publish_date': published or None,
                'publishers': [info['publisher']] if info.get('publisher') else [],
                'number_of_pages': info.get('pageCount'),
                'format': info.get('printType'),
                'isbns': isbns,
                'language': info.get('language') or 'unknown',
                'cover': cover,
                'source': 'google',
            }],
        })
    return results


@app.get("/search")
def search_openlibrary(title: Optional[str] = None, author: Optional[str] = None, q: Optional[str] = None,
                       include_all_languages: bool = False,
                       current_user: dict = Depends(get_current_user)):
    """Search OpenLibrary by title and/or author and return candidate works with
    their editions. Editions default to English only; pass include_all_languages=true
    to keep translations as well. Falls back to Google Books when OpenLibrary
    returns nothing."""
    params = {"limit": 10}
    if q:
        params['q'] = q.strip()
    else:
        if title and title.strip():
            params['title'] = title.strip()
        if author and author.strip():
            params['author'] = author.strip()
    if not any(k in params for k in ('q', 'title', 'author')):
        return []

    try:
        r = requests.get(f"{OL_BASE}/search.json", params=params, timeout=8)
        docs = r.json().get('docs', []) if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        docs = None

    if docs is None:
        # OpenLibrary is unreachable or erroring; Google Books can still answer.
        google = _google_search(title, author, q)
        if google:
            return google
        raise HTTPException(status_code=502, detail="Search failed")

    results = []
    for doc in docs[:10]:
        work_key = doc.get('key')
        isbns = list(doc.get('isbn', []) or [])
        entries_map = {}
        edition_keys = list(doc.get('edition_key', []) or [])

        if work_key:
            ordered_keys = []
            for entry in _fetch_work_editions(work_key):
                key = entry.get('key') or ''
                olid = key.split('/')[-1]
                if not olid:
                    continue
                english = _is_english(entry.get('languages'))
                # Keep editions that are English or that declare no language at all.
                if not include_all_languages and english is False:
                    continue
                ordered_keys.append(olid)
                entries_map[olid] = entry
                isbns.extend(_collect_isbns(entry))
            if ordered_keys:
                edition_keys = ordered_keys

        editions_meta = []
        if edition_keys:
            details = _fetch_edition_details(edition_keys)
            for olid in edition_keys:
                detail = details.get(f"OLID:{olid}") or {}
                entry = entries_map.get(olid) or {}
                if not detail and not entry:
                    continue

                english = _is_english(entry.get('languages') or detail.get('languages'))
                if not include_all_languages and english is False:
                    continue

                edition_isbns = list(dict.fromkeys(_collect_isbns(detail) + _collect_isbns(entry)))
                isbns.extend(edition_isbns)

                cover = None
                if detail.get('cover'):
                    cover = detail['cover'].get('medium') or detail['cover'].get('small')
                elif entry.get('covers'):
                    cover_ids = [c for c in entry['covers'] if isinstance(c, int) and c > 0]
                    if cover_ids:
                        cover = f"https://covers.openlibrary.org/b/id/{cover_ids[0]}-M.jpg"

                publishers = [p.get('name') if isinstance(p, dict) else p
                              for p in (detail.get('publishers') or entry.get('publishers') or [])]

                editions_meta.append({
                    'olid': olid,
                    'title': _edition_title(entry, detail, doc.get('title')),
                    'publish_date': detail.get('publish_date') or entry.get('publish_date'),
                    'publishers': publishers,
                    'number_of_pages': detail.get('number_of_pages') or entry.get('number_of_pages'),
                    'format': entry.get('physical_format') or detail.get('physical_format'),
                    'isbns': edition_isbns,
                    'language': 'eng' if english else ('unknown' if english is None else 'other'),
                    'cover': cover,
                })

        results.append({
            'title': doc.get('title'),
            'authors': doc.get('author_name', []),
            'publish_year': doc.get('first_publish_year') or (doc.get('publish_year') and doc.get('publish_year')[0]),
            'edition_keys': edition_keys,
            'isbns': list(dict.fromkeys(isbns)),
            'work_key': work_key,
            'editions': editions_meta,
        })

    return _merge_google_results(results, title, author, q)


def _merge_google_results(results: List[dict], title: Optional[str], author: Optional[str],
                          q: Optional[str]) -> List[dict]:
    """Append Google Books hits that OpenLibrary did not already return.

    OpenLibrary answering at all is not the same as it answering *usefully*: a
    search for a recent title often comes back with unrelated works, which used
    to mean the Google fallback never ran. Merging instead of falling back means
    a book Google knows about always shows up, marked so its origin is obvious."""
    google = _google_search(title, author, q)
    if not google:
        return results

    def isbn_keys(entry: dict) -> set:
        keys = {re.sub(r'[^0-9Xx]', '', str(i)) for i in (entry.get('isbns') or [])}
        for ed in entry.get('editions') or []:
            keys |= {re.sub(r'[^0-9Xx]', '', str(i)) for i in (ed.get('isbns') or [])}
        return {k for k in keys if k}

    seen: set = set()
    for entry in results:
        seen |= isbn_keys(entry)

    def title_key(entry: dict) -> str:
        return re.sub(r'[^a-z0-9]', '', (entry.get('title') or '').lower())

    def author_key(entry: dict) -> str:
        first = (entry.get('authors') or [''])[0] or ''
        return re.sub(r'[^a-z0-9]', '', first.lower())

    # Title alone is not an identity: plenty of unrelated books share one, and
    # dropping on title would have hidden a Google hit whenever OpenLibrary
    # happened to return a different book of the same name.
    seen_books = {(title_key(r), author_key(r)) for r in results if r.get('title')}

    merged = list(results)
    for entry in google:
        keys = isbn_keys(entry)
        if keys & seen:
            continue
        # Same title *and* author from both sources: OpenLibrary's record has
        # real editions, so keep it rather than showing the book twice.
        if (title_key(entry), author_key(entry)) in seen_books:
            continue
        merged.append(entry)
        seen |= keys
        seen_books.add((title_key(entry), author_key(entry)))
    return merged[:20]

@app.get("/edition/{olid}")
def get_edition(olid: str, current_user: dict = Depends(get_current_user)):
    """Fetch edition metadata by OpenLibrary OLID (e.g., OL12345M)"""
    olid = olid.strip()
    url = f"https://openlibrary.org/api/books?bibkeys=OLID:{olid}&format=json&jscmd=data"
    try:
        r = requests.get(url, timeout=6)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Edition lookup failed")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Edition lookup failed")
    data = r.json()
    key = f"OLID:{olid}"
    if key not in data:
        return {}
    item = data[key]
    return {
        "title": item.get("title"),
        "authors": [a.get("name") for a in item.get("authors", [])],
        "publish_date": item.get("publish_date"),
        "isbns": item.get("identifiers", {}).get("isbn_10") or item.get("identifiers", {}).get("isbn_13") or item.get("identifiers", {}).get("isbn") or []
    }

@app.get("/lookup/{isbn}")
def lookup_isbn(isbn: str, current_user: dict = Depends(get_current_user)):
    """Look an ISBN up on OpenLibrary, falling back to Google Books."""
    isbn = isbn.strip()
    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    item = None
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            item = r.json().get(f"ISBN:{isbn}")
    except (requests.RequestException, ValueError):
        item = None

    if item:
        authors = [a.get("name") for a in item.get("authors", [])]
        return {
            "title": item.get("title"),
            "authors": authors,
            "publish_date": item.get("publish_date"),
            "olid": clean_olid(item.get("key")) or _lookup_olid_by_isbn(isbn),
            # Worth resolving even when OpenLibrary answered: storing it makes
            # later tag and cover lookups a single request.
            "google_id": _resolve_google_id(isbn, item.get("title"),
                                            authors[0] if authors else None),
            "source": "openlibrary",
        }

    # OpenLibrary has nothing (or is down): try Google Books before giving up.
    google_item = _google_volume_item(isbn)
    info = (google_item or {}).get("volumeInfo")
    if not info:
        return {}
    title = info.get("title")
    subtitle = info.get("subtitle")
    return {
        "title": f"{title}: {subtitle}" if title and subtitle else title,
        "authors": info.get("authors") or [],
        "publish_date": info.get("publishedDate"),
        # Google has no OpenLibrary id, but OpenLibrary may still know the
        # edition even when its books API returned nothing useful.
        "olid": _lookup_olid_by_isbn(isbn),
        "google_id": google_item.get("id"),
        "source": "google",
    }


@app.get("/diagnostics/search")
def diagnose_search(title: Optional[str] = None, author: Optional[str] = None,
                    q: Optional[str] = None,
                    current_user: dict = Depends(get_current_user_flexible)):
    """Show the exact Google Books queries a search would run and what each one
    returns, so a missing book can be traced to the query rather than guessed at.

    Declared before /diagnostics/{isbn} so the literal path wins the match."""
    report: dict = {
        "google_enabled": GOOGLE_BOOKS_ENABLED,
        "google_api_key_set": bool(GOOGLE_BOOKS_API_KEY),
        "attempts": [],
    }
    for query in _google_query_variants(title, author, q):
        params = {'q': query, 'maxResults': 10, 'country': 'US'}
        if GOOGLE_BOOKS_API_KEY:
            params['key'] = GOOGLE_BOOKS_API_KEY
        attempt: dict = {"query": query}
        try:
            r = requests.get(GOOGLE_BOOKS_URL, params=params, timeout=8)
            payload = r.json() if r.content else {}
            items = payload.get('items') or []
            attempt["http_status"] = r.status_code
            attempt["total_items"] = payload.get('totalItems')
            attempt["results"] = [
                {"title": (i.get('volumeInfo') or {}).get('title'),
                 "authors": (i.get('volumeInfo') or {}).get('authors'),
                 "categories": (i.get('volumeInfo') or {}).get('categories') or []}
                for i in items[:10]
            ]
            if r.status_code != 200:
                attempt["error"] = (payload.get('error') or {}).get('message') or r.text[:200]
        except (requests.RequestException, ValueError) as e:
            attempt["error"] = str(e)
        report["attempts"].append(attempt)
        if attempt.get("results"):
            attempt["used"] = True
            break
    return report


@app.get("/diagnostics/{isbn}")
def diagnose_sources(isbn: str, title: Optional[str] = None, author: Optional[str] = None,
                     current_user: dict = Depends(get_current_user_flexible)):
    """Show what each catalogue actually holds for an ISBN, and what the genre
    extractor makes of it. Useful when a book comes back with fewer tags than
    expected — it separates "the source has nothing" from "we dropped it"."""
    cleaned = re.sub(r'[^0-9Xx]', '', isbn or '')
    # Always report live data: a cached miss from a few minutes ago would make a
    # working fix look broken.
    _GOOGLE_CACHE.clear()
    report: dict = {
        "isbn": cleaned,
        "google_enabled": GOOGLE_BOOKS_ENABLED,
        "google_api_key_set": bool(GOOGLE_BOOKS_API_KEY),
    }

    # --- OpenLibrary ---
    ol_subjects: List[str] = []
    try:
        r = requests.get(f"{OL_BASE}/api/books?bibkeys=ISBN:{cleaned}&format=json&jscmd=data", timeout=8)
        item = r.json().get(f"ISBN:{cleaned}") if r.status_code == 200 else None
        if item:
            ol_subjects = [s.get('name') if isinstance(s, dict) else s for s in (item.get('subjects') or [])]
        report["openlibrary"] = {
            "http_status": r.status_code,
            "found": bool(item),
            "title": (item or {}).get("title"),
            "raw_subjects": ol_subjects,
            "extracted_genres": extract_genres(ol_subjects),
        }
    except (requests.RequestException, ValueError) as e:
        report["openlibrary"] = {"error": str(e)}

    # --- Google Books, unfiltered so quota errors are visible ---
    params = {'q': f'isbn:{cleaned}', 'maxResults': 1, 'country': 'US'}
    if GOOGLE_BOOKS_API_KEY:
        params['key'] = GOOGLE_BOOKS_API_KEY
    try:
        r = requests.get(GOOGLE_BOOKS_URL, params=params, timeout=8)
        payload = r.json() if r.content else {}
        items = payload.get('items') or []
        item = items[0] if items else None
        info = (item.get('volumeInfo') or {}) if item else {}
        detail_categories = _google_categories_for(item)
        google: dict = {
            "http_status": r.status_code,
            "found": bool(items),
            "volume_id": (item or {}).get("id"),
            "title": info.get("title"),
            "authors": info.get("authors"),
            # What the search endpoint returned, versus the fuller per-volume
            # record. These differing is the normal case, not a fault.
            "search_categories": info.get("categories") or [],
            "detail_categories": detail_categories,
            "extracted_genres": extract_genres(detail_categories),
            "image_links": list((info.get('imageLinks') or {}).keys()),
        }
        if r.status_code != 200:
            google["error"] = (payload.get('error') or {}).get('message') or r.text[:200]

        # Mirror the real path: when the ISBN is not indexed, the sibling search
        # falls back to the title and author we already hold for the book.
        sibling_title = info.get('title') or clean(title) or (report.get("openlibrary") or {}).get("title")
        sibling_author = (info.get('authors') or [None])[0] or clean(author)
        if sibling_author and ',' in sibling_author:
            sibling_author = sibling_author.split(',')[0].strip()
        google["sibling_search"] = {"title": sibling_title, "author": sibling_author}
        if sibling_title:
            siblings = _google_sibling_volumes(sibling_title, sibling_author)
            google["siblings"] = [
                {"title": (s.get("volumeInfo") or {}).get("title"),
                 "authors": (s.get("volumeInfo") or {}).get("authors"),
                 "search_categories": (s.get("volumeInfo") or {}).get("categories") or [],
                 "detail_categories": _google_categories_for(s)}
                for s in siblings[:5]
            ]
            google["sibling_genres"] = _google_sibling_categories(sibling_title, sibling_author)
        report["google"] = google
    except (requests.RequestException, ValueError) as e:
        report["google"] = {"error": str(e)}

    report["final_tags"] = _fetch_genres(
        None, cleaned,
        clean(title) or (report.get("google") or {}).get("title") or (report.get("openlibrary") or {}).get("title"),
        clean(author) or ", ".join((report.get("google") or {}).get("authors") or []) or None)
    return report
