# Repository Guidelines

## Project Structure & Module Organization

- `main.py` exposes the application; `app/main.py` defines its factory and lifecycle.
- `app/api/` handles HTTP routes and dependencies; `services.py` owns business rules and transaction commits; `repositories.py` owns database queries.
- `app/models.py` defines SQLAlchemy models; `schemas.py` defines Pydantic contracts. `app/core/` contains configuration, logging, and errors; `app/agents/` contains the Agent execution interface.
- `frontend/` is the independent Vite frontend (HTML/CSS/ES modules); run npm test and npm run build there.
- `migrations/` contains Alembic revisions, `tests/` contains API/frontend tests and fixtures, and `docs/architecture.md` explains architectural boundaries.

## Build, Test, and Development Commands

Use Python 3.12. Run these PowerShell commands from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
if (!(Test-Path .env)) { Copy-Item .env.example .env }
python -m alembic upgrade head
python -m uvicorn main:app --reload --no-access-log
```

Frontend runs at :5173; the API runs at :8000 with development docs at /docs. API root does not serve HTML.

- `python -m pytest -q`: run tests.
- `python -m ruff check .`: check lint rules and imports.
- `python -m ruff format --check .`: verify formatting; omit `--check` to format.
- `docker build -t fastapi-demo:local .`: build the production image.

## Coding Style & Naming Conventions

Use four-space Python indentation, type annotations, `snake_case` functions/modules, and `PascalCase` classes. Ruff targets Python 3.12 with a 100-character line length. Match existing two-space JavaScript indentation and `camelCase` names.

Keep SQL out of routes and commits in services. Never share an async database session between concurrent tasks. For schema changes, add an Alembic revision and update the readiness revision check in `app/api/routes.py`.

## Testing Guidelines

Use pytest, pytest-asyncio, and HTTPX; name files `test_*.py` and functions `test_*`. Reuse `tests/conftest.py` fixtures, which apply real migrations. Cover changed behavior, validation failures, authentication, and relevant version conflicts. No numeric coverage threshold is configured.

CI tests SQLite and PostgreSQL. Set `TEST_DATABASE_URL` only to a disposable database: fixtures downgrade migrations and delete tables.

## Commit & Pull Request Guidelines

Git history is unavailable in this checkout. Use concise imperative commits, optionally prefixed with `feat:`, `fix:`, or `docs:`. PRs should describe behavior changes, link relevant issues, report checks, explain migrations/configuration changes, and include screenshots for UI changes.

## Security & Configuration

Never commit `.env` or credentials. Use HttpOnly session cookies and CSRF headers; never store passwords or session tokens in browser storage or logs. Update dependency declarations and lock files together. Every TodoRepository requires the authenticated owner_id. Legacy unowned tasks stay private and must not be assigned implicitly. Auth migrations and deployment constraints are documented in docs/authentication.md.
