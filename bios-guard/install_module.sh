#!/bin/bash
# Install the patched hp-bioscfg via DKMS on one Proxmox VE node: ./install_module.sh root@<node>
# Installs dkms + proxmox-default-headers (so future kernels rebuild it), registers the module, builds it for every
# installed kernel that has headers, and loads it. The module lands in updates/dkms and overrides the stock one.
set -euo pipefail
host=$1; here=$(cd "$(dirname "$0")" && pwd); v=1.0
ssh "$host" "DEBIAN_FRONTEND=noninteractive apt-get install -y -q dkms proxmox-default-headers proxmox-headers-\$(uname -r) >/dev/null"
ssh "$host" "rm -rf /usr/src/hp-bioscfg-fix-$v && mkdir -p /usr/src/hp-bioscfg-fix-$v"
scp -q "$here"/dkms/hp-bioscfg-fix-$v/{*.c,*.h,Makefile,dkms.conf} "$host:/usr/src/hp-bioscfg-fix-$v/"
# Build for every installed kernel, not only the running one: if apt pulls in a new kernel before the source is
# registered, its DKMS hook skips the module and the next boot loads the stock driver.
ssh "$host" "dkms remove -q hp-bioscfg-fix/$v --all 2>/dev/null || true; dkms add -q hp-bioscfg-fix/$v; \
  for k in \$(ls /lib/modules); do [ -d /lib/modules/\$k/build ] && dkms install -q hp-bioscfg-fix/$v -k \$k; done; depmod -a \
  && modprobe -r hp_bioscfg && modprobe hp_bioscfg && echo \"\$(hostname): \$(modinfo -n hp_bioscfg)\""
