---
name: deploy-cluster-monitoring
description: Deploy Proxmox textfile metrics and review their Grafana alert rules.
---

# Deploy Proxmox textfile metrics and review their Grafana alert rules

1. Use the README's Textfile metrics section to install the collectors and their node units. Fill in any machine-specific settings on the target hosts.
2. Enable the documented timers and check that the collectors write their metrics into the node-exporter textfile directory.
3. Prepare the local cluster settings from the example config. From `grafana/`, run `./cluster_rules.py --print` to inspect rule JSON offline.
4. Use `./cluster_rules.py --dry-run` to preview changes against the target Grafana. Apply rules with the README's command after review.
5. Follow the README's dashboard section and verify the metrics and rules against the configured datasource.
