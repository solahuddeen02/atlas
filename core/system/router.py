from __future__ import annotations

import subprocess

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth.dependencies import CurrentUser, get_current_user
from core.db.engine import engine
from core.db.session import get_db
import core.plugin_loader as plugin_loader

router = APIRouter(prefix="/system", tags=["system"])


def _require_owner(current_user: CurrentUser) -> None:
    if current_user.role != "owner":
        raise HTTPException(status_code=403, detail="owner only")


@router.get("/plugins")
def list_plugins(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_owner(current_user)
    return plugin_loader.list_installed(db)


class InstallRequest(BaseModel):
    name: str
    runtime: str = "inprocess"


@router.post("/plugins/install")
def install_plugin(
    body: InstallRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_owner(current_user)
    try:
        return plugin_loader.runtime_install(
            body.name, db, engine=engine, app=request.app, runtime_mode=body.runtime
        )
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=422, detail=f"pip install failed for 'atlas-plugin-{body.name}'")
    except ImportError:
        raise HTTPException(status_code=404, detail=f"plugin '{body.name}' not found")
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/plugins/{name}")
def uninstall_plugin(
    name: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_owner(current_user)
    result = plugin_loader.uninstall(name, db)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=f"plugin '{name}' not installed")
    if result["status"] == "blocked":
        raise HTTPException(status_code=409, detail=result["reason"])
    return result
