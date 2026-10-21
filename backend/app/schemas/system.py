from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app_name: str
    version: str
    host: str
    code: str | None = None
    detail: str | None = None
    safety_backup_id: str | None = None
    safety_backup_filename: str | None = None

