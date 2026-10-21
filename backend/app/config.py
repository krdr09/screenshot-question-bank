from pathlib import Path

from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.portable_settings import (
    PortableSettingsSource,
    configured_data_dir,
    validate_relationship_setting_values,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """本地应用设置；服务监听地址被限制为回环地址。"""

    model_config = SettingsConfigDict(
        env_prefix="QUESTION_PRACTICE_",
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
    )

    app_name: str = "截图题库 OCR 与刷题系统"
    app_version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str | None = None
    auto_create_schema: bool = False
    start_background_worker: bool = True
    ocr_engine: Literal["mock", "paddle"] = Field(
        default="paddle",
        validation_alias=AliasChoices("OCR_ENGINE", "QUESTION_PRACTICE_OCR_ENGINE"),
    )
    ocr_device: Literal["auto", "cpu", "gpu"] = Field(
        default="auto",
        validation_alias=AliasChoices("OCR_DEVICE", "QUESTION_PRACTICE_OCR_DEVICE"),
    )
    ocr_confidence_threshold: float = Field(
        default=0.98,
        ge=0,
        le=1,
        validation_alias=AliasChoices(
            "OCR_CONFIDENCE_THRESHOLD",
            "QUESTION_PRACTICE_OCR_CONFIDENCE_THRESHOLD",
        ),
    )
    mock_ocr_fixture_path: Path | None = None
    frontend_dist: Path = PROJECT_ROOT / "frontend" / "dist"
    max_import_files: int = 200
    max_single_file_bytes: int = 25 * 1024 * 1024
    max_import_bytes: int = 2 * 1024 * 1024 * 1024
    overlap_band_ratio: float = Field(default=0.45, gt=0, le=1)
    overlap_text_weight: float = Field(default=0.6, ge=0, le=1)
    overlap_visual_weight: float = Field(default=0.4, ge=0, le=1)
    overlap_candidate_threshold: float = Field(default=0.55, ge=0, le=1)
    overlap_auto_threshold: float = Field(default=0.85, ge=0, le=1)
    overlap_ambiguity_margin: float = Field(default=0.03, ge=0, le=1)
    overlap_visual_min_ratio: float = Field(default=0.15, gt=0, le=1)
    overlap_visual_max_ratio: float = Field(default=0.45, gt=0, le=1)
    overlap_visual_ratio_step: float = Field(default=0.05, gt=0, le=1)
    duplicate_text_threshold: float = Field(default=0.97, ge=0, le=1)
    duplicate_hash_distance: int = Field(default=6, ge=0, le=64)
    duplicate_review_margin: float = Field(default=0.03, ge=0, le=1)
    duplicate_hash_review_margin: int = Field(default=2, ge=0, le=64)
    max_backup_upload_bytes: int = Field(default=4 * 1024**3, gt=0)
    max_backup_entries: int = Field(default=10_000, gt=0)
    max_backup_member_bytes: int = Field(default=4 * 1024**3, gt=0)
    max_backup_uncompressed_bytes: int = Field(default=8 * 1024**3, gt=0)
    max_backup_compression_ratio: float = Field(default=200.0, gt=1)
    max_backup_path_length: int = Field(default=500, gt=0)
    backup_token_ttl_seconds: int = Field(default=1_800, gt=0)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        portable_source = PortableSettingsSource(
            settings_cls,
            configured_data_dir(init_settings, PROJECT_ROOT / "data"),
        )
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            portable_source,
            file_secret_settings,
        )

    @field_validator("host")
    @classmethod
    def enforce_loopback(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("服务只允许绑定 127.0.0.1")
        return value

    @model_validator(mode="after")
    def enforce_relationship_invariants(self):
        validate_relationship_setting_values(self)
        return self

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"


settings = Settings()
