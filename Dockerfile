FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data && chmod +x entrypoint.sh

ENV ATLAS_DATA_DIR=data

EXPOSE 8000

CMD ["./entrypoint.sh"]
