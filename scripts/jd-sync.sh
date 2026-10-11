#!/usr/bin/env bash
# Sync day folders from a remote output tree into a local folder and keep only
# the last JD_SYNC_DAYS days on the local side (default 30, today included).
# Day folders older than that window are deleted locally after the sync
# succeeds. Nothing on the remote is deleted.
#
# Usage (this repo has no "Run it" section in the README):
#   JD_SYNC_REMOTE=host:/path/to/output/ \
#   JD_SYNC_LOCAL=/path/to/jd-output \
#   scripts/jd-sync.sh [--dry-run]
#
# JD_SYNC_REMOTE  rsync source (required). A missing trailing slash is added
#                 so day folders land at the top of JD_SYNC_LOCAL.
# JD_SYNC_LOCAL   local output folder (required; must already exist).
# JD_SYNC_DAYS    window length in days, today included (optional, default 30).
#
# --dry-run prints the days that would be synced and the local day folders
# that would be deleted, and changes nothing. A real run prints how many days
# were in the sync window and how many local folders were removed.

set -euo pipefail

is_leap() {
  local y
  y=$1
  if [ $((y % 400)) -eq 0 ]; then
    return 0
  fi
  if [ $((y % 100)) -eq 0 ]; then
    return 1
  fi
  [ $((y % 4)) -eq 0 ]
}

days_in_month() {
  case $2 in
    1|3|5|7|8|10|12) printf '31\n' ;;
    4|6|9|11) printf '30\n' ;;
    2)
      if is_leap "$1"; then
        printf '29\n'
      else
        printf '28\n'
      fi
      ;;
    *)
      return 1
      ;;
  esac
}

is_valid_date() {
  local name re y m d dim
  name=$1
  re='^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
  if ! [[ "$name" =~ $re ]]; then
    return 1
  fi
  y=$((10#${name:0:4}))
  m=$((10#${name:5:2}))
  d=$((10#${name:8:2}))
  if [ "$m" -lt 1 ] || [ "$m" -gt 12 ]; then
    return 1
  fi
  dim=$(days_in_month "$y" "$m")
  if [ "$d" -lt 1 ] || [ "$d" -gt "$dim" ]; then
    return 1
  fi
}

next_day() {
  local ymd y m d dim
  ymd=$1
  y=$((10#${ymd:0:4}))
  m=$((10#${ymd:5:2}))
  d=$((10#${ymd:8:2}))
  dim=$(days_in_month "$y" "$m")
  if [ "$d" -lt "$dim" ]; then
    d=$((d + 1))
  else
    d=1
    m=$((m + 1))
    if [ "$m" -gt 12 ]; then
      m=1
      y=$((y + 1))
    fi
  fi
  printf '%04d-%02d-%02d\n' "$y" "$m" "$d"
}

shift_back() {
  local ymd n y m d dim
  ymd=$1
  n=$2
  y=$((10#${ymd:0:4}))
  m=$((10#${ymd:5:2}))
  d=$((10#${ymd:8:2}))
  while [ "$n" -gt 0 ]; do
    if [ "$d" -gt "$n" ]; then
      d=$((d - n))
      n=0
    else
      n=$((n - d))
      m=$((m - 1))
      if [ "$m" -lt 1 ]; then
        m=12
        y=$((y - 1))
      fi
      dim=$(days_in_month "$y" "$m")
      d=$dim
    fi
  done
  printf '%04d-%02d-%02d\n' "$y" "$m" "$d"
}

date_older() {
  # YYYY-MM-DD compares in calendar order under byte-wise collation.
  # The assignment is inside a subshell so it does not leak into rsync.
  [ "$1" != "$2" ] || return 1
  (
    LC_ALL=C
    [[ "$1" < "$2" ]]
  )
}

fill_sync_days() {
  local cursor guard
  sync_days=()
  cursor=$window_start
  guard=0
  while :; do
    sync_days+=("$cursor")
    guard=$((guard + 1))
    if [ "$cursor" = "$today" ]; then
      break
    fi
    if [ "$guard" -gt "$window_days" ]; then
      printf 'jd-sync: window generation did not reach today\n' >&2
      exit 1
    fi
    cursor=$(next_day "$cursor")
  done
}

collect_old_local_days() {
  local path name
  old_names=()
  shopt -s nullglob
  for path in "$local_dir"/* "$local_dir"/.[!.]* "$local_dir"/..?*; do
    if [ -L "$path" ]; then
      continue
    fi
    if [ ! -d "$path" ]; then
      continue
    fi
    name=${path##*/}
    if ! is_valid_date "$name"; then
      continue
    fi
    if date_older "$name" "$window_start"; then
      old_names+=("$name")
    fi
  done
  shopt -u nullglob
}

each_old_local_day() {
  local name sorted
  if [ "${#old_names[@]}" -eq 0 ]; then
    return 0
  fi
  sorted=$(printf '%s\n' "${old_names[@]}" | LC_ALL=C sort)
  while IFS= read -r name; do
    if [ -z "$name" ]; then
      continue
    fi
    if ! is_valid_date "$name"; then
      printf 'jd-sync: refused to delete unexpected name: %s\n' "$name" >&2
      exit 1
    fi
    if [ "$dry_run" -eq 1 ]; then
      printf 'would delete: %s\n' "$name"
    else
      rm -rf -- "${local_dir:?}/${name:?}"
      removed=$((removed + 1))
    fi
  done <<EOF
$sorted
EOF
}

dry_run=0
if [ "$#" -gt 1 ]; then
  printf 'jd-sync: unexpected arguments\n' >&2
  exit 1
fi
if [ "$#" -eq 1 ]; then
  if [ "$1" != "--dry-run" ]; then
    printf 'jd-sync: unknown argument: %s\n' "$1" >&2
    exit 1
  fi
  dry_run=1
fi

if [ -z "${JD_SYNC_REMOTE:-}" ]; then
  printf 'jd-sync: JD_SYNC_REMOTE is required (rsync source, for example host:/path/to/output/)\n' >&2
  exit 1
fi
if [ -z "${JD_SYNC_LOCAL:-}" ]; then
  printf 'jd-sync: JD_SYNC_LOCAL is required (local output folder)\n' >&2
  exit 1
fi

remote=$JD_SYNC_REMOTE
local_dir=$JD_SYNC_LOCAL
window_days=${JD_SYNC_DAYS:-30}
days_re='^[1-9][0-9]*$'
if ! [[ "$window_days" =~ $days_re ]]; then
  printf 'jd-sync: JD_SYNC_DAYS must be a positive integer (got: %s)\n' "$window_days" >&2
  exit 1
fi
if [ ! -d "$local_dir" ]; then
  printf 'jd-sync: JD_SYNC_LOCAL is not a directory: %s\n' "$local_dir" >&2
  exit 1
fi

today=$(date +%Y-%m-%d)
if ! is_valid_date "$today"; then
  printf 'jd-sync: could not read today from date +%%Y-%%m-%%d (got: %s)\n' "${today:-empty}" >&2
  exit 1
fi

window_start=$(shift_back "$today" "$((window_days - 1))")
sync_days=()
fill_sync_days

if [ "$dry_run" -eq 1 ]; then
  for day in "${sync_days[@]}"; do
    printf 'would sync: %s\n' "$day"
  done
  old_names=()
  removed=0
  collect_old_local_days
  each_old_local_day
  exit 0
fi

case $remote in
  */) ;;
  *) remote="${remote}/" ;;
esac

rsync_args=(-avz)
for day in "${sync_days[@]}"; do
  rsync_args+=("--include=${day}/***")
done
rsync_args+=("--exclude=*")

if ! rsync "${rsync_args[@]}" -- "$remote" "$local_dir"; then
  printf 'jd-sync: sync failed; local day folders were not pruned\n' >&2
  exit 1
fi

old_names=()
removed=0
collect_old_local_days
each_old_local_day
printf 'days synced: %s\n' "${#sync_days[@]}"
printf 'folders removed: %s\n' "$removed"
