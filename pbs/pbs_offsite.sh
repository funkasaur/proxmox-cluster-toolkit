#!/usr/bin/env bash
# pbs_offsite.sh - copy a Proxmox Backup Server datastore to cloud storage (any rclone remote) as ZFS send streams.
# Runs on a separate host that has ssh access to PBS (as root) and an rclone remote; nothing is stored locally.
#   - each run snapshots $DS@offsite-<YYYYmmdd-HHMM> on PBS and streams `zfs send -c` over ssh straight into
#     `rclone rcat`: a full stream starts a chain (on $FULL_DOW, or when there is no usable base), other days send an
#     incremental from the previous offsite snapshot
#   - layout: $DST/chain-<full stamp>/NNN-{full,incr}-<stamp>.zfs (+ .md5 sidecar); every upload's md5 (computed here
#     on the stream) must equal the remote's md5 or the run fails and the snapshot is dropped (needs a remote that
#     reports md5, e.g. Google Drive)
#   - keeps the newest KEEP_CHAINS chains on the remote and only the newest offsite snapshot on PBS (the base for the
#     next incremental)
#   - why streams, not the chunk files: a datastore has tens of thousands of small chunk files in 65,536 folders,
#     which cloud APIs upload at a few files per second
#   - if the guests' backups are encrypted client-side by PVE, the copy is useless without that key: keep a copy of
#     the key (and its paper key) somewhere other than the cluster
#   - Google Drive's 750 GB/day upload cap: status 3, snapshot dropped, the next run retries
#   - the --drive-* flags only matter on Google Drive remotes; rclone ignores them elsewhere
# Required: PBS=root@<pbs host>  DST=<rclone remote:path>. Optional: DS (default rpool/datastore), KEEP_CHAINS (2),
# FULL_DOW (7 = Sunday, `date +%u`), FORCE_FULL=1, LOG, PROM (node-exporter textfile; alert PbsOffsiteStale).
# Example cron: 10 7 * * *  PBS=root@pbs.example.com DST=gdrive:pbs-offsite /opt/pbs/pbs_offsite.sh
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${LOG:-$DIR/offsite.log}"
PROM="${PROM:-$DIR/pbs_offsite.prom}"
PBS="${PBS:?set PBS=root@<pbs host>}"
DS="${DS:-rpool/datastore}"
DST="${DST:?set DST=<rclone remote:path>}"
KEEP_CHAINS="${KEEP_CHAINS:-2}"
FULL_DOW="${FULL_DOW:-7}"   # date +%u: 7 = Sunday
FORCE_FULL="${FORCE_FULL:-0}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 "$PBS")

exec 9>"$DIR/.offsite.lock"
flock -n 9 || { echo "$(date -Is) SKIP another run is active" >>"$LOG"; exit 0; }
say() { echo "$(date -Is) $*" >>"$LOG"; }

# status: 0 ok, 1 failed, 3 stopped at Google's daily upload cap (retries next run)
write_prom() {
    local status=$1 kind=${2:-none} bytes=${3:-0} drive=${4:-0} now ok
    now=$(date +%s)
    ok=$(awk '$1 == "pbs_offsite_last_success_timestamp_seconds" {print $2}' "$PROM" 2>/dev/null)
    [[ $status == 0 ]] && ok=$now
    {
        echo "# HELP pbs_offsite_status Last run: 0 ok, 1 failed, 3 stopped at the daily upload cap."
        echo "# TYPE pbs_offsite_status gauge"
        echo "pbs_offsite_status $status"
        echo "pbs_offsite_last_run_timestamp_seconds $now"
        echo "pbs_offsite_last_success_timestamp_seconds ${ok:-0}"
        echo "pbs_offsite_last_stream_bytes{kind=\"$kind\"} $bytes"
        echo "pbs_offsite_remote_bytes $drive"
    } >"$PROM.tmp" && chmod 644 "$PROM.tmp" && mv "$PROM.tmp" "$PROM"
}

STAMP=$(date +%Y%m%d-%H%M)
NEW="$DS@offsite-$STAMP"
say "start $PBS:$DS -> $DST"
snaps=$("${SSH[@]}" "zfs list -H -t snapshot -o name -s creation $DS" 2>>"$LOG") || {
    say "FAILED cannot reach PBS"; write_prom 1; exit 1; }
BASE=$(grep "@offsite-" <<<"$snaps" | tail -n 1)
CHAIN=$(rclone lsf --dirs-only "$DST" 2>/dev/null | grep '^chain-' | sort | tail -n 1 | tr -d /)

# Incremental only if: not a full day, the base snapshot exists on PBS, and the newest remote chain ends with that base.
KIND=full
if [[ $FORCE_FULL != 1 && $(date +%u) != "$FULL_DOW" && -n $BASE && -n $CHAIN ]]; then
    last=$(rclone lsf --files-only --include '*.zfs' "$DST/$CHAIN" 2>>"$LOG" | sort | tail -n 1)
    [[ $last == *"-${BASE##*@offsite-}.zfs" ]] && KIND=incr
fi
if [[ $KIND == full ]]; then CHAIN="chain-$STAMP"; N=0; else
    N=$(rclone lsf --files-only --include '*.zfs' "$DST/$CHAIN" 2>>"$LOG" | wc -l); fi
NAME=$(printf '%03d-%s-%s.zfs' "$N" "$KIND" "$STAMP")

"${SSH[@]}" "zfs snapshot $NEW" >>"$LOG" 2>&1 || { say "FAILED zfs snapshot $NEW"; write_prom 1; exit 1; }
if [[ $KIND == full ]]; then SEND="zfs send -c $NEW"; else SEND="zfs send -c -i @${BASE#*@} $NEW"; fi
say "$KIND -> $CHAIN/$NAME ($SEND)"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
"${SSH[@]}" "$SEND" | tee >(md5sum | cut -d' ' -f1 >"$TMP/md5") >(wc -c >"$TMP/bytes") |
    rclone rcat --drive-chunk-size 256M --drive-stop-on-upload-limit --log-file "$LOG" --log-level NOTICE \
        "$DST/$CHAIN/$NAME"
rcs=("${PIPESTATUS[@]}")
sleep 2   # let the tee'd md5sum/wc finish writing
local_md5=$(cat "$TMP/md5" 2>/dev/null); bytes=$(cat "$TMP/bytes" 2>/dev/null || echo 0)
drive_md5=$(rclone md5sum "$DST/$CHAIN/$NAME" 2>>"$LOG" | cut -d' ' -f1)

if [[ ${rcs[0]} != 0 || ${rcs[2]} != 0 || -z $local_md5 || $local_md5 != "$drive_md5" ]]; then
    "${SSH[@]}" "zfs destroy $NEW" >>"$LOG" 2>&1
    rclone deletefile "$DST/$CHAIN/$NAME" >>"$LOG" 2>&1
    if tail -n 30 "$LOG" | grep -qiE 'upload limit|uploadLimitExceeded|userRateLimitExceeded'; then
        say "PARTIAL stopped at Google's daily upload cap; snapshot dropped, retry next run"; write_prom 3 "$KIND"; exit 0
    fi
    say "FAILED send rc=${rcs[0]} rclone rc=${rcs[2]} md5 local=$local_md5 drive=$drive_md5; snapshot dropped"
    write_prom 1 "$KIND"; exit 1
fi
echo "$local_md5  $NAME" | rclone rcat "$DST/$CHAIN/${NAME%.zfs}.md5" 2>>"$LOG"

# Only the new snapshot stays on PBS (base for tomorrow); older offsite snapshots go.
for s in $(grep "@offsite-" <<<"$snaps"); do "${SSH[@]}" "zfs destroy $s" >>"$LOG" 2>&1; done
# Keep the newest KEEP_CHAINS chains on the remote.
mapfile -t chains < <(rclone lsf --dirs-only "$DST" 2>>"$LOG" | grep '^chain-' | sort | tr -d /)
for (( i = 0; i < ${#chains[@]} - KEEP_CHAINS; i++ )); do
    say "dropping old ${chains[i]}"
    rclone delete --drive-use-trash=false "$DST/${chains[i]}" >>"$LOG" 2>&1 && rclone rmdir "$DST/${chains[i]}" >>"$LOG" 2>&1
done
drive=$(rclone size --json "$DST" 2>>"$LOG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["bytes"])' 2>/dev/null || echo 0)
say "OK $KIND $NAME $(( bytes / 1000000 )) MB, md5 $local_md5 matches the remote; remote total $(( drive / 1000000 )) MB"
write_prom 0 "$KIND" "$bytes" "$drive"
