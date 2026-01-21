# core/domain/objects.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, select, update
from sqlalchemy.orm import Session

from core.db.models import Object


def _utc_iso() -> str:
    return datetime.utcnow().isoformat()


# -------------------------
# Create / Update primitives
# -------------------------

def create_object(
    db: Session,
    obj_type: str,
    name: str,
    parent_id: int | None = None,
    *,
    commit: bool = True,
) -> int:
    """
    If commit=False: flush only (so caller can manage a bigger transaction).
    """
    obj = Object(
        type=obj_type,
        name=name,
        parent_id=parent_id,
        created_at=_utc_iso(),
    )
    db.add(obj)

    if commit:
        db.commit()
        db.refresh(obj)
    else:
        db.flush()       # assign PK without committing
        db.refresh(obj)  # load obj.id

    return obj.id


def attach_storage(
    db: Session,
    obj_id: int,
    storage_key: str,
    *,
    commit: bool = True,
) -> None:
    db.execute(
        update(Object)
        .where(Object.id == obj_id)
        .values(storage_key=storage_key)
    )
    if commit:
        db.commit()


def attach_metadata(
    db: Session,
    obj_id: int,
    size: int | None,
    mime_type: str | None,
    created_at: str | None,
    *,
    commit: bool = True,
) -> None:
    db.execute(
        update(Object)
        .where(Object.id == obj_id)
        .values(
            size=size,
            mime_type=mime_type,
            created_at=created_at or _utc_iso(),
        )
    )
    if commit:
        db.commit()


def move_object(db: Session, obj_id: int, new_parent_id: int | None) -> None:
    db.execute(
        update(Object)
        .where(Object.id == obj_id)
        .values(parent_id=new_parent_id)
    )
    db.commit()


def soft_delete_object(db: Session, obj_id: int) -> None:
    db.execute(
        update(Object)
        .where(Object.id == obj_id)
        .values(deleted_at=_utc_iso())
    )
    db.commit()


def restore_object(db: Session, obj_id: int) -> None:
    db.execute(
        update(Object)
        .where(Object.id == obj_id)
        .values(deleted_at=None)
    )
    db.commit()


# -------------------------
# Read queries
# -------------------------

def get_object(db: Session, obj_id: int) -> dict[str, Any] | None:
    stmt = select(Object.id, Object.type, Object.name, Object.storage_key).where(Object.id == obj_id)
    row = db.execute(stmt).one_or_none()
    if not row:
        return None

    return {
        "id": row[0],
        "type": row[1],
        "name": row[2],
        "storage": row[3],
    }


def list_objects(
    db: Session,
    obj_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conditions = [Object.deleted_at.is_(None)]
    if obj_type:
        conditions.append(Object.type == obj_type)

    stmt = (
        select(
            Object.id,
            Object.type,
            Object.name,
            Object.storage_key,
            Object.size,
            Object.mime_type,
            Object.created_at,
        )
        .where(and_(*conditions))
        .order_by(desc(Object.created_at))
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(stmt).all()
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


def list_photos(
    db: Session,
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
) -> list[dict[str, Any]]:
    conditions = [
        Object.deleted_at.is_(None),
        Object.mime_type.like("image/%"),
    ]
    if q:
        conditions.append(Object.name.like(f"%{q}%"))

    stmt = (
        select(
            Object.id,
            Object.type,
            Object.name,
            Object.storage_key,
            Object.size,
            Object.mime_type,
            Object.created_at,
        )
        .where(and_(*conditions))
        .order_by(desc(Object.created_at))
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(stmt).all()
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
    db: Session,
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
) -> list[dict[str, Any]]:
    conditions = [
        Object.deleted_at.is_(None),
        Object.type == "file",
    ]
    if q:
        conditions.append(Object.name.like(f"%{q}%"))

    stmt = (
        select(
            Object.id,
            Object.type,
            Object.name,
            Object.storage_key,
            Object.size,
            Object.mime_type,
            Object.created_at,
        )
        .where(and_(*conditions))
        .order_by(desc(Object.created_at))
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(stmt).all()
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


def create_folder(db: Session, name: str, parent_id: int | None = None) -> int:
    folder = Object(
        type="folder",
        name=name,
        parent_id=parent_id,
        created_at=_utc_iso(),
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder.id


def list_folder(db: Session, parent_id: int | None) -> list[dict[str, Any]]:
    conditions = [Object.deleted_at.is_(None)]
    if parent_id is None:
        conditions.append(Object.parent_id.is_(None))
    else:
        conditions.append(Object.parent_id == parent_id)

    stmt = (
        select(
            Object.id,
            Object.type,
            Object.name,
            Object.size,
            Object.mime_type,
            Object.created_at,
        )
        .where(and_(*conditions))
        # mimic: ORDER BY type DESC, name
        .order_by(desc(Object.type), Object.name)
    )

    rows = db.execute(stmt).all()
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


def list_trash(db: Session, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    stmt = (
        select(Object.id, Object.type, Object.name, Object.deleted_at)
        .where(Object.deleted_at.is_not(None))
        .order_by(desc(Object.deleted_at))
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(stmt).all()
    return [
        {
            "id": r[0],
            "type": r[1],
            "name": r[2],
            "deleted_at": r[3],
        }
        for r in rows
    ]


# -------------------------
# Recursive trash
# -------------------------

def get_children_ids(db: Session, parent_id: int) -> list[int]:
    stmt = select(Object.id).where(
        and_(
            Object.parent_id == parent_id,
            Object.deleted_at.is_(None),
        )
    )
    rows = db.execute(stmt).all()
    return [r[0] for r in rows]


def trash_object_recursive(db: Session, obj_id: int) -> None:
    """
    Mark object and all descendants as deleted.
    Uses the same DB session throughout recursion (much faster than reconnecting).
    """
    now = _utc_iso()

    db.execute(
        update(Object)
        .where(Object.id == obj_id)
        .values(deleted_at=now)
    )
    db.commit()

    children = get_children_ids(db, obj_id)
    for child_id in children:
        trash_object_recursive(db, child_id)
