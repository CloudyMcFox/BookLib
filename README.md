# Book Library

A small self-hosted web app for cataloguing a personal book collection. Runs
anywhere Docker does — it was built to live on a Synology NAS.

- Add books manually, or look them up on [OpenLibrary](https://openlibrary.org)
  by title/author and pick the exact edition (cover, publisher, publish date,
  page count) or by ISBN.
- Browse, search, edit and delete your library from the browser, with undo.
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

## Configuration

All settings live in `.env` (never committed — see `.env.example`).

| Variable         | Purpose                                                        |
| ---------------- | -------------------------------------------------------------- |
| `SECRET_KEY`     | Signs JWT access tokens. **Generate your own.**                 |
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

Upload a CSV with the headers `title,author,isbn,notes` (only `title` is
required). Rows whose ISBN already exists are skipped.

## API

All routes except `/health` and `/token` require an
`Authorization: Bearer <token>` header.

| Method   | Path              | Description                              |
| -------- | ----------------- | ---------------------------------------- |
| `GET`    | `/health`         | Liveness check                           |
| `POST`   | `/token`          | Exchange username/password for a JWT     |
| `GET`    | `/books`          | List books, optional `?q=` search        |
| `POST`   | `/books`          | Add a book                               |
| `GET`    | `/books/{id}`     | Fetch one book                           |
| `PUT`    | `/books/{id}`     | Update a book                            |
| `DELETE` | `/books/{id}`     | Delete a book                            |
| `POST`   | `/books/import`   | Bulk import a CSV file                   |
| `GET`    | `/search`         | OpenLibrary search by `title`/`author`   |
| `GET`    | `/lookup/{isbn}`  | OpenLibrary lookup by ISBN               |
| `GET`    | `/edition/{olid}` | OpenLibrary edition detail by OLID       |

`/search` returns English editions by default; pass
`include_all_languages=true` to include translations.

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
