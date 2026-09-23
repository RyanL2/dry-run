from __future__ import annotations

from dryrun.sandbox.trace import parse_trace

SAMPLE = '''\
100 execve("/home/u/.local/share/dryrun/bwrap/0.11.0/bwrap", ["bwrap", "--unshare-all"], 0x7ffd /* 5 vars */) = 0
102 execve("/usr/local/sbin/bash", ["bash", "-c", "x"], 0x55 /* 3 vars */) = -1 ENOENT (No such file or directory)
102 execve("/usr/bin/bash", ["bash", "-c", "x"], 0x55 /* 3 vars */) = 0
103 execve("/usr/bin/rm", ["rm", "-rf", "b"], 0x55 /* 3 vars */ <unfinished ...>
104 connect(3, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("1.1.1.1")}, 16) = -1 ENETUNREACH (Network is unreachable)
103 <... execve resumed>) = 0
104 connect(4, {sa_family=AF_INET, sin_port=htons(8080), sin_addr=inet_addr("127.0.0.1")}, 16) = -1 ECONNREFUSED (Connection refused)
104 connect(5, {sa_family=AF_INET, sin_port=htons(53), sin_addr=inet_addr("127.0.0.53")}, 16) = -1 ECONNREFUSED (Connection refused)
104 sendto(6, "\\x12\\x34", 30, MSG_NOSIGNAL, {sa_family=AF_INET6, sin6_port=htons(53), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "2001:db8::1", &sin6_addr), sin6_scope_id=0}, 28) = -1 ENETUNREACH (Network is unreachable)
104 connect(7, {sa_family=AF_INET6, sin6_port=htons(443), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "2606:4700::1111", &sin6_addr), sin6_scope_id=0}, 28) = -1 ENETUNREACH (Network is unreachable)
104 connect(8, {sa_family=AF_UNIX, sun_path="/run/docker.sock"}, 110) = -1 ENOENT (No such file or directory)
104 sendto(9, "abc", 3, 0, NULL, 0) = 3
'''


def test_execs_are_successful_only_and_skip_bwrap():
    s = parse_trace(SAMPLE)
    assert s.execs == ["bash", "rm"]
    assert s.pids == 4


def test_network_attempts_classified():
    s = parse_trace(SAMPLE)
    assert {"kind": "connect", "target": "1.1.1.1:443"} in s.net
    assert {"kind": "dns", "target": "127.0.0.53:53"} in s.net
    assert {"kind": "dns", "target": "[2001:db8::1]:53"} in s.net
    assert {"kind": "connect", "target": "[2606:4700::1111]:443"} in s.net
    assert not any("8080" in n["target"] for n in s.net)
    assert s.unix_connects == ["/run/docker.sock"]


def test_empty_trace():
    s = parse_trace("")
    assert (s.execs, s.pids, s.net, s.unix_connects) == ([], 0, [], [])
