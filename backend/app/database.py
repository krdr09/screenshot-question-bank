from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, settings: Settings) -> None:
        self.url = settings.resolved_database_url
        self.engine = create_engine(
            self.url,
            connect_args={"check_same_thread": False}
            if self.url.startswith("sqlite")
            else {},
        )
        if self.url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )

    @staticmethod
    def _ensure_sqlite_parent(url: str) -> None:
        prefix = "sqlite:///"
        if not url.startswith(prefix) or url == "sqlite:///:memory:":
            return
        Path(url.removeprefix(prefix)).parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_schema(self) -> None:
        from app import models  # noqa: F401

        self.prepare()
        Base.metadata.create_all(self.engine)

    def prepare(self) -> None:
        self._ensure_sqlite_parent(self.url)

    def dispose(self) -> None:
        self.engine.dispose()

    def sqlite_path(self) -> Path:
        url = make_url(self.url)
        if (
            url.drivername != "sqlite"
            or not url.database
            or url.database == ":memory:"
        ):
            raise ValueError("完整备份只支持本地 SQLite 文件数据库")
        return Path(url.database).resolve()


def get_session(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.database
    with database.session_factory() as session:
        yield session

