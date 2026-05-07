from plugins.tenant.router import router

manifest = {"name": "tenant", "version": "1.0"}
routers = [router]


def migrate(engine):
    pass  # uses core tables (invite_tokens)
