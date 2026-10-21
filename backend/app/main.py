from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.system import router as system_router
from app.config import Settings, settings
from app.database import Database
from app.logging_config import close_logging, configure_logging
from app.ocr.base import OcrEngine
from app.ocr.paddle import create_lazy_ocr_engine
from app.services.availability import (
    ServiceAvailabilityState,
    fail_closed_recovery,
)
from app.services.backups.errors import BackupBusyError, BackupError
from app.services.backups.maintenance import MaintenanceCoordinator
from app.services.backups.manager import BackupManager
from app.services.backups.recovery import (
    RestoreRecoverySentinel,
    recovery_public_detail,
)
from app.services.library.deletion_recovery import BatchDeletionJournal
from app.workers.ocr_worker import PersistentOcrWorker


class ServiceGateMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        availability: ServiceAvailabilityState,
        maintenance: MaintenanceCoordinator,
    ) -> None:
        self.app = app
        self.availability = availability
        self.maintenance = maintenance

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith("/api/") or path == "/api/health":
            await self.app(scope, receive, send)
            return

        snapshot = self.availability.snapshot()
        recovery = fail_closed_recovery(snapshot)
        if recovery is not None:
            await JSONResponse(
                status_code=503,
                content={
                    "code": "BACKUP_RECOVERY_REQUIRED",
                    "detail": recovery_public_detail(recovery),
                },
            )(scope, receive, send)
            return
        if snapshot.mode == "restart_required":
            await JSONResponse(
                status_code=503,
                content={
                    "code": "RESTART_REQUIRED",
                    "detail": "数据已恢复，请重启应用后继续使用",
                },
            )(scope, receive, send)
            return

        exclusive_endpoint = (scope.get("method"), path) in {
            ("POST", "/api/backups"),
            ("POST", "/api/backups/restore"),
        }
        if exclusive_endpoint:
            await self.app(scope, receive, send)
            return
        try:
            with self.maintenance.activity("API request"):
                await self.app(scope, receive, send)
        except BackupBusyError as error:
            await JSONResponse(
                status_code=error.status_code,
                content={
                    "code": error.code,
                    "detail": error.public_message,
                },
            )(scope, receive, send)


def create_app(
    current: Settings = settings, ocr_engine: OcrEngine | None = None
) -> FastAPI:
    database = Database(current)
    maintenance = MaintenanceCoordinator()
    availability = ServiceAvailabilityState()
    recovery_sentinel = RestoreRecoverySentinel(current.data_dir)
    worker = PersistentOcrWorker(
        database,
        current,
        ocr_engine
        or create_lazy_ocr_engine(
            current.ocr_engine,
            current.ocr_device,
            mock_fixture_path=current.mock_ocr_fixture_path,
        ),
        maintenance=maintenance,
    )
    backup_manager = BackupManager(
        database=database,
        settings=current,
        worker=worker,
        maintenance=maintenance,
        mark_restart_required=availability.require_restart,
        recovery_sentinel=recovery_sentinel,
        mark_recovery_required=availability.require_recovery,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log_handler = configure_logging(current.data_dir / "logs")
        try:
            pending_recovery = backup_manager.pending_recovery()
            if pending_recovery is not None:
                availability.require_recovery(pending_recovery)
            else:
                database.prepare()
                backup_manager.cleanup_startup()
                if current.auto_create_schema:
                    database.create_schema()
                with database.session_factory() as session:
                    BatchDeletionJournal(current.data_dir).recover(session)
                backup_manager.start_token_cleanup()
                if current.start_background_worker:
                    worker.start()
            yield
        finally:
            try:
                backup_manager.stop_token_cleanup()
            finally:
                try:
                    worker.stop()
                finally:
                    try:
                        database.dispose()
                    finally:
                        close_logging(log_handler)

    application = FastAPI(
        title=current.app_name,
        version=current.app_version,
        lifespan=lifespan,
    )
    application.add_middleware(
        ServiceGateMiddleware,
        availability=availability,
        maintenance=maintenance,
    )

    @application.exception_handler(BackupError)
    async def handle_backup_error(_: Request, error: BackupError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "detail": error.public_message},
        )

    application.include_router(system_router, prefix="/api")
    from app.api.backups import router as backups_router
    from app.api.imports import router as imports_router
    from app.api.library import router as library_router
    from app.api.ordering import router as ordering_router
    from app.api.practice import router as practice_router
    from app.api.review import router as review_router
    from app.api.wrong_book import router as wrong_book_router

    application.include_router(backups_router, prefix="/api")
    application.include_router(imports_router, prefix="/api")
    application.include_router(library_router, prefix="/api")
    application.include_router(ordering_router, prefix="/api")
    application.include_router(practice_router, prefix="/api")
    application.include_router(review_router, prefix="/api")
    application.include_router(wrong_book_router, prefix="/api")
    application.state.database = database
    application.state.settings = current
    application.state.ocr_worker = worker
    application.state.maintenance = maintenance
    application.state.backup_manager = backup_manager
    application.state.service_availability = availability
    mount_frontend(application, current.frontend_dist)
    return application


def mount_frontend(application: FastAPI, dist_dir: Path) -> None:
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        application.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    index_file = dist_dir / "index.html"
    if index_file.is_file():

        @application.get("/{path:path}", include_in_schema=False)
        def frontend(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/"):
                raise HTTPException(status_code=404, detail="未找到 API")
            requested = dist_dir / path
            if path and requested.is_file() and dist_dir in requested.resolve().parents:
                return FileResponse(requested)
            return FileResponse(index_file)


app = create_app()
