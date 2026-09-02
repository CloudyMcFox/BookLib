# Contributing to BookLib

Thank you for helping improve BookLib.

## Before starting

- Search existing issues and pull requests before opening a duplicate.
- Use an issue to discuss substantial behavioral or architectural changes
  before implementing them.
- Never commit `.env`, databases, access tokens, API keys, passwords, or private
  library data.

## Development setup

Follow the [local development instructions](README.md#local-development) for
the backend and frontend. For a complete deployment, follow the
[quick-start instructions](README.md#quick-start).

## Making a change

1. Fork the repository and create a focused branch from `main`.
2. Keep changes limited to one fix or feature.
3. Follow the existing code style and update documentation when behavior or
   configuration changes.
4. Add or update tests when the project has coverage for the changed behavior.
5. Run the relevant checks before opening a pull request.

Current baseline checks:

```bash
python -m py_compile \
  backend/app/main.py \
  backend/create_user.py \
  backend/change_password.py \
  backend/list_users.py

cd frontend
npm ci
npm run build
```

When Docker is available, also validate the complete image build:

```bash
docker compose build
```

## Pull requests

Describe:

- What changed and why
- How the change was tested
- Any setup, migration, or compatibility impact

Do not put credentials, private server addresses, database contents, or
personal borrower information in pull requests, screenshots, logs, or test
fixtures.
