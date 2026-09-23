"""Parse `strace -f -o` output into the process and network parts of the EffectRecord."""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_LINE = re.compile(r"^(?P<pid>\d+)\s+(?P<rest>.*)$")
_CALL = re.compile(r"^(?P<call>execve|execveat|connect|sendto|sendmsg)\((?P<args>.*)$")
_RESUMED = re.compile(r"^<\.\.\. (?P<call>execve|execveat) resumed>.*=\s*(?P<ret>-?\d+)")
_EXEC_PATH = re.compile(r'^(?:\d+,\s*)?"(?P<path>(?:[^"\\]|\\.)*)"')
_V4 = re.compile(r'sa_family=AF_INET, sin_port=htons\((?P<port>\d+)\), sin_addr=inet_addr\("(?P<ip>[^"]+)"\)')
_V6 = re.compile(r'sa_family=AF_INET6, sin6_port=htons\((?P<port>\d+)\).*?inet_pton\(AF_INET6, "(?P<ip>[^"]+)"')
_UNIX = re.compile(r'sa_family=AF_UNIX, sun_path=(?P<path>@?"(?:[^"\\]|\\.)*")')
_RET = re.compile(r"\)\s+=\s+(?P<ret>-?\d+)")


@dataclass
class TraceSummary:
    execs: list[str] = field(default_factory=list)
    pids: int = 0
    net: list[dict] = field(default_factory=list)
    unix_connects: list[str] = field(default_factory=list)


def _net_entry(call: str, args: str) -> dict | None:
    m = _V4.search(args)
    if m:
        ip, port, target = m["ip"], int(m["port"]), f'{m["ip"]}:{m["port"]}'
    else:
        m = _V6.search(args)
        if not m:
            return None
        ip, port, target = m["ip"], int(m["port"]), f'[{m["ip"]}]:{m["port"]}'
    if port == 53:
        return {"kind": "dns", "target": target}
    try:
        if ipaddress.ip_address(ip).is_loopback:
            return None
    except ValueError:
        pass
    return {"kind": "connect" if call == "connect" else "send", "target": target}


def parse_trace(text: str) -> TraceSummary:
    s = TraceSummary()
    pids: set[str] = set()
    pending: dict[str, str] = {}
    successful: list[str] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        pid, rest = m["pid"], m["rest"]
        pids.add(pid)
        resumed = _RESUMED.match(rest)
        if resumed:
            if resumed["ret"] == "0" and pid in pending:
                successful.append(pending.pop(pid))
            continue
        c = _CALL.match(rest)
        if not c:
            continue
        call, args = c["call"], c["args"]
        if call in ("execve", "execveat"):
            pm = _EXEC_PATH.match(args)
            name = os.path.basename(pm["path"]) if pm else "?"
            if "<unfinished ...>" in args:
                pending[pid] = name
            else:
                rets = list(_RET.finditer(args))
                if rets and rets[-1]["ret"] == "0":
                    successful.append(name)
            continue
        um = _UNIX.search(args)
        if um:
            s.unix_connects.append(um["path"].strip("@").strip('"'))
            continue
        entry = _net_entry(call, args)
        if entry and entry not in s.net:
            s.net.append(entry)
    s.execs = successful[1:]
    s.pids = len(pids)
    return s


def parse_trace_file(path: Path) -> TraceSummary:
    try:
        return parse_trace(Path(path).read_text(errors="replace"))
    except FileNotFoundError:
        return TraceSummary()
