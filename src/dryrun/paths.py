"""Where Dry Run keeps things. Environment variables override every location (tests use them)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

BWRAP_VERSION = "0.11.0"


def state_dir() -> Path:
    return Path(os.environ.get("DRYRUN_STATE_DIR") or Path.home() / ".local" / "state" / "dryrun")


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def socket_path() -> Path:
    return Path(os.environ.get("DRYRUN_SOCKET") or runtime_dir() / "dryrun.sock")


def bwrap_path() -> Path:
    default = Path.home() / ".local" / "share" / "dryrun" / "bwrap" / BWRAP_VERSION / "bwrap"
    return Path(os.environ.get("DRYRUN_BWRAP") or default)


def ensure_private_dir(path: Path) -> Path:
    """Create path (and parents) owned by us with mode 0700; refuse symlinks and foreign owners."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"{path} is not a real directory")
    if st.st_uid != os.getuid():
        raise PermissionError(f"{path} is owned by uid {st.st_uid}")
    os.chmod(path, 0o700)
    return path
