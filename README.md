# Book Library

A small self-hosted web app for cataloguing a personal book collection. Runs
anywhere Docker does — it was built to live on a Synology NAS.

- Add books manually, or look them up on [OpenLibrary](https://openlibrary.org)
  by title/author and pick the exact edition (cover, publisher, publish date,
  page count) or by ISBN, with [Google Books](https://books.google.com) as a
  fallback for anything OpenLibrary does not know about.
- Browse, search, edit and delete your library from the browser, with undo.
- Check books out under a borrower's name and check them back in; guests can
  check books out without gaining catalogue-editing or return access.
- Sort the library by title, author, series, date added or shelf location.
- Record where each book physically lives, on shelves you define yourself.
- Tags: genres extracted from OpenLibrary, editable inline, with a tag filter.
- Series: the series and volume number, read out of OpenLibrary's series field
  or the published title, kept as separate fields so a series sorts in reading
  order and shown as "The Stormlight Archive (3)". Click a series in the
  library table to show every book in it.
- Descriptions from Google Books or OpenLibrary, collapsed to a one-line preview
  in the table and opened a row at a time.
- Both can be filled in across the whole listing at once from the Manage tab.
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

### Requirements

- Docker Engine or Docker Desktop with Docker Compose v2
- Python 3 available once to generate the signing secret
- A reverse proxy with HTTPS before exposing BookLib outside your machine or
  private network

```bash
git clone https://github.com/CloudyMcFox/BookLib.git
cd BookLib
cp .env.example .env
mkdir -p data
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the generated value into `SECRET_KEY` in `.env`. The backend deliberately
refuses to start with an empty, short, or example signing key.

Start the application:

```bash
docker compose up -d --build
```

Create your first user (there is no public registration endpoint):

```bash
docker compose exec backend python create_user.py alice
```

The script prompts for the password without putting it in your shell history.
Then open <http://127.0.0.1:3006> and sign in.

Confirm both the frontend and the proxied API are responding:

```bash
docker compose ps
curl http://127.0.0.1:3006/health
```

The expected health response is `{"status":"ok"}`. If a container is not
healthy, inspect it with `docker compose logs backend` or
`docker compose logs frontend`.

## Managing users

There is no public registration endpoint; accounts are managed with the scripts
in `backend/`. Running them in the container is recommended because it already
has the correct dependencies and database path.

```bash
docker compose exec backend python create_user.py alice
docker compose exec backend python list_users.py
docker compose exec backend python change_password.py alice
```

The create and password-change scripts prompt twice with hidden input. They also
accept a password as an argument for automation, but doing so may save it in
shell history. `BOOKLIB_DB` selects a different database when running the
scripts outside Compose. Changing a password does not invalidate tokens already
issued; those expire according to `ACCESS_TOKEN_EXPIRE_MINUTES`.

## Configuration

All settings live in `.env` (never committed — see `.env.example`).

| Variable         | Purpose                                                        |
| ---------------- | -------------------------------------------------------------- |
| `SECRET_KEY`     | Signs JWT access tokens. **Generate your own.**                 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime (default `480`, i.e. 8 hours)       |
| `GUEST_ACCESS_ENABLED` | Explicitly enable public guest browsing and checkout (default `false`) |
| `GUEST_TOKEN_EXPIRE_MINUTES` | Guest session lifetime (default `120`)             |
| `GOOGLE_BOOKS_API_KEY` | Optional key for the Google Books fallback (recommended) |
| `GOOGLE_BOOKS_ENABLED` | Set to `false` to disable the Google Books fallback |
| `DEFAULT_SHELF_COLUMNS` / `DEFAULT_SHELF_ROWS` | Size of the shelf seeded on a new database (default `6` / `8`) |
| `BACKEND_PORT`   | Loopback-only host port for API diagnostics (default `8882`)    |
| `FRONTEND_PORT`  | Loopback-only host port for the UI (default `3006`)             |
| `CORS_ORIGINS`   | Comma-separated origins allowed to call the API directly        |
| `VITE_API_BASE`  | Backend URL baked into the frontend bundle at build time        |

Generate a secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Pointing the frontend at the backend

The default and recommended deployment is **same origin**. Leave
`VITE_API_BASE` and `CORS_ORIGINS` empty, direct your HTTPS reverse proxy to
`127.0.0.1:3006`, and let the frontend nginx container proxy API requests to the
private backend service. The browser never needs direct access to port 8882.

For example:

```text
https://books.example.com -> http://127.0.0.1:3006
```

Both published Compose ports bind to `127.0.0.1` by default. Do not change the
backend binding to `0.0.0.0` on an Internet-facing host.

If the frontend and API must use separate public origins, set
`VITE_API_BASE=https://api.example.com` and
`CORS_ORIGINS=https://books.example.com`. Put both origins behind HTTPS.
`VITE_API_BASE` is compiled into the JavaScript bundle, so rebuild after
changing it:

```bash
docker compose build frontend && docker compose up -d frontend
```

Never use a wildcard CORS origin with credentials. List only frontend origins
you control.

## Data

The library lives in `data/books.db`, mounted into the backend container as
`/data/books.db`. The whole `data/` directory is gitignored and excluded from
Docker build contexts, so database contents cannot be copied into an image
layer.

For a simple consistent backup, briefly stop the backend before copying the
database:

```bash
docker compose stop backend
cp data/books.db /your/backup/location/books-$(date +%F).db
docker compose start backend
```

Restore only while the backend is stopped, then start it again.

### Updating an installation

Back up `data/books.db`, then update from the repository:

```bash
git pull --ff-only
docker compose up -d --build --remove-orphans
docker compose ps
curl http://127.0.0.1:3006/health
```

Compose replaces the application containers without changing `.env` or
`data/`. Review the release notes and `.env.example` before each upgrade in case
a release adds a configuration variable or a manual migration step.

For Windows-to-server deployments, `deploy.ps1` copies source while preserving
the destination's `.env` and `data/` directory:

```powershell
.\deploy.ps1 -Destination \\bookserver\srv\booklib -DryRun
.\deploy.ps1 -Destination \\bookserver\srv\booklib
```

If the API returns `attempt to write a readonly database`, the file or its
directory is not writable by the container user:

```bash
docker compose exec -w / backend sh -c 'id -u; id -g'
sudo chown -R <uid>:<gid> ./data
```

## CSV import

Upload a CSV with the headers `title,author,isbn,olid,google_id,tags,notes,series,series_index,description`
(only `title` is required). Separate tags with `;` or `|`, since `,` is the column
separator. A `series` written as "Discworld #5" is split into the name and the
number, so `series_index` only needs supplying when the name does not carry it.
Each CSV row is imported as its own physical copy, even when ISBNs repeat.

## API

All routes except `/health`, `/auth/config`, `/token` and `/token/guest` require
an OAuth2 bearer token in the `Authorization` request header.

Tokens carry a role. Accounts made with `create_user.py` get `admin` and may do
anything; a token from `/token/guest` gets `guest` and cannot edit the catalogue
or check books in. Guests may use the checkout route.

| Method   | Path              | Description                              |
| -------- | ----------------- | ---------------------------------------- |
| `GET`    | `/health`         | Liveness check                           |
| `POST`   | `/token`          | Exchange username/password for a JWT     |
| `POST`   | `/token/guest`    | Get a browse/checkout guest JWT, no credentials |
| `GET`    | `/auth/config`    | Whether guest access is enabled          |
| `GET`    | `/me`             | Caller's username, role and `read_only` flag |
| `GET`    | `/guest-checkout/isbn/{isbn}` | Exact ISBN summary and availability for App Clip checkout |
| `GET`    | `/books`          | List books, optional search, sorting, catalogue filters, and `?checked_out=true|false` |
| `POST`   | `/books`          | Add a book                               |
| `GET`    | `/books/{id}`     | Fetch one book                           |
| `PUT`    | `/books/{id}`     | Update a book                            |
| `DELETE` | `/books/{id}`     | Delete a book                            |
| `POST`   | `/books/{id}/checkout` | Check out a book under a borrower name |
| `POST`   | `/books/{id}/checkin` | Check a book back in (admin only)       |
| `POST`   | `/books/import`   | Bulk import a CSV file                   |
| `GET`    | `/books/{id}/cover` | Stored cover image                    |
| `POST`   | `/books/{id}/cover` | Upload a cover image                   |
| `POST`   | `/books/{id}/cover/lookup` | Find a cover on OpenLibrary   |
| `DELETE` | `/books/{id}/cover` | Remove the stored cover                |
| `POST`   | `/books/{id}/olid/lookup` | Resolve the OLID from the ISBN |
| `POST`   | `/books/{id}/google/lookup` | Resolve the Google Books volume id |
| `POST`   | `/books/{id}/tags/lookup` | Add genre tags, `?replace=true` to swap |
| `POST`   | `/books/{id}/format/lookup` | Fetch the binding from OpenLibrary |
| `POST`   | `/books/{id}/series/lookup` | Fetch the series and volume number |
| `POST`   | `/books/{id}/description/lookup` | Fetch the description        |
| `GET`    | `/tags`           | Tags in use, with book counts            |
| `GET`    | `/series`         | Series in use, with book counts          |
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

`POST /books` returns `409` when its ISBN already exists. Repeat the request
with `?allow_duplicate=true` after confirming that another physical copy should
be added. Book responses include `copy_count`, and duplicate copies show that
total beside the title in the library. Clicking the count filters the library
to those physical copies.

Books with the same normalized title and author but different ISBNs show an
**Other edition available** badge. Clicking it filters the library to those
editions.

Cover images are stored as BLOBs in the `cover` column of the `books` table, with
the content type in `cover_mime`. Adding a book fetches its cover automatically
(from the chosen edition, its OLID, or its ISBN, then Google Books); for books
that have none, use the **Lookup** button in the Manage tab, paste a direct image
address, or **Upload** your own file. List responses never carry the image data —
they return a `has_cover` flag, and the bytes are served on demand from
`GET /books/{id}/cover`.

Format detection runs before a new book is returned so the chosen physical
edition has its binding immediately. Series, description, cover and genre
enrichment then runs in the background on an isolated database connection;
reload the listing to see metadata that finished after the add response.

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

Diagnostic endpoints are administrator-only. Obtain a token through the normal
login endpoint, then send it in the `Authorization` header rather than putting
it in the URL.

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8882/token \
  -d 'username=YOURUSER' \
  --data-urlencode 'password=YOURPASSWORD' |
  python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
```

`GET /diagnostics/{isbn}` reports what each catalogue actually holds and what the
genre extractor made of it, which separates "the source has nothing" from "we
dropped it". Pass `?title=` and `?author=` to mirror what a real lookup would
use when Google has not indexed the ISBN:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8882/diagnostics/9781668068168?title=Songs%20of%20the%20Dead&author=Brandon%20Sanderson"
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
SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  BOOKLIB_DB=/tmp/booklib-dev.db \
  CORS_ORIGINS=http://localhost:5173 \
  uvicorn app.main:app --reload --port 8882

# Frontend
cd frontend
npm install
VITE_API_BASE=http://localhost:8882 npm run dev
```

## Security notes

- `.env` and `data/` are gitignored and excluded from Docker builds.
- The backend refuses to start without a strong, non-example `SECRET_KEY`.
- Back up `SECRET_KEY`; changing it invalidates every issued token.
- There is no public registration; users are created with `create_user.py`.
- Guest access is disabled by default. Enabling it allows anyone who can reach
  the application to browse public catalogue fields and check out available
  books under a supplied name. Private notes, borrower identities, checkout
  timestamps, and shelf locations are not returned to guests.
- Stored covers require an authorization header; access tokens are never placed
  in image URLs.
- Cover lookup URLs are restricted to HTTPS images from OpenLibrary and Google
  Books. Upload other cover files directly.
- Compose binds the frontend and backend to loopback. Put the frontend behind an
  HTTPS reverse proxy before exposing it to the Internet, and do not publish the
  plaintext backend port publicly.

## App Clip invocation service

`infrastructure/app-clip-worker.js` is the stateless Cloudflare Worker used by
the BookLib App Clip invocation domain. It serves Apple's association file and
a browser fallback that confirms the destination before opening a self-hosted
server. Deployment and QR URL details are available on the
[App Clip documentation page](https://cloudymcfox.github.io/BookLib/app-clip/).

## License

BookLib is available under the [MIT License](LICENSE).
