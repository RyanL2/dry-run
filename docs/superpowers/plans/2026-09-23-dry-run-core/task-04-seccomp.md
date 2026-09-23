### Task 4: Seccomp filter (isolation I2, I4, I6, I7)

**Files:**
- Create: `src/dryrun/sandbox/__init__.py` (empty), `src/dryrun/sandbox/seccomp.py`, `tests/unit/test_seccomp.py`

**Interfaces:**
- Produces:
  - `build_filter() -> bytes`: raw `struct sock_filter[]` for x86_64, in the format `bwrap --seccomp FD` reads
  - `DENIED_SYSCALLS: dict[str, int]`

The filter behaves as follows:

| Call | Result |
|---|---|
| a non-x86_64 architecture | kills the process |
| x32 syscalls | EPERM |
| everything in `DENIED_SYSCALLS` | EPERM |
| `clone3` | ENOSYS, so glibc falls back to `clone` |
| `clone` with `CLONE_NEWUSER` | EPERM |
| `socket(AF_UNIX, …)` | EAFNOSUPPORT (`socketpair` stays allowed) |
| `ioctl` TIOCSTI / TIOCLINUX | EPERM |
| anything else | allowed |

- [ ] **Step 1: Write the failing test**

`tests/unit/test_seccomp.py`:
```python
from __future__ import annotations

import errno
import json
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from dryrun.sandbox.seccomp import DENIED_SYSCALLS, build_filter

pytestmark = pytest.mark.skipif(platform.machine() != "x86_64" or sys.platform != "linux",
                                reason="x86_64 Linux only")

CHILD = r'''
import ctypes, errno, json, os, socket, subprocess, sys
libc = ctypes.CDLL(None, use_errno=True)
data = open(sys.argv[1], "rb").read()
buf = ctypes.create_string_buffer(data, len(data))
class Prog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]
prog = Prog(len(data) // 8, ctypes.cast(buf, ctypes.c_void_p))
assert libc.prctl(38, 1, 0, 0, 0) == 0              # PR_SET_NO_NEW_PRIVS
assert libc.prctl(22, 2, ctypes.byref(prog), 0, 0) == 0  # PR_SET_SECCOMP, FILTER
def sock(family):
    try:
        socket.socket(family, socket.SOCK_STREAM).close(); return 0
    except OSError as e:
        return e.errno
def sysc(nr, *args):
    r = libc.syscall(nr, *args)
    return 0 if r >= 0 else ctypes.get_errno()
res = {
    "af_unix": sock(socket.AF_UNIX),
    "af_vsock": sock(40),
    "af_packet": sock(17),
    "af_inet": sock(socket.AF_INET),
    "af_inet6": sock(socket.AF_INET6),
    "socketpair": len(socket.socketpair()),
    "io_uring_setup": sysc(425, 1, None),
    "keyctl": sysc(250, 0, 0, 0, 0, 0),
    "ptrace": sysc(101, 0, 0, 0, 0),
    "unshare_user": sysc(272, 0x10000000),
    "clone3": sysc(435, None, 0),
    "subprocess_true": subprocess.run(["true"]).returncode,
}
print(json.dumps(res))
'''


def test_filter_is_whole_instructions_and_checks_arch_first():
    prog = build_filter()
    assert len(prog) % 8 == 0 and 8 < len(prog) < 8 * 255
    code, jt, jf, k = __import__("struct").unpack("<HBBI", prog[:8])
    assert (code, k) == (0x20, 4)  # BPF_LD|BPF_W|BPF_ABS, offsetof(seccomp_data, arch)


def test_denylist_contains_the_dangerous_calls():
    for name in ["ptrace", "keyctl", "bpf", "io_uring_setup", "io_uring_enter", "mount", "unshare",
                 "setns", "perf_event_open", "userfaultfd", "process_vm_writev", "kexec_load"]:
        assert name in DENIED_SYSCALLS


def test_filter_blocks_in_a_real_child(tmp_path: Path):
    f = tmp_path / "filter.bpf"
    f.write_bytes(build_filter())
    out = subprocess.run([sys.executable, "-c", CHILD, str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["af_unix"] == errno.EAFNOSUPPORT
    assert res["af_vsock"] == errno.EAFNOSUPPORT
    assert res["af_packet"] == errno.EAFNOSUPPORT
    assert res["af_inet"] == 0 and res["af_inet6"] == 0
    assert res["socketpair"] == 2
    assert res["io_uring_setup"] == errno.EPERM
    assert res["keyctl"] == errno.EPERM
    assert res["ptrace"] == errno.EPERM
    assert res["unshare_user"] == errno.EPERM
    assert res["clone3"] == errno.ENOSYS
    assert res["subprocess_true"] == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... bash scripts/dev/test.sh tests/unit/test_seccomp.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.sandbox'`)

- [ ] **Step 3: Implement**

`src/dryrun/sandbox/__init__.py`: empty file.

`src/dryrun/sandbox/seccomp.py`:
```python
"""Classic-BPF seccomp filter for the shadow sandbox (x86_64). Loaded by `bwrap --seccomp FD`.

Denylist in the spirit of Docker's default profile, plus io_uring (it can create sockets without
passing the socket() filter), AF_UNIX sockets (host services such as docker/dbus/dryrund) and
terminal injection ioctls. See ARCHITECTURE §7.1 rows I2, I4, I6, I7.
"""
from __future__ import annotations

import errno
import struct

AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000

BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_JGE_K = 0x35
BPF_JSET_K = 0x45
BPF_RET_K = 0x06

RET_ALLOW = 0x7FFF0000
RET_ERRNO = 0x00050000
RET_KILL_PROCESS = 0x80000000

OFF_NR = 0
OFF_ARCH = 4


def _arg_lo(i: int) -> int:
    return 16 + 8 * i  # low 32 bits of seccomp_data.args[i] (little endian)


SYS_ioctl = 16
SYS_socket = 41
SYS_clone = 56
SYS_clone3 = 435
AF_UNIX = 1
AF_INET, AF_INET6, AF_NETLINK = 2, 10, 16
# Only these socket families may be created. AF_UNIX would reach host services (docker, dbus, dryrund);
# AF_VSOCK would reach the Windows host from WSL2 (vsock is not isolated by a network namespace).
ALLOWED_FAMILIES = (AF_INET, AF_INET6, AF_NETLINK)
TIOCSTI = 0x5412
TIOCLINUX = 0x541C
CLONE_NEWUSER = 0x10000000

DENIED_SYSCALLS: dict[str, int] = {
    "ptrace": 101, "syslog": 103, "vhangup": 153, "pivot_root": 155, "adjtimex": 159, "acct": 163,
    "settimeofday": 164, "mount": 165, "umount2": 166, "swapon": 167, "swapoff": 168, "reboot": 169,
    "iopl": 172, "ioperm": 173, "init_module": 175, "delete_module": 176, "quotactl": 179,
    "lookup_dcookie": 212, "clock_settime": 227, "kexec_load": 246, "add_key": 248, "request_key": 249,
    "keyctl": 250, "unshare": 272, "perf_event_open": 298, "name_to_handle_at": 303,
    "open_by_handle_at": 304, "clock_adjtime": 305, "setns": 308, "process_vm_readv": 310,
    "process_vm_writev": 311, "kcmp": 312, "finit_module": 313, "kexec_file_load": 320, "bpf": 321,
    "userfaultfd": 323, "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
    "open_tree": 428, "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433,
    "pidfd_getfd": 438, "mount_setattr": 442,
}


class _Asm:
    def __init__(self) -> None:
        self.ins: list[list] = []
        self.labels: dict[str, int] = {}

    def label(self, name: str) -> None:
        self.labels[name] = len(self.ins)

    def emit(self, code: int, k: int = 0, jt: int | str = 0, jf: int | str = 0) -> None:
        self.ins.append([code, jt, jf, k])

    def assemble(self) -> bytes:
        out = bytearray()
        for i, (code, jt, jf, k) in enumerate(self.ins):
            out += struct.pack("<HBBI", code, self._rel(i, jt), self._rel(i, jf), k)
        return bytes(out)

    def _rel(self, i: int, target: int | str) -> int:
        if isinstance(target, int):
            return target
        distance = self.labels[target] - i - 1
        if not 0 <= distance <= 255:
            raise ValueError(f"jump to {target} out of range: {distance}")
        return distance


def build_filter() -> bytes:
    a = _Asm()
    a.emit(BPF_LD_W_ABS, OFF_ARCH)
    a.emit(BPF_JEQ_K, AUDIT_ARCH_X86_64, jt=0, jf="kill")
    a.emit(BPF_LD_W_ABS, OFF_NR)
    a.emit(BPF_JGE_K, X32_SYSCALL_BIT, jt="eperm", jf=0)
    for nr in sorted(DENIED_SYSCALLS.values()):
        a.emit(BPF_JEQ_K, nr, jt="eperm", jf=0)
    a.emit(BPF_JEQ_K, SYS_clone3, jt="enosys", jf=0)
    a.emit(BPF_JEQ_K, SYS_socket, jt="socket", jf=0)
    a.emit(BPF_JEQ_K, SYS_ioctl, jt="ioctl", jf=0)
    a.emit(BPF_JEQ_K, SYS_clone, jt="clone", jf=0)
    a.emit(BPF_RET_K, RET_ALLOW)

    a.label("socket")  # allowlist of address families; everything else (AF_UNIX, AF_VSOCK, ...) refused
    a.emit(BPF_LD_W_ABS, _arg_lo(0))
    for family in ALLOWED_FAMILIES:
        a.emit(BPF_JEQ_K, family, jt="allow", jf=0)
    a.emit(BPF_RET_K, RET_ERRNO | errno.EAFNOSUPPORT)
    a.label("allow")
    a.emit(BPF_RET_K, RET_ALLOW)

    a.label("ioctl")
    a.emit(BPF_LD_W_ABS, _arg_lo(1))
    a.emit(BPF_JEQ_K, TIOCSTI, jt="eperm", jf=0)
    a.emit(BPF_JEQ_K, TIOCLINUX, jt="eperm", jf=0)
    a.emit(BPF_RET_K, RET_ALLOW)

    a.label("clone")
    a.emit(BPF_LD_W_ABS, _arg_lo(0))
    a.emit(BPF_JSET_K, CLONE_NEWUSER, jt="eperm", jf=0)
    a.emit(BPF_RET_K, RET_ALLOW)

    a.label("eperm")
    a.emit(BPF_RET_K, RET_ERRNO | errno.EPERM)
    a.label("enosys")
    a.emit(BPF_RET_K, RET_ERRNO | errno.ENOSYS)
    a.label("eafnosupport")
    a.emit(BPF_RET_K, RET_ERRNO | errno.EAFNOSUPPORT)
    a.label("kill")
    a.emit(BPF_RET_K, RET_KILL_PROCESS)
    return a.assemble()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `... bash scripts/dev/test.sh tests/unit/test_seccomp.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/sandbox tests/unit/test_seccomp.py
git commit -m "feat: x86_64 seccomp filter (AF_UNIX, io_uring, ptrace, keyrings, userns, TIOCSTI)"
```
