from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .storage import repository_data_dir


class RepositoryBusyError(RuntimeError):
    pass


_REGISTRY_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def repository_operation_lock(repo_root: Path) -> Iterator[None]:
    data_dir = repository_data_dir(repo_root)
    data_dir.mkdir(parents=True, exist_ok=True)
    key = str(data_dir)
    with _REGISTRY_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())

    if not thread_lock.acquire(blocking=False):
        raise RepositoryBusyError("该仓库正在执行其他操作，请稍后再试。")

    lock_file = (data_dir / ".operation.lock").open("a+b")
    try:
        _acquire_file_lock(lock_file)
        try:
            yield
        finally:
            _release_file_lock(lock_file)
    finally:
        lock_file.close()
        thread_lock.release()


def _acquire_file_lock(lock_file) -> None:
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise RepositoryBusyError("该仓库正在执行其他操作，请稍后再试。") from exc


def _release_file_lock(lock_file) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
