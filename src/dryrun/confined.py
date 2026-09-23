"""Path operations confined beneath a root directory (spec S4): every component is opened with
O_NOFOLLOW|O_DIRECTORY relative to its parent fd and the final step uses *at() syscalls, so a directory
swapped for a symlink between shadow and commit cannot redirect a write outside the root."""
from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat

from dryrun.fingerprint import Fp, fp_of

_libc = ctypes.CDLL(None, use_errno=True)
_libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
AT_FDCWD = -100
RENAME_NOREPLACE = 1
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_PATH = getattr(os, "O_PATH", 0o10000000) | os.O_NOFOLLOW | os.O_CLOEXEC


class ConfinementError(OSError):
    pass


def _parts(rel: str) -> list[str]:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\0" in rel:
        raise ConfinementError(errno.EINVAL, f"bad path {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ConfinementError(errno.EINVAL, f"bad path {rel!r}")
    return parts


def _renameat2(src_dir: int, src: str, dst_dir: int, dst: str, flags: int) -> None:
    if _libc.renameat2(src_dir, os.fsencode(src), dst_dir, os.fsencode(dst), flags) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), dst)


def _chmod_fd_path(dir_fd: int, name: str, mode: int) -> None:
    pfd = os.open(name, _PATH, dir_fd=dir_fd)
    try:
        if stat.S_ISLNK(os.fstat(pfd).st_mode):
            return
        os.chmod(f"/proc/self/fd/{pfd}", mode)
    finally:
        os.close(pfd)


class Root:
    def __init__(self, path: str | os.PathLike) -> None:
        self.path = os.fspath(path)
        self.fd = os.open(self.path, _DIR)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "Root":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _parent(self, rel: str) -> tuple[int, str]:
        parts = _parts(rel)
        fd = os.dup(self.fd)
        try:
            for comp in parts[:-1]:
                try:
                    nfd = os.open(comp, _DIR, dir_fd=fd)
                except OSError as e:
                    if e.errno in (errno.ELOOP, errno.ENOTDIR):
                        st = os.stat(comp, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISLNK(st.st_mode):
                            raise ConfinementError(e.errno, f"{rel}: component {comp!r} is a symlink")
                        # A plain non-directory (e.g. a file the ChangeSet replaces with a directory):
                        # the target simply does not exist yet. Not a confinement problem.
                        raise NotADirectoryError(errno.ENOTDIR, f"{rel}: component {comp!r} is not a directory")
                    raise
                os.close(fd)
                fd = nfd
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def lstat(self, rel: str) -> os.stat_result | None:
        try:
            fd, name = self._parent(rel)
        except (FileNotFoundError, NotADirectoryError):
            return None
        try:
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        finally:
            os.close(fd)

    def readlink(self, rel: str) -> str:
        fd, name = self._parent(rel)
        try:
            return os.readlink(name, dir_fd=fd)
        finally:
            os.close(fd)

    def unlink(self, rel: str) -> None:
        fd, name = self._parent(rel)
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(fd)

    def rmtree(self, rel: str) -> None:
        fd, name = self._parent(rel)
        try:
            _rmtree_at(fd, name)
        finally:
            os.close(fd)

    def mkdir(self, rel: str, mode: int = 0o700) -> None:
        fd, name = self._parent(rel)
        try:
            os.mkdir(name, mode, dir_fd=fd)
        except FileExistsError:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                raise
        finally:
            os.close(fd)

    def rename_in(self, src_abs: str, rel: str, *, noreplace: bool) -> None:
        fd, name = self._parent(rel)
        flags = RENAME_NOREPLACE if noreplace else 0
        try:
            try:
                _renameat2(AT_FDCWD, src_abs, fd, name, flags)
            except OSError as e:
                if e.errno != errno.EXDEV:
                    if e.errno == errno.EEXIST:
                        raise FileExistsError(e.errno, e.strerror, rel) from None
                    raise
                self._copy_then_rename(src_abs, fd, name, flags)
        finally:
            os.close(fd)

    @staticmethod
    def _copy_then_rename(src_abs: str, dir_fd: int, name: str, flags: int) -> None:
        tmp = f".{name}.dryrun-{secrets.token_hex(4)}"
        st = os.lstat(src_abs)
        with open(src_abs, "rb") as src:
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                          dir_fd=dir_fd)
            try:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    os.write(out, chunk)
                os.fchmod(out, stat.S_IMODE(st.st_mode))
                os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
                os.fsync(out)
            finally:
                os.close(out)
        _renameat2(dir_fd, tmp, dir_fd, name, flags)
        os.unlink(src_abs)

    def symlink(self, rel: str, target: str) -> None:
        fd, name = self._parent(rel)
        try:
            os.symlink(target, name, dir_fd=fd)
        finally:
            os.close(fd)

    def chmod(self, rel: str, mode: int) -> None:
        fd, name = self._parent(rel)
        try:
            _chmod_fd_path(fd, name, mode)
        finally:
            os.close(fd)

    def fsync_parent(self, rel: str) -> None:
        try:
            fd, name = self._parent(rel)
        except (FileNotFoundError, NotADirectoryError, ConfinementError):
            return
        try:
            os.fsync(fd)
            try:
                ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
            except OSError:
                return
            try:
                os.fsync(ffd)
            finally:
                os.close(ffd)
        finally:
            os.close(fd)

    def fingerprint_subtree(self, rel: str) -> dict[str, Fp]:
        fd, name = self._parent(rel)
        out: dict[str, Fp] = {}
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            out[rel] = fp_of(st)
            if stat.S_ISDIR(st.st_mode):
                _walk_at(fd, name, rel, out)
        finally:
            os.close(fd)
        return out


def _open_dir_at(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _DIR, dir_fd=parent_fd)
    except PermissionError:
        _chmod_fd_path(parent_fd, name, 0o700)
        return os.open(name, _DIR, dir_fd=parent_fd)


def _walk_at(parent_fd: int, name: str, rel: str, out: dict[str, Fp]) -> None:
    dfd = _open_dir_at(parent_fd, name)
    try:
        for entry in os.scandir(dfd):
            st = entry.stat(follow_symlinks=False)
            child = f"{rel}/{entry.name}"
            out[child] = fp_of(st)
            if stat.S_ISDIR(st.st_mode):
                _walk_at(dfd, entry.name, child, out)
    finally:
        os.close(dfd)


def _rmtree_at(parent_fd: int, name: str) -> None:
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    dfd = _open_dir_at(parent_fd, name)
    try:
        if (st.st_mode & 0o300) != 0o300:
            _chmod_fd_path(parent_fd, name, stat.S_IMODE(st.st_mode) | 0o700)
        for entry in list(os.scandir(dfd)):
            _rmtree_at(dfd, entry.name)
    finally:
        os.close(dfd)
    os.rmdir(name, dir_fd=parent_fd)
