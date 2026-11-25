#!/usr/bin/env bash
# Load a snapshot of the production database into the local test volume, so that
# "scripts/docker-test.sh run" shows your real history. Run it again with a newer snapshot to refresh.
#
#   scripts/load-snapshot.sh                  load instance/trial.db
#   scripts/load-snapshot.sh path/to/file.db  load another snapshot
#
# Take the snapshot on Unraid while the app is running (a consistent copy, safe mid-write):
#   docker exec myenergi-monitor python -c "import sqlite3; s=sqlite3.connect('/app/instance/readings.db'); \
#     d=sqlite3.connect('/app/instance/trial.db'); s.backup(d); d.close()"
# then copy /mnt/user/appdata/myenergi-monitor/trial.db to instance/ here (git ignores instance/).
#
# The volume's current database is kept as readings.db.bak (one level of undo); uploads stay as they are.
# The snapshot brings its settings with it (e.g. the tariff). The local app keeps recording into the copy
# while it runs, and the copy never goes back to Unraid.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC=${1:-instance/trial.db}
VOLUME=myenergi-test-data
[ -f "$SRC" ] || { echo "No snapshot at $SRC (see the comment at the top of this script)." >&2; exit 1; }

if docker ps --format '{{.Names}}' | grep -qx myenergi-local; then
  echo "The local app (myenergi-local) is running: stop it with Ctrl+C first, then load the snapshot." >&2
  exit 1
fi

check=$(python3 - "$SRC" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
first, last, n = c.execute("SELECT min(poll), max(poll), count(*) FROM flows").fetchone()
from datetime import datetime, timezone
f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else "-"
print(ok, f"{n} polls, {f(first)} to {f(last)}")
EOF
) || { echo "$SRC is not a readable database." >&2; exit 1; }
[ "${check%% *}" = ok ] || { echo "$SRC failed SQLite's integrity check: ${check}" >&2; exit 1; }
echo "Snapshot: ${check#ok }"

DIR=$(cd "$(dirname "$SRC")" && pwd)
docker volume create "$VOLUME" >/dev/null
docker run --rm -v "$VOLUME":/data -v "$DIR":/src:ro alpine sh -euc '
  [ -f /data/readings.db ] && mv -f /data/readings.db /data/readings.db.bak
  rm -f /data/readings.db-journal
  cp "/src/$1" /data/readings.db
  mkdir -p /data/uploads
  chown 99:100 /data /data/readings.db /data/uploads
' sh "$(basename "$SRC")"
echo "Loaded into volume $VOLUME (previous database kept as readings.db.bak)."
echo "Start it with: scripts/docker-test.sh run"
