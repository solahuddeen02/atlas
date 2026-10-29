# core/apps/objects/access.py
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from core.db.models import User
from core.domain.objects import get_object

# Upper bound on folder depth when walking ancestors; guards against pre-existing cycles.
MAX_ANCESTOR_DEPTH = 10_000


def get_owned_object(db: Session, obj_id: int, current_user: User) -> dict:
    """
    Return the object if it exists and belongs to current_user.
    "Not yours" is reported exactly like "not found" (404) so ids can't be probed.
    """
    obj = get_object(db, obj_id)
    if not obj or obj["owner_id"] != current_user.id:
        raise HTTPException(status_code=404, detail="object not found")
    return obj


def get_owned_folder(db: Session, folder_id: int, current_user: User) -> dict:
    """
    Return the folder if it is current_user's own, live (not trashed) folder; 404 otherwise.
    Use this for every id that refers to a *target* folder (parent_id, new_parent_id).
    """
    obj = get_object(db, folder_id)
    if (
        not obj
        or obj["owner_id"] != current_user.id
        or obj["type"] != "folder"
        or obj["deleted_at"] is not None
    ):
        raise HTTPException(status_code=404, detail="folder not found")
    return obj


def ensure_not_moving_into_itself(db: Session, obj_id: int, target_folder: dict) -> None:
    """Reject moving an object into itself or into one of its own descendants (400)."""
    current = target_folder
    for _ in range(MAX_ANCESTOR_DEPTH):
        if current["id"] == obj_id:
            raise HTTPException(status_code=400, detail="cannot move an object into itself or its descendants")
        parent_id = current["parent_id"]
        if parent_id is None:
            return
        current = get_object(db, parent_id)
        if current is None:
            return
    raise HTTPException(status_code=400, detail="folder hierarchy too deep or cyclic")
