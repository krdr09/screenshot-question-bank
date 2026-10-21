from fastapi import APIRouter, Request

from app.schemas.system import HealthResponse
from app.services.availability import fail_closed_recovery
from app.services.backups.recovery import recovery_public_detail


router = APIRouter(tags=["系统"])


@router.get(
    "/health",
    response_model=HealthResponse,
    response_model_exclude_none=True,
)
def health(request: Request) -> HealthResponse:
    current = request.app.state.settings
    availability = request.app.state.service_availability
    snapshot = availability.snapshot()
    recovery = fail_closed_recovery(snapshot)
    if recovery is not None:
        return HealthResponse(
            status="recovery_required",
            app_name=current.app_name,
            version=current.app_version,
            host=current.host,
            code="BACKUP_RECOVERY_REQUIRED",
            detail=recovery_public_detail(recovery),
            safety_backup_id=recovery.safety_backup_id,
            safety_backup_filename=recovery.safety_backup_filename,
        )
    return HealthResponse(
        status="ok",
        app_name=current.app_name,
        version=current.app_version,
        host=current.host,
    )
