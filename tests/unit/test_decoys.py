from __future__ import annotations

import base64
from pathlib import Path

from dryrun.sandbox.decoys import make_decoys, new_token, scan


def test_decoys_only_for_existing_paths_and_mirror_names(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("REAL KEY")
    (home / ".netrc").write_text("machine x password REAL")
    token = new_token()
    mounts = make_decoys(tmp_path / "decoys", home, [".ssh", ".netrc", ".aws"], token)
    dests = {dst for _, dst in mounts}
    assert dests == {str(home / ".ssh"), str(home / ".netrc")}
    ssh_src = next(src for src, dst in mounts if dst.endswith(".ssh"))
    content = (Path(ssh_src) / "id_ed25519").read_text()
    assert token in content and "REAL" not in content


def test_scan_finds_plain_hex_and_base64_at_every_alignment(tmp_path: Path):
    token = new_token()
    secret = f"DRYRUN-DECOY {token} .ssh/id_ed25519\n".encode()
    blobs = {
        "stdout": b"leak: " + secret,
        "stderr": b"nothing here",
        "workspace/out.hex": secret.hex().encode(),
        "workspace/out.b64a": base64.b64encode(secret),
        "workspace/out.b64b": base64.b64encode(b"x" + secret),
        "workspace/out.b64c": base64.b64encode(b"xy" + secret),
    }
    hits = {h["where"] for h in scan(token, blobs)}
    assert hits == {"stdout", "workspace/out.hex", "workspace/out.b64a", "workspace/out.b64b",
                    "workspace/out.b64c"}
