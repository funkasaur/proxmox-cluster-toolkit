#!/usr/bin/python3
"""Proxmox HA status -> node-exporter textfile collector.

Run on every cluster node (systemd timer). Every node writes the same cluster-wide view, so alert rules use
max/min by sid and keep working with a node down.

Metrics: pve_ha_service_ok{sid}, pve_ha_service_on_preferred{sid,node}, pve_ha_service_info{sid,node,state,
request_state}, pve_ha_node_maintenance{node}, pve_ha_node_online{node}, pve_ha_quorate,
pve_ha_metrics_timestamp_seconds.

"Preferred" = the highest-priority nodes of the service's node-affinity rule (/cluster/ha/rules, Proxmox VE 9).
On older releases without HA rules, pve_ha_service_on_preferred is simply not written.
"""
import json
import os
import subprocess
import time

OUT = os.environ.get("PVE_HA_METRICS_OUT", "/var/lib/prometheus/node-exporter/pve_ha.prom")


def pvesh(path):
    r = subprocess.run(["pvesh", "get", path, "--output-format", "json"],
                       capture_output=True, text=True, timeout=60, check=True)
    return json.loads(r.stdout or "null")


def preferred_nodes(rules):
    preferred = {}
    for r in rules or []:
        if r.get("type") != "node-affinity" or r.get("disable"):
            continue
        nodes = [n.split(":") for n in r["nodes"].split(",")]
        pri = {n[0]: int(n[1]) if len(n) > 1 else 0 for n in nodes}
        top = {n for n, p in pri.items() if p == max(pri.values())}
        for sid in r["resources"].split(","):
            preferred[sid] = top
    return preferred


def render(current, manager, rules, now=None):
    preferred = preferred_nodes(rules)
    L = ["# HELP pve_ha_service_ok 1 while the HA service is in its requested state.",
         "# TYPE pve_ha_service_ok gauge",
         "# HELP pve_ha_service_on_preferred 1 while the service runs on a top-priority node of its node-affinity rule.",
         "# TYPE pve_ha_service_on_preferred gauge",
         "# HELP pve_ha_service_info HA service placement and state.",
         "# TYPE pve_ha_service_info gauge"]
    for s in current or []:
        if s.get("type") != "service":
            continue
        sid, node, state, req = s["sid"], s.get("node", ""), s.get("state", ""), s.get("request_state", "")
        L.append(f'pve_ha_service_info{{sid="{sid}",node="{node}",state="{state}",request_state="{req}"}} 1')
        L.append(f'pve_ha_service_ok{{sid="{sid}"}} {int(state == req)}')
        if sid in preferred:
            L.append(f'pve_ha_service_on_preferred{{sid="{sid}",node="{node}"}} {int(node in preferred[sid])}')
    L += ["# HELP pve_ha_node_maintenance 1 while the node is in HA maintenance mode.",
          "# TYPE pve_ha_node_maintenance gauge",
          "# HELP pve_ha_node_online 1 while the HA manager sees the node online (or in maintenance).",
          "# TYPE pve_ha_node_online gauge"]
    for node, st in ((manager or {}).get("node_status") or {}).items():
        L.append(f'pve_ha_node_maintenance{{node="{node}"}} {int(st == "maintenance")}')
        L.append(f'pve_ha_node_online{{node="{node}"}} {int(st in ("online", "maintenance"))}')
    quorum = next((s for s in current or [] if s.get("type") == "quorum"), {})
    L.append("# HELP pve_ha_quorate 1 while the cluster is quorate.")
    L.append(f"pve_ha_quorate {int(quorum.get('quorate', 0))}")
    L.append("# HELP pve_ha_metrics_timestamp_seconds When this file was written.")
    L.append(f"pve_ha_metrics_timestamp_seconds {int(now if now is not None else time.time())}")
    return "\n".join(L) + "\n"


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main():
    try:
        rules = pvesh("/cluster/ha/rules")
    except subprocess.CalledProcessError:  # no HA rules API (before Proxmox VE 9)
        rules = []
    write(OUT, render(pvesh("/cluster/ha/status/current"),
                      (pvesh("/cluster/ha/status/manager_status") or {}).get("manager_status", {}), rules))


if __name__ == "__main__":
    main()
