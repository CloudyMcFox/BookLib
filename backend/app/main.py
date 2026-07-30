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
    notes: Optional[str] = None
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
conn.commit()

# Never SELECT * from books: the cover BLOB would be loaded for every row.
BOOK_COLUMNS = "id, title, author, isbn, olid, notes, created_at, length(cover) AS cover_size"

# Whitelisted ORDER BY clauses, so the sort parameter can never be injected.
# Books added before created_at existed sort by id, which preserves insert order.
SORT_CLAUSES = {
    'title': "title COLLATE NOCASE {dir}, id {dir}",
    'author': "CASE WHEN author IS NULL OR author='' THEN 1 ELSE 0 END, author COLLATE NOCASE {dir}, id {dir}",
    'added': "COALESCE(created_at,'') {dir}, id {dir}",
}
DEFAULT_SORT = 'added'


def order_by(sort: Optional[str], direction: Optional[str]) -> str:
    clause = SORT_CLAUSES.get((sort or DEFAULT_SORT).lower(), SORT_CLAUSES[DEFAULT_SORT])
    dir_sql = 'ASC' if (direction or '').lower() == 'asc' else 'DESC'
    return " ORDER BY " + clause.format(dir=dir_sql)


def now_iso() -> str:
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


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
    r'|reading level|specimens|manual for civilization', re.I)
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

    result = out[:MAX_LOOKUP_TAGS]
    # OpenLibrary barely uses the 'romantasy' subject, so derive it: a book
    # tagged both Fantasy and Romance is the genre by definition.
    lowered = {t.lower() for t in result}
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


def _fetch_genres(olid: Optional[str], isbn: Optional[str]) -> List[str]:
    """Fetch genre tags for an edition from OpenLibrary subjects. Tries the
    edition first, then falls back to the subjects on its parent work."""
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


def _store_cover(book_id: int, isbn: Optional[str], olid: Optional[str], cover_url: Optional[str] = None, only_cover_url: bool = False) -> bool:
    """Best effort: find a cover for the book and save it in the database."""
    for url in _cover_candidates(isbn, olid, cover_url, only_cover_url):
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
               current_user: dict = Depends(get_current_user)):
    """List books. Optional ?q= search, ?sort=title|author|added, ?dir=asc|desc,
    and ?tags=a,b with ?match=any|all (default any)."""
    order = order_by(sort, dir)
    where = []
    params: List = []
    if q:
        pattern = f"%{q.strip()}%"
        where.append("(title LIKE ? OR author LIKE ? OR isbn LIKE ? OR olid LIKE ?)")
        params.extend([pattern, pattern, pattern, pattern])

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
    values, except created_at which is only touched when a value is supplied."""
    title = clean(b.title)
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    added = clean_timestamp(b.created_at)
    try:
        conn.execute("UPDATE books SET title=?, author=?, isbn=?, olid=?, notes=? WHERE id=?", (title, clean(b.author), clean(b.isbn), clean_olid(b.olid), clean(b.notes), book_id))
        if added:
            conn.execute("UPDATE books SET created_at=? WHERE id=?", (added, book_id))
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if clean(b.cover_url):
        _store_cover(book_id, clean(b.isbn), clean_olid(b.olid), clean(b.cover_url))
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
    # Allow an explicit added date (e.g. when restoring a deleted book via undo).
    b.created_at = clean_timestamp(b.created_at) or now_iso()
    try:
        cur = conn.execute("INSERT INTO books (title, author, isbn, olid, notes, created_at) VALUES (?,?,?,?,?,?)", (b.title, b.author, b.isbn, b.olid, b.notes, b.created_at))
        conn.commit()
        b.id = cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Grab a cover while we are adding, so most books never need a manual lookup.
    b.has_cover = _store_cover(b.id, b.isbn, b.olid, clean(b.cover_url))
    b.cover_url = None
    # Tags: use what the caller supplied, otherwise fetch OpenLibrary subjects.
    supplied = normalize_tags(b.tags)
    b.tags = set_book_tags(b.id, supplied) if supplied else add_book_tags(b.id, _fetch_genres(b.olid, b.isbn))
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
    """Find a cover for an existing book (by explicit URL, stored OLID, or ISBN)."""
    cur = conn.execute("SELECT id, isbn, olid FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    supplied = clean(cover_url)
    if not _store_cover(book_id, row['isbn'], row['olid'], supplied, only_cover_url=bool(supplied)):
        if supplied:
            raise HTTPException(status_code=400, detail="That address did not return a usable image. It needs to be a direct link to an image file under 5 MB.")
        raise HTTPException(status_code=404, detail="No cover found for this book")
    cur = conn.execute(f"SELECT {BOOK_COLUMNS} FROM books WHERE id=?", (book_id,))
    return book_from_row(cur.fetchone())

@app.post("/books/{book_id}/tags/lookup", response_model=Book)
def lookup_book_tags(book_id: int, replace: bool = False, current_user: dict = Depends(get_current_user)):
    """Fetch genre tags from OpenLibrary. Adds to the book's tags by default;
    pass replace=true to swap them out (used by the Refresh button, so stale or
    noisy tags do not stick around)."""
    cur = conn.execute("SELECT id, isbn, olid FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if not clean(row['isbn']) and not clean(row['olid']):
        raise HTTPException(status_code=400, detail="This book needs an ISBN or OLID to look up tags")
    found = _fetch_genres(row['olid'], row['isbn'])
    if not found:
        raise HTTPException(status_code=404, detail="No tags found on OpenLibrary for this book")
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

@app.post("/books/import")
async def import_books(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    """Accepts a CSV file with headers: title,author,isbn,olid,tags,notes
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
        notes = clean(row.get('notes') or row.get('Notes'))
        tags = normalize_tags(re.split(r'[;|]', row.get('tags') or row.get('Tags') or ''))
        try:
            cur = conn.execute("INSERT OR IGNORE INTO books (title, author, isbn, olid, notes, created_at) VALUES (?,?,?,?,?,?)", (title, author, isbn, olid, notes, added_at))
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


@app.get("/search")
def search_openlibrary(title: Optional[str] = None, author: Optional[str] = None, q: Optional[str] = None,
                       include_all_languages: bool = False,
                       current_user: dict = Depends(get_current_user)):
    """Search OpenLibrary by title and/or author and return candidate works with
    their editions. Editions default to English only; pass include_all_languages=true
    to keep translations as well."""
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
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Search failed")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Search failed")

    results = []
    for doc in r.json().get('docs', [])[:10]:
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
    return results

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
    """Lookup ISBN via OpenLibrary and return basic metadata."""
    isbn = isbn.strip()
    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    try:
        r = requests.get(url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Lookup failed")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Lookup failed")
    data = r.json()
    key = f"ISBN:{isbn}"
    if key not in data:
        return {}
    item = data[key]
    return {
        "title": item.get("title"),
        "authors": [a.get("name") for a in item.get("authors", [])],
        "publish_date": item.get("publish_date"),
        "olid": clean_olid(item.get("key")) or _lookup_olid_by_isbn(isbn),
    }
