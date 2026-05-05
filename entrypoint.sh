#!/bin/sh
set -e
alembic upgrade head
exec uvicorn core.main:app --host 0.0.0.0 --port 8000
