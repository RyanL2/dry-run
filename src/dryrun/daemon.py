"""dryrund: long-lived asyncio server behind the hook (spec F2, F13, F16, F17)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from dryrun import rpc
from dryrun.commit import recover_all
from dryrun.config import Config, load_config
from dryrun.paths import socket_path, state_dir
from dryrun.pipeline import Gate, Pipeline
from dryrun.sandbox.spawn import preflight
from dryrun.store import Store
from dryrun.types import Decision

log = logging.getLogger("dryrun")


def _ask(reason: str, rule: str) -> dict:
    return Decision("ask", "passthrough", reason, [rule]).to_json()


class Daemon:
    def __init__(self, cfg: Config, store: Store, pipeline, sock_path: Path, *,
                 canary_fn: Callable[[], tuple[bool, str]] | None = None) -> None:
        self.cfg, self.store, self.pipeline = cfg, store, pipeline
        self.sock_path = Path(sock_path)
        self.canary_fn = canary_fn
        self.executor = ThreadPoolExecutor(max_workers=max(1, cfg.shadow.max_concurrent))
        self._server: asyncio.AbstractServer | None = None
        self._stopping = threading.Event()

    def _run_canaries(self) -> None:
        gate: Gate = getattr(self.pipeline, "gate", Gate())
        while not self._stopping.is_set():
            try:
                ok, detail = self.canary_fn() if self.canary_fn else (False, "no self-test configured")
            except Exception as exc:
                ok, detail = False, f"self-test crashed: {exc}"
            gate.ok, gate.detail = ok, detail
            (log.info if ok else log.error)("isolation self-test: %s (%s)", "PASS" if ok else "FAIL", detail)
            if self._stopping.wait(self.cfg.shadow.canary_interval_h * 3600):
                return

    def _peer_ok(self, writer) -> bool:
        sock = writer.get_extra_info("socket")
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            return uid == os.getuid()
        except OSError:
            return False

    async def _handle(self, reader, writer) -> None:
        resp: dict
        try:
            if not self._peer_ok(writer):
                writer.close()
                return
            req = await asyncio.wait_for(rpc.read_request(reader), 5)
            op = req.get("op")
            if op == "prompt":
                await asyncio.get_running_loop().run_in_executor(
                    None, self.pipeline.handle_prompt, str(req.get("session_id", "")), str(req.get("text", "")))
                resp = {"ok": True}
            elif op == "status":
                gate = getattr(self.pipeline, "gate", Gate())
                resp = {"ok": True, "gate_ok": gate.ok, "gate_detail": gate.detail}
            elif op == "pretool":
                resp = await self._pretool(req)
            else:
                resp = {"ok": False, "error": f"unknown op {op!r}"}
        except Exception as exc:
            resp = _ask(f"Dry Run error: {str(exc)[:120]}", "S.error")
        try:
            await rpc.write_response(writer, resp)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _pretool(self, req: dict) -> dict:
        deadline_s = float(req.get("deadline_ms", 58_000)) / 1000 - 0.5
        deadline_s = max(0.5, min(deadline_s, self.cfg.shadow.wall_clock_s + 25))
        cancel = threading.Event()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.executor, self.pipeline.handle_pretool, req, cancel)
        try:
            decision = await asyncio.wait_for(asyncio.shield(fut), deadline_s)
        except asyncio.TimeoutError:
            cancel.set()
            return _ask("Dry Run could not finish in time; review manually", "S.deadline")
        return decision.to_json()

    async def serve(self) -> None:
        if self.sock_path.exists():
            try:
                rpc.call(self.sock_path, {"op": "status"}, 1)
            except (OSError, ValueError, TimeoutError):
                self.sock_path.unlink()
            else:
                raise RuntimeError(f"dryrund already running on {self.sock_path}")
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self.sock_path))
        finally:
            os.umask(old)
        threading.Thread(target=self._run_canaries, daemon=True).start()
        loop = asyncio.get_running_loop()
        cleanup = loop.create_task(self._cleanup_loop())
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            cleanup.cancel()
            self._stopping.set()
            self.executor.shutdown(wait=False, cancel_futures=True)
            try:
                self.sock_path.unlink()
            except FileNotFoundError:
                pass

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                self.store.cleanup(ttl_s=self.cfg.policy.pending_ttl_min * 60)
            except Exception as exc:
                log.error("cleanup failed: %s", exc)
            await asyncio.sleep(60)

    def stop(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.close()
            for task in asyncio.all_tasks():
                task.cancel()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dryrund")
    ap.add_argument("--allow-root", action="store_true")
    ap.add_argument("--config", type=Path)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s dryrund %(levelname)s %(message)s")
    if os.geteuid() == 0 and not args.allow_root:
        print("dryrund: refusing to run as root (use a normal user or --allow-root)", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    store = Store(state_dir())
    for run_id in recover_all(store):
        log.warning("completed interrupted commit %s", run_id)
    problems = preflight(cfg.shadow, allow_root=args.allow_root)
    gate = Gate(ok=None if not problems else False, detail="; ".join(problems) or "pending")
    pipeline = Pipeline(cfg, store, gate=gate)
    if problems:
        def canary_fn() -> tuple[bool, str]:
            return False, "; ".join(problems)
    else:
        from dryrun.canary import run_gate

        def canary_fn() -> tuple[bool, str]:
            return run_gate(cfg, store)
    daemon = Daemon(cfg, store, pipeline, socket_path(), canary_fn=canary_fn)
    try:
        asyncio.run(daemon.serve())
    except KeyboardInterrupt:
        pass
    return 0
