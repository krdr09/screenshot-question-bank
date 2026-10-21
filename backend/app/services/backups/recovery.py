from __future__ import annotations

import json
import logging
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


logger = logging.getLogger(__name__)
SENTINEL_VERSION = 1
SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})?\Z")
UTC_ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|\+00:00)\Z"
)


@dataclass(frozen=True)
class RestoreRecoveryState:
    operation_id: str | None
    safety_backup_id: str | None
    safety_backup_filename: str | None
    created_at: str | None


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and SAFE_IDENTIFIER.fullmatch(value) is not None


def _valid_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or UTC_ISO_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() == timedelta(0)


def _valid_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 255
        and value not in {".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
        and not any(character in '<>:"|?*' for character in value)
        and not value.endswith((" ", "."))
        and not any(ord(character) < 32 for character in value)
    )


def _validated_state(payload: object) -> RestoreRecoveryState:
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != SENTINEL_VERSION
    ):
        raise ValueError("invalid recovery sentinel")
    operation_id = payload.get("operation_id")
    safety_backup_id = payload.get("safety_backup_id")
    safety_backup_filename = payload.get("safety_backup_filename")
    created_at = payload.get("created_at")
    if not (
        _valid_identifier(operation_id)
        and _valid_identifier(safety_backup_id)
        and _valid_basename(safety_backup_filename)
        and _valid_utc_timestamp(created_at)
    ):
        raise ValueError("invalid recovery sentinel")
    return RestoreRecoveryState(
        operation_id=operation_id,
        safety_backup_id=safety_backup_id,
        safety_backup_filename=safety_backup_filename,
        created_at=created_at,
    )


def recovery_public_detail(state: RestoreRecoveryState) -> str:
    backup = (
        f"预恢复安全备份：{state.safety_backup_filename}"
        if state.safety_backup_filename
        else "预恢复安全备份"
    )
    return (
        "恢复失败且自动回滚不完整，系统已停止写入。"
        f"请停止应用，保留回滚材料，并使用{backup}进行人工恢复。"
    )


class RestoreRecoverySentinel:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / ".restore-recovery-required.json"

    def arm(
        self,
        *,
        operation_id: str,
        safety_backup_id: str,
        safety_backup_filename: str,
    ) -> RestoreRecoveryState:
        state = RestoreRecoveryState(
            operation_id=operation_id,
            safety_backup_id=safety_backup_id,
            safety_backup_filename=safety_backup_filename,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        payload = {"version": SENTINEL_VERSION, **asdict(state)}
        _validated_state(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f"{self.path.stem}.{uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as target:
                json.dump(payload, target, ensure_ascii=False, sort_keys=True)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("恢复哨兵临时文件清理失败", exc_info=True)
            raise
        return state

    def load(self) -> RestoreRecoveryState | None:
        try:
            marker_stat = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            logger.critical("恢复哨兵无法访问，应用将保持封锁", exc_info=True)
            return RestoreRecoveryState(None, None, None, None)
        if not stat.S_ISREG(marker_stat.st_mode):
            return RestoreRecoveryState(None, None, None, None)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return _validated_state(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.critical("恢复哨兵无法解析，应用将保持封锁", exc_info=True)
            return RestoreRecoveryState(None, None, None, None)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
