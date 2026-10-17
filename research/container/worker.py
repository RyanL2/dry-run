"""Trusted stdlib-only collector for a deliberately restricted filesystem pilot.

This is NOT an arbitrary-command runner. Public command strings are never executed.
Only the fixed coreutils operations below are accepted, with validated literal paths.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import socket
import stat
import subprocess
import sys
import time

ROOT = Path('/workspace')
OPERATIONS = {'copy', 'delete', 'touch', 'truncate', 'chmod', 'move', 'mkdir'}


def relpath(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,160}', value)
            or any(p in ('', '.', '..') or p.startswith('-') for p in value.split('/'))
            or PurePosixPath(value).is_absolute()):
        raise ValueError('unsafe relative path')
    return value


def validate_case(case):
    if not isinstance(case, dict) or set(case) != {'operation', 'target', 'source', 'files', 'directories'}:
        raise ValueError('wrong execution fields')
    if case['operation'] not in OPERATIONS:
        raise ValueError('operation is not on the reviewed allowlist')
    relpath(case['target'])
    if case['source'] is not None:
        relpath(case['source'])
    if (case['operation'] in {'copy', 'move'}) != (case['source'] is not None):
        raise ValueError('source does not match operation')
    if not isinstance(case['files'], dict) or len(case['files']) > 40:
        raise ValueError('invalid fixture files')
    for path, content in case['files'].items():
        relpath(path)
        if not isinstance(content, str) or len(content.encode()) > 32768:
            raise ValueError('fixture too large')
    if not isinstance(case['directories'], list) or len(case['directories']) > 40:
        raise ValueError('invalid fixture directories')
    for path in case['directories']:
        relpath(path)


def command(case):
    target = relpath(case['target'])
    op = case['operation']
    if op == 'copy':
        return ['/bin/cp', '--', relpath(case['source']), target]
    if op == 'move':
        return ['/bin/mv', '--', relpath(case['source']), target]
    return {
        'delete': ['/bin/rm', '--', target],
        'touch': ['/usr/bin/touch', '--', target],
        'truncate': ['/usr/bin/truncate', '-s', '0', '--', target],
        'chmod': ['/bin/chmod', '755', '--', target],
        'mkdir': ['/bin/mkdir', '--', target],
    }[op]


def snapshot(root=ROOT):
    result = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('unsupported filesystem entry')
        row = {'kind': 'dir' if path.is_dir() else 'file', 'mode': stat.S_IMODE(info.st_mode)}
        if row['kind'] == 'file':
            if info.st_size > 1024 * 1024:
                raise ValueError('file exceeded collection limit')
            row.update(size=info.st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        result[path.relative_to(root).as_posix()] = row
        if len(result) > 200:
            raise ValueError('too many entries')
    return result


def bounded(argv, timeout=3):
    with subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True,
                          env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp', 'LC_ALL': 'C'}) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out.decode(errors='replace'), err.decode(errors='replace'), False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate(timeout=2)
            return proc.returncode, out.decode(errors='replace'), err.decode(errors='replace'), True


def probe():
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    checks = {'uid_nonroot': os.getuid() == 1000,
              'caps_empty': all(int(status[key].strip(), 16) == 0 for key in ('CapEff', 'CapPrm', 'CapBnd')),
              'no_new_privileges': status['NoNewPrivs'].strip() == '1',
              'seccomp_filter': status['Seccomp'].strip() == '2',
              'docker_socket_absent': not Path('/var/run/docker.sock').exists(),
              'host_paths_absent': not any(Path(p).exists() for p in ('/mnt/c', '/host', '/run/desktop/mnt/host')),
              'network_only_loopback': set(os.listdir('/sys/class/net')) == {'lo'}}
    try:
        os.setuid(0)
        checks['cannot_become_root'] = False
    except PermissionError:
        checks['cannot_become_root'] = True
    try:
        Path('/opt/pilot-write-probe').write_text('probe')
        checks['root_readonly'] = False
    except OSError as exc:
        checks['root_readonly'] = exc.errno in (errno.EROFS, errno.EACCES)
    with socket.socket() as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(('192.0.2.1', 9))
            checks['egress_blocked'] = False
        except OSError as exc:
            checks['egress_blocked'] = exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPERM)
    limits = {name: Path('/sys/fs/cgroup', name).read_text().strip()
              for name in ('memory.max', 'memory.swap.max', 'pids.max', 'cpu.max')}
    checks['memory_limit'] = limits['memory.max'] == str(256 * 1024 * 1024)
    checks['swap_disabled'] = limits['memory.swap.max'] == '0'
    checks['pid_limit'] = limits['pids.max'] == '32'
    quota, period = limits['cpu.max'].split()
    checks['cpu_limit'] = quota != 'max' and int(quota) / int(period) <= 1
    for root, maximum in ((ROOT, 32 * 1024 * 1024), (Path('/tmp'), 16 * 1024 * 1024)):
        fs = os.statvfs(root)
        checks[f'{root.name}_disk_bounded'] = 0 < fs.f_blocks * fs.f_frsize <= maximum
    checks['timeout_kills_child'] = bounded([sys.executable, '-I', '-c', 'import time; time.sleep(10)'], .1)[3]
    # Prove the workspace tmpfs enforces its advertised capacity; this is bounded to 33 MiB.
    fill = ROOT / 'capacity-probe'
    fill.mkdir()
    try:
        for index in range(33):
            with (fill / str(index)).open('wb') as stream:
                stream.write(b'x' * 1024 * 1024)
        checks['disk_limit_enforced'] = False
    except OSError as exc:
        checks['disk_limit_enforced'] = exc.errno == errno.ENOSPC
    finally:
        for path in fill.iterdir():
            path.unlink()
        fill.rmdir()
    return {'checks': checks, 'limits': limits, 'passed': all(checks.values())}


def execute(case):
    validate_case(case)
    os.umask(0o022)
    for directory in case['directories']:
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    for name, content in case['files'].items():
        path = ROOT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    before = snapshot()
    start = time.monotonic()
    code, out, err, timed_out = bounded(command(case))
    after = snapshot()
    return {'before': before, 'after': after, 'argv': command(case), 'exit_code': code,
            'stdout': out, 'stderr': err, 'timed_out': timed_out,
            'wall_ms': round((time.monotonic() - start) * 1000)}


if __name__ == '__main__':
    payload = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError('input too large')
    request = json.loads(payload)
    result = probe() if request == {'probe': True} else execute(request)
    print(json.dumps(result, sort_keys=True))
