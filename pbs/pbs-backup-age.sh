#!/bin/bash
# Newest completed snapshot per backup group in a PBS datastore -> node-exporter textfile. Run on the PBS host (timer: 5 min).
# A snapshot is complete once index.json.blob (the manifest) exists. Dir names are the UTC start time.
set -u
DS=${PBS_DATASTORE_PATH:-/rpool/datastore}; NAME=${PBS_DATASTORE:-main}
OUT=${TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}/pbs_backups.prom; TMP=$OUT.$$
shopt -s nullglob

prom_label() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  printf '%s' "$value"
}

scan_namespace() {
  local root=$1 namespace=$2 t g id s last n timestamp labels child
  for t in vm ct host; do
    for g in "$root/$t"/*/; do
      id=$(basename "$g"); last=""; n=0
      for s in "$g"*/; do
        [ -f "$s/index.json.blob" ] || continue
        n=$((n+1))
        last=$(basename "$s")
      done
      [ -n "$last" ] || continue
      timestamp=$(date -u -d "${last/T/ }" +%s) || continue
      labels="datastore=\"$(prom_label "$NAME")\",type=\"$t\",id=\"$(prom_label "$id")\",namespace=\"$(prom_label "$namespace")\""
      printf 'pbs_backup_last_timestamp_seconds{%s} %s\n' "$labels" "$timestamp"
      printf 'pbs_backup_snapshots{%s} %s\n' "$labels" "$n"
    done
  done
  for child in "$root/ns"/*/; do
    scan_namespace "${child%/}" "${namespace:+$namespace/}$(basename "$child")"
  done
}

{
echo "# HELP pbs_backup_last_timestamp_seconds Start time of the newest completed snapshot per group."
echo "# TYPE pbs_backup_last_timestamp_seconds gauge"
echo "# HELP pbs_backup_snapshots Completed snapshots per group."
echo "# TYPE pbs_backup_snapshots gauge"
scan_namespace "$DS" ""
} > "$TMP" && chmod 644 "$TMP" && mv "$TMP" "$OUT"
