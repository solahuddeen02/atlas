from plugins.admin.router import router

manifest = {"name": "admin", "version": "1.0"}
routers = [router]


def migrate(engine):
    pass  # uses core tables
