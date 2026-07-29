from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
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
from fastapi.middleware.cors import CORSMiddleware

# DB and auth config
import os
DB_PATH = "books.db"
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-to-a-secure-random-string")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8

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


class Book(BaseModel):
    id: Optional[int] = None
    title: str
    author: Optional[str] = None
    isbn: Optional[str] = None
    notes: Optional[str] = None

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
conn.commit()

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
def list_books(q: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    # Optional search over title, author, isbn
    if q:
        pattern = f"%{q.strip()}%"
        cur = conn.execute("SELECT * FROM books WHERE title LIKE ? OR author LIKE ? OR isbn LIKE ?", (pattern, pattern, pattern))
    else:
        cur = conn.execute("SELECT * FROM books")
    rows = cur.fetchall()
    return [Book(**dict(r)) for r in rows]

@app.delete("/books/{book_id}")
def delete_book(book_id: int, current_user: dict = Depends(get_current_user)):
    cur = conn.execute("DELETE FROM books WHERE id=?", (book_id,))
    conn.commit()
    return {"deleted": cur.rowcount}

@app.put("/books/{book_id}", response_model=Book)
def update_book(book_id: int, b: Book, current_user: dict = Depends(get_current_user)):
    """Update an existing book. All fields in the payload will overwrite stored values."""
    title = clean(b.title)
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    try:
        conn.execute("UPDATE books SET title=?, author=?, isbn=?, notes=? WHERE id=?", (title, clean(b.author), clean(b.isbn), clean(b.notes), book_id))
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cur = conn.execute("SELECT * FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return Book(**dict(row))

@app.post("/books", response_model=Book)
def add_book(b: Book, current_user: dict = Depends(get_current_user)):
    title = clean(b.title)
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    b.title, b.author, b.isbn, b.notes = title, clean(b.author), clean(b.isbn), clean(b.notes)
    try:
        cur = conn.execute("INSERT INTO books (title, author, isbn, notes) VALUES (?,?,?,?)", (b.title, b.author, b.isbn, b.notes))
        conn.commit()
        b.id = cur.lastrowid
        return b
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/books/{book_id}", response_model=Book)
def get_book(book_id: int, current_user: dict = Depends(get_current_user)):
    cur = conn.execute("SELECT * FROM books WHERE id=?", (book_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return Book(**dict(row))

@app.post("/books/import")
async def import_books(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    """Accepts a CSV file with headers: title,author,isbn,notes"""
    content = (await file.read()).decode('utf-8')
    reader = csv.DictReader(io.StringIO(content))
    inserted = 0
    for row in reader:
        title = clean(row.get('title') or row.get('Title'))
        if not title:
            continue
        author = clean(row.get('author') or row.get('Author'))
        isbn = clean(row.get('isbn') or row.get('ISBN'))
        notes = clean(row.get('notes') or row.get('Notes'))
        try:
            conn.execute("INSERT OR IGNORE INTO books (title, author, isbn, notes) VALUES (?,?,?,?)", (title, author, isbn, notes))
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
        "publish_date": item.get("publish_date")
    }
