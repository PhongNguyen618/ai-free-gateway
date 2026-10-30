from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("ai_free_gateway.lifecycle")


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when an OS-backed instance lock indicates another process is already serving the DB."""
    pass


def get_process_creation_time(pid: int) -> float:
    """Retrieve creation time of a process to protect against PID reuse."""
    try:
        import psutil
        p = psutil.Process(pid)
        return p.create_time()
    except Exception:
        # Fallback using file stat or epoch
        return float(pid)


@dataclass
class InstanceMetadata:
    pid: int
    process_creation_time: float
    executable: str
    project_root: str
    instance_uuid: str
    started_at: str
    db_path: str


class InstanceLock:
    """
    OS-backed file lock preventing multiple gateway workers from corrupting the same SQLite database
    or executing concurrent orphan cleanups (N05). Automatically released upon process termination.
    """

    def __init__(self, db_path: str, project_root: Optional[Path] = None):
        db_p = Path(db_path).resolve()
        self.db_path = str(db_p)
        self.project_root = (project_root or Path.cwd()).resolve()
        self.lock_file_path = db_p.parent / f"{db_p.name}.lock"
        self.metadata_file_path = self.project_root / ".gateway.instance.json"
        self.pid_file_path = self.project_root / ".gateway.pid"

        self._file_handle: Optional[Any] = None
        self._metadata: Optional[InstanceMetadata] = None
        self._is_locked: bool = False

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    @property
    def is_acquired(self) -> bool:
        return self._is_locked

    @property
    def metadata(self) -> Optional[InstanceMetadata]:
        return self._metadata

    def acquire(self) -> InstanceMetadata:
        """
        Attempt to acquire an OS-backed exclusive non-blocking lock.
        Raises InstanceAlreadyRunningError if already locked by another live process.
        """
        pid = os.getpid()
        creation_time = get_process_creation_time(pid)
        inst_uuid = str(uuid.uuid4())

        # Open lock file
        try:
            self._file_handle = open(self.lock_file_path, "a+b")
            self._file_handle.seek(0, os.SEEK_END)
            if self._file_handle.tell() == 0:
                self._file_handle.write(b"1")
                self._file_handle.flush()
        except Exception as e:
            raise RuntimeError(f"Could not open instance lock file at {self.lock_file_path}: {e}")

        # Attempt OS-level non-blocking lock
        try:
            if sys.platform == "win32":
                import msvcrt
                # Seek to beginning before locking
                self._file_handle.seek(0)
                msvcrt.locking(self._file_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError, PermissionError):
            if self._file_handle:
                try:
                    self._file_handle.close()
                except Exception:
                    pass
                self._file_handle = None
            raise InstanceAlreadyRunningError(
                f"Another AI Free Gateway instance is already running with database: {self.db_path}"
            )

        self._is_locked = True
        self._metadata = InstanceMetadata(
            pid=pid,
            process_creation_time=creation_time,
            executable=sys.executable,
            project_root=str(self.project_root),
            instance_uuid=inst_uuid,
            started_at=datetime.now(timezone.utc).isoformat(),
            db_path=self.db_path,
        )

        # Atomic metadata write
        temp_meta = self.project_root / f".gateway.instance.tmp.{inst_uuid}"
        try:
            temp_meta.write_text(json.dumps(asdict(self._metadata), indent=2), encoding="utf-8")
            temp_meta.replace(self.metadata_file_path)
            self.pid_file_path.write_text(str(pid), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed writing instance metadata: {e}")
            temp_meta.unlink(missing_ok=True)

        logger.info(f"Acquired instance lock for PID {pid} (UUID: {inst_uuid})")
        return self._metadata

    def release(self) -> None:
        """Release OS lock and clean up metadata files owned by this instance."""
        if not self._is_locked or not self._file_handle:
            return

        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    self._file_handle.seek(0)
                    msvcrt.locking(self._file_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
        finally:
            try:
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None
            self._is_locked = False

        # Only clean up files if they match our instance UUID
        if self._metadata:
            try:
                if self.metadata_file_path.exists():
                    data = json.loads(self.metadata_file_path.read_text(encoding="utf-8"))
                    if data.get("instance_uuid") == self._metadata.instance_uuid:
                        self.metadata_file_path.unlink(missing_ok=True)
                        if self.pid_file_path.exists() and self.pid_file_path.read_text(encoding="utf-8").strip() == str(self._metadata.pid):
                            self.pid_file_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Error during metadata cleanup: {e}")

        try:
            self.lock_file_path.unlink(missing_ok=True)
        except Exception:
            pass

        logger.info("Released instance lock.")
