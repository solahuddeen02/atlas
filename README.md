# Atlas

A self-hosted personal ecosystem inspired by Google Drive and Photos — built on a single object-centric architecture.

> Early development — personal R&D project, not production-ready.

---

## Vision

Atlas is a unified self-hosted platform where every entity (file, photo, folder) is a single `Object` differentiated by `type`. Multiple apps (Drive, Photos, and future apps) are built on top of the same core.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI |
| Database | SQLite → PostgreSQL (via SQLAlchemy + Alembic) |
| Storage | Local filesystem → MinIO / S3 |
| Frontend | React, Vite, Tailwind CSS |
| Auth | JWT (register / login) |

---

## Features

### Drive
- Upload files into folders
- Folder navigation with breadcrumb
- Create, rename, delete (soft) folders and files
- Download files
- Search by filename
- Pagination (load more)

### Photos
- Photo gallery with authenticated thumbnails
- Full preview modal
- Search by filename
- Delete photos
- Pagination (load more)

### Trash
- Soft delete with restore
- Permanent delete (removes DB row + storage file)
- Empty trash in one click
- Pagination (load more)

### Auth & Security
- JWT-based register / login / logout
- Per-user file isolation (`owner_id`)
- Ownership check on every object endpoint (403 on mismatch)
- Protected routes via `get_current_user` dependency

---

## Project Structure

```
atlas/
├── core/
│   ├── apps/
│   │   ├── auth/       — register, login endpoints
│   │   ├── drive/      — folder navigation, create folder
│   │   ├── objects/    — upload, download, trash, rename, delete
│   │   └── photos/     — photo listing
│   ├── domain/         — business logic (objects lifecycle)
│   ├── storage/        — storage abstraction (local / MinIO)
│   └── db/             — SQLAlchemy models, sessions
├── frontend/
│   └── src/            — React components
├── alembic/            — database migrations
├── scripts/            — dev scripts (run.ps1)
├── data/               — file storage at runtime (gitignored)
└── atlas.db            — SQLite database (gitignored)
```

---

## Getting Started

### Prerequisites
- Python 3.11+
- Node.js 18+

### Backend

```powershell
# Bootstrap (create .venv + install dependencies)
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 bootstrap

# Run database migrations
.\.venv\Scripts\python.exe -m alembic upgrade head

# Start backend
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 dev
```

Backend available at: `http://127.0.0.1:8000`  
API docs: `http://127.0.0.1:8000/docs`

### Frontend

```powershell
cd frontend
npm install

# Copy and configure env
copy .env.example .env

npm run dev
```

Frontend available at: `http://localhost:5173`

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ATLAS_DATA_DIR` | `data/` | Local file storage path |
| `ATLAS_STORAGE_BACKEND` | `local` | `local` or `minio` |
| `SECRET_KEY` | — | JWT signing secret |
| `VITE_API_URL` | `http://127.0.0.1:8000` | Backend URL (frontend) |

---

## Roadmap

- [x] Phase 1 — Foundation (object model, upload, download)
- [x] Phase 2 — Stability (Alembic, Docker, error handling)
- [x] Phase 3 — Auth + Storage abstraction
- [x] Phase 4 — Frontend dashboard (Drive, Photos, Trash)
- [ ] Plugin / app registry
- [ ] Management UI

---

## Disclaimer

APIs, data models, and internal structure may change at any time.  
Intended for personal use, learning, and experimentation.
