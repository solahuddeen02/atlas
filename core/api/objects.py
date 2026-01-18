from fastapi import APIRouter, UploadFile, File
from fastapi.responses import FileResponse
from core.domain.objects import (
    create_object,
    attach_storage,
    attach_metadata,
    list_objects,
    get_object,
    list_photos,
    list_drive_objects,
    create_folder,
    list_folder,
)
from core.storage.local import save_file
from datetime import datetime


router = APIRouter()

@router.post("/objects/upload")
def upload_object(
    obj_type: str,
    name: str,
    parent_id: int | None = None,
    file: UploadFile = File(...)
):
    obj_id = create_object(obj_type, name, parent_id)

    path = save_file(obj_id, file.file)

    size = file.size
    mime_type = file.content_type
    created_at = datetime.utcnow().isoformat()

    attach_storage(obj_id, path)
    attach_metadata(obj_id, size, mime_type, created_at)

    return {
        "id": obj_id,
        "name": name,
        "size": size,
        "mime_type": mime_type,
        "created_at": created_at,
    }


@router.get("/objects/{obj_id}/download")
def download_object(obj_id: int):
    obj = get_object(obj_id)
    if not obj:
        return {"error": "object not found"}

    return FileResponse(
        path=obj["storage"],
        filename=obj["name"],
        media_type="application/octet-stream"
    )

@router.get("/objects")
def list_objects_api(
    obj_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    return list_objects(
        obj_type=obj_type,
        limit=limit,
        offset=offset,
    )

@router.get("/photos")
def list_photos_api(
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
):
    return list_photos(limit, offset, q)

@router.get("/drive")
def list_drive_api(
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
):
    return list_drive_objects(limit, offset, q)

@router.post("/folders")
def create_folder_api(
    name: str,
    parent_id: int | None = None,
):
    folder_id = create_folder(name, parent_id)
    return {
        "id": folder_id,
        "name": name,
        "parent_id": parent_id,
        "type": "folder",
    }

@router.get("/folders/{folder_id}")
def list_folder_api(folder_id: int):
    return list_folder(folder_id)

@router.get("/drive/root")
def drive_root():
    return list_folder(None)
