#!/usr/bin/python3
"""Proxmox storage replication status -> node-exporter textfile collector.

Run on every node that can be a replication source. The source node reports its jobs; after a migration Proxmox
flips the job to the new source, and the old one writes a file with no job lines (only the timestamp).

Metrics: pve_replication_last_sync_timestamp_seconds{repl_id,guest,source,target} (0 = never),
pve_replication_failures, pve_replication_duration_seconds, pve_replication_metrics_timestamp_seconds.
"""
import json
import os
import socket
import subprocess
import time

OUT = os.environ.get("PVE_REPLICATION_METRICS_OUT", "/var/lib/prometheus/node-exporter/pve_replication.prom")


def render(jobs, node, now=None):
    L = ["# HELP pve_replication_last_sync_timestamp_seconds Last successful sync (0 = never).",
         "# TYPE pve_replication_last_sync_timestamp_seconds gauge",
         "# HELP pve_replication_failures Consecutive failed syncs.",
         "# TYPE pve_replication_failures gauge",
         "# HELP pve_replication_duration_seconds Duration of the last sync.",
         "# TYPE pve_replication_duration_seconds gauge"]
    for j in jobs or []:
        lab = f'repl_id="{j["id"]}",guest="{j["guest"]}",source="{j.get("source", node)}",target="{j["target"]}"'
        L.append(f"pve_replication_last_sync_timestamp_seconds{{{lab}}} {j.get('last_sync', 0)}")
        L.append(f"pve_replication_failures{{{lab}}} {j.get('fail_count', 0)}")
        L.append(f"pve_replication_duration_seconds{{{lab}}} {j.get('duration', 0)}")
    L.append("# HELP pve_replication_metrics_timestamp_seconds When this file was written.")
    L.append(f"pve_replication_metrics_timestamp_seconds {int(now if now is not None else time.time())}")
    return "\n".join(L) + "\n"


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main():
    node = socket.gethostname().split(".")[0]
    r = subprocess.run(["pvesh", "get", f"/nodes/{node}/replication", "--output-format", "json"],
                       capture_output=True, text=True, timeout=60, check=True)
    write(OUT, render(json.loads(r.stdout or "[]"), node))


if __name__ == "__main__":
    main()
