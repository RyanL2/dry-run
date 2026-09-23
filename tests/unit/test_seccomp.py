from __future__ import annotations

import errno
import json
import platform
import struct
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
    code, jt, jf, k = struct.unpack("<HBBI", prog[:8])
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
