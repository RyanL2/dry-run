#!/bin/bash
# Spike 0 (throwaway): verify the ARCHITECTURE §7 layout on this kernel. Run as dryrundev.
# Touches only ~/spike0 and its own run dir; the "escape" probes target nothing real.
set -uo pipefail
BW="$HOME/.local/share/dryrun/bwrap/0.11.0/bwrap"
S="$HOME/spike0"; WS="$S/ws"; STATE="$HOME/.local/state/dryrun"; RUN="$STATE/runs/spike0"
rm -rf "$S" "$RUN"; mkdir -p "$WS/sub" "$RUN"/{ws.up,ws.wk,tmp.up,tmp.wk,tmp.lower} "$S/decoy/.ssh"
echo from-real-tmp > "$RUN/tmp.lower/agent_script.sh"
echo keep > "$WS/a.txt"; echo del > "$WS/sub/b.txt"; echo SECRET-DECOY > "$S/decoy/.ssh/id_ed25519"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && echo REAL-SECRET > "$HOME/.ssh/id_ed25519"
echo "real-home-before: $(ls -a "$HOME" | tr '\n' ' ')"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

cat > "$S/inner.sh" <<'EOS'
WS="$1"
echo "--- inside: uid=$(id -u) pid1=$(cat /proc/1/comm)"
echo new > "$WS/new.txt" && echo "ws write ok"
rm "$WS/sub/b.txt" && echo "ws delete ok"
echo t > /tmp/dryrun_spike_tmp.txt && echo "tmp write ok; lower copy: $(cat /tmp/agent_script.sh)"
touch "$HOME/should_not_exist" 2>/dev/null && echo "LEAK: wrote real HOME" || echo "home write blocked"
touch /mnt/c/Users/Public/dryrun_spike_canary 2>/dev/null && echo "LEAK: wrote /mnt/c" || echo "/mnt/c write blocked"
touch /usr/lib/wsl/x 2>/dev/null && echo "LEAK: wrote /usr/lib/wsl" || echo "/usr/lib/wsl write blocked"
ls /run | head -3 | tr '\n' ' '; echo "<- /run contents"
ls "$HOME/.local/state/dryrun" 2>&1 | head -2 | tr '\n' ' '; echo "<- state dir contents"
cat "$HOME/.ssh/id_ed25519" 2>&1
python3 - <<'EOP'
import socket
for path in ["/tmp/.X11-unix/X0", "/run/dbus/system_bus_socket"]:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(path); print("LEAK-WITHOUT-SECCOMP: unix connect ok", path)
    except OSError as e:
        print("unix connect blocked", path, e.errno)
try:
    socket.create_connection(("1.1.1.1", 443), 1); print("LEAK: tcp ok")
except OSError as e:
    print("tcp blocked", e.errno)
EOP
EOS

strace -f -qq --seccomp-bpf -e trace=execve,connect -o "$RUN/trace" \
  "$BW" --unshare-all --die-with-parent --new-session --cap-drop ALL \
  --ro-bind / / --dev /dev --proc /proc \
  --overlay-src "$WS" --overlay "$RUN/ws.up" "$RUN/ws.wk" "$WS" \
  --overlay-src "$RUN/tmp.lower" --overlay "$RUN/tmp.up" "$RUN/tmp.wk" /tmp \
  --tmpfs /run --tmpfs /mnt/wsl --tmpfs /mnt/wslg \
  --tmpfs "$STATE" \
  --ro-bind "$S/decoy/.ssh" "$HOME/.ssh" \
  --setenv HOME "$HOME" \
  -- bash "$S/inner.sh" "$WS"
echo "bwrap exit: $?"
echo "--- outside"
echo "real ws: $(ls "$WS" "$WS/sub" | tr '\n' ' ')"
echo "real tmp file exists? $([ -e /tmp/dryrun_spike_tmp.txt ] && echo YES-LEAK || echo no)"
echo "real-home-after: $(ls -a "$HOME" | tr '\n' ' ')"
echo "upper: $(cd "$RUN/ws.up" && find . -printf '%p:%y ' )"
echo "whiteout: $(stat -c '%F %t:%T' "$RUN/ws.up/sub/b.txt" 2>&1)"
echo "trace execs: $(grep -c execve "$RUN/trace")  connects: $(grep connect "$RUN/trace" | sed 's/.*connect(//' | cut -c1-60 | tr '\n' ' ')"
echo "--- cgroup limits"
systemd-run --user --scope -q -p MemoryMax=100M -p MemorySwapMax=0 -- python3 -c 'b=bytearray(300*1024*1024); print("LEAK: allocated 300M")'
echo "mem exit: $?"
systemd-run --user --scope -q -p TasksMax=20 -- python3 -c '
import os,sys
n=0
try:
    for _ in range(100):
        if os.fork()==0: os._exit(0) if False else __import__("time").sleep(2); os._exit(0)
        n+=1
except OSError as e: print("fork blocked after", n, e.errno)
else: print("LEAK: forked", n)'
echo "--- nice/ionice"
nice -n 19 ionice -c3 sh -c 'echo "nice=$(nice) ionice=$(ionice)"'
rm -rf "$S" "$RUN"
