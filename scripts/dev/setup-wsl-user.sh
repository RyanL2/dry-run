#!/bin/bash
# One-time dev setup, run as root inside WSL. Creates the non-root user that runs
# dryrund and every test. Reverse with:
#   loginctl disable-linger dryrundev; userdel -r dryrundev
set -euo pipefail
U=dryrundev
id "$U" >/dev/null 2>&1 || useradd -m -s /bin/bash "$U"
loginctl enable-linger "$U"
UID_N=$(id -u "$U")
for _ in $(seq 1 50); do
  [ -S "/run/user/$UID_N/bus" ] && break
  sleep 0.1
done
echo "user: $(id "$U")"
echo "bus: $(ls -la "/run/user/$UID_N/bus" 2>&1)"
echo "controllers: $(cat "/sys/fs/cgroup/user.slice/user-$UID_N.slice/user@$UID_N.service/cgroup.controllers" 2>&1)"
