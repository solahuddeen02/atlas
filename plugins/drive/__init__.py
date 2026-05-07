from plugins.drive.router import router

manifest = {"name": "drive", "version": "1.0", "depends_on": ["objects"]}
routers = [router]


def migrate(engine):
    pass  # uses core tables
