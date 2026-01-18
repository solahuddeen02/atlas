from core.db.database import get_connection
from datetime import datetime

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            storage_key TEXT,
            size INTEGER,
            mime_type TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def create_object(obj_type: str, name: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO objects (type, name, created_at) VALUES (?, ?, ?)",
        (obj_type, name, datetime.utcnow().isoformat())
    )
    obj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return obj_id

def attach_storage(obj_id: int, storage_key: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE objects SET storage_key=? WHERE id=?",
        (storage_key, obj_id)
    )
    conn.commit()
    conn.close()

def get_object(obj_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, type, name, storage_key FROM objects WHERE id=?",
        (obj_id,)
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "id": row[0],
        "type": row[1],
        "name": row[2],
        "storage": row[3],
    }

def list_objects():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, type, name, storage_key FROM objects ORDER BY id DESC"
    )
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": row[0],
            "type": row[1],
            "name": row[2],
            "storage": row[3],
        }
        for row in rows
    ]

def list_objects(
    obj_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    conn = get_connection()
    cur = conn.cursor()

    query = "SELECT id, type, name, storage_key, size, mime_type, created_at FROM objects"
    params = []

    if obj_type:
        query += " WHERE type=?"
        params.append(obj_type)

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    return [
    {
        "id": row[0],
        "type": row[1],
        "name": row[2],
        "storage": row[3],
        "size": row[4],
        "mime_type": row[5],
        "created_at": row[6],
    }
    for row in rows
]

def attach_metadata(obj_id: int, size: int, mime_type: str, created_at: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE objects
        SET size=?, mime_type=?, created_at=?
        WHERE id=?
        """,
        (size, mime_type, created_at, obj_id)
    )
    conn.commit()
    conn.close()

def list_photos(limit: int = 20, offset: int = 0):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, type, name, storage_key, size, mime_type, created_at
        FROM objects
        WHERE mime_type LIKE 'image/%'
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": row[0],
            "type": "photo",
            "name": row[2],
            "storage": row[3],
            "size": row[4],
            "mime_type": row[5],
            "created_at": row[6],
        }
        for row in rows
    ]
