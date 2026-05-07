# Atlas

A self-hosted platform for personal and team file management — built on a plugin architecture and a single object-centric data model.

> Early development — personal R&D project, not production-ready.

---

## Vision

Atlas is a platform, not an app. Every feature (Drive, Photos, Trash) is a plugin installed on top of a minimal core. The core provides auth, multi-tenancy, storage abstraction, an event bus, and a plugin runtime — nothing else.

Every entity (file, folder, photo) is a single `Object` differentiated by `type`. Plugins share this model via `core/objects/domain.py` — no cross-plugin imports.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI |
| Database | SQLite → PostgreSQL (SQLAlchemy + Alembic) |
| Storage | Local filesystem → MinIO / S3 |
| Frontend | React, Vite, Tailwind CSS |
| Auth | JWT (register / login) |

---

## Architecture

### Plugin system

Atlas ships with no bundled apps. Plugins are installed by the user:

```
POST /admin/plugins/install  {"name": "drive", "runtime": "inprocess"}
```

Each plugin is a Python package (local `plugins/name/` or pip `atlas_plugin_name`) with a standard interface:

```python
manifest = {"name": "drive", "version": "1.0"}
routers   = [router]

def migrate(engine): ...   # create plugin-owned tables
def startup(db): ...       # subscribe events, recover state
```

Plugins can declare which runtimes they support via `manifest["supported_runtimes"]`.

### Plugin runtimes

Plugins can run in three isolation modes:

| Mode | How | Crash isolation | Overhead |
|---|---|---|---|
| `inprocess` | Same Python process (default) | No | None |
| `subprocess` | Separate Python process, HTTP proxy | Yes | ~1ms + 30–50 MB RAM |
| `container` | Docker container, HTTP proxy | Yes | ~1ms + 50–100 MB RAM |

`subprocess` and `container` are planned — not yet implemented.

### Tenant isolation (progressive)

Atlas supports three isolation levels, switchable via `ATLAS_TENANT_MODE`:

| Mode | Description | Use case |
|---|---|---|
| `single` | One default tenant, no multi-tenancy | Family at home |
| `shared` | Multiple tenants, one DB, row-level isolation (default) | Small team / growing |
| `isolated` | Each tenant has its own DB and storage namespace | Enterprise / separate orgs |

Upgrading between levels requires only config changes, no code changes:

```
single  →  shared   : set ATLAS_TENANT_MODE=shared
shared  →  isolated : set ATLAS_TENANT_MODE=isolated
                      set ATLAS_TENANT_<SLUG>_DB_URL=sqlite:///./family.db
                      export tenant data → new DB
```

### Event bus

Side effects (e.g. thumbnail generation) run through an in-process pub/sub bus — not inline in request handlers:

```python
bus.publish("object.uploaded", {"id": obj_id, "file_data": data})
# → on_object_uploaded() runs in the objects plugin subscriber
```

Handler errors are isolated: one failing handler cannot crash others.

### Storage abstraction

`StorageBackend` ABC in `core/storage/base.py`. Implementations: `LocalStorage`, `MinIOStorage`.

Both support tenant namespacing:
```python
get_storage(tenant_slug="family")  # → data/family/ or MinIO prefix family/
get_storage()                      # → data/ or MinIO bucket root
```

### Role-based UI

The frontend is a Google-style launcher where each app opens at its own URL. Sections are visible based on JWT role:

| Role | Sees |
|---|---|
| `member` | Personal apps (Drive, Photos, Trash) |
| `admin` | + Workspace (stats, invite) |
| `owner` | + System (plugin install/uninstall) |

---

## Project structure

```
atlas/
├── core/
│   ├── auth/           — JWT register/login, dependencies
│   ├── bus/            — in-process event bus
│   ├── db/             — models, engine, sessions, migrations
│   │   └── tenant_registry.py  — progressive DB isolation
│   ├── objects/        — shared object domain (cross-plugin logic)
│   ├── runtime/        — PluginRuntime ABC + implementations
│   │   ├── inprocess.py
│   │   ├── subprocess_rt.py  (planned)
│   │   └── container.py      (planned)
│   ├── storage/        — StorageBackend ABC, local + MinIO
│   └── plugin_loader.py
├── plugins/            — installable feature modules
│   ├── objects/        — upload, download, share links, trash
│   ├── drive/          — folder navigation
│   ├── photos/         — photo gallery, thumbnails
│   ├── admin/          — stats, plugin registry API
│   └── tenant/         — invite tokens, workspace management
├── frontend/
│   └── src/            — React + Vite (Launcher, AppShell, per-app views)
├── alembic/            — database migrations
└── data/               — runtime file storage (gitignored)
```

---

## Getting started

### Prerequisites
- Python 3.11+
- Node.js 18+

### Backend

```powershell
# Bootstrap
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 bootstrap

# Migrate database
.\.venv\Scripts\python.exe -m alembic upgrade head

# Start
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 dev
```

API: `http://127.0.0.1:8000` — Docs: `http://127.0.0.1:8000/docs`

### Frontend

```powershell
cd frontend
npm install
copy .env.example .env   # set VITE_API_URL
npm run dev
```

Frontend: `http://localhost:5173`

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ATLAS_ENABLED_PLUGINS` | `objects,drive,photos,admin,tenant` | Plugins loaded at startup |
| `ATLAS_TENANT_MODE` | `shared` | `single` / `shared` / `isolated` |
| `ATLAS_TENANT_<SLUG>_DB_URL` | — | Per-tenant DB URL (isolated mode) |
| `ATLAS_DATA_DIR` | `data/` | Local file storage root |
| `STORAGE_BACKEND` | `local` | `local` or `minio` |
| `DATABASE_URL` | `sqlite:///./atlas.db` | SQLAlchemy database URL |
| `SECRET_KEY` | — | JWT signing secret |
| `VITE_API_URL` | `http://127.0.0.1:8000` | Backend URL (frontend build) |

---

## Roadmap

- [x] Phase 1 — Foundation (object model, upload, download)
- [x] Phase 2 — Stability (Alembic, Docker, error handling)
- [x] Phase 3 — Auth + storage abstraction
- [x] Phase 4 — Frontend launcher + role-based UI
- [x] Phase 5 — Plugin architecture (registry, event bus, core domain)
- [ ] Plugin runtime: subprocess isolation
- [ ] Plugin runtime: container isolation
- [ ] Tenant isolation: isolated mode (per-tenant DB)
- [ ] Enable/disable plugins per tenant
- [ ] WebDAV
- [ ] PWA

---

## Disclaimer

APIs, data models, and internal structure may change at any time.
Intended for personal use, learning, and experimentation.
