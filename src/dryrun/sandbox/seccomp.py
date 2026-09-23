"""Classic-BPF seccomp filter for the shadow sandbox (x86_64). Loaded by `bwrap --seccomp FD`.

Denylist in the spirit of Docker's default profile, plus io_uring (it can create sockets without
passing the socket() filter), a socket address-family allowlist (AF_UNIX would reach host services such
as docker/dbus/dryrund; AF_VSOCK would reach the Windows host from WSL2) and terminal injection ioctls.
See ARCHITECTURE §7.1 rows I2, I4, I6, I7, I12.
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
    a.label("kill")
    a.emit(BPF_RET_K, RET_KILL_PROCESS)
    return a.assemble()
