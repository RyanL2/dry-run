#!/bin/bash
# Spike 0b (throwaway): why does an overlay with lower=/tmp fail?
BW="$HOME/.local/share/dryrun/bwrap/0.11.0/bwrap"
R="$HOME/.local/state/dryrun/runs/spike0b"; rm -rf "$R"; mkdir -p "$R"/{up,wk}
echo "== /tmp mount: $(findmnt -no SOURCE,FSTYPE,OPTIONS /tmp 2>&1)"
echo "== submounts under /tmp: $(findmnt -rn -o TARGET | grep '^/tmp' | tr '\n' ' ')"
echo "== home fs: $(findmnt -no SOURCE,FSTYPE -T "$HOME")  tmp fs: $(findmnt -no SOURCE,FSTYPE -T /tmp)"
"$BW" --unshare-all --ro-bind / / --dev /dev --proc /proc \
  --overlay-src /tmp --overlay "$R/up" "$R/wk" /tmp -- true 2>"$R/err"; echo "A exit=$? $(cat "$R/err")"
mkdir -p "$HOME/spike0b_lower"; rm -rf "$R"/{up,wk}; mkdir -p "$R"/{up,wk}
"$BW" --unshare-all --ro-bind / / --dev /dev --proc /proc \
  --overlay-src "$HOME/spike0b_lower" --overlay "$R/up" "$R/wk" /tmp -- true 2>"$R/err"; echo "B (lower=home dir, mounted at /tmp) exit=$? $(cat "$R/err")"
rm -rf "$R"/{up,wk}; mkdir -p "$R"/{up,wk}
unshare -Urm sh -c "mount -t overlay overlay -o lowerdir=/tmp,upperdir=$R/up,workdir=$R/wk,userxattr /tmp && echo C unshare-mount ok || echo C unshare-mount failed" 2>&1
dmesg 2>/dev/null | tail -3
rm -rf "$R" "$HOME/spike0b_lower"
