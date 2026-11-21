#!/bin/bash
# Newest completed snapshot per backup group in a PBS datastore -> node-exporter textfile. Run on the PBS host (timer: 5 min).
# A snapshot is complete once index.json.blob (the manifest) exists. Dir names are the UTC start time.
set -u
DS=${PBS_DATASTORE_PATH:-/rpool/datastore}; NAME=${PBS_DATASTORE:-main}
OUT=${TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}/pbs_backups.prom; TMP=$OUT.$$
{
echo "# HELP pbs_backup_last_timestamp_seconds Start time of the newest completed snapshot per group."
echo "# TYPE pbs_backup_last_timestamp_seconds gauge"
echo "# HELP pbs_backup_snapshots Completed snapshots per group."
echo "# TYPE pbs_backup_snapshots gauge"
for g in "$DS"/{vm,ct,host}/*/; do
  [ -d "$g" ] || continue
  t=$(basename "$(dirname "$g")"); id=$(basename "$g"); last=""; n=0
  for s in "$g"*/; do [ -f "$s/index.json.blob" ] || continue; n=$((n+1)); last=$(basename "$s"); done
  [ -n "$last" ] || continue
  echo "pbs_backup_last_timestamp_seconds{datastore=\"$NAME\",type=\"$t\",id=\"$id\"} $(date -u -d "${last/T/ }" +%s)"
  echo "pbs_backup_snapshots{datastore=\"$NAME\",type=\"$t\",id=\"$id\"} $n"
done
} > "$TMP" && chmod 644 "$TMP" && mv "$TMP" "$OUT"
