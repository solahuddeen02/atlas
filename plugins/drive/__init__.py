from plugins.drive.router import router

manifest = {"name": "drive", "version": "1.0"}
routers = [router]


def migrate(engine):
    pass  # uses core tables
