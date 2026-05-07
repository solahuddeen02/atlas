# core/storage/factory.py
from __future__ import annotations

import os

from core.storage.base import StorageBackend


def get_storage(tenant_slug: str | None = None) -> StorageBackend:
    """Return the configured storage backend, optionally namespaced to a tenant.

    tenant_slug=None  → global layout (data/ or MinIO bucket root)
    tenant_slug="xyz" → tenant-namespaced (data/xyz/ or MinIO prefix xyz/)

    Isolation levels:
      single   — slug is always None
      shared   — slug passed per-request, files namespaced by slug
      isolated — slug maps to a separate storage path/bucket
    """
    backend = os.getenv("STORAGE_BACKEND", "local")

    if backend == "minio":
        from core.storage.minio import MinIOStorage
        return MinIOStorage(tenant_slug=tenant_slug)

    from core.storage.local import LocalStorage
    return LocalStorage(tenant_slug=tenant_slug)
