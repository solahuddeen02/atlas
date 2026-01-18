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
            parent_id INTEGER,
            storage_key TEXT,
            size INTEGER,
            mime_type TEXT,
            created_at TEXT,
            deleted_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def create_object(obj_type: str, name: str, parent_id: int | None = None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO objects (type, name, parent_id)
        VALUES (?, ?, ?)
        """,
        (obj_type, name, parent_id),
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

def list_objects(
    obj_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    conn = get_connection()
    cur = conn.cursor()

    query = """
        SELECT id, type, name, storage_key, size, mime_type, created_at
        FROM objects
    """
    conditions = []
    params = []

    # hide trash
    conditions.append("deleted_at IS NULL")

    if obj_type:
        conditions.append("type = ?")
        params.append(obj_type)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r[0],
            "type": r[1],
            "name": r[2],
            "storage": r[3],
            "size": r[4],
            "mime_type": r[5],
            "created_at": r[6],
        }
        for r in rows
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

def list_photos(
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
):
    conn = get_connection()
    cur = conn.cursor()

    query = """
        SELECT id, type, name, storage_key, size, mime_type, created_at
        FROM objects
    """
    conditions = [
        "mime_type LIKE 'image/%'",
        "deleted_at IS NULL",
    ]
    params = []

    if q:
        conditions.append("name LIKE ?")
        params.append(f"%{q}%")

    query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r[0],
            "type": "photo",
            "name": r[2],
            "storage": r[3],
            "size": r[4],
            "mime_type": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]

def list_drive_objects(
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
):
    conn = get_connection()
    cur = conn.cursor()

    query = """
        SELECT id, type, name, storage_key, size, mime_type, created_at
        FROM objects
    """
    conditions = ["type = 'file'", "deleted_at IS NULL"]
    params = []

    if q:
        conditions.append("name LIKE ?")
        params.append(f"%{q}%")

    query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r[0],
            "type": "file",
            "name": r[2],
            "storage": r[3],
            "size": r[4],
            "mime_type": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]

def create_folder(name: str, parent_id: int | None = None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO objects (type, name, parent_id, created_at)
        VALUES ('folder', ?, ?, datetime('now'))
        """,
        (name, parent_id),
    )
    folder_id = cur.lastrowid
    conn.commit()
    conn.close()
    return folder_id

def list_folder(parent_id: int | None):
    conn = get_connection()
    cur = conn.cursor()

    query = """
        SELECT id, type, name, size, mime_type, created_at
        FROM objects
    """
    conditions = ["deleted_at IS NULL"]
    params = []

    if parent_id is None:
        conditions.append("parent_id IS NULL")
    else:
        conditions.append("parent_id = ?")
        params.append(parent_id)

    query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY type DESC, name"

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r[0],
            "type": r[1],
            "name": r[2],
            "size": r[3],
            "mime_type": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]

def move_object(obj_id: int, new_parent_id: int | None):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE objects
        SET parent_id = ?
        WHERE id = ?
        """,
        (new_parent_id, obj_id),
    )

    conn.commit()
    conn.close()

def soft_delete_object(obj_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE objects
        SET deleted_at = datetime('now')
        WHERE id = ?
        """,
        (obj_id,),
    )
    conn.commit()
    conn.close()

def restore_object(obj_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE objects
        SET deleted_at = NULL
        WHERE id = ?
        """,
        (obj_id,),
    )
    conn.commit()
    conn.close()

def list_trash(limit: int = 20, offset: int = 0):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, type, name, deleted_at
        FROM objects
        WHERE deleted_at IS NOT NULL
        ORDER BY deleted_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )

    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r[0],
            "type": r[1],
            "name": r[2],
            "deleted_at": r[3],
        }
        for r in rows
    ]

def get_children_ids(parent_id: int) -> list[int]:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM objects WHERE parent_id = ? AND deleted_at IS NULL",
        (parent_id,),
    )

    rows = cur.fetchall()
    conn.close()

    return [r[0] for r in rows]

def trash_object_recursive(obj_id: int):
    now = datetime.utcnow().isoformat()

    conn = get_connection()
    cur = conn.cursor()

    # mark self
    cur.execute(
        "UPDATE objects SET deleted_at=? WHERE id=?",
        (now, obj_id),
    )

    conn.commit()
    conn.close()

    # find children
    children = get_children_ids(obj_id)

    for child_id in children:
        trash_object_recursive(child_id)