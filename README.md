# Book Library

A small self-hosted web app for cataloguing a personal book collection. Runs
anywhere Docker does — it was built to live on a Synology NAS.

- Add books manually, or look them up on [OpenLibrary](https://openlibrary.org)
  by title/author and pick the exact edition (cover, publisher, publish date,
  page count) or by ISBN, with [Google Books](https://books.google.com) as a
  fallback for anything OpenLibrary does not know about.
- Browse, search, edit and delete your library from the browser, with undo.
- Sort the library by title, author, date added or shelf location.
- Record where each book physically lives, on shelves you define yourself.
- Tags: genres extracted from OpenLibrary, editable inline, with a tag filter.
- Format: the binding — Hardcover, Paperback and so on — taken from OpenLibrary
  per book, editable by hand, and filterable, including "no format" for the
  books still missing one.
- Cover images are downloaded from OpenLibrary and stored in the database, with
  a manual lookup/upload button for books that are missing one.
- Bulk import from CSV.
- JWT authentication, with accounts created server side (no public signup).

## Stack

| Part     | Tech                                        |
| -------- | ------------------------------------------- |
| Backend  | FastAPI + SQLite, JWT auth (`pbkdf2_sha256`) |
| Frontend | React (Vite), served by nginx                |
| Deploy   | Docker Compose                               |

## Quick start

```bash
git clone https://github.com/<you>/booklib.git
cd booklib
cp .env.example .env          # then edit it — at minimum set SECRET_KEY
docker compose up -d --build
```

Create your first user (there is no public registration endpoint):

```bash
docker compose exec -w /app backend python create_user.py alice s3cret
```

Then open <http://localhost:3006> and log in.

## Managing users

There is no public registration endpoint; accounts are managed with the scripts
in `backend/`. They only need `sqlite3` (stdlib) and `passlib`, so you can run
them straight on the host next to `books.db` or inside the container.

```bash
# on the server, from the directory holding books.db
sudo python3 create_user.py alice s3cret     # add a user
sudo python3 list_users.py                   # list accounts
sudo python3 change_password.py alice        # prompts twice, hidden
sudo python3 change_password.py alice s3cret # or pass it inline

# or inside the container
docker compose exec -w /app backend python list_users.py
```

`list_users.py` and `change_password.py` accept `--db PATH` (or the `BOOKLIB_DB`
environment variable) when `books.db` is not in the current directory. Changing
a password does not invalidate tokens that were already issued; those expire on
their own after 8 hours.

## Configuration

All settings live in `.env` (never committed — see `.env.example`).

| Variable         | Purpose                                                        |
| ---------------- | -------------------------------------------------------------- |
| `SECRET_KEY`     | Signs JWT access tokens. **Generate your own.**                 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime (default `480`, i.e. 8 hours)       |
| `GOOGLE_BOOKS_API_KEY` | Optional key for the Google Books fallback (recommended) |
| `GOOGLE_BOOKS_ENABLED` | Set to `false` to disable the Google Books fallback |
| `DEFAULT_SHELF_COLUMNS` / `DEFAULT_SHELF_ROWS` | Size of the shelf seeded on a new database (default `6` / `8`) |
| `BACKEND_PORT`   | Host port for the API (default `8882`)                          |
| `FRONTEND_PORT`  | Host port for the UI (default `3006`)                           |
| `VITE_API_BASE`  | Backend URL baked into the frontend bundle at build time        |

Generate a secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Pointing the frontend at the backend

`VITE_API_BASE` is compiled into the JavaScript bundle, so **you must rebuild
the frontend after changing it**:

```bash
docker compose build frontend && docker compose up -d frontend
```

Two supported setups:

1. **Separate hostname (recommended).** Give the API its own subdomain in your
   reverse proxy (e.g. `api.example.com` → `backend:8882`, `books.example.com`
   → `frontend:80`) and set `VITE_API_BASE=https://api.example.com`.
2. **Same origin.** If your proxy serves both the UI and the API from one
   hostname, leave `VITE_API_BASE` empty — the app then calls its own origin.

Once you have a fixed public origin, tighten CORS in
`backend/app/main.py` (`origins = ["*"]`) to just that domain.

## Data

The library lives in a SQLite file at `backend/books.db`, bind mounted from the
host so it survives rebuilds. It is gitignored. Back it up by copying the file.

If the API returns `attempt to write a readonly database`, the file or its
directory is not writable by the container user:

```bash
docker compose exec -w / backend sh -c 'id -u; id -g'
sudo chown -R <uid>:<gid> ./backend
```

## CSV import

Upload a CSV with the headers `title,author,isbn,olid,google_id,tags,notes` (only `title`
is required). Separate tags with `;` or `|`, since `,` is the column separator.
Rows whose ISBN already exists are skipped.

## API

All routes except `/health` and `/token` require an
`Authorization: Bearer <token>` header.

| Method   | Path              | Description                              |
| -------- | ----------------- | ---------------------------------------- |
| `GET`    | `/health`         | Liveness check                           |
| `POST`   | `/token`          | Exchange username/password for a JWT     |
| `GET`    | `/books`          | List books, optional `?q=` search (title, author, ISBN, OLID, notes) + `?sort=`/`?dir=` + `?tags=`/`?match=` + `?format=`/`?has_format=` |
| `POST`   | `/books`          | Add a book                               |
| `GET`    | `/books/{id}`     | Fetch one book                           |
| `PUT`    | `/books/{id}`     | Update a book                            |
| `DELETE` | `/books/{id}`     | Delete a book                            |
| `POST`   | `/books/import`   | Bulk import a CSV file                   |
| `GET`    | `/books/{id}/cover` | Stored cover image (accepts `?token=`) |
| `POST`   | `/books/{id}/cover` | Upload a cover image                   |
| `POST`   | `/books/{id}/cover/lookup` | Find a cover on OpenLibrary   |
| `DELETE` | `/books/{id}/cover` | Remove the stored cover                |
| `POST`   | `/books/{id}/olid/lookup` | Resolve the OLID from the ISBN |
| `POST`   | `/books/{id}/google/lookup` | Resolve the Google Books volume id |
| `POST`   | `/books/{id}/tags/lookup` | Add genre tags, `?replace=true` to swap |
| `POST`   | `/books/{id}/format/lookup` | Fetch the binding from OpenLibrary |
| `GET`    | `/tags`           | Tags in use, with book counts            |
| `GET`    | `/formats`        | Known bindings, and any others in use    |
| `GET`    | `/shelves`        | Shelves with their sizes and book counts |
| `POST`   | `/shelves`        | Add a shelf                              |
| `PUT`    | `/shelves/{id}`   | Rename or resize a shelf                 |
| `DELETE` | `/shelves/{id}`   | Delete a shelf, unplacing its books      |
| `GET`    | `/shelves/{id}/layout` | A shelf plus what is in each slot   |
| `PUT`    | `/books/{id}/location` | Set or clear where a book lives     |
| `GET`    | `/search`         | OpenLibrary search by `title`/`author`   |
| `GET`    | `/lookup/{isbn}`  | OpenLibrary lookup by ISBN               |
| `GET`    | `/edition/{olid}` | OpenLibrary edition detail by OLID       |
| `GET`    | `/diagnostics/{isbn}` | What each catalogue holds for an ISBN |
| `GET`    | `/diagnostics/search` | The Google queries a search would run |

`/search` returns English editions by default; pass
`include_all_languages=true` to include translations.

`/books` accepts `sort=title|author|added` (default `added`) and `dir=asc|desc`
(default `desc`). Books added before the `created_at` column existed fall back to
insert order. `created_at` is editable: send a `YYYY-MM-DD` date (or a full
timestamp) on `POST`/`PUT /books`; omit it on `PUT` to leave the stored value
untouched. Dates are stored and displayed in UTC.

Cover images are stored as BLOBs in the `cover` column of the `books` table, with
the content type in `cover_mime`. Adding a book fetches its cover automatically
(from the chosen edition, its OLID, or its ISBN, then Google Books); for books
that have none, use the **Lookup** button in the Manage tab, paste a direct image
address, or **Upload** your own file. List responses never carry the image data —
they return a `has_cover` flag, and the bytes are served on demand from
`GET /books/{id}/cover`.

Books have a dedicated `olid` column for the OpenLibrary edition id. It is filled
in automatically when you add from search results and is optional everywhere
else; the Manage tab has a **Lookup** button that resolves it from the ISBN.
Existing rows that stored `OLID:OL12345M` in their notes are migrated into the
column on first start.

There is a matching `google_id` column for the Google Books volume id (e.g.
`otCEEQAAQBAJ`). Storing it is worthwhile because a tag or cover lookup would
otherwise have to *search* for the volume before it can fetch it — with the id on
hand that becomes a single request, and it pins the book to one exact edition
instead of relying on a title match. Both ids share a **Sources** column in the
Manage tab, each linking to its catalogue page, with a lookup button when
missing. A stored id that no longer resolves falls back to searching, so a stale
value degrades rather than breaks.

Tags come from OpenLibrary subjects and live in their own `tags` / `book_tags`
tables. Rather than storing subjects verbatim (they include awards, bestseller
lists, plot nouns and scan housekeeping), the backend extracts **genres**: it
prefers BISAC-style subjects such as `FICTION / Science Fiction / Hard Science
Fiction` — each segment becomes a tag — and falls back to a curated genre
vocabulary for books that have none. Junk like `nyt:*`, `award:hugo_award=1966`,
"New York Times reviewed" and "Large type books" is dropped, and a lookup adds at
most 8 tags. One tag is derived rather than read: a book that comes back both
*Fantasy* and *Romance* also gets *Romantasy*, since OpenLibrary's own
`romantasy` subject is applied to only a few hundred works.

Adding a book fetches tags automatically. In the Manage tab **Lookup tags** adds
them to an untagged book, while **Refresh tags** replaces what is there (so tags
stored by an older version get cleaned up); **Refresh all tags** does that for
every book currently listed. Tags are editable inline as a comma-separated list,
long lists collapse to the first 3 with a `… +N` toggle, and the tag cloud above
the table filters the library (select several and tick *Match all selected* to
narrow instead of widen).

## Shelf locations

Every book can record where it physically lives: which shelf, and which slot on
it. Column 1, row 1 is the top left.

Shelves are **data, not constants** — name and size live in a `shelves` table and
are edited on the **Bookshelf** tab, so a second bookcase or a different size never
needs a redeploy. One 6×8 shelf is seeded on first start; change
`DEFAULT_SHELF_COLUMNS` / `DEFAULT_SHELF_ROWS` to seed a different size.

Books carry `shelf_id`, `shelf_column` and `shelf_row`, all nullable and all set
together. The **Bookshelf** tab draws each shelf and lists what is in a slot when
you click it, which is the quickest way to see what is where. Books can be
**dragged onto a slot** to move them, and with more than one shelf, dragging over
a shelf's name switches to it so a book can be moved between shelves in one
gesture. Drag and drop is a mouse affordance — on a touch screen use the
**Move** button, which opens the same picker. In the Manage tab
the **Location** column opens a picker: click a slot on the shelf, or type the
numbers. Slots already holding a book are shaded, and the picker lists what is
there before you save.

A few behaviours worth knowing:

- **Two books may share a slot.** Thin books pair up and shelves get reshuffled,
  so this is an ordinary situation rather than an error. The picker lists what is
  already in the slot for information; nothing blocks the save.
- **Shrinking a shelf is refused** while books sit in the slots that would be cut
  off, and says how many. Losing a position silently is worse than being made to
  move the books first.
- **Deleting a shelf unplaces its books**, never deletes them. The confirmation
  says how many are affected.
- **An edit that never mentions the location leaves it alone.** `PUT /books/{id}`
  distinguishes an absent location from an explicit null, so renaming a book from
  the table cannot quietly unshelve it.

`/books` gains `?shelf_id=` and `?placed=false` (books with no location yet, for
working through a backlog), plus `sort=location`, which puts unplaced books last.

## Google Books fallback

OpenLibrary is the primary source, but its coverage is patchy — newer and
self-published titles often have no subjects, no cover, or no record at all.
Google Books is consulted as a fallback in four places:

| Feature | Order |
| ------- | ----- |
| Tags | OpenLibrary edition subjects + parent work subjects, **merged with** Google `categories` |
| Covers | explicit URL → OpenLibrary by OLID → by ISBN → Google `imageLinks` |
| ISBN lookup | OpenLibrary → Google volume metadata |
| Format | OpenLibrary only — Google's `printType` is `BOOK`/`MAGAZINE` and says nothing about binding |
| Title/author search | OpenLibrary results, **merged with** Google hits it did not already return |

A stored `google_id` short-circuits the Google half of all of these: the volume
is fetched directly instead of being searched for first.

Search and tags merge rather than fall back, because OpenLibrary answering at all
is not the same as it answering usefully: a search for a recent title often
returns unrelated works, which would stop a pure fallback from ever running.
Google hits are deduplicated against the OpenLibrary ones by ISBN and by title,
and the OpenLibrary record wins a tie because it carries real edition data.
Google entries are marked *via Google Books* in the results.

Google's own categories can be as thin as a single `Fiction` even for a book it
knows well, so when an ISBN's record is that sparse the backend also checks other
Google volumes with the same title and merges their categories in.

Google's categories are already BISAC-style (`Fiction / Science Fiction / Hard
Science Fiction`), so they feed straight into the same genre extractor. One
wrinkle: the **search** endpoint collapses `categories` to a single top-level
subject, so a book whose real subjects are `Fiction / Fantasy / Urban` and
`Fiction / Fantasy / Historical` comes back as just `["Fiction"]`. The backend
therefore follows up with a per-volume `GET /volumes/{id}`, which returns the
full list. Google cover URLs are upgraded to `https` and stripped of the
`edge=curl` page-curl effect, and the largest available size is preferred.
Search results from Google are reshaped to match the OpenLibrary response, so
the edition picker renders them identically.

Set `GOOGLE_BOOKS_API_KEY` in `.env`. It works without one, but Google shares a
per-IP anonymous quota that is frequently already exhausted (HTTP 429), so the
fallback will be unreliable. Enable the *Books API* in a Google Cloud project and
create an API key. Set `GOOGLE_BOOKS_ENABLED=false` to turn the fallback off.

Results are cached in-process for 10 minutes, including misses, so adding a book
does not hit Google three times for the same ISBN.

### Diagnosing a book with missing tags

Both diagnostic endpoints accept `?token=<jwt>` as well as an `Authorization`
header, so you can paste the URL straight into a browser. Get a token with:

```bash
curl -s -X POST http://localhost:8882/token \
     -d 'username=YOURUSER&password=YOURPASS' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'
```

`GET /diagnostics/{isbn}` reports what each catalogue actually holds and what the
genre extractor made of it, which separates "the source has nothing" from "we
dropped it". Pass `?title=` and `?author=` to mirror what a real lookup would
use when Google has not indexed the ISBN:

```
http://localhost:8882/diagnostics/9781668068168?title=Songs+of+the+Dead&author=Brandon+Sanderson&token=YOUR_TOKEN
```

```jsonc
{
  "google_api_key_set": true,
  "openlibrary": { "found": false, "raw_subjects": [], "extracted_genres": [] },
  "google": {
    "http_status": 200,
    "found": true,
    "volume_id": "otCEEQAAQBAJ",
    "search_categories": ["Fiction"],                    // what the search endpoint gives
    "detail_categories": ["Fiction / Fantasy / Urban"],  // what GET /volumes/{id} gives
    "extracted_genres": ["Fiction", "Fantasy", "Urban"],
    "sibling_search": { "title": "Songs of the Dead", "author": "Brandon Sanderson" },
    "siblings": [],
    "sibling_genres": []
  },
  "final_tags": ["Fiction", "Fantasy", "Urban"]
}
```

Read it top down:

| Field | Meaning |
| ----- | ------- |
| `google.http_status: 429` | Quota exhausted — set `GOOGLE_BOOKS_API_KEY` |
| `google_api_key_set: false` | The key is not reaching the container |
| `google.found: false` | Google has not indexed that ISBN; the sibling search by title takes over |
| `search_categories` thin, `detail_categories` full | Normal — the per-volume fetch is doing its job |
| `siblings: []` | No Google volume matched the title and author |
| `detail_categories: []` everywhere | Google genuinely holds no genres for this book |
| `sibling_genres` full but `final_tags` thin | The bug is in our merging, not the source |

`GET /diagnostics/search?title=…&author=…` does the same for searching: it lists
the exact Google queries that would run, in order, with the titles and categories
each returned and which one was used.

The diagnostics endpoints clear the lookup cache before running, so they always
report live data.

Interactive docs are at `http://localhost:8882/docs`.

## Local development

```bash
# Backend
cd backend
pip install -r requirements.txt
SECRET_KEY=dev uvicorn app.main:app --reload --port 8882

# Frontend
cd frontend
npm install
VITE_API_BASE=http://localhost:8882 npm run dev
```

## Security notes

- `.env` and `backend/books.db` are gitignored — keep it that way.
- Back up `SECRET_KEY`; changing it invalidates every issued token.
- There is no public registration; users are created with `create_user.py`.
- Put the app behind HTTPS before exposing it to the internet.
