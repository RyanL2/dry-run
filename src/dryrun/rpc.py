"""dryrun.rpc/1: one newline-terminated JSON request and one JSON response per unix-socket connection.
Stdlib only (the hook imports this)."""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

MAX_LINE = 4 * 1024**2


def call(sock_path: Path, request: dict, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(max(0.05, timeout))
        s.connect(str(sock_path))
        s.sendall(json.dumps(request).encode() + b"\n")
        buf = bytearray()
        while not buf.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("dryrund did not answer in time")
            s.settimeout(remaining)
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_LINE:
                raise ValueError("response too large")
    obj = json.loads(bytes(buf).decode())
    if not isinstance(obj, dict):
        raise ValueError("response is not an object")
    return obj


async def read_request(reader) -> dict:
    line = await reader.readuntil(b"\n")
    if len(line) > MAX_LINE:
        raise ValueError("request too large")
    obj = json.loads(line.decode())
    if not isinstance(obj, dict):
        raise ValueError("request is not an object")
    return obj


async def write_response(writer, obj: dict) -> None:
    writer.write(json.dumps(obj).encode() + b"\n")
    await writer.drain()
