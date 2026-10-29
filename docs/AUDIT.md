# Atlas — Pre-Redesign Audit

- **Date:** 2026-10-29
- **Audited ref:** `main` @ `a278d2f` (the merge of PR #1). This is also the checked-out branch.
- **Also inspected:** `origin/dev` @ `60be895`, which is **8 commits ahead of `main`**. Those commits add tenants, share links, invites, thumbnails, Postgres and a `core/` vs `apps/` split. See [§6](#6-the-unmerged-origindev-branch).
- **Scope:** read-only. No application code was changed. Every test and probe ran from a scratch directory, outside the repo.

---

## TL;DR

| Area | Verdict | One-line reason |
|---|---|---|
| Object model / DB schema | **Rewrite** | No FKs, no indexes, text timestamps, free-text `type`, and the migrations break on real data |
| Domain (`core/domain/objects.py`) | **Refactor** | The logic is mostly right, but it's a grab-bag of dict-returning functions with no invariants |
| Auth | **Refactor** | A working shape on a forgeable default secret, plus an unmaintained JWT library |
| Ownership / authorization | **Rewrite** | Only the target object is checked. `parent_id` targets are not, so users can write into each other's trees |
| Storage abstraction | **Refactor** | A good interface, but keys are absolute paths and recovery bypasses it. MinIO is half-built |
| HTTP routers | **Refactor** | Thin and readable. There are no response models and the API mixes query params with JSON bodies |
| Frontend | **Keep (shell) / refactor** | Builds and works. No router, no API layer, thumbnails download full images |
| Tests | **Rewrite** | **12 of 19 fail**. They predate auth and leak into the real `./atlas.db` |
| CI | **Refactor** | Red on `main`. Unpinned deps, no frontend job, no migration check |
| Docker | **Rewrite** | Migrations run at *build* time against the wrong DB path. Hard-coded public `SECRET_KEY` |
| Secrets in git history | **Clean** | No real credentials found. Only placeholder defaults and an empty SQLite WAL/SHM pair (details in §4) |

**The three most urgent issues:**
1. **Anyone can forge a JWT for any user** when `SECRET_KEY` is unset. The code falls back silently to a constant that is public in this repo. The same goes for the value hard-coded in `docker-compose.yml`. Verified in §3.1.
2. **Cross-user writes.** `parent_id` and `new_parent_id` are never ownership-checked. User B can create folders, upload files and move objects into user A's folders. Verified in §3.2.
3. **The test suite and CI have been red since auth landed.** Nothing currently guards a refactor.

---

## 1. What works vs. half-built

### 1.1 Test results

Run on Python 3.11.x (matching CI) with deps resolved fresh from `requirements.txt`, using CI's env vars (`DATABASE_URL`, `ATLAS_DATA_DIR` pointed at scratch):

```
12 failed, 7 passed, 2 warnings in 2.53s
```

| Test | Result | Cause |
|---|---|---|
| `test_auth.py` (5 tests) | ✅ pass | |
| `test_health.py::test_health_ok` | ✅ pass | |
| `test_recovery.py::test_recovery_deletes_tmp_files` | ✅ pass | |
| `test_folders.py` (4 tests) | ❌ fail | No `Authorization` header (401). They also send `name` as a **query param**, but the endpoint now takes a JSON body (422 even with auth) |
| `test_move.py` (2 tests) | ❌ fail | No auth header |
| `test_trash.py` (4 tests) | ❌ fail | No auth header |
| `test_upload_flow.py` (1 test) | ❌ fail | No auth header |
| `test_recovery.py::test_startup_recovery_…` | ❌ fail | `IntegrityError: NOT NULL constraint failed: objects.owner_id`. The test inserts an `Object` without an `owner_id` |

The same run on Python 3.13 with the newest resolved deps gives the same 12/7 split.

**Structural test problems** (each verified):
- **Tests write to the real dev database.** The app lifespan calls `init_db()` on the module-level engine, and `test_recovery.py` imports `SessionLocal` directly. Running `pytest` from the repo root without `DATABASE_URL` created `./atlas.db` in the working tree. I deleted that file afterwards. `test_paths` patches `core.db.session.SessionLocal` through `__dict__`, which does nothing for modules that already imported the name.
- The lifespan's `recover_incomplete_uploads()` runs against the real engine on every `TestClient` start.
- No test sends a token, so **every ownership/403 path is untested**. There are no tests for rename, permanent delete, empty trash, photos, download-by-other-user or pagination.
- No frontend tests at all.

### 1.2 Feature matrix (main)

I verified behaviour with an authenticated scratch probe that ran the real app against a temp DB.

| Feature | State | Notes |
|---|---|---|
| Register / login / JWT | ✅ works | Accepts an empty password (verified). No rate limiting |
| Upload (local storage) | ✅ works | Atomic tmp-then-`os.replace`. DB row created with `status=uploading` and flipped to `ready` in one transaction |
| Download | ✅ works | `application/octet-stream` + `attachment`. Downloading a folder returns 404 "file not found on storage" |
| Folders: create / list / navigate | ✅ works | See the ownership gaps in §3.2 |
| Move | ⚠️ API only | No UI. Accepts moves into self, into a file, into a non-existent id, or into another user's folder (all verified 200) |
| Rename | ✅ works | |
| Soft delete (recursive) | ✅ works | All descendants get `deleted_at`, so **each child shows up individually in Trash** |
| Restore | ⚠️ broken for folders | Restores only the clicked row. A restored folder comes back **empty** (verified) and its children stay in Trash. Restoring a child whose parent is still trashed orphans it |
| Permanent delete | ⚠️ unsafe | Works on **non-trashed** objects too (verified). On a folder it deletes only the folder row, which orphans its children and their blobs |
| Empty trash | ✅ works | Deletes the caller's trashed rows, then blobs (best-effort) |
| Photos | ⚠️ partial | Classified by the **client-supplied** `Content-Type`. Server `q` search exists but the UI filters client-side. Photos also appear in Drive |
| Drive search | ⚠️ client-side only | Filters only the items already loaded on the current page/folder |
| Pagination | ✅ works | `limit` is unbounded (`limit=100000` accepted) and a negative `offset` is accepted |
| Startup recovery | ⚠️ local only | Uses `os.path` directly, so it does nothing for MinIO. Unsafe with more than one worker (see §2.4) |
| MinIO backend | ❌ half-built | `minio` isn't in `requirements.txt`. The whole object is buffered in RAM. `secure=False`. Never tested |
| "App registry" (README roadmap) | ⚠️ cosmetic | `frontend/src/apps.js` is a static array of three components. No backend concept |
| Management UI | ❌ absent | Unchecked in the roadmap, correctly |

### 1.3 Frontend

- `npm ci && vite build`: ✅ **builds** (≈205 kB JS, 64 kB gzip).
- `eslint .`: ❌ **4 errors, 3 warnings**
  - `api.jsx:15`: unused `_` and an empty `catch` block
  - `Toast.jsx:5`: `react-refresh/only-export-components`
  - `DriveView.jsx:36`: `set-state-in-effect`
  - `exhaustive-deps` warnings in DriveView, PhotosView and TrashView
- Leftovers from the Vite template: `App.css`, `assets/hero.png`, `assets/react.svg`, `assets/vite.svg`, `public/icons.svg` (none referenced). `frontend/README.md` is the stock template. `index.html` `<title>` is still "frontend".
- No router: the active view is `useState` and the URL never changes. Login/Register toggles through state.
- An expired or invalid token gives a toast on every request but **doesn't log the user out**, so the user stays stuck on an erroring screen.
- `PhotoCard` fetches the **full original file** for every thumbnail: 20 full-resolution downloads per page.
- Upload sends one file at a time, with no progress bar, size check or drag-drop.
- UI copy mixes Thai and English.

### 1.4 CI (`.github/workflows/ci.yml`)

- Runs `pytest` on Python 3.11 on every push and PR. **Given §1.1 it's currently red on `main`.**
- Deps are `>=`-ranged with no lockfile, so CI resolves different versions on every run (I got Starlette 1.x and SQLAlchemy 2.1 today).
- Has a fallback branch that installs deps "if requirements.txt doesn't exist", which is dead code.
- No frontend lint/build job, no `alembic upgrade` check, no Docker build, no secret scanning or dependency audit.

### 1.5 Docker

Verified by running the same Alembic config outside the container:

- **`alembic/env.py` ignores `DATABASE_URL`.** It always uses `alembic.ini`'s `sqlite:///./atlas.db`. (`origin/dev` fixes this.)
- `Dockerfile` runs `RUN alembic upgrade head` **at build time**. That migrates `/app/atlas.db` inside the image layer, not the `/app/db/atlas.db` volume the app uses. At runtime the app's `init_db()` → `create_all()` creates the schema on the volume with **no `alembic_version` table**, so Alembic and the real DB diverge from the first boot.
- **The `owner_id` migration fails on any DB that already has rows.** `ADD COLUMN owner_id INTEGER NOT NULL` with no default gives `Cannot add a NOT NULL column with default value NULL` (reproduced). Every pre-auth database is non-upgradable.
- `docker-compose.yml` hard-codes `SECRET_KEY=change-this-in-production` and MinIO `minioadmin/minioadmin`. It starts MinIO (ports 9000/9001 published to the host) even though `STORAGE_BACKEND=local`.
- The container runs as root. There's no healthcheck. The frontend isn't containerised. `.dockerignore` doesn't exclude `.env`, `frontend/node_modules` or `frontend/.env`.
- `scripts/run.ps1` has a `docker` task that can't be invoked, because it's missing from `ValidateSet`. The script is Windows-only, and so are the README instructions.

---

## 2. Architecture as built

### 2.1 Module map

```
core/main.py            FastAPI app, lifespan (create_all + recovery), CORS, router wiring
core/apps/auth/         router (register/login), jwt.py (hashing + JWT), dependencies.get_current_user
core/apps/objects/      router: upload, download, list, move, trash, restore, rename, delete, permanent, empty
core/apps/drive/        router: /drive/root, /folders (create), /folders/{id} (list)
core/apps/photos/       router: /photos
core/domain/objects.py  ~500 lines: every query + mutation + startup recovery for Object
core/storage/           StorageBackend ABC, LocalStorage, MinIOStorage, factory (env-driven)
core/db/                engine (module-level, env-driven), session, models (Object, User), init (create_all)
```

Dependency direction: `apps/* → domain → db.models`, and `apps/objects → storage`. `auth` is imported by every app. That's reasonable in principle, but see §2.3.

### 2.2 How the `Object` model is actually used

The README pitch is "every entity is a single `Object` differentiated by `type`". In practice:

- `type` is a **free-text string supplied by the client** (`obj_type` query param on upload). The probe stored `type="anything"`. The only values with meaning are `"folder"` (checked by the frontend and by `create_folder`) and `"file"` (what the UI sends).
- **Photos are not a type.** They're `mime_type LIKE 'image/%'`, based on the browser-supplied `Content-Type`. `list_photos` then **overwrites** `type` to `"photo"` in its response, so one row reports different types from different endpoints.
- Folder-ness is implicit: a folder is an `Object` with no `storage_key`. Nothing stops a file from becoming a `parent_id`, and nothing stops a folder from being "downloaded".
- `parent_id` and `owner_id` are plain integers with **no foreign keys**. `PRAGMA foreign_keys=ON` is set but has nothing to enforce. There are **no indexes** on `owner_id`, `parent_id`, `deleted_at`, `status` or `mime_type`, the columns every query filters on.
- Lifecycle is split across two columns: `status ∈ {uploading, ready, failed}` and `deleted_at`. `failed` rows are never cleaned up and are invisible everywhere.
- Timestamps are **TEXT**, in two formats: `datetime.utcnow().isoformat()` (naive) from the domain and `datetime.now(timezone.utc).isoformat()` (with `+00:00`) from the upload router. Sorting on `created_at` is lexicographic over mixed formats.
- There's no uniqueness rule for names within a folder, no depth/cycle guard, and no size/quota accounting.

The single-table idea is workable, but today it's a loosely typed bag with no invariants enforced by either the DB or the domain layer.

### 2.3 Coupling problems

1. **Routers own the transaction and storage orchestration.** `upload_object` stitches together four domain calls with `commit=False`, a storage call, and manual rollback/cleanup. The `commit: bool` flag on domain functions leaks transaction control upward. Other domain functions (`move_object`, `rename_object`, …) commit internally, so a multi-step operation can't be atomic.
2. **Authorization lives in the router** (`_get_owned_object`), not the domain. Domain functions accept `owner_id: int | None = None` and **skip the owner filter when it's `None`**, so the default is "all users". Drive and photos routers don't use `_get_owned_object` at all.
3. **Domain returns hand-built dicts** with per-function key sets (`"storage"` vs `storage_key`, `type` overridden for photos). Routers return those dicts raw. There are no Pydantic response models, so the API contract is undocumented in OpenAPI and leaks internals (§3.4).
4. **The storage key is a filesystem path.** `LocalStorage.save` returns `str(data_dir / id)`, which is absolute when `ATLAS_DATA_DIR` is absolute. That path is persisted, so moving the data dir or changing the env var invalidates every row. `delete`/`exists` trust the stored path verbatim.
5. **Recovery bypasses the storage abstraction.** It does `os.listdir`/`os.remove` on `ATLAS_DATA_DIR` and guesses `data_dir/<id>`.
6. **The router switches behaviour on env.** `download_object` reads `STORAGE_BACKEND` itself to decide redirect vs. file response, so the storage interface doesn't express "how do I serve this".
7. **Engine and config are import-time globals** (`DATABASE_URL`, `SECRET_KEY`, `engine`) read via `os.getenv` in four different modules. There's no settings object, which is why tests can't isolate the DB (§1.1).
8. `@event.listens_for(Engine, "connect")` is registered on the **`Engine` class**, so it applies its SQLite pragmas to *every* engine in the process, including test engines.
9. `core/storage/local.py` keeps a "backward-compatible" `save_file()` with no callers.

### 2.4 Operational hazards

- **Multi-worker startup recovery is destructive.** Each worker's lifespan deletes **every** `.*.tmp` in the data dir and marks **every** `uploading` row `failed`. With `uvicorn --workers N`, or a rolling restart, one worker's boot kills another worker's in-flight uploads.
- `init_db()` (`create_all`) runs on every boot even though the comment says "dev only". This is what masks the broken Alembic setup (§1.5).
- `trash_object_recursive` runs one `SELECT` per node (an N+1 walk) and doesn't scope children by owner.
- Errors are swallowed (`except Exception: pass`) in five places, which hides storage leaks.
- Logging is a single `print()`.

### 2.5 README vs. code

| README says | Code does |
|---|---|
| Config var `ATLAS_STORAGE_BACKEND` | Code reads **`STORAGE_BACKEND`** (`core/storage/factory.py:14`, `core/apps/objects/router.py:103`) |
| `SECRET_KEY` default "—" (implying required) | Silently defaults to `dev-secret-change-in-production` |
| "Ownership check on every object endpoint (403 on mismatch)" | Only on the object in the path. `parent_id` / `new_parent_id` are never checked (§3.2) |
| "Database: SQLite → PostgreSQL (via SQLAlchemy + Alembic)" | Alembic is bypassed at runtime (`create_all`). No Postgres driver in `requirements.txt` on `main` |
| "Storage: Local → MinIO / S3" | `minio` isn't installed. Compose sets `local` but still starts MinIO |
| "Search by filename" (Drive) | Client-side filter over the current page only |
| "Photo gallery with authenticated thumbnails" | Full-size originals, not thumbnails |
| "Restore" (Trash) | Folder restore loses its contents (§1.2) |
| "Phase 2 — Stability (Alembic, Docker…)" ✅ | Docker migrations target the wrong DB, and the `owner_id` migration can't upgrade existing data |
| Getting started is PowerShell-only | Linux/macOS have no documented path (CI is Ubuntu) |
| Project tree lists `atlas.db` at root | It's gitignored, and Docker uses `db/atlas.db` |

---

## 3. Security review

Severity is for a self-hosted, internet-exposed deployment.

### 3.1 Auth / JWT

| # | Sev | Finding | Where |
|---|---|---|---|
| A1 | **Critical** | `SECRET_KEY` silently falls back to the public constant `dev-secret-change-in-production`. **Verified:** a token minted offline with that constant (`{"sub":"1"}`) is accepted, and `GET /objects` returns user 1's files. The compose value `change-this-in-production` is equally public. The app must refuse to start without a strong key. | `core/apps/auth/jwt.py:10`, `docker-compose.yml` |
| A2 | High | `python-jose` is effectively unmaintained and has had algorithm-confusion and JWE DoS CVEs. Move to `PyJWT` (or `authlib`). `passlib` is also unmaintained, and the `bcrypt<4` pin exists only to keep passlib working. Use `bcrypt` or `argon2-cffi` directly. | `requirements.txt` |
| A3 | Medium | No server-side session state. Logout only deletes the token from `localStorage`, and tokens live 24 h with no revocation or rotation. A stolen token stays valid for its whole lifetime. Deactivating a user does work, because `is_active` is checked per request. | `jwt.py:12`, `App.jsx:10` |
| A4 | Medium | The token is stored in `localStorage`, so any XSS gives full account takeover. Consider an HttpOnly, SameSite cookie plus CSRF protection, or at minimum a strict CSP. | `LoginPage.jsx:22` |
| A5 | Medium | Open self-registration with no password policy: an empty password is accepted (verified). No length limits on username/password. bcrypt silently truncates at 72 bytes. | `core/apps/auth/router.py:22-50` |
| A6 | Medium | No rate limiting or lockout on `/auth/login`. | |
| A7 | Low | Username enumeration through timing: bcrypt verification runs only when the user exists. `/register` also reveals taken names, which is acceptable but worth noting. | `router.py:59` |
| A8 | Low | A validly signed token without `sub`, or with a non-integer `sub`, raises `KeyError`/`ValueError`, which becomes a 500 (verified). No `iss`/`aud`/`iat`/`nbf` claims. `username` in the payload is unused. | `dependencies.py:25` |
| A9 | Low | `datetime.utcnow()` is deprecated (warnings in the test run). It works today, but should be `datetime.now(UTC)`. | `jwt.py:26`, `router.py:44` |

### 3.2 Ownership / authorization

| # | Sev | Finding | Verified |
|---|---|---|---|
| O1 | **High** | `POST /folders` with `parent_id` set to **another user's folder** succeeds. | ✅ 200 |
| O2 | **High** | `POST /objects/upload?parent_id=<other user's folder>` succeeds. | ✅ 200 |
| O3 | **High** | `POST /objects/{own}/move?new_parent_id=<other user's folder>` succeeds. So do moves into self (which creates a cycle and hides the subtree), into a file, or into a non-existent id. | ✅ 200 ×3 |
| O4 | Medium | Consequence of O1–O3: when the victim trashes their folder, `trash_object_recursive` → `get_children_ids` (no owner filter) **also trashes the attacker's planted objects**. The victim's `empty_trash` then skips them (it's owner-scoped), leaving orphans. Cross-tenant side effects like these are exactly what the tenant redesign must rule out. | by code reading |
| O5 | Low | 403 vs 404 differs by existence, which lets one user enumerate how many object ids exist globally. Return 404 for "not yours". | ✅ |
| O6 | Low | Domain functions default `owner_id=None` and treat that as "no filter". One missed argument means a cross-user leak. Make ownership a required, non-optional part of every query. | by code reading |
| O7 | Low | `DELETE /objects/{id}/permanent` works on live (non-trashed) objects, so one click from any client skips the trash safety net. | ✅ |

What *is* correct: `_get_owned_object` blocks read, rename, trash, restore, permanent delete and move of **another user's object by id** (verified: Bob gets 403 downloading Alice's file). All list endpoints filter by `owner_id`.

### 3.3 File upload / download

| # | Sev | Finding |
|---|---|---|
| F1 | **High** | **No upload size limit** anywhere (app, Uvicorn or proxy config), and no per-user quota. With local storage that means unlimited disk fill. `MinIOStorage.save` does `src.read()`, so the **whole upload sits in RAM**: trivial memory DoS. |
| F2 | Medium | **Content-type is trusted from the client.** `mime_type = file.content_type` is stored and drives Photos classification (verified: a 2-byte file labelled `image/png` appears in Photos). Detect the type server-side (magic bytes) and treat the client value as a hint. |
| F3 | Low | Download is safe against content sniffing **for local storage**: it forces `application/octet-stream` + `Content-Disposition: attachment` (verified). Add `X-Content-Type-Options: nosniff` anyway. The **MinIO** branch redirects to a presigned URL whose response headers come from MinIO, and since `put_object` stores no content-type, it's octet-stream by luck rather than by design. |
| F4 | ✅ OK | **Path traversal: not exploitable as built.** The on-disk name is the integer object id (`data_dir/<id>`), never the user-supplied `name`. A `name="../../etc/x"` upload was stored harmlessly (verified). `name` only reaches `Content-Disposition`, which Starlette encodes. Remaining risks: (a) `LocalStorage.delete/exists` trust whatever path is in `storage_key`, so any future bug that lets a user influence `storage_key` becomes arbitrary file delete/read; (b) `name` has no length or character validation on upload (rename validates 1–255 chars, upload doesn't). |
| F5 | Low | `obj_type` is free text, so a client can upload a blob with `type="folder"` and create an object that is both a folder and a file. |
| F6 | Low | Startup recovery deletes any dot-file ending in `.tmp` in the data dir. Harmless today, but it's a blanket delete in a user-data directory. |
| F7 | Low | Upload errors return `upload failed: <ExceptionClassName>`, which leaks internals. Failed uploads leave permanent `status=failed` rows. |

### 3.4 Information disclosure and misc

| # | Sev | Finding |
|---|---|---|
| I1 | Medium | `GET /objects` and `GET /photos` return `"storage"`: the **absolute server filesystem path** (verified, e.g. `/…/data/5`). Drop it from responses. |
| I2 | Low | CORS is hard-coded to `http://localhost:5173`, so any real deployment origin must be patched in code. `allow_methods/headers=["*"]`. Credentials aren't enabled, which is fine for Bearer auth. |
| I3 | Low | No security headers (CSP, HSTS, nosniff, frame-options). `/docs` and `/openapi.json` are public. |
| I4 | Low | Pagination `limit` is unbounded, so one request can dump an entire table. `q` in `list_photos` isn't escaped for `%`/`_` (no injection, since it's parameterised, but wildcards are user-controllable). |
| I5 | Info | Dependencies are unpinned (`>=`) with no lockfile and no `pip-audit` or Dependabot, so a supply-chain change lands silently on the next CI run or build. |

### 3.5 Secrets handling (current tree)

- No `.env` loading in the backend. Config comes straight from `os.getenv` with **insecure defaults for every secret** (`SECRET_KEY`, `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY` = `minioadmin`).
- `docker-compose.yml` commits literal credentials: `SECRET_KEY`, and the MinIO root user/password. (On `origin/dev` also `POSTGRES_PASSWORD` and a `DATABASE_URL` with the password embedded.) They're placeholders, but compose files tend to get deployed as-is.
- Root `.gitignore` covers `*.db*` and `data/`, but **not `.env`**. Only `frontend/.gitignore` ignores `.env`. A backend `.env` added later would be committed.
- `.dockerignore` doesn't exclude `.env`, so a local `.env` would get baked into the image.

---

## 4. Secret scan of the full git history

**Method:**
- Ran `git fetch --all`. Scanned **all refs**: `main`, `origin/main` and `origin/dev`. That's **59 commits** (51 on `main`, plus 8 dev-only commits). No tags, no stashes, and `git fsck --unreachable` reported no dangling objects in this clone.
- Listed every path ever added or deleted on any ref.
- Ran a pattern grep of every tree at every commit for: `SECRET_KEY`, password/token/api-key/access-key words, `BEGIN … PRIVATE KEY`, AWS `AKIA…`, GitHub `ghp_`/`github_pat_`, `sk-…`, Slack `xox?-`, JWT `eyJ….eyJ`, and DB URLs with inline credentials.
- Ran a Shannon-entropy scan (>4.0 bits/char, ≥20 chars) over every added line in `git log --all -p`. `package-lock.json`, SVGs and PNGs were excluded.
- Inspected the committed SQLite files by content type and table/hash strings.
- No dedicated scanner (gitleaks or trufflehog) was installed. That would be a good CI addition.

**Results: no real credentials, private keys, API tokens or JWTs were ever committed.** Every hit is a placeholder default or a variable name. All entropy hits were identifiers and URLs (false positives).

| Item | Commit (first added → removed) | Path | Assessment |
|---|---|---|---|
| SQLite shared-memory index | `a27aaa2` → deleted in `b84cfeb` | `atlas.db-shm` (32 KB) | SQLite WAL index only. It holds no row data, schema, usernames or hashes (`CREATE TABLE`/bcrypt markers: none). No action needed. Still reachable in history |
| SQLite write-ahead log | `a27aaa2` → deleted in `b84cfeb` | `atlas.db-wal` (0 bytes) | Empty. No data |
| `atlas.db` itself | never committed | — | ✅ |
| Hard-coded JWT fallback secret | `610f812` (still present on `main` and `dev`) | `core/apps/auth/jwt.py` (moved to `core/auth/jwt.py` on `dev`) | Placeholder, but **functionally a real secret on any deployment that doesn't set `SECRET_KEY`** (see A1). Treat as compromised forever |
| Compose `SECRET_KEY` literal | `eed37f1` (still present) | `docker-compose.yml` | Same: public, so any deployment using it is forgeable |
| MinIO root user/password `minioadmin` | `eed37f1` (still present) | `docker-compose.yml`, `core/storage/minio.py` | Upstream MinIO default. Placeholder |
| Postgres password + credentialed `DATABASE_URL` | `4bcfb85` (on `origin/dev` only) | `docker-compose.yml` | Placeholder (`atlas`/`atlas`) |
| `.env` / `*.env` | only `frontend/.env.example` (`b976784`) | `frontend/.env.example` | Contains only `VITE_API_URL`. Fine |
| Private keys / certs (`*.pem`, `*.key`, `id_rsa*`, `*.p12`) | none | — | ✅ |
| Test passwords (`secret123` etc.) | many | `tests/*` | Test fixtures, not secrets |

**Recommendation:** no history rewrite is needed. Rewriting this public repo would cost more than it buys, because nothing real leaked. What *is* needed is making the placeholders non-functional: fail fast on missing or default `SECRET_KEY`, move compose secrets to an untracked `.env` with a committed `.env.example`, add `.env` to the root `.gitignore` and `.dockerignore`, and add gitleaks to CI. **If any real deployment ever ran with either default `SECRET_KEY`, rotate that key and consider every token issued under it compromised.**

---

## 5. Keep / refactor / rewrite

| Part | Decision | Reasons |
|---|---|---|
| **Object-centric single-table concept** | **Keep (idea)** | It fits Drive + Photos + future apps well, and the frontend and domain already think this way. |
| **DB schema & models** (`core/db/models.py`) | **Rewrite** | Add real FKs (`parent_id → objects.id`, `owner_id → users.id`, later `tenant_id`). Make `type` an enum (`file`, `folder`) and treat photos as a derived view or tag. Use proper `DateTime(timezone=True)` columns. Add indexes on `(owner_id, parent_id, deleted_at)` and `(owner_id, mime_type)`. Add a unique `(parent_id, name)` per owner if wanted, plus a `content_hash`. The current shape can't be evolved cleanly, because every migration would fight the text timestamps and missing constraints. |
| **Alembic migrations** | **Rewrite** (squash to a new baseline) | Three migrations, one of which fails on real data and none of which the runtime actually uses. Start from a fresh baseline that matches the new models. Remove `create_all` from app startup. Make `env.py` honour `DATABASE_URL` (dev already does). |
| **Domain layer** (`core/domain/objects.py`) | **Refactor** | The behaviours are right and worth porting: atomic upload, recursive trash, recovery. Restructure as a service with **required** owner/tenant scoping on every query. It should own the transaction (unit-of-work, no `commit=` flags), return typed models, and enforce invariants: parent must exist, be a folder and be owned by the caller; no cycles; permanent delete only from trash and recursive; restore restores the subtree. Replace the N+1 tree walk with a recursive CTE. |
| **Auth** (`core/apps/auth/`) | **Refactor** | The structure (register/login/dependency) is fine. Required changes: fail-fast settings, PyJWT, direct bcrypt/argon2, password policy, login rate limit, standard claims, defensive payload parsing, refresh/revocation (or cookie sessions). |
| **Authorization** | **Rewrite** | Move it from per-router helpers into the domain/service boundary as a single policy (`can_read/can_write(user, obj)`) that's applied to the target **and to every referenced id** (`parent_id`, `new_parent_id`). Return 404 for "not yours". This is the prerequisite for tenants and share links. |
| **Storage abstraction** (`core/storage/`) | **Refactor** | Keep the ABC. Change keys to be **backend-relative** (`objects/<uuid>`), never filesystem paths. Add `open/stream` and a `serve()` that returns a response or URL, so routers don't branch on env. Move recovery and tmp cleanup into the backend. Make MinIO stream with `length=-1, part_size=…`, set content-type, add `minio` to deps, and test it against a MinIO container. |
| **HTTP routers** | **Refactor** | Thin enough to keep, but they need consistent JSON bodies (upload metadata as form fields, not query params), Pydantic **response models** (dropping `storage`), bounded pagination with total counts, server-side search, an upload size limit, a server-side MIME sniff, and uniform error shapes. Decide whether Drive/Photos are separate routers or filters over one `/objects` resource. Today the split is arbitrary (`/folders` lives in Drive, `/objects/*/move` lives in Objects). |
| **Startup recovery** | **Refactor** | Make it idempotent and multi-worker safe. Scope by age (`uploading` older than N minutes) instead of "everything", and run it from a single process (a CLI/cron job) rather than every worker's lifespan. |
| **Config** | **Rewrite** | Use a single `pydantic-settings` `Settings` object, injected rather than imported as a global. That fixes test isolation, the `STORAGE_BACKEND` vs `ATLAS_STORAGE_BACKEND` mismatch, CORS origins and secret defaults in one place. |
| **Frontend shell** (React + Vite + Tailwind) | **Keep** | The modern stack builds cleanly, and the components are small and readable. |
| **Frontend structure** | **Refactor** | Add a router (URLs for folders and views), a typed API client (one module, 401 → logout), and a data-fetching layer (TanStack Query) to replace hand-rolled pagination state. Use real thumbnails from the backend. Add multi-upload with progress, a move UI and server-side search. Fix the lint errors, delete the template leftovers, and pick one UI language or add i18n. |
| **Tests** | **Rewrite** | Fixtures must build the app from an injected `Settings` and DB, so nothing touches `./atlas.db`. Add an `auth_headers(user)` fixture plus a two-user fixture. Cover every endpoint for the owner case **and** the other-user case (the O1–O3 regressions), the trash/restore/permanent semantics, upload limits, and a migration test (`alembic upgrade head` on an empty DB and on a seeded old one). Add frontend smoke tests (Vitest + Testing Library, or Playwright). |
| **CI** | **Refactor** | Pin deps with a lockfile (uv/pip-tools). Add jobs: backend tests + ruff + mypy, frontend lint + build, `alembic upgrade head` against SQLite and Postgres services, `docker build`, gitleaks and `pip-audit`/`npm audit`. Make it required on PRs. Today it's red and has been ignored. |
| **Docker / compose** | **Rewrite** | Use a multi-stage build, a non-root user and a healthcheck. Migrate at **runtime** via an entrypoint (dev's `entrypoint.sh` is the right idea). Read secrets from `.env` with no literals in compose. Run Postgres by default. Put MinIO behind a profile and don't publish its console port. Ship a frontend image (or a static build served by a reverse proxy). Fix `.dockerignore`. |
| **Dev scripts / docs** | **Refactor** | Add a cross-platform `Makefile`/`justfile` (or `uv run` tasks) next to `run.ps1`. Rewrite the README from the new config table and make it match the code. Replace the stock `frontend/README.md`. |

---

## 6. The unmerged `origin/dev` branch

`origin/dev` is 8 commits (+1594/−1076 lines over 44 files) ahead of `main` and hasn't been merged. The redesign should decide explicitly whether to build on it or treat it as a prototype. Quick findings:

**What it adds:**
- Postgres + `psycopg2`, an `entrypoint.sh` that migrates at runtime, and `alembic/env.py` honouring `DATABASE_URL` (these fix §1.5's two worst Docker bugs).
- A `core/` (platform) vs `apps/` (features) split.
- A tenant model with roles, plus token-based invites.
- Share links with expiry, listing and revocation.
- Pillow thumbnails on upload, backend search with debounce, and an admin stats endpoint.

**Test status on `dev`:** `tests/test_recovery.py` **fails at collection** (`ModuleNotFoundError: core.domain`, because the module moved). Excluding that file: **6 passed, 11 failed**, for the same auth-header reasons as `main`.

**Security notes worth carrying forward:**
- **Shared links keep working after the object is trashed.** `get_object_by_share_token` checks `status == "ready"` and expiry but not `deleted_at`.
- `tenant_id` and `role` are taken **from the JWT** and not re-checked against `TenantMember`. Demoting or removing a member doesn't take effect until the token expires, and with A1 a forged token can claim `role: "owner"` in any tenant.
- Invite redemption is check-then-update without a row lock or conditional update, so two concurrent joins can both consume one invite.
- Expiry comparisons are string comparisons of ISO timestamps (`expires_at < _utc_iso()`), which only works if every writer uses the identical format.
- `make_thumbnail` opens user images with Pillow with no `Image.MAX_IMAGE_PIXELS` / decompression-bomb guard and no file size cap. Thumbnails are generated in-request.
- The same placeholder secrets (A1, compose literals), plus Postgres `atlas/atlas`.

**Recommendation:** port dev's Docker/Alembic fixes and its `core/` vs `apps/` boundary into the redesign. Reimplement tenants, share links and invites on top of the new authorization layer (§5, "Authorization: rewrite") instead of merging them as-is.

---

## Appendix: how to reproduce

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATABASE_URL=sqlite:////tmp/ci_test.db ATLAS_DATA_DIR=/tmp/ci_data .venv/bin/python -m pytest -rA
```

```bash
cd frontend && npm ci && npx eslint . && npx vite build
```

Set `DATABASE_URL`, or the tests will create `./atlas.db` in the repo (§1.1).
