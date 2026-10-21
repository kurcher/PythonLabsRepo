#!/usr/bin/env bash
# Lab 7 load test with oha.
#   bash scripts/loadtest_lab07.sh                 # native uvicorn: 1 worker async def / def
#   MODE=docker bash scripts/loadtest_lab07.sh     # the deployable image: 1 worker x2, 4 workers
#   bash scripts/loadtest_lab07.sh https://<deployed-host>   # + one row against the deployment
# Needs `oha` (or OHA=/path/to/oha), build/index-pos.bin and data/chapters/.
set -euo pipefail
OHA=${OHA:-oha}
INDEX=${INDEX:-build/index-pos.bin}
QUERY='/search?q=whale+ship&k=10'
DURATION=${DURATION:-15s}

row() {  # label concurrency url
  "$OHA" -z "$DURATION" -c "$2" --no-tui --output-format json "$3" > build/oha.json 2>/dev/null || true
  python - "$1" "$2" <<'PY'
import json, sys
d = json.load(open("build/oha.json"))
s, p = d["summary"], d["latencyPercentiles"]
codes = d.get("statusCodeDistribution", {})
ok = codes.get("200", 0); total = sum(codes.values()) or 1
print(f"| {sys.argv[1]} | `/search` | {sys.argv[2]} | {s['requestsPerSec']:.0f} "
      f"| {p['p50'] * 1000:.0f} | {p['p95'] * 1000:.0f} | {p['p99'] * 1000:.0f} | {100 * ok / total:.0f}% |")
PY
}

wait_healthy() {  # port — never measure a server that is not up
  for _ in $(seq 1 600); do curl -sf "localhost:$1/health" >/dev/null && return 0; sleep 0.5; done
  echo "server on :$1 never became healthy" >&2
  exit 1
}

native_up() {  # port mode — uvicorn directly (one worker: Windows can't share the socket)
  INDEX_PATH=$INDEX SEARCH_MODE=$2 python -m uvicorn findex.web.app:create_app --factory \
    --port "$1" --log-level warning &
  PID=$!
  wait_healthy "$1"
}

docker_up() {  # port mode workers — full index + corpus mounted into the image
  local here
  here=$(pwd -W 2>/dev/null || pwd)  # a Windows path under Git Bash, POSIX elsewhere
  # MSYS_NO_PATHCONV stops Git Bash rewriting /srv/... into C:/Program Files/Git/srv/...
  MSYS_NO_PATHCONV=1 docker run -d --rm --name "findex-lt-$1" -p "$1:8000" \
    -e INDEX_PATH=/idx/index-pos.bin -e SEARCH_MODE="$2" \
    -v "$here/build:/idx:ro" -v "$here/data/chapters:/srv/data/chapters:ro" \
    findex:0.7.0 findex serve --host 0.0.0.0 --port 8000 --workers "$3" >/dev/null
  wait_healthy "$1"
  sleep $(( $3 > 1 ? 30 : 2 ))  # /health answers from the first worker; let the rest load too
}

echo "| Target | Route | Concurrency | RPS | p50 (ms) | p95 | p99 | 200 OK |"
echo "|---|---|---|---|---|---|---|---|"
if [[ "${MODE:-native}" == docker ]]; then
  docker_up 8811 async 1; row "docker, 1 worker, \`async def\`" 50 "http://127.0.0.1:8811$QUERY"; docker stop findex-lt-8811 >/dev/null
  docker_up 8812 sync 1;  row "docker, 1 worker, \`def\`" 50 "http://127.0.0.1:8812$QUERY"; docker stop findex-lt-8812 >/dev/null
  docker_up 8813 sync 4;  row "docker, 4 workers, \`def\`" 50 "http://127.0.0.1:8813$QUERY"; docker stop findex-lt-8813 >/dev/null
else
  native_up 8801 async; row "local, 1 worker, \`async def\`" 50 "http://127.0.0.1:8801$QUERY"; kill $PID; wait $PID 2>/dev/null || true
  native_up 8802 sync;  row "local, 1 worker, \`def\`" 50 "http://127.0.0.1:8802$QUERY"; kill $PID; wait $PID 2>/dev/null || true
fi
if [[ $# -ge 1 ]]; then row "deployed ($1)" 20 "$1$QUERY"; fi
