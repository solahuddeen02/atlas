FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/db /app/data

ENV DATABASE_URL=sqlite:///./db/atlas.db
ENV ATLAS_DATA_DIR=data

RUN alembic upgrade head

EXPOSE 8000

CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]