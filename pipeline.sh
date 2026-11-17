#!/usr/bin/env bash
set -euo pipefail

GROUP="$1"                     # e.g. 02
BASE="https://opendata.geoportal.gov.pl/InneDane/Drzewa3D/LOD1/2023"
mkdir -p work "out/$GROUP"

proc_pid=""

process_async() {              # process, then delete the zip
  local zip="$1" id="$2"
  ( python3 process.py "$zip" "out/$GROUP/$id.json.gz" && rm -f "$zip" ) &
  proc_pid=$!
}

while read -r rel; do
  id=$(basename "$rel" .zip)
  [ -f "out/$GROUP/$id.json.gz" ] && { echo "skip $id"; continue; }

  echo "downloading $rel"
  curl -fL --retry 8 --retry-all-errors --retry-delay 10 -C - \
       -o "work/$id.zip" "$BASE/$rel"

  # wait for the previous file's processing before starting the next one
  # (it ran while we were downloading)
  [ -n "$proc_pid" ] && wait "$proc_pid"

  process_async "work/$id.zip" "$id"
done < <(grep "^$GROUP/" files.txt)

[ -n "$proc_pid" ] && wait "$proc_pid"
echo "group $GROUP done"