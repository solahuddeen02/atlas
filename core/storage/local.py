import os
import shutil

BASE_PATH = "data/objects"

def save_file(object_id: int, file):
    os.makedirs(BASE_PATH, exist_ok=True)
    path = os.path.join(BASE_PATH, str(object_id))
    with open(path, "wb") as buffer:
        shutil.copyfileobj(file, buffer)
    return path
