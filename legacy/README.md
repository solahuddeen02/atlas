# Legacy app (frozen)

This is Atlas before the redesign: a small Drive/Photos app where every file, photo and folder is one `Object` with a `type`. It lives here so the new platform ([../README.md](../README.md)) can be built next to it.

- **Frozen.** It gets security fixes only (the Phase 0 patches in [PLAN-v1](../docs/PLAN-v1.md)). No new features.
- **Not part of the new platform.** Nothing in `atlas/`, `modules/` or `deploy/` uses it.
- **What happens to it:** its Drive functionality is ported into modules in the **First modules** milestone ([ARCHITECTURE §8](../docs/ARCHITECTURE.md#8-mapping-todays-code-to-future-modules-first-modules)). Its SQLite data is imported once at that point.
- Personal R&D code: not production-ready, and its APIs may change.

## Stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI |
| Database | SQLite (SQLAlchemy + Alembic) |
| Storage | Local filesystem, or MinIO / S3 |
| Frontend | React, Vite, Tailwind CSS |
| Auth | JWT (register / login), HS256 with `SECRET_KEY` |

## Features

**Drive**
- Upload files into folders.
- Folder navigation with breadcrumbs.
- Create, rename and (soft-)delete folders and files.
- Download files.
- Search by file name.
- Pagination ("load more").

**Photos**
- Photo gallery with authenticated thumbnails.
- Full preview.
- Search by file name.
- Delete photos.
- Pagination.

**Trash**
- Soft delete with restore.
- Permanent delete (removes the DB row and the stored file). Only trashed objects can be deleted permanently.
- Empty trash in one click.
- Pagination.

**Auth and access**
- JWT-based register, login and logout. The app refuses to start without a strong `SECRET_KEY`.
- Every object has an `owner_id`.
- Another user's object returns **404**, the same as one that doesn't exist. This includes object ids passed in a request: `parent_id` on folder create and upload, and `new_parent_id` on move, which must be your own folder and can't create a cycle.
  - This is the state since the Phase 0 security patches. Before them, referenced ids weren't checked at all ([AUDIT](../docs/AUDIT.md) O1–O3).
  - Known gap: listing another user's folder returns `200 []`. It reveals nothing, so Phase 0 left it. The new platform returns 404 there by design (PLAN-v1, Phase 6).
- Uploads are size-capped while streaming (`ATLAS_MAX_UPLOAD_BYTES`), and responses never include storage paths.

## Project structure

```
legacy/
├── core/
│   ├── apps/
│   │   ├── auth/       — register, login endpoints
│   │   ├── drive/      — folder navigation, create folder
│   │   ├── objects/    — upload, download, trash, rename, delete
│   │   └── photos/     — photo listing
│   ├── domain/         — business logic (object lifecycle)
│   ├── storage/        — storage abstraction (local / MinIO)
│   ├── db/             — SQLAlchemy models, sessions
│   └── security.py     — SECRET_KEY validation, upload cap
├── frontend/
│   └── src/            — React components
├── alembic/            — database migrations
├── tests/              — test suite
├── scripts/            — dev scripts (run.ps1)
├── data/               — file storage at runtime (git-ignored)
└── atlas.db            — SQLite database (git-ignored)
```

## Running it

Run everything from inside `legacy/`. More commands are in [COMMANDS.md](COMMANDS.md).

**Prerequisites:** Python 3.11+ and Node.js 18+.

### Backend

```powershell
cd legacy

# Create .venv and install dependencies
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 bootstrap

# Run database migrations
.\.venv\Scripts\python.exe -m alembic upgrade head

# Set a strong SECRET_KEY first (the backend refuses to start without one)
$env:SECRET_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"

# Start the backend
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 dev
```

The backend listens on `http://127.0.0.1:8000`, with API docs at `/docs`.

### Frontend

```powershell
cd legacy\frontend
npm install
copy .env.example .env
npm run dev
```

The frontend runs at `http://localhost:5173`.

### Docker

```bash
cd legacy
cp .env.example .env   # then fill in every value; never commit .env
docker compose up -d --build
```

### Tests

```bash
cd legacy
pip install -r requirements.txt
pytest
```

## Configuration

Read from the environment (or `.env` with Docker Compose).

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | **required** | JWT signing secret. The app refuses to start if it's missing, shorter than 43 characters, too repetitive, or a known placeholder. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DATABASE_URL` | `sqlite:///./atlas.db` | SQLAlchemy URL |
| `ATLAS_DATA_DIR` | `data` | Local file storage path |
| `STORAGE_BACKEND` | `local` | `local` or `minio` |
| `MINIO_ENDPOINT` | `localhost:9000` | MinIO host:port (with `STORAGE_BACKEND=minio`) |
| `MINIO_ACCESS_KEY` | *(none)* | MinIO access key |
| `MINIO_SECRET_KEY` | *(none)* | MinIO secret key |
| `MINIO_BUCKET` | `atlas` | MinIO bucket |
| `ATLAS_MAX_UPLOAD_BYTES` | `104857600` (100 MB) | Maximum upload size; larger uploads get `413` |
| `VITE_API_URL` | `http://127.0.0.1:8000` | Backend URL (frontend) |

The storage variable is `STORAGE_BACKEND`. Earlier versions of this README called it `ATLAS_STORAGE_BACKEND`, which the code never read.

The Docker Compose file (`docker-compose.yml`) also reads `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` from `.env` for the MinIO container.
