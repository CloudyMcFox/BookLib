# Book Library

A small self-hosted web app for cataloguing a personal book collection. Runs
anywhere Docker does — it was built to live on a Synology NAS.

- Add books manually, or look them up on [OpenLibrary](https://openlibrary.org)
  by title/author and pick the exact edition (cover, publisher, publish date,
  page count) or by ISBN.
- Browse, search, edit and delete your library from the browser, with undo.
- Sort the library by title, author or date added.
- Tags: genres extracted from OpenLibrary, editable inline, with a tag filter.
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

Upload a CSV with the headers `title,author,isbn,olid,tags,notes` (only `title`
is required). Separate tags with `;` or `|`, since `,` is the column separator.
Rows whose ISBN already exists are skipped.

## API

All routes except `/health` and `/token` require an
`Authorization: Bearer <token>` header.

| Method   | Path              | Description                              |
| -------- | ----------------- | ---------------------------------------- |
| `GET`    | `/health`         | Liveness check                           |
| `POST`   | `/token`          | Exchange username/password for a JWT     |
| `GET`    | `/books`          | List books, optional `?q=` search + `?sort=`/`?dir=` + `?tags=`/`?match=` |
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
| `POST`   | `/books/{id}/tags/lookup` | Add genre tags, `?replace=true` to swap |
| `GET`    | `/tags`           | Tags in use, with book counts            |
| `GET`    | `/search`         | OpenLibrary search by `title`/`author`   |
| `GET`    | `/lookup/{isbn}`  | OpenLibrary lookup by ISBN               |
| `GET`    | `/edition/{olid}` | OpenLibrary edition detail by OLID       |

`/search` returns English editions by default; pass
`include_all_languages=true` to include translations.

`/books` accepts `sort=title|author|added` (default `added`) and `dir=asc|desc`
(default `desc`). Books added before the `created_at` column existed fall back to
insert order. `created_at` is editable: send a `YYYY-MM-DD` date (or a full
timestamp) on `POST`/`PUT /books`; omit it on `PUT` to leave the stored value
untouched. Dates are stored and displayed in UTC.

Cover images are stored as BLOBs in the `books` table. Adding a book fetches its
cover automatically (from the chosen edition, its OLID, or its ISBN); for books
that have none, use the **Lookup** button in the Manage tab, or **Upload** your
own image.

Books have a dedicated `olid` column for the OpenLibrary edition id. It is filled
in automatically when you add from search results and is optional everywhere
else; the Manage tab has a **Lookup** button that resolves it from the ISBN.
Existing rows that stored `OLID:OL12345M` in their notes are migrated into the
column on first start.

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
