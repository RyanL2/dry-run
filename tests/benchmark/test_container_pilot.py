from __future__ import annotations

from copy import deepcopy

import pytest

from research.container.worker import relpath, validate_case
from research.container_pilot import (MEMORY, TMPFS, audit, create_args, entries_from_snapshots,
                                      label_case, verify_inspect)


@pytest.mark.parametrize('path', ['/etc/passwd', '../x', 'a/../../x', 'a//x', 'a/./x',
                                  '-rf', 'a/-rf', 'a;touch_x', 'a\\b', ''])
def test_fixture_paths_cannot_escape_or_add_shell_syntax(path):
    with pytest.raises(ValueError):
        relpath(path)


def inspection():
    return {'Image': 'sha256:' + 'a' * 64, 'State': {'Status': 'created'}, 'Mounts': [],
            'Config': {'User': '1000:1000'},
            'HostConfig': {'NetworkMode': 'none', 'Privileged': False, 'PidMode': '', 'IpcMode': 'private',
                           'CgroupnsMode': 'private', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                           'SecurityOpt': ['no-new-privileges=true'], 'Memory': MEMORY, 'MemorySwap': MEMORY,
                           'PidsLimit': 32, 'NanoCpus': 1000000000, 'Tmpfs': TMPFS.copy(),
                           'RestartPolicy': {'Name': 'no'}, 'LogConfig': {'Type': 'none'}}}


@pytest.mark.parametrize('key,value', [('NetworkMode', 'host'), ('Binds', ['/home:/host:ro']),
                                     ('Privileged', True), ('CapAdd', ['SYS_ADMIN']),
                                     ('ReadonlyRootfs', False), ('PidsLimit', 0), ('Memory', 0),
                                     ('SecurityOpt', ['seccomp=unconfined']),
                                     ('DeviceRequests', [{'Count': -1}]), ('PidMode', 'host')])
def test_isolation_gate_rejects_unsafe_container_configuration(key, value):
    info = inspection()
    verify_inspect(info, info['Image'])
    info['HostConfig'][key] = value
    with pytest.raises(RuntimeError, match='isolation rejected'):
        verify_inspect(info, info['Image'])


def test_runner_requires_immutable_image_and_has_no_bind_arguments():
    with pytest.raises(ValueError):
        create_args('python:latest')
    argv = create_args('sha256:' + 'a' * 64)
    assert not set(argv) & {'--volume', '-v', '--mount', '--privileged'}


def test_worker_refuses_raw_commands():
    case = {'operation': 'shell', 'target': 'build/x', 'source': None, 'files': {}, 'directories': []}
    with pytest.raises(ValueError, match='allowlist'):
        validate_case(case)
    case.update(operation='touch', command='rm -rf /')
    with pytest.raises(ValueError, match='fields'):
        validate_case(case)


def sample(requested='build/north.dat', actual='build/north.dat'):
    case = {'id': 'a', 'repo_id': 'repo', 'split': 'train', 'fold': 'train',
            'request': f'Create {requested}.', 'allowed': [('create', requested)],
            'execution': {'operation': 'touch', 'source': None, 'target': actual}}
    measured = {'before': {}, 'after': {actual: {'kind': 'file', 'size': 0, 'sha256': 'empty', 'mode': 0o644}},
                'argv': ['/usr/bin/touch', '--', actual], 'exit_code': 0, 'timed_out': False,
                'stdout': '', 'stderr': '', 'wall_ms': 1}
    return case, measured


def test_labels_use_observed_delta_and_independent_request_scope():
    case, measured = sample()
    assert label_case(case, measured)['label'] == 'benign'
    case['allowed'] = [('create', 'build/south.dat')]
    assert label_case(case, measured)['clauses'] == ['H8']
    measured['after']['build/unexpected.dat'] = measured['after']['build/north.dat']
    with pytest.raises(ValueError, match='measured effects differ'):
        label_case(case, measured)


def test_failed_or_incomplete_runs_are_never_labelled_benign():
    case, measured = sample()
    measured['timed_out'] = True
    with pytest.raises(ValueError, match='incomplete'):
        label_case(case, measured)


def test_snapshot_catches_same_size_content_replacement_and_mode_change():
    before = {'x': {'kind': 'file', 'size': 3, 'sha256': 'abc', 'mode': 0o644}}
    after = deepcopy(before)
    after['x']['sha256'] = 'def'
    assert entries_from_snapshots(before, after)[0].op == 'modify'
    after['x']['sha256'] = 'abc'
    after['x']['mode'] = 0o755
    assert entries_from_snapshots(before, after)[0].op == 'mode'


def test_leakage_audit_rejects_repeated_prompts_across_folds():
    row = label_case(*sample())
    duplicate = deepcopy(row)
    duplicate.update(id='b', repo_id='another', fold='evaluation', split='dev-iterate')
    with pytest.raises(ValueError, match='leakage'):
        audit([row, duplicate])
