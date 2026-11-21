#!/usr/bin/env python3
"""Proxmox cluster overview dashboard: every node side by side, one row per metric.

Sources: Prometheus job `node` (node-exporter + the textfile metrics from ../textfile/) and job `smart`
(smartctl_exporter), names from config.json. The data source is a dashboard variable (${datasource}), so the JSON
imports into any Grafana. Queries select by job, not by host list: a new node shows up by itself.

RAM "used" counts the ZFS ARC (Linux does not report it as available); the ARC panel shows how much of it is cache.

  ./cluster_dashboard.py --config ../config.json --out proxmox-cluster.json   # write JSON for Dashboards > Import
  GRAFANA_URL=... GRAFANA_TOKEN_FILE=... ./cluster_dashboard.py --push         # POST /api/dashboards/db (overwrite)
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import grafana_rules as gr  # noqa: E402  (Grafana API client)

DS = {"type": "prometheus", "uid": "${datasource}"}
G, Y, R = "green", "yellow", "red"


def build(cfg, uid="pve-cluster", title="Proxmox cluster"):
    N = f'job="{cfg["jobs"]["node"]}"'
    J = f'job="{cfg["jobs"]["smart"]}"'
    CPU = f'100 * (1 - avg by (host) (rate(node_cpu_seconds_total{{{N},mode="idle"}}[$__rate_interval])))'
    RAM = f"100 * (1 - node_memory_MemAvailable_bytes{{{N}}} / node_memory_MemTotal_bytes{{{N}}})"
    ROOT = (f'100 * (1 - node_filesystem_avail_bytes{{{N},mountpoint="/"}} '
            f'/ node_filesystem_size_bytes{{{N},mountpoint="/"}})')
    CPUTEMP = f'max by (host) (node_hwmon_temp_celsius{{{N},chip=~"platform_coretemp.*"}})'
    SSDTEMP = f'max by (host) (smartctl_device_temperature{{{J},temperature_type="current"}})'
    NET = 'device!~"lo|tap.*|fw.*|veth.*"'
    DISK = 'device=~"sd.|nvme.n."'
    ids = iter(range(1, 1000))

    def panel(kind, ptitle, exprs, x, y, w, h, unit="", legend="{{host}}", defaults=None, options=None):
        return {"id": next(ids), "type": kind, "title": ptitle, "datasource": DS,
                "gridPos": {"x": x, "y": y, "w": w, "h": h},
                "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e,
                             "legendFormat": lf if lf is not None else legend,
                             "instant": kind == "stat", "range": kind != "stat"}
                            for i, (e, lf) in enumerate(exprs)],
                "fieldConfig": {"defaults": {"unit": unit, **(defaults or {})}, "overrides": []},
                "options": options or {}}

    def stat(ptitle, expr, x, y, unit, steps, decimals=0, legend="{{host}}"):
        return panel("stat", ptitle, [(expr, None)], x, y, 4, 4, unit, legend=legend,
                     defaults={"decimals": decimals, "color": {"mode": "thresholds"},
                               "thresholds": {"mode": "absolute",
                                              "steps": [{"color": c, "value": v} for v, c in steps]}},
                     options={"reduceOptions": {"calcs": ["lastNotNull"]}, "orientation": "vertical",
                              "textMode": "value_and_name", "colorMode": "background", "graphMode": "none"})

    def ts(ptitle, exprs, x, y, unit, w=12, h=8):
        return panel("timeseries", ptitle, exprs, x, y, w, h, unit,
                     defaults={"custom": {"fillOpacity": 8, "lineWidth": 1, "showPoints": "never"}},
                     options={"legend": {"displayMode": "table", "placement": "right",
                                         "calcs": ["lastNotNull", "max"]},
                              "tooltip": {"mode": "multi", "sort": "desc"}})

    panels = [
        stat("Uptime", f"time() - node_boot_time_seconds{{{N}}}", 0, 0, "dtdurations",
             [(None, R), (600, Y), (3600, G)]),
        stat("CPU %", CPU, 4, 0, "percent", [(None, G), (70, Y), (90, R)]),
        stat("RAM used % (incl. ZFS ARC)", RAM, 8, 0, "percent", [(None, G), (85, Y), (95, R)]),
        stat("Root disk %", ROOT, 12, 0, "percent", [(None, G), (75, Y), (90, R)]),
        stat("CPU temp", CPUTEMP, 16, 0, "celsius", [(None, G), (80, Y), (90, R)]),
        stat("SSD temp", SSDTEMP, 20, 0, "celsius", [(None, G), (55, Y), (65, R)]),
        # Cluster state from the textfile collectors (empty on a host that doesn't run them).
        stat("HA services not OK", "count(pve_ha_service_ok == 0) or vector(0)", 0, 4, "none",
             [(None, G), (1, R)], legend="services"),
        stat("Corosync links down", "count(pve_corosync_link_connected == 0) or vector(0)", 4, 4, "none",
             [(None, G), (1, R)], legend="links"),
        stat("Quorate", "min(pve_ha_quorate)", 8, 4, "none", [(None, R), (1, G)], legend="cluster"),
        panel("timeseries", "Replication age", [
            ("time() - max by (repl_id, source, target) (pve_replication_last_sync_timestamp_seconds > 0)",
             "{{repl_id}} {{source}} → {{target}}")], 12, 4, 12, 4, "s",
            defaults={"custom": {"fillOpacity": 8, "lineWidth": 1, "showPoints": "never"}},
            options={"legend": {"displayMode": "list", "placement": "right"}}),
        ts("CPU %", [(CPU, None)], 0, 8, "percent"),
        ts("Load (1 min) per core", [(f"node_load1{{{N}}} / on (host) count by (host) "
                                      f'(node_cpu_seconds_total{{{N},mode="idle"}})', None)], 12, 8, "none"),
        ts("RAM used", [(f"node_memory_MemTotal_bytes{{{N}}} - node_memory_MemAvailable_bytes{{{N}}}", "{{host}} used"),
                        (f"node_memory_MemTotal_bytes{{{N}}}", "{{host}} total")], 0, 16, "bytes"),
        ts("ZFS ARC size (cache, counted as used)", [(f"node_zfs_arc_size{{{N}}}", None)], 12, 16, "bytes"),
        ts("Network in/out (all NICs, excl. lo/tap/fw/veth)",
           [(f"sum by (host) (rate(node_network_receive_bytes_total{{{N},{NET}}}[$__rate_interval]))", "{{host}} in"),
            (f"-sum by (host) (rate(node_network_transmit_bytes_total{{{N},{NET}}}[$__rate_interval]))", "{{host}} out")],
           0, 24, "Bps"),
        ts("Disk read/write",
           [(f"sum by (host) (rate(node_disk_read_bytes_total{{{N},{DISK}}}[$__rate_interval]))", "{{host}} read"),
            (f"-sum by (host) (rate(node_disk_written_bytes_total{{{N},{DISK}}}[$__rate_interval]))", "{{host}} write")],
           12, 24, "Bps"),
        ts("CPU / SSD temperature", [(CPUTEMP, "{{host}} CPU"), (SSDTEMP, "{{host}} SSD")], 0, 32, "celsius"),
        ts("ZFS pool not ONLINE (0 = healthy)",
           [(f'max by (host, zpool) (node_zfs_zpool_state{{{N},state!="online"}})', "{{host}} {{zpool}}")],
           12, 32, "none"),
    ]
    links = [{"title": t, "type": "link", "url": u, "targetBlank": True} for t, u in cfg.get("links", {}).items()]
    return {"uid": uid, "title": title, "tags": ["proxmox", "pve", "pbs"], "timezone": "browser",
            "refresh": "1m", "time": {"from": "now-24h", "to": "now"}, "schemaVersion": 39,
            "templating": {"list": [{"name": "datasource", "label": "Prometheus", "type": "datasource",
                                     "query": "prometheus", "current": {}, "hide": 0}]},
            "panels": panels, "links": links}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Proxmox cluster dashboard")
    ap.add_argument("--config", default=os.path.join(HERE, "..", "config.json"))
    ap.add_argument("--out", help="write the dashboard JSON to this file")
    ap.add_argument("--push", action="store_true", help="create/overwrite it in Grafana (GRAFANA_URL, GRAFANA_TOKEN)")
    a = ap.parse_args(argv)
    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    dash = build(cfg)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(dash, f, indent=1)
        print(f"wrote {a.out}")
    if a.push:
        try:
            s, r = gr.Grafana().call("POST", "/api/dashboards/db",
                                     {"dashboard": dash, "folderUid": cfg["grafana"]["folder_uid"],
                                      "overwrite": True, "message": "cluster_dashboard.py"})
        except gr.GrafanaError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print("dashboard", s, r.get("url") if s == 200 and isinstance(r, dict) else r)
        return 0 if s == 200 else 1
    if not a.out:
        print(json.dumps(dash, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
