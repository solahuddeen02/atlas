"""Alembic environment. Run through `atlas-admin migrate`, never with a URL in a file.

`atlas.kernel.db.migrate` opens the connection (as atlas_migrator, from Settings)
and passes it in `config.attributes["connection"]`. There is no offline mode and
no fallback database, so a misconfiguration can't silently create one.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection

from atlas.kernel.db.metadata import SCHEMA, metadata

# TODO(phase-7): add installed modules' version_locations (each labelled with the
# module id) and their schemas to include_name.


def _include_name(name: str | None, type_: str, parent_names: object) -> bool:
    if type_ == "schema":
        return name == SCHEMA
    return True


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        include_schemas=True,
        include_name=_include_name,
        version_table_schema=SCHEMA,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported. Use `atlas-admin migrate`.")

_connection = context.config.attributes.get("connection")
if not isinstance(_connection, Connection):
    raise RuntimeError("Run migrations with `atlas-admin migrate`, which supplies the connection.")
run_migrations(_connection)
