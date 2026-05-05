from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, desc, select, update
from sqlalchemy.orm import Session

from core.db.models import Object, ShareLink


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_object(
    db: Session,
    obj_type: str,
    name: str,
    parent_id: int | None = None,
    owner_id: int | None = None,
    tenant_id: int | None = None,
    *,
    commit: bool = True,
) -> int:
    obj = Object(
        type=obj_type,
        name=name,
        parent_id=parent_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        created_at=_utc_iso(),
        status="uploading",
    )
    db.add(obj)

    if commit:
        db.commit()
        db.refresh(obj)
    else:
        db.flush()
        db.refresh(obj)

    return obj.id


def attach_storage(db: Session, obj_id: int, storage_key: str, *, commit: bool = True) -> None:
    db.execute(update(Object).where(Object.id == obj_id).values(storage_key=storage_key))
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
        update(Object).where(Object.id == obj_id).values(
            size=size, mime_type=mime_type, created_at=created_at or _utc_iso()
        )
    )
    if commit:
        db.commit()


def set_status(db: Session, obj_id: int, status: str, *, commit: bool = True) -> None:
    db.execute(update(Object).where(Object.id == obj_id).values(status=status))
    if commit:
        db.commit()


def move_object(db: Session, obj_id: int, new_parent_id: int | None) -> None:
    db.execute(update(Object).where(Object.id == obj_id).values(parent_id=new_parent_id))
    db.commit()


def soft_delete_object(db: Session, obj_id: int) -> None:
    db.execute(update(Object).where(Object.id == obj_id).values(deleted_at=_utc_iso()))
    db.commit()


def rename_object(db: Session, obj_id: int, name: str) -> None:
    db.execute(update(Object).where(Object.id == obj_id).values(name=name))
    db.commit()


def restore_object(db: Session, obj_id: int) -> None:
    db.execute(update(Object).where(Object.id == obj_id).values(deleted_at=None))
    db.commit()


def permanent_delete_object(db: Session, obj_id: int) -> str | None:
    row = db.execute(select(Object.storage_key).where(Object.id == obj_id)).one_or_none()
    storage_key = row[0] if row else None
    db.execute(delete(Object).where(Object.id == obj_id))
    db.commit()
    return storage_key


def empty_trash(db: Session, tenant_id: int) -> list[str]:
    rows = db.execute(
        select(Object.storage_key).where(
            and_(Object.deleted_at.is_not(None), Object.tenant_id == tenant_id)
        )
    ).all()
    storage_keys = [r[0] for r in rows if r[0]]
    db.execute(
        delete(Object).where(
            and_(Object.deleted_at.is_not(None), Object.tenant_id == tenant_id)
        )
    )
    db.commit()
    return storage_keys


def get_object(db: Session, obj_id: int) -> dict[str, Any] | None:
    row = db.execute(
        select(Object.id, Object.type, Object.name, Object.storage_key, Object.status, Object.owner_id, Object.tenant_id)
        .where(Object.id == obj_id)
    ).one_or_none()
    if not row:
        return None
    return {"id": row[0], "type": row[1], "name": row[2], "storage": row[3], "status": row[4], "owner_id": row[5], "tenant_id": row[6]}


def list_objects(
    db: Session,
    obj_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    tenant_id: int | None = None,
) -> list[dict[str, Any]]:
    conditions = [Object.deleted_at.is_(None), Object.status == "ready"]
    if obj_type:
        conditions.append(Object.type == obj_type)
    if tenant_id is not None:
        conditions.append(Object.tenant_id == tenant_id)

    rows = db.execute(
        select(Object.id, Object.type, Object.name, Object.storage_key, Object.size, Object.mime_type, Object.created_at, Object.status)
        .where(and_(*conditions))
        .order_by(desc(Object.created_at))
        .limit(limit).offset(offset)
    ).all()
    return [{"id": r[0], "type": r[1], "name": r[2], "storage": r[3], "size": r[4], "mime_type": r[5], "created_at": r[6], "status": r[7]} for r in rows]


def list_photos(
    db: Session,
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
    tenant_id: int | None = None,
) -> list[dict[str, Any]]:
    conditions = [Object.deleted_at.is_(None), Object.status == "ready", Object.mime_type.like("image/%")]
    if q:
        conditions.append(Object.name.like(f"%{q}%"))
    if tenant_id is not None:
        conditions.append(Object.tenant_id == tenant_id)

    rows = db.execute(
        select(Object.id, Object.type, Object.name, Object.storage_key, Object.size, Object.mime_type, Object.created_at, Object.status)
        .where(and_(*conditions))
        .order_by(desc(Object.created_at))
        .limit(limit).offset(offset)
    ).all()
    return [{"id": r[0], "type": "photo", "name": r[2], "storage": r[3], "size": r[4], "mime_type": r[5], "created_at": r[6], "status": r[7]} for r in rows]


def create_folder(
    db: Session,
    name: str,
    parent_id: int | None = None,
    owner_id: int | None = None,
    tenant_id: int | None = None,
) -> int:
    folder = Object(type="folder", name=name, parent_id=parent_id, owner_id=owner_id, tenant_id=tenant_id, created_at=_utc_iso(), status="ready")
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder.id


def list_folder(
    db: Session,
    parent_id: int | None,
    tenant_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conditions = [Object.deleted_at.is_(None), Object.status == "ready"]
    conditions.append(Object.parent_id.is_(None) if parent_id is None else Object.parent_id == parent_id)
    if tenant_id is not None:
        conditions.append(Object.tenant_id == tenant_id)

    rows = db.execute(
        select(Object.id, Object.type, Object.name, Object.size, Object.mime_type, Object.created_at, Object.status)
        .where(and_(*conditions))
        .order_by(desc(Object.type), Object.name)
        .limit(limit).offset(offset)
    ).all()
    return [{"id": r[0], "type": r[1], "name": r[2], "size": r[3], "mime_type": r[4], "created_at": r[5], "status": r[6]} for r in rows]


def list_trash(
    db: Session,
    limit: int = 20,
    offset: int = 0,
    tenant_id: int | None = None,
) -> list[dict[str, Any]]:
    conditions = [Object.deleted_at.is_not(None)]
    if tenant_id is not None:
        conditions.append(Object.tenant_id == tenant_id)

    rows = db.execute(
        select(Object.id, Object.type, Object.name, Object.deleted_at, Object.status)
        .where(and_(*conditions))
        .order_by(desc(Object.deleted_at))
        .limit(limit).offset(offset)
    ).all()
    return [{"id": r[0], "type": r[1], "name": r[2], "deleted_at": r[3], "status": r[4]} for r in rows]


def get_children_ids(db: Session, parent_id: int) -> list[int]:
    rows = db.execute(
        select(Object.id).where(and_(Object.parent_id == parent_id, Object.deleted_at.is_(None), Object.status == "ready"))
    ).all()
    return [r[0] for r in rows]


def trash_object_recursive(db: Session, obj_id: int) -> None:
    now = _utc_iso()
    queue = [obj_id]
    while queue:
        current_id = queue.pop()
        db.execute(update(Object).where(Object.id == current_id).values(deleted_at=now))
        queue.extend(get_children_ids(db, current_id))
    db.commit()


def create_share_link(
    db: Session,
    obj_id: int,
    created_by: int,
    tenant_id: int,
    expires_at: str | None = None,
) -> str:
    token = secrets.token_urlsafe(32)
    link = ShareLink(
        token=token,
        object_id=obj_id,
        tenant_id=tenant_id,
        created_by=created_by,
        expires_at=expires_at,
        created_at=_utc_iso(),
    )
    db.add(link)
    db.commit()
    return token


def get_object_by_share_token(db: Session, token: str) -> dict[str, Any] | None:
    row = db.execute(
        select(
            ShareLink.expires_at,
            Object.id, Object.name, Object.storage_key, Object.mime_type, Object.status,
        )
        .join(Object, Object.id == ShareLink.object_id)
        .where(ShareLink.token == token)
    ).one_or_none()
    if not row:
        return None
    expires_at, obj_id, name, storage_key, mime_type, status = row
    if expires_at and expires_at < _utc_iso():
        return None
    if status != "ready":
        return None
    return {"id": obj_id, "name": name, "storage": storage_key, "mime_type": mime_type}


def recover_incomplete_uploads(db: Session, data_dir: str) -> dict[str, int]:
    recovered = deleted_files = deleted_tmps = 0

    try:
        if os.path.isdir(data_dir):
            for name in os.listdir(data_dir):
                if name.startswith(".") and name.endswith(".tmp"):
                    try:
                        os.remove(os.path.join(data_dir, name))
                        deleted_tmps += 1
                    except Exception:
                        pass
    except Exception:
        pass

    stuck = db.execute(select(Object.id, Object.storage_key).where(Object.status == "uploading")).all()
    if not stuck:
        return {"recovered": 0, "deleted_files": deleted_files, "deleted_tmp": deleted_tmps}

    for obj_id, storage_key in stuck:
        path = storage_key or os.path.join(data_dir, str(obj_id))
        if path and os.path.exists(path):
            try:
                os.remove(path)
                deleted_files += 1
            except Exception:
                pass
        db.execute(update(Object).where(Object.id == obj_id).values(status="failed", storage_key=None, size=None, mime_type=None))
        recovered += 1

    db.commit()
    return {"recovered": recovered, "deleted_files": deleted_files, "deleted_tmp": deleted_tmps}
