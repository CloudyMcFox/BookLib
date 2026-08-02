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

# initialize DB
conn = get_conn()
conn.execute("""CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT,
    isbn TEXT UNIQUE,
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
conn.execute("CREATE INDEX IF NOT EXISTS idx_books_shelf ON books(shelf_id)")

# Seed one shelf so the feature works out of the box rather than presenting an
# empty picker on first use.
if not conn.execute("SELECT 1 FROM shelves LIMIT 1").fetchone():
    conn.execute("INSERT INTO shelves (name, columns, rows, sort_order, created_at) VALUES (?,?,?,?,?)",
                 ("Bookshelf", DEFAULT_SHELF_COLUMNS, DEFAULT_SHELF_ROWS, 0,
                  datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')))
conn.commit()

# Never SELECT * from books: the cover BLOB would be loaded for every row.
BOOK_COLUMNS = "id, title, author, isbn, olid, google_id, notes, created_at, shelf_id, shelf_column, shelf_row, length(cover) AS cover_size"

# Whitelisted ORDER BY clauses, so the sort parameter can never be injected.
# Books added before created_at existed sort by id, which preserves insert order.
SORT_CLAUSES = {
    'title': "title COLLATE NOCASE {dir}, id {dir}",
    'author': "CASE WHEN author IS NULL OR author='' THEN 1 ELSE 0 END, author COLLATE NOCASE {dir}, id {dir}",
    'added': "COALESCE(created_at,'') {dir}, id {dir}",
    # Unplaced books sort last either way, so the list does not open on a block
    # of blanks.
    'location': "CASE WHEN shelf_id IS NULL THEN 1 ELSE 0 END, shelf_id {dir}, shelf_row {dir}, shelf_column {dir}, id {dir}",
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
    user = get_user(username)
    if user is None:
        raise credentials_exception
    return user


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
    access_token = create_access_token(data={"sub": user['username']})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/books", response_model=List[Book])
def list_books(q: Optional[str] = None, sort: Optional[str] = None, dir: Optional[str] = None,
               tags: Optional[str] = None, match: Optional[str] = None,
               shelf_id: Optional[int] = None, placed: Optional[bool] = None,
               current_user: dict = Depends(get_current_user)):
    """List books. Optional ?q= search, ?sort=title|author|added, ?dir=asc|desc,
    ?tags=a,b with ?match=any|all (default any), ?shelf_id= to limit to one
    shelf, and ?placed=false to find books with no location yet."""
    order = order_by(sort, dir)
    where = []
    params: List = []
    if q:
        pattern = f"%{q.strip()}%"
        where.append("(title LIKE ? OR author LIKE ? OR isbn LIKE ? OR olid LIKE ?)")
        params.extend([pattern, pattern, pattern, pattern])

    if shelf_id is not None:
        where.append("shelf_id = ?")
        params.append(shelf_id)

    if placed is not None:
        where.append("shelf_id IS NOT NULL" if placed else "shelf_id IS NULL")

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
def add_shelf(s: Shelf, current_user: dict = Depends(get_current_user)):
    name = clean(s.name)
    if not name:
        raise HTTPException(status_code=400, detail="A shelf needs a name")
    validate_shelf_size(s.columns, s.rows)
    cur = conn.execute("INSERT INTO shelves (name, columns, rows, sort_order, created_at) VALUES (?,?,?,?,?)",
                       (name, s.columns, s.rows, s.sort_order, now_iso()))
    conn.commit()
    return shelf_row_to_model(get_shelf(cur.lastrowid), 0)


@app.put("/shelves/{shelf_id}", response_model=Shelf)
def update_shelf(shelf_id: int, s: Shelf, current_user: dict = Depends(get_current_user)):
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
def delete_shelf(shelf_id: int, current_user: dict = Depends(get_current_user)):
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
def delete_book(book_id: int, current_user: dict = Depends(get_current_user)):
    cur = conn.execute("DELETE FROM books WHERE id=?", (book_id,))
    conn.execute("DELETE FROM book_tags WHERE book_id=?", (book_id,))
    prune_orphan_tags()
    conn.commit()
    return {"deleted": cur.rowcount}

@app.put("/books/{book_id}", response_model=Book)
def update_book(book_id: int, b: Book, current_user: dict = Depends(get_current_user)):
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
def add_book(b: Book, current_user: dict = Depends(get_current_user)):
    title = clean(b.title)
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    b.title, b.author, b.isbn, b.notes = title, clean(b.author), clean(b.isbn), clean(b.notes)
    b.olid = clean_olid(b.olid)
    b.google_id = clean_google_id(b.google_id)
    # Allow an explicit added date (e.g. when restoring a deleted book via undo).
    b.created_at = clean_timestamp(b.created_at) or now_iso()
    b.shelf_id, b.shelf_column, b.shelf_row = resolve_location(b.shelf_id, b.shelf_column, b.shelf_row)
    try:
        cur = conn.execute("INSERT INTO books (title, author, isbn, olid, google_id, notes, created_at, shelf_id, shelf_column, shelf_row) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (b.title, b.author, b.isbn, b.olid, b.google_id, b.notes, b.created_at,
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
    return b

@app.get("/books/{book_id}", response_model=Book)
def get_book(book_id: int, current_user: dict = Depends(get_current_user)):
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
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
def lookup_book_cover(book_id: int, cover_url: Optional[str] = None, current_user: dict = Depends(get_current_user)):
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
def lookup_book_tags(book_id: int, replace: bool = False, current_user: dict = Depends(get_current_user)):
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
def lookup_book_olid(book_id: int, current_user: dict = Depends(get_current_user)):
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

@app.post("/books/{book_id}/google/lookup", response_model=Book)
def lookup_book_google_id(book_id: int, current_user: dict = Depends(get_current_user)):
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
async def upload_book_cover(book_id: int, file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
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
def delete_book_cover(book_id: int, current_user: dict = Depends(get_current_user)):
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
def set_book_location(book_id: int, loc: BookLocation, current_user: dict = Depends(get_current_user)):
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
async def import_books(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    """Accepts a CSV file with headers: title,author,isbn,olid,google_id,tags,notes
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
        tags = normalize_tags(re.split(r'[;|]', row.get('tags') or row.get('Tags') or ''))
        try:
            cur = conn.execute("INSERT OR IGNORE INTO books (title, author, isbn, olid, google_id, notes, created_at) VALUES (?,?,?,?,?,?,?)", (title, author, isbn, olid, google_id, notes, added_at))
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

