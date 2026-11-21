# Agent instructions for proxmox-cluster-toolkit

## Scope

Monitoring and maintenance helpers for Proxmox VE and Proxmox Backup Server. Python tools
use the standard library.

## Repository layout

- `textfile/` contains corosync, HA and replication collectors and schedules.
- `smart/` holds SMART monitoring configuration.
- `grafana/` contains the alert rules and dashboard builders.
- `maintenance/pve-maint` handles HA maintenance.
- `pbs/` contains off-site copy and snapshot-age tooling.
- `bios-guard/` contains BIOS drift checks, systemd units and a vendored DKMS patch.
- `tests/` contains unit tests and the PromQL syntax checker.

## Run and test

- Run `python3 -m unittest discover -s tests -t .` from the root for offline tests.
- Run `python3 tests/check_promql.py` for the Docker-based PromQL syntax check.
- Use `./cluster_rules.py --print` from `grafana/` to inspect generated JSON without API
  calls.
- The rules builder's `--dry-run` reads Grafana. The default invocation applies rules.
- For runtime deployment, follow the README section for the individual collector, unit or
  helper.

## Conventions and safety

- Keep hostnames, endpoints and machine-specific BIOS targets in local config. The samples
  use example node names.
- Use `bios_guard.py --dump` to establish which attributes are writable before setting
  targets.
- The documented driver installation is required for BIOS fixes on affected hardware.
- Review verified backup and retention behavior before changing off-site copy or pruning.

## Collector and backup checks

- Keep the HA alert queries tolerant of each node reporting the same cluster view.
- Replication is reported by each job's source node. Preserve that ownership when VMs
  migrate.
- Keep encrypted backup keys outside the cluster and preserve the separation between copying
  streams and testing a restore.
