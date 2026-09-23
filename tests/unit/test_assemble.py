from __future__ import annotations

from dryrun.sandbox.assemble import filter_env

DENY = ("*TOKEN*", "*SECRET*", "*KEY*", "AWS_*")


def test_filter_env_drops_secrets_and_host_sockets():
    env = {"PATH": "/bin", "HOME": "/h", "GITHUB_TOKEN": "x", "AWS_REGION": "r", "MY_API_KEY": "k",
           "SSH_AUTH_SOCK": "/s", "DBUS_SESSION_BUS_ADDRESS": "d", "WSL_INTEROP": "w", "DISPLAY": ":0",
           "XDG_RUNTIME_DIR": "/run/user/1", "EDITOR": "vim"}
    out = filter_env(env, DENY)
    assert out["PATH"] == "/bin" and out["HOME"] == "/h" and out["EDITOR"] == "vim"
    for gone in ["GITHUB_TOKEN", "AWS_REGION", "MY_API_KEY", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS",
                 "WSL_INTEROP", "DISPLAY", "XDG_RUNTIME_DIR"]:
        assert gone not in out
    assert out["LC_MESSAGES"] == "C"


def test_filter_env_supplies_defaults():
    out = filter_env({}, DENY)
    assert out["PATH"].startswith("/usr/local/bin")
