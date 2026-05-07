# Commands

## Backend

```powershell
# Start dev server (uvicorn --reload)
.\scripts\run.ps1 dev

# Install Python dependencies
.\scripts\run.ps1 bootstrap

# Run tests
.\scripts\run.ps1 test
```

## Alembic

```powershell
# Apply all pending migrations
.venv\Scripts\alembic upgrade head

# Create new migration (autogenerate from model changes)
.venv\Scripts\alembic revision --autogenerate -m "description"

# Rollback one step
.venv\Scripts\alembic downgrade -1

# Show current migration version
.venv\Scripts\alembic current
```

## Frontend

```powershell
# Install dependencies
cd frontend; npm install

# Start dev server (standard — localhost:5173)
cd frontend; npm run dev

# Start dev server bound to all interfaces (required for subdomain routing)
cd frontend; npm run dev -- --host
```

## Subdomain dev setup (atlas.local)

No extra tools needed — Vite handles subdomains directly.

```powershell
# 1. Add hosts entries (once only — opens UAC prompt)
.\scripts\setup-hosts.ps1

# 2. Start backend
.\scripts\run.ps1 dev

# 3. Start frontend bound to all interfaces
cd frontend; npm run dev -- --host
```

Open `http://atlas.local` (portal) in your browser.
Apps: `http://drive.atlas.local`, `http://photos.atlas.local`, `http://admin.atlas.local`

> `setup-hosts.ps1` adds a Windows port proxy (80 → 5173) so no nginx is needed in dev.
> Cookie domain `.atlas.local` is shared — login once, all subdomains authenticated.
> To undo: `.\scripts\setup-hosts.ps1 -Remove`

## Production (nginx + wildcard SSL)

nginx is only needed in production to serve on port 80/443.

```bash
# Build frontend
cd frontend && npm run build

# Copy nginx config and replace DOMAIN with your actual domain
cp nginx/prod.conf /etc/nginx/sites-available/atlas.conf
# edit: replace all "DOMAIN" occurrences with e.g. "myatlas.com"

# Wildcard SSL via Certbot (DNS challenge)
certbot certonly --manual --preferred-challenges dns \
  -d myatlas.com -d "*.myatlas.com"

nginx -t && systemctl reload nginx
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-secret-...` | JWT signing key — change in production |
| `ATLAS_DOMAIN` | `atlas.local` | Base domain for cookie scope and CORS |
| `ATLAS_COOKIE_SECURE` | `false` | Set `true` in production (requires HTTPS) |
| `ATLAS_DATA_DIR` | `data/` | Local file storage root |
| `ATLAS_STORAGE_BACKEND` | `local` | `local` or `minio` |
| `ATLAS_ENABLED_PLUGINS` | `objects,drive,photos` | Plugins loaded on startup |
| `VITE_API_URL` | `http://127.0.0.1:8000` | Backend URL for frontend |
| `VITE_BASE_DOMAIN` | `atlas.local` | Base domain for subdomain routing |
