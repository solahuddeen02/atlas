from fastapi import APIRouter, UploadFile, File
from core.domain.objects import create_object, attach_storage
from core.storage.local import save_file

from fastapi.responses import FileResponse
from core.domain.objects import get_object
from core.domain.objects import list_objects

router = APIRouter()

@router.post("/objects/upload")
def upload_object(
    obj_type: str,
    name: str,
    file: UploadFile = File(...)
):
    # 1. create object
    obj_id = create_object(obj_type, name)

    # 2. save file
    path = save_file(obj_id, file.file)

    # 3. update storage ref
    attach_storage(obj_id, path)

    return {
        "id": obj_id,
        "name": name,
        "storage": path
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
