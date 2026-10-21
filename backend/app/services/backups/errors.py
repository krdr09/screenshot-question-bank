class BackupError(Exception):
    def __init__(self, code: str, public_message: str, status_code: int) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.status_code = status_code


class BackupValidationError(BackupError):
    def __init__(self, message: str = "备份文件无效，无法恢复") -> None:
        super().__init__("BACKUP_INVALID", message, 400)


class BackupBusyError(BackupError):
    def __init__(self, message: str = "系统正在处理其他任务，请稍后重试") -> None:
        super().__init__("BACKUP_BUSY", message, 409)


class BackupNotFoundError(BackupError):
    def __init__(self, message: str = "备份不存在") -> None:
        super().__init__("BACKUP_NOT_FOUND", message, 404)


class BackupTokenExpiredError(BackupError):
    def __init__(self, message: str = "恢复验证已过期，请重新验证备份") -> None:
        super().__init__("BACKUP_TOKEN_EXPIRED", message, 410)


class BackupRestoreError(BackupError):
    def __init__(self, message: str = "恢复失败，原有数据已保留") -> None:
        super().__init__("BACKUP_RESTORE_FAILED", message, 500)


class BackupRecoveryRequiredError(BackupError):
    def __init__(self, message: str) -> None:
        super().__init__("BACKUP_RECOVERY_REQUIRED", message, 500)


class BackupInternalError(BackupError):
    def __init__(self, message: str = "备份操作失败，请查看本地日志") -> None:
        super().__init__("BACKUP_OPERATION_FAILED", message, 500)
