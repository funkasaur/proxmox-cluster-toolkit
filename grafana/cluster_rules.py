#!/usr/bin/env python3
"""Grafana alert rules for a Proxmox VE cluster, its standalone boxes and Proxmox Backup Server.

Two rule groups in one folder:
  smart   SMART health of every node's boot/ZFS disk (smartctl_exporter), PBS up, datastore space, ZFS pools
  health  node down, quorum, HA services, corosync links, replication, PBS backup age and off-site copy, BIOS and AMT

Data: Prometheus jobs `node` (node-exporter with the textfile metrics from ../textfile/) and `smart`
(smartctl_exporter), both with a `host` label per target. Names and limits come from config.json
(copy config.example.json).

Usage:
  GRAFANA_URL=http://grafana:3000 GRAFANA_TOKEN_FILE=~/.grafana-token ./cluster_rules.py --config ../config.json
  ./cluster_rules.py --print                 # dump the rule JSON, no API calls
  ./cluster_rules.py --dry-run               # read from Grafana, print the writes instead of sending them

Each run is idempotent. The group update replaces the whole group, so grafana_rules.apply() refuses to run if the
group holds rules this file doesn't list (hand-made ones), unless you pass --prune.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import grafana_rules as gr  # noqa: E402


def regex(names):
    return "|".join(names)


def absent_each(metric_selector, hosts):
    """One `absent()` per host, so a host that vanished from Prometheus still alerts."""
    return [f"on() (absent({metric_selector.replace('HOST', h)}) * 1)" for h in hosts]


class Builder:
    def __init__(self, cfg):
        self.cfg = cfg
        g = cfg["grafana"]
        self.folder, self.ds = g["folder_uid"], g["datasource_uid"]
        self.N = f'job="{cfg["jobs"]["node"]}"'
        self.J = f'job="{cfg["jobs"]["smart"]}"'
        self.cluster = list(cfg.get("cluster_nodes", []))
        self.standalone = list(cfg.get("standalone_nodes", []))
        self.pbs = list(cfg.get("pbs_hosts", []))
        self.hosts = self.cluster + self.standalone + self.pbs

    def rule(self, group, uid, title, expr, for_, severity, description, no_data="OK"):
        return gr.rule(uid, title, expr, folder_uid=self.folder, group=group, datasource_uid=self.ds,
                       for_=for_, severity=severity, description=description, no_data=no_data)

    # ---- SMART group -------------------------------------------------------------------------------------------
    def smart(self):
        g, J, N, cfg = self.cfg["grafana"]["smart_group"], self.J, self.N, self.cfg
        s = cfg.get("smart", {})
        ATTR = "smartctl_device_attribute"
        VAL = f'{ATTR}{{{J},attribute_value_type="value"}}'
        THR = f'{ATTR}{{{J},attribute_value_type="thresh"}}'
        EXIT = f"smartctl_device_smartctl_exit_status{{{J}}}"
        BB = f'{ATTR}{{{J},attribute_id="169",attribute_value_type="raw"}}'
        DEV = f"smartctl_device{{{J}}}"
        R = []

        # Standalone boxes are often off on purpose: their exporter-down alert needs node-exporter still up
        # (a powered-off box is StandaloneNodeDown in the health group).
        not_sa = f',host!~"{regex(self.standalone)}"' if self.standalone else ""
        down = [f"max by (host) (1 - up{{{J}{not_sa}}})"]
        if self.standalone:
            down.append(f'(max by (host) (1 - up{{{J},host=~"{regex(self.standalone)}"}}) '
                        f'and on(host) max by (host) (up{{{N}}}) == 1)')
        down += absent_each(f'up{{{J},host="HOST"}}', self.hosts)
        R.append(self.rule(g, "pve-smart-exporter-down", "PveSmartExporterDown", " or ".join(down), "10m", "warning",
                           "Prometheus cannot scrape smartctl_exporter on {{ $labels.host }} (:9633). The node is down "
                           "or rebooting, or the unit stopped: `systemctl status smartctl-exporter` on the node. SMART "
                           "alerts for it are blind until it is back.", no_data="Alerting"))
        # smartctl's own verdict, or exit bit 3 (8) = "SMART status check returned DISK FAILING".
        R.append(self.rule(g, "pve-disk-failing", "PveDiskFailing",
                           f"max by (host, device) ((1 - smartctl_device_smart_status{{{J}}}) + (floor({EXIT} / 8) % 2))",
                           "5m", "critical",
                           "The disk {{ $labels.device }} on {{ $labels.host }} reports SMART health FAILED. Expect it "
                           "to die soon: move the node's guests elsewhere and replace the drive."))
        # Exit bits 4-7: 16 prefail attribute at threshold, 32 attribute past threshold, 64 self-test errors,
        # 128 device error log entries. Value = those bits / 16.
        R.append(self.rule(g, "pve-disk-smart-warning", "PveDiskSmartWarning",
                           f"max by (host, device) (floor({EXIT} / 16))", "10m", "warning",
                           "smartctl flags a problem on {{ $labels.host }} {{ $labels.device }} (value = exit bits / 16, "
                           "added together: 1 prefail attribute at threshold, 2 attribute past threshold, 4 self-test "
                           "failed, 8 error log has entries). On the node: `smartctl -x /dev/{{ $labels.device }}`."))
        # Counters that should stay 0, plus growth of attribute 169. Many SSDs ship with a non-zero factory count
        # there, so only growth over a day counts, and the day after a drive swap (new serial) is skipped.
        R.append(self.rule(g, "pve-disk-errors", "PveDiskErrors",
                           f'max by (host, device, attribute_name) ({ATTR}{{{J},attribute_id=~"5|171|172|187|199",'
                           f'attribute_value_type="raw"}} > 0) or (max by (host, device, attribute_name) '
                           f'({BB} - {BB} offset 1d > 0) unless on (host, device) '
                           f'({DEV} unless on (host, device, serial_number) {DEV} offset 1d))',
                           "0s", "warning",
                           "{{ $labels.host }} {{ $labels.device }}: {{ $labels.attribute_name }} = {{ $value }} (for "
                           "attribute 169: growth in the last day). Reallocations, program/erase failures or "
                           "uncorrectable reads mean the SSD is wearing out: plan a replacement. UDMA_CRC errors point "
                           "at the SATA cable or connector."))
        R.append(self.rule(g, "pve-disk-near-threshold", "PveDiskNearThreshold",
                           f"max by (host, device, attribute_name) (((11 - ({VAL} - ignoring(attribute_value_type) {THR})) > 0) "
                           f"and ignoring(attribute_value_type) {THR} > 0)",
                           "1h", "warning",
                           "{{ $labels.host }} {{ $labels.device }}: {{ $labels.attribute_name }} is within 10 points "
                           "of its failure threshold (value = 11 - margin)."))
        # Attribute 241 (Total_LBAs_Written): the raw unit depends on the drive (GiB on some SSDs, 512-byte LBAs on
        # others). Check yours with `smartctl -A` and set the limits in the same unit; 0 = no check for that host.
        lim, per = s.get("writes_gib_limit", 0), {h: v for h, v in s.get("writes_gib_limit_per_host", {}).items()}
        w = []
        if lim:
            sel = f',host!~"{regex(per)}"' if per else ""
            w.append(f'max by (host, device) ({ATTR}{{{J}{sel},attribute_id="241",attribute_value_type="raw"}}) > {lim}')
        w += [f'max by (host, device) ({ATTR}{{{J},host="{h}",attribute_id="241",attribute_value_type="raw"}}) > {v}'
              for h, v in sorted(per.items()) if v]
        if w:
            R.append(self.rule(g, "pve-disk-writes-high", "PveDiskWritesHigh", " or ".join(w), "1h", "warning",
                               "{{ $labels.host }} {{ $labels.device }} has written {{ $value }} (attribute 241 raw "
                               "units), past the limit set in config.json (pick about 80 % of the drive's TBW "
                               "rating). Plan a replacement before the rated endurance runs out."))
        hot = s.get("disk_hot_celsius", 60)
        R.append(self.rule(g, "pve-disk-hot", "PveDiskHot",
                           f'max by (host, device) (smartctl_device_temperature{{{J},temperature_type="current"}}) > {hot}',
                           "15m", "warning",
                           f"{{{{ $labels.host }}}} {{{{ $labels.device }}}} is over {hot} °C (value = °C). A fan or "
                           "the airflow is failing, an NVMe drive lacks a heatsink, or the room is too hot."))
        # NVMe: the SATA attribute rules above see nothing. Error-log entries alert on growth, not on old ones.
        R.append(self.rule(g, "pve-nvme-errors", "PveNvmeErrors",
                           f"max by (host, device) (smartctl_device_critical_warning{{{J}}} > 0) or "
                           f"max by (host, device) (smartctl_device_media_errors{{{J}}} > 0) or "
                           f"max by (host, device) (delta(smartctl_device_num_err_log_entries{{{J}}}[1d]) > 0)",
                           "0s", "warning",
                           "{{ $labels.host }} {{ $labels.device }} (NVMe): critical warning set, media errors, or new "
                           "error-log entries today (value = the count). `smartctl -x /dev/{{ $labels.device }}`. "
                           "Media errors or a critical warning mean: plan a replacement."))
        used = s.get("nvme_used_percent", 80)
        R.append(self.rule(g, "pve-nvme-wear", "PveNvmeWear",
                           f"max by (host, device) (smartctl_device_percentage_used{{{J}}} > {used}) or "
                           f"max by (host, device) ((smartctl_device_available_spare{{{J}}} - "
                           f"smartctl_device_available_spare_threshold{{{J}}}) < 10)",
                           "1h", "warning",
                           f"{{{{ $labels.host }}}} {{{{ $labels.device }}}} (NVMe) is wearing out: over {used} % of "
                           "rated wear used, or spare blocks within 10 points of the threshold. Plan a replacement."))
        R.append(self.rule(g, "pve-smart-data-missing", "PveSmartDataMissing",
                           f"max by (host) ((up{{{J}}} == 1) unless on(host) smartctl_device_smart_status{{{J}}}) "
                           f"or max by (host, device) (({EXIT} % 4) > 0)",
                           "30m", "warning",
                           "smartctl_exporter on {{ $labels.host }} is up but returns no SMART data (or smartctl could "
                           "not open the device), so the other SMART alerts are blind. On the node: `smartctl --scan` "
                           "and `journalctl -u smartctl-exporter`."))
        R.append(self.rule(g, "pve-zpool-unhealthy", "PveZpoolUnhealthy",
                           f'max by (host, zpool) (node_zfs_zpool_state{{{N},state!="online"}}) > 0', "5m", "critical",
                           "ZFS pool {{ $labels.zpool }} on {{ $labels.host }} is not ONLINE (degraded, faulted or "
                           "suspended). `zpool status -v` on the host; on a PBS datastore, run a verify job before "
                           "trusting the backups."))
        if self.pbs:
            sel = f'up{{{N},host="HOST"}}'
            R.append(self.rule(g, "pbs-down", "PbsDown",
                               " or ".join([f'max by (host) (1 - up{{{N},host=~"{regex(self.pbs)}"}})']
                                           + absent_each(sel, self.pbs)),
                               "10m", "warning",
                               "Prometheus cannot reach node-exporter on Proxmox Backup Server {{ $labels.host }} for 10 "
                               "min. It is down or off the network, so the next scheduled backups will fail.",
                               no_data="Alerting"))
            mp = cfg.get("pbs_datastore_mountpoint")
            if mp:
                R.append(self.rule(g, "pbs-datastore-low", "PbsDatastoreLow",
                                   f'min by (host) (100 * node_filesystem_avail_bytes{{{N},host=~"{regex(self.pbs)}",mountpoint="{mp}"}} '
                                   f'/ node_filesystem_size_bytes{{{N},host=~"{regex(self.pbs)}",mountpoint="{mp}"}}) < 20',
                                   "30m", "warning",
                                   "The PBS datastore on {{ $labels.host }} has {{ $value }} % free. Backups fail when "
                                   "it fills: lower the prune retention, run garbage collection (it frees space only "
                                   "after a prune), or add disk."))
        return R

    # ---- health group ------------------------------------------------------------------------------------------
    def health(self):
        g, J, N, cfg = self.cfg["grafana"]["health_group"], self.J, self.N, self.cfg
        jobs = f'job=~"{cfg["jobs"]["node"]}|{cfg["jobs"]["smart"]}"'
        R = []
        # Down = both exporters unscrapable. Not ping: Intel AMT (and other BMCs sharing the NIC) answer ping while
        # the host is powered off.
        if self.cluster:
            R.append(self.rule(g, "pve-node-down", "PveNodeDown",
                               f'1 - max by (host) (up{{{jobs},host=~"{regex(self.cluster)}"}})', "5m", "critical",
                               "Proxmox node {{ $labels.host }}: neither node-exporter (:9100) nor smartctl_exporter "
                               "(:9633) answers for 5 min, so the node is off, hung or off the network. With HA, its "
                               "guests restart elsewhere once it is fenced; without HA, see the README's manual "
                               "failover notes. A two-node cluster also loses quorum now."))
        if self.standalone:
            R.append(self.rule(g, "pve-standalone-down", "StandaloneNodeDown",
                               f'1 - max by (host) (up{{{jobs},host=~"{regex(self.standalone)}"}})', "10m", "warning",
                               "Standalone Proxmox box {{ $labels.host }}: neither exporter answers for 10 min. Off on "
                               "purpose? Silence it in Grafana. Otherwise check power, the BMC/AMT, or the KVM."))
        R.append(self.rule(g, "pve-quorum-lost", "PveQuorumLost",
                           "max by (host) (1 - pve_ha_quorate)", "5m", "critical",
                           "Node {{ $labels.host }} sees the cluster as not quorate. Guests keep running but nothing "
                           "can be started, migrated or changed, and HA nodes may fence themselves. `pvecm status` on "
                           "the node."))
        age = cfg.get("replication_max_age_s", 3600)
        R.append(self.rule(g, "pve-replication-stale", "PveReplicationStale",
                           f"(((time() - max by (repl_id) (pve_replication_last_sync_timestamp_seconds)) > bool {age})"
                           " + (max by (repl_id) (pve_replication_failures) > bool 0)"
                           " + on() group_left() ((time() - min(pve_replication_metrics_timestamp_seconds)) > bool 1800))"
                           " or on() (absent(pve_replication_last_sync_timestamp_seconds) * 1)", "15m", "warning",
                           f"Replication job {{{{ $labels.repl_id }}}} has not synced in {age // 60} min, is failing, or "
                           "a node's metrics timer stopped (no repl_id = no replication data at all). The copy on the "
                           "target node is getting old. `pvesr status` on the source node; timer: `systemctl status "
                           "pve-replication-metrics.timer`.", no_data="Alerting"))
        R.append(self.rule(g, "pve-ha-service-problem", "PveHaServiceProblem",
                           "(1 - min by (sid) (pve_ha_service_ok))"
                           " + on() group_left() ((time() - max(pve_ha_metrics_timestamp_seconds)) > bool 1800)"
                           " or on() (absent(pve_ha_service_ok) * 1)", "10m", "critical",
                           "HA service {{ $labels.sid }} has not been in its requested state for 10 min (error, fence, "
                           "recovery, stuck migration), or no node has written HA metrics for 30 min (no sid = no HA "
                           "data at all). `ha-manager status`.", no_data="Alerting"))
        R.append(self.rule(g, "pve-corosync-link-down", "PveCorosyncLinkDown",
                           "max by (host, link) (1 - pve_corosync_link_connected)"
                           " + on() group_left() ((time() - max(pve_corosync_metrics_timestamp_seconds)) > bool 600)"
                           " or on() (absent(pve_corosync_link_connected) * 1)", "5m", "warning",
                           "Corosync link {{ $labels.link }} on {{ $labels.host }} has lost a peer for 5 min, or no "
                           "node wrote corosync metrics for 10 min. The cluster still runs on the other link, but one "
                           "more failure costs quorum. `corosync-cfgtool -s`; check the cable and the switch.",
                           no_data="Alerting"))
        R.append(self.rule(g, "pve-ha-off-preferred-node", "PveHaOffPreferredNode",
                           "1 - min by (sid) (pve_ha_service_on_preferred)", "30m", "warning",
                           "HA service {{ $labels.sid }} has run away from its preferred node for 30 min: a failover "
                           "happened and the preferred node is still down, or failback did not happen. `ha-manager "
                           "status`."))
        R.append(self.rule(g, "pve-ha-node-maintenance", "PveHaNodeMaintenance",
                           "max by (node) (pve_ha_node_maintenance)", "3h", "warning",
                           "Proxmox node {{ $labels.node }} has been in HA maintenance mode for 3 h; its HA guests stay "
                           "elsewhere and it takes no failovers. Done? `pve-maint disable {{ $labels.node }}`."))
        if self.pbs:
            R.append(self.rule(g, "pbs-backup-stale", "PbsBackupStale",
                               "(time() - max by (datastore, namespace, type, id) "
                               "(pbs_backup_last_timestamp_seconds)) > bool 93600", "5m", "warning",
                               "The latest completed backup for {{ $labels.type }}/{{ $labels.id }} in "
                               "datastore {{ $labels.datastore }} (namespace '{{ $labels.namespace }}') is older "
                               "than 26 h. Check the PBS task log and pbs-backup-age.timer."))
        # Keep healthy nodes in the result so their alert instances can clear.
        R.append(self.rule(g, "bios-guard-problem", "BiosGuardProblem",
                           "(max by (node) (bios_guard_drift_settings) > 0)"
                           " or on (node) (1 - min by (node) "
                           '({__name__=~"bios_guard_bios_ok|bios_guard_amt_ok"}))',
                           "5m", "warning",
                           "bios-guard found BIOS drift, a BIOS problem or a failed BIOS or AMT check on "
                           "{{ $labels.node }}. Check its report for detected drift and whether it was fixed."))
        if cfg.get("offsite_max_age_s"):
            age = cfg["offsite_max_age_s"]
            R.append(self.rule(g, "pbs-offsite-stale", "PbsOffsiteStale",
                               f"((time() - max(pbs_offsite_last_success_timestamp_seconds)) > bool {age})"
                               " + (max(pbs_offsite_status) == bool 1)"
                               " or on() (absent(pbs_offsite_last_success_timestamp_seconds) * 1)", "15m", "warning",
                               f"The off-site copy of the PBS datastore (pbs/pbs_offsite.sh) failed or has not "
                               f"succeeded in {age // 3600} h. See its log; status 3 = stopped at the cloud's daily "
                               "upload cap and resumes next run. A leftover snapshot: `zfs list -t snapshot` on PBS.",
                               no_data="Alerting"))
        return R

    def groups(self):
        g = self.cfg["grafana"]
        return {"smart": (g["smart_group"], self.smart()), "health": (g["health_group"], self.health())}


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply the Proxmox cluster alert rules to Grafana")
    ap.add_argument("--config", default=os.path.join(HERE, "..", "config.json"))
    ap.add_argument("--group", choices=["smart", "health", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true", help="read from Grafana, print writes instead of sending them")
    ap.add_argument("--prune", action="store_true", help="allow the group update to delete rules not listed here")
    ap.add_argument("--print", action="store_true", help="print the rules as JSON and exit")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    groups = Builder(cfg).groups()
    chosen = [k for k in ("smart", "health") if a.group in (k, "all")]
    if a.print:
        print(json.dumps([r for k in chosen for r in groups[k][1]], indent=2))
        return 0
    try:
        graf = gr.Grafana(dry_run=a.dry_run)
        folder = cfg["grafana"]["folder_uid"]
        status, _ = graf.call("GET", f"/api/folders/{folder}")
        if status != 200:
            status, body = graf.call("POST", "/api/folders",
                                     {"uid": folder, "title": cfg["grafana"].get("folder_title", folder)})
            print(f"folder {folder}: HTTP {status}")
        for k in chosen:
            name, rules = groups[k]
            gr.apply(graf, rules, folder_uid=folder, group=name,
                     interval_s=cfg["grafana"].get("interval_s", 60), prune=a.prune)
    except (gr.GrafanaError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
