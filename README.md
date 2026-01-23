Atlas

Atlas is a personal self-hosted ecosystem core inspired by Google-style services
(Drive, Photos, Notion-like apps), designed to be modular, storage-centric,
and extensible.

Status: Early development (experimental / personal R&D)


VISION

The goal of Atlas is to build a unified self-hosted ecosystem that:

- Stores files, photos, and structured data in one system
- Uses a single object model as the core abstraction
- Allows multiple apps (Drive, Photos, Notes, Dashboard, etc.) to be built on top
- Can evolve into a server OS–like platform managed entirely via web UI
- Can migrate between storage backends (local to S3 / MinIO)

Atlas is intentionally built core-first before UI, authentication, or apps.


CORE PHILOSOPHY

Object-Centric Architecture

Everything in Atlas is an object:
- Files
- Photos
- Folders
- Future entities (notes, tasks, dashboards, apps)

Each object contains:
- Metadata (type, name, timestamps, status)
- A reference to storage (not storage logic itself)

This design keeps the core flexible and future-proof.


Separation of Concerns

- API layer: HTTP endpoints (FastAPI)
- Domain layer: business logic and object lifecycle
- Storage layer: physical storage (local now, extensible later)
- Database: metadata only (SQLite for development)


PROJECT STRUCTURE

atlas/
  core/
    api/        HTTP endpoints (FastAPI routers)
    domain/     Core business logic (objects, lifecycle, rules)
    storage/    Storage backends (local now, S3/MinIO later)
    db/         Database layer (SQLAlchemy, schema, sessions)

  tests/        Pytest-based test suite
  scripts/      Development scripts (no venv activation required)
    run.ps1

  data/         Runtime file storage (gitignored)
  requirements.txt
  README.md
  .gitignore


CURRENT FEATURES

- Object creation and lifecycle management
- File upload and download
- Local filesystem storage
- SQLite metadata database using SQLAlchemy
- Atomic upload handling (database + file)
- Crash-safe recovery on startup
- Automated test suite (pytest)


DEVELOPMENT (WINDOWS FRIENDLY, NO VENV ACTIVATION)

Atlas is designed to run without activating a virtual environment.
This avoids issues with PowerShell execution policy restrictions.

Prerequisites:
- Python 3.11


Bootstrap (create .venv and install dependencies)

powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 bootstrap


Run development server

powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 dev

Server will be available at:
http://127.0.0.1:8000


Run tests

powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 test


Direct commands (advanced usage, still no activate)

.\.venv\Scripts\python.exe -m uvicorn core.main:app --reload
.\.venv\Scripts\python.exe -m pytest


ROADMAP (HIGH LEVEL)

- Dashboard and object browsing UI
- App separation (Drive / Photos / Notes)
- User and permission system
- Storage abstraction (S3 / MinIO)
- Plugin and app registry
- Server OS–style installer and management UI


DISCLAIMER

This project is under active development.

APIs, data models, and internal structure may change at any time.
This is not production-ready.

Currently intended for personal use, learning, and experimentation.
Licensing and contribution guidelines will be defined later.


NOTES

Atlas is built as a long-term foundation, not a finished product.

The focus is correctness, extensibility, and architectural clarity
before performance tuning, UI polish, or public release.
