"""Unit of work: one transaction per use. The only way kernel code gets a Session."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy import Engine
from sqlalchemy.orm import Session


class UnitOfWork:
    """`with UnitOfWork(engine) as session:` commits on success, rolls back on error."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session: Session | None = None

    def __enter__(self) -> Session:
        self._session = Session(self._engine, autoflush=True, expire_on_commit=False)
        self._session.begin()
        return self._session

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None
