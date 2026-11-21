# proxmox-cluster-toolkit

Monitoring and maintenance tools for a small Proxmox VE cluster (plus standalone Proxmox boxes and a Proxmox Backup
Server), built around Prometheus node-exporter and Grafana-managed alerting.

| Folder | What it does |
|---|---|
| `textfile/` | Three node-exporter textfile collectors + systemd timers: **corosync link** state per peer, **HA** service state / placement / maintenance / quorum, **storage replication** age and failures |
| `smart/` | systemd unit for `smartctl_exporter` on each node, plus a smartd self-test schedule |
| `grafana/` | **Alert rules as code** (two groups: SMART and cluster health, 22 rules) and a **cluster dashboard** builder |
| `maintenance/` | `pve-maint`: HA node-maintenance on/off that waits until the node is empty and lists the guests HA won't move |
| `pbs/` | `pbs_offsite.sh`: off-site copy of a PBS datastore to any rclone remote as verified ZFS send streams |
| `bios-guard/` | Daily BIOS + Intel AMT drift check (and fix) for HP business desktops used as nodes, with a **patched `hp-bioscfg` driver** that makes BIOS writes work |

Everything is driven by config files (`config.example.json`, `bios-guard/targets.example.json`); no host names or
addresses are in the code. Python 3, standard library only.

## 1. Textfile metrics (`textfile/`)

On every cluster node (Debian's `prometheus-node-exporter` reads `/var/lib/prometheus/node-exporter/*.prom`):

```sh
cp textfile/pve-*-metrics.py /usr/local/bin/
cp textfile/systemd/pve-*-metrics.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pve-ha-metrics.timer pve-corosync-metrics.timer pve-replication-metrics.timer
```

| Metric | Labels | Notes |
|---|---|---|
| `pve_corosync_link_connected` | `link`, `peer` | from `corosync-cfgtool -s`, every minute. With two links in passive mode a dead link is otherwise silent until the second one fails |
| `pve_corosync_cfgtool_ok` | | 0 if `corosync-cfgtool` failed |
| `pve_ha_service_ok` | `sid` | 1 while the service is in its requested state |
| `pve_ha_service_on_preferred` | `sid`, `node` | 1 while it runs on a top-priority node of its node-affinity rule (HA rules: Proxmox VE 9) |
| `pve_ha_service_info` | `sid`, `node`, `state`, `request_state` | always 1 |
| `pve_ha_node_maintenance`, `pve_ha_node_online` | `node` | from the HA manager |
| `pve_ha_quorate` | | 1 while quorate |
| `pve_replication_last_sync_timestamp_seconds` | `repl_id`, `guest`, `source`, `target` | 0 = never synced |
| `pve_replication_failures`, `pve_replication_duration_seconds` | same | consecutive failures, last duration |
| `pve_*_metrics_timestamp_seconds` | | when the file was written (alerts catch a stopped timer) |

Every node writes the same cluster-wide HA view, so the HA alerts use `min`/`max by (sid)` and keep working with a
node down. Replication jobs are reported by their source node; after a migration Proxmox flips the job and the old
source writes no job lines.

## 2. SMART (`smart/`)

On each node:

1. **smartd** runs the self-tests. In `/etc/smartd.conf`, give each node its own hour so they don't overlap, e.g.
   `DEVICESCAN -d removable -n standby -a -s (S/../.././03|L/../../7/04) -m root -M exec /usr/share/smartmontools/smartd-runner`
   (short test daily 03:00, long test Sunday 04:00). smartd's mail usually goes nowhere on a node without a relay;
   the alerts come from Grafana instead: the exporter's smartctl exit status carries a failed self-test.
2. **smartctl_exporter**: put the binary from the latest release of
   [prometheus-community/smartctl_exporter](https://github.com/prometheus-community/smartctl_exporter/releases) in
   `/usr/local/bin/`, copy `smart/smartctl-exporter.service` to `/etc/systemd/system/`, `systemctl enable --now
   smartctl-exporter`. It listens on `:9633`.
3. **Prometheus**: two jobs, `node-pve` (:9100) and `smartctl-pve` (:9633), each target with a `host` label:
   `examples/prometheus.yml`. Other job names work: set them in `config.json`.

## 3. Grafana alert rules (`grafana/cluster_rules.py`)

```sh
cp config.example.json config.json    # node names, job names, data source uid, limits
cd grafana
./cluster_rules.py --print            # the rule JSON, no API calls
export GRAFANA_URL=http://grafana:3000 GRAFANA_TOKEN_FILE=~/.config/grafana-token   # service account, alert-rule write
./cluster_rules.py --dry-run          # reads Grafana, prints the writes
./cluster_rules.py                    # creates the folder if needed, then both groups
```

> **Warning.** Setting a group's interval goes through Grafana's rule-group endpoint, which **replaces the whole
> group**: a rule in that group that this file doesn't list would be deleted. `grafana_rules.apply()` therefore
> reads the group first and **refuses** if it holds other rules. Keep hand-made rules in another group, or pass
> `--prune` if you really want them gone.

| Group | Rule | Fires when |
|---|---|---|
| SMART | PveSmartExporterDown | smartctl_exporter unscrapable or missing (standalone boxes: only while node-exporter is up) |
| | PveDiskFailing (critical) | SMART health FAILED |
| | PveDiskSmartWarning | smartctl exit bits 4-7 (attribute at/past threshold, self-test failed, error log entries) |
| | PveDiskErrors | reallocated / program / erase / uncorrectable / CRC counters > 0, or attribute 169 grew in a day (skipped for a day after a drive swap) |
| | PveDiskNearThreshold | any normalized attribute within 10 of its threshold |
| | PveDiskWritesHigh | attribute 241 past the configured limit (see below) |
| | PveDiskHot | drive over `disk_hot_celsius` for 15 min |
| | PveNvmeErrors, PveNvmeWear | NVMe critical warning, media errors, new error-log entries; wear over `nvme_used_percent` or spare near threshold |
| | PveSmartDataMissing | exporter up but no SMART data |
| | PveZpoolUnhealthy (critical) | any ZFS pool not ONLINE |
| | PbsDown, PbsDatastoreLow | PBS node-exporter down; datastore under 20 % free |
| Health | PveNodeDown (critical) | both exporters of a cluster node down for 5 min |
| | StandaloneNodeDown | same for standalone boxes, warning after 10 min |
| | PveQuorumLost (critical) | a node reports the cluster not quorate |
| | PveReplicationStale | a job older than `replication_max_age_s`, failing, a node's timer stopped, or no data |
| | PveHaServiceProblem (critical) | a service out of its requested state for 10 min, or no HA data |
| | PveCorosyncLinkDown | a link lost a peer for 5 min, or no corosync data |
| | PveHaOffPreferredNode | a service away from its preferred node for 30 min |
| | PveHaNodeMaintenance | a node left in maintenance for 3 h |
| | PbsOffsiteStale | `pbs_offsite.sh` failed or no success within `offsite_max_age_s` |

Notes:
- **Node down is not ping.** Intel AMT (and BMCs sharing the NIC) answer ping while the host is off, so "down"
  means neither exporter answers.
- **Attribute 241 units differ per drive**: some SSDs report GiB, others 512-byte LBAs. Check `smartctl -A` and set
  `writes_gib_limit` (default) and `writes_gib_limit_per_host` in the drive's unit, about 80 % of its TBW rating;
  `0` turns the check off.
- Attribute 169 (bad-block count on some SSDs) often ships non-zero from the factory, so only growth alerts.
- `tests/check_promql.py` syntax-checks every query with `promtool` (Docker).

## 4. Dashboard (`grafana/cluster_dashboard.py`)

`./cluster_dashboard.py --out proxmox-cluster.json` writes JSON for *Dashboards > Import*; `--push` creates or
overwrites it through the API. Data source = dashboard variable, so it imports into any Grafana. Panels: uptime,
CPU, RAM (counting the ZFS ARC as used), root disk, CPU/SSD temperature, HA services not OK, corosync links down,
quorum, replication age, load per core, ARC size, network, disk I/O, ZFS pool state. Queries select by job, so a new
node appears by itself. For a per-host deep dive, the community "Node Exporter Full" dashboard works with the same job.

## 5. Node maintenance (`maintenance/pve-maint`)

```sh
pve-maint status
pve-maint enable pve2      # HA moves its services away; waits until none is left (--timeout 600, --no-wait)
reboot / work on pve2
pve-maint disable pve2     # waits until the HA manager sees it online; affinity rules move services back
```

`enable` lists guests that are **not** HA-managed and still running on the node: HA leaves those alone, so shut them
down or migrate them before a reboot.

**Two nodes, replication, no HA** (manual failover): on the surviving node run `pvecm expected 1`, move the guest
configs from `/etc/pve/nodes/<dead>/qemu-server/` to `/etc/pve/nodes/<alive>/qemu-server/`, then `qm start` (the
replica is as old as the last sync). With three nodes, let HA do this.

## 6. PBS off-site copy (`pbs/pbs_offsite.sh`)

Runs on a host that can ssh to PBS as root and has an rclone remote. Each run snapshots the datastore's ZFS dataset,
pipes `zfs send -c` (a full stream once a week, incrementals between) straight into `rclone rcat`, checks the
stream's md5 against the remote's, keeps the newest `KEEP_CHAINS` chains, and writes `pbs_offsite_*` metrics for the
PbsOffsiteStale rule. Streams instead of the chunk files because a datastore has tens of thousands of small files,
which cloud storage APIs upload slowly. Options are in the script header.

```sh
PBS=root@pbs.example.com DST=gdrive:pbs-offsite PROM=/var/lib/prometheus/node-exporter/pbs_offsite.prom ./pbs/pbs_offsite.sh
```

If PVE encrypts the backups (recommended), **the copy is useless without the encryption key**: keep the key file and
its paper key somewhere other than the cluster.

Restore (test it once):
1. On a host with ZFS (PBS itself, or a fresh PBS install), receive the newest chain in order, full first:
   `for f in $(rclone lsf --include '*.zfs' REMOTE/chain-<stamp> | sort); do rclone cat REMOTE/chain-<stamp>/$f | ssh root@<pbs> zfs receive -F -u rpool/restored; done`
2. `zfs set mountpoint=/rpool/restored rpool/restored && zfs mount rpool/restored`
3. `proxmox-backup-manager datastore create restored /rpool/restored --reuse-datastore true`, then
   `proxmox-backup-manager verify restored`
4. Give the PVE user and token read access to `/datastore/restored` (a token only gets what its user also has), add it
   in PVE as PBS storage with the same encryption key, and restore from the UI.
5. Clean up: remove the ACLs, `proxmox-backup-manager datastore remove restored`, `zfs destroy -r rpool/restored`.

## 7. bios-guard (`bios-guard/`)

For HP business desktops (tested: EliteDesk 800 G3 Mini) used as Proxmox nodes. Their BIOS can lose settings, for
example after a power loss: "After Power Loss" back to Off, PCIe power management back on. bios-guard runs daily on a
monitoring host:

- **BIOS**: over ssh, reads every `hp-bioscfg` attribute (`/sys/class/firmware-attributes/hp-bioscfg/`), compares
  with `targets.json`, and with `--fix` writes drifted writable settings back, reloads the module and reads them
  again from the BIOS. Changes apply at the next boot: it reports "reboot pending" and never reboots. Settings in
  `never_write` (e.g. "Unconfigure AMT on next boot") are checked, never written.
- **AMT**: WS-Man reads on `:16992` with a read-only AMT account: control mode ACM, no user consent, SOL + IDE-R on,
  KVM on. CCM activation from the OS needs on-screen consent for KVM, which is useless headless, so a failure here
  is fixed in MEBx (Ctrl+P) or an AMT admin tool.
- Output: `last.json`, a summary on stdout, optional textfile metrics (`--prom`: `bios_guard_bios_ok`,
  `bios_guard_amt_ok`, `bios_guard_patched_module`, ...), and with `--notify` a webhook and/or Telegram message
  (`notify.env.example`) only when something is wrong.

```sh
cp bios-guard/targets.example.json bios-guard/targets.json   # nodes, ssh targets, AMT addresses, wanted values
mkdir -p ~/.config/bios-guard && install -m 600 bios-guard/amt.env.example ~/.config/bios-guard/amt.env   # then edit
./bios-guard/bios_guard.py --dump pve1                       # every attribute with RW/RO, to pick targets
./bios-guard/install_module.sh root@pve1                     # patched driver via DKMS (needed for --fix)
./bios-guard/bios_guard.py --fix                             # or the systemd timer in bios-guard/systemd/
```

**Why a patched driver:** the stock `hp-bioscfg` reads but cannot write on these boards when no BIOS setup password
is set: every write fails with BIOS code 0x4, because the driver sizes the security buffer 16 bytes too small. The
fix is one function; details in `bios-guard/dkms/hp-bioscfg-fix-1.0/README.md`. `install_module.sh` builds it with
DKMS for every installed kernel, so kernel updates rebuild it. If a build ever fails, the stock module loads, reads
still work, and the summary says **PATCHED MODULE NOT LOADED** until you rerun `install_module.sh`.

The write fix is meant for the mainline kernel (`upstream-fix.patch`); once a kernel you run carries it, drop the
DKMS module.

## Tests

```sh
python3 -m unittest discover -s tests -t .     # 44 tests, no network, no cluster needed
python3 tests/check_promql.py                  # promtool syntax check of all 44 queries (Docker)
```

## License

MIT (`LICENSE`), except `bios-guard/dkms/hp-bioscfg-fix-1.0/`, which is derived from the Linux kernel and is
**GPL-2.0** (its own `LICENSE`; SPDX headers on every file).

### PBS backup age
`pbs/pbs-backup-age.sh` + `pbs/pbs-backup-age.{service,timer}` run on the PBS host every 5 min and write `pbs_backup_last_timestamp_seconds` / `pbs_backup_snapshots` per backup group (completed snapshots only) to the node-exporter textfile dir. Settings: `PBS_DATASTORE_PATH` (default `/rpool/datastore`), `PBS_DATASTORE` (label, default `main`), `TEXTFILE_DIR`. Alert idea: `time() - max by (id) (pbs_backup_last_timestamp_seconds) > 26*3600`.
