from plugins.objects.router import router, share_router

manifest = {"name": "objects", "version": "1.0"}
routers = [router, share_router]


def migrate(engine):
    pass  # uses core tables (objects, share_links)


def startup(db):
    import os
    from core.bus import bus
    from plugins.objects.thumbnail import on_object_uploaded
    bus.subscribe("object.uploaded", on_object_uploaded)

    from core.objects.domain import recover_incomplete_uploads
    stats = recover_incomplete_uploads(db, data_dir=os.getenv("ATLAS_DATA_DIR", "data"))
    if stats["recovered"] or stats["deleted_files"] or stats["deleted_tmp"]:
        print(f"[objects] recovery: {stats}")
