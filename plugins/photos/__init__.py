from plugins.photos.router import router

manifest = {"name": "photos", "version": "1.0"}
routers = [router]


def migrate(engine):
    pass  # uses core tables
