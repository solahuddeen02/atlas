# Commands

## Backend

```bash
# Start dev server
.\scripts\run.ps1 dev

# Install dependencies
.\scripts\run.ps1 bootstrap

# Run tests
.\scripts\run.ps1 test
```

## Alembic

```bash
# Apply all pending migrations
.venv\Scripts\python.exe -m alembic upgrade head

# Create new migration (autogenerate from model changes)
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "description"

# Rollback one step
.venv\Scripts\python.exe -m alembic downgrade -1

# Show current migration version
.venv\Scripts\python.exe -m alembic current
```

## Frontend

```bash
# Start dev server
cd frontend
npm run dev

# Install dependencies
npm install
```
