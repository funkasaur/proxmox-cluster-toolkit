#!/usr/bin/python3
"""Corosync knet link status -> node-exporter textfile collector.

With two corosync links in passive mode, a dead link is silent until the other one fails too. Every node writes
its view of each link to each peer (from `corosync-cfgtool -s`), so an alert can fire while the cluster still runs.

Metrics: pve_corosync_link_connected{link,peer}, pve_corosync_cfgtool_ok, pve_corosync_metrics_timestamp_seconds.
"""
import os
import re
import subprocess
import time

OUT = os.environ.get("PVE_COROSYNC_METRICS_OUT", "/var/lib/prometheus/node-exporter/pve_corosync.prom")


def render(out, ok, now=None):
    L = ["# HELP pve_corosync_link_connected 1 while this node's corosync link reaches the peer.",
         "# TYPE pve_corosync_link_connected gauge"]
    link = None
    for line in out.splitlines():
        m = re.match(r"LINK ID (\d+)", line)
        if m:
            link = m.group(1)
            continue
        m = re.match(r"\s*nodeid:\s*(\d+):\s*(\S+)", line)
        if m and link is not None and m.group(2) != "localhost":
            L.append(f'pve_corosync_link_connected{{link="{link}",peer="{m.group(1)}"}} {int(m.group(2) == "connected")}')
    L.append("# HELP pve_corosync_cfgtool_ok 1 if corosync-cfgtool ran.")
    L.append(f"pve_corosync_cfgtool_ok {int(ok)}")
    L.append("# HELP pve_corosync_metrics_timestamp_seconds When this file was written.")
    L.append(f"pve_corosync_metrics_timestamp_seconds {int(now if now is not None else time.time())}")
    return "\n".join(L) + "\n"


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main():
    try:
        out = subprocess.run(["corosync-cfgtool", "-s"], capture_output=True, text=True, timeout=30, check=True).stdout
        ok = True
    except (OSError, subprocess.SubprocessError):
        out, ok = "", False
    write(OUT, render(out, ok))


if __name__ == "__main__":
    main()
