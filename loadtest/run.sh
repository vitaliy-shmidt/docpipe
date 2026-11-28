#!/usr/bin/env bash
# Reproducible local load tests for DocPipe monitoring (Phase M7).
#
# Isolated: own Docker network, containers named dpm7-*, synthetic upstream,
# synthetic requests, data under $WORKROOT. Never point this at a real
# installation. Needs Docker and bash; run from the repository root:
#
#   loadtest/run.sh build                  # image docpipe-m7:local from the working tree
#   loadtest/run.sh overhead matrix dash probes unreachable overflow pressure
#   loadtest/run.sh cleanup
#
# Results: $RESULTS/<scenario>/... (JSON per run). Resource limits below are
# part of the method and printed into every result.
set -euo pipefail

IMG=${IMG:-docpipe-m7:local}
NET=dpm7-net
SRC=$(pwd)
WORKROOT=${WORKROOT:-/tmp/dpm7}
RESULTS=${RESULTS:-$WORKROOT/results}
APP_CPUS=${APP_CPUS:-2}
APP_MEM=${APP_MEM:-1g}
LOAD_CPUS=${LOAD_CPUS:-1.5}
UID_APP=1000

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }

build() {
  docker build -q -t "$IMG" . >/dev/null
  docker image inspect "$IMG" --format '{{.Id}} {{.Size}}'
}

net() {
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
  if ! docker ps --format '{{.Names}}' | grep -qx dpm7-fake; then
    docker rm -f dpm7-fake >/dev/null 2>&1 || true
    docker run -d --name dpm7-fake --network "$NET" -v "$SRC/loadtest:/load:ro" "$IMG" \
      python /load/fake_upstream.py >/dev/null
    sleep 1
  fi
}

fake_mode() { docker exec dpm7-fake python -c \
  "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:9000/__mode/$1', method='POST'))"; }

in_work() {  # run python helper as uid 1000 against $W
  docker run --rm -u "$UID_APP" -v "$W:/work" -v "$SRC/loadtest:/load:ro" -e WORK=/work -e PYTHONPATH=/app \
    "$IMG" python "/load/$1" "${@:2}"
}

# app NAME VARIANT WORKERS [dotted.override=value ...]
app() {
  local name=$1 variant=$2 workers=$3
  shift 3
  W=$WORKROOT/$name
  rm -rf "$W"
  mkdir -p "$W" "$RESULTS/$name"
  chown "$UID_APP:$UID_APP" "$W"
  in_work prepare.py config "$variant" "$@"
  if [ "$variant" = web ]; then in_work prepare.py users 8 >/dev/null; fi
  if [ -n "${SEED_MIB:-}" ]; then in_work prepare.py seed "$SEED_MIB" > "$RESULTS/$name/seed.json"; fi
  docker rm -f dpm7-app >/dev/null 2>&1 || true
  docker run -d --name dpm7-app --network "$NET" --cpus "$APP_CPUS" --memory "$APP_MEM" \
    -v "$W:/work" -v "$SRC/loadtest:/load:ro" -e DOCPIPE_CONFIG=/work/config.yaml "$IMG" \
    uvicorn system.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers --workers "$workers" >/dev/null
  for _ in $(seq 1 60); do
    if docker exec dpm7-app python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)" 2>/dev/null
    then break; fi
    sleep 1
  done
  printf '{"variant":"%s","workers":%s,"app_cpus":"%s","app_mem":"%s","load_cpus":"%s","overrides":"%s"}\n' \
    "$variant" "$workers" "$APP_CPUS" "$APP_MEM" "$LOAD_CPUS" "$*" > "$RESULTS/$name/setup.json"
}

proc() { docker exec dpm7-app python /load/prepare.py proc; }
dbinfo() { docker exec -e WORK=/work dpm7-app python /load/prepare.py db; }
cpu_usec() { docker exec dpm7-app sh -c "grep usage_usec /sys/fs/cgroup/cpu.stat" | awk '{print $2}'; }

idle() {  # idle CPU of the whole container over $1 seconds, % of one core
  local secs=$1 a b
  a=$(cpu_usec); sleep "$secs"; b=$(cpu_usec)
  awk -v a="$a" -v b="$b" -v s="$secs" 'BEGIN { printf "{\"idle_seconds\":%d,\"cpu_percent_of_one_core\":%.3f}\n", s, (b-a)/(s*10000) }'
}

load() {  # load NAME loadgen-args...
  local name=$1
  shift
  docker run --rm --network container:dpm7-app --cpus "$LOAD_CPUS" -u "$UID_APP" \
    -v "$W:/work" -v "$SRC/loadtest:/load:ro" -e PYTHONPATH=/app "$IMG" \
    python /load/loadgen.py --out /work/load.json "$@" >/dev/null 2>"$RESULTS/$name/loadgen.err"     || log "loadgen failed for $name (see loadgen.err)"
  cp "$W/load.json" "$RESULTS/$name/load.json" 2>/dev/null || true
}

finish() {  # wait for the last flush, then collect DB / process data
  local name=$1
  sleep "${FLUSH_WAIT:-8}"
  proc > "$RESULTS/$name/proc_after.json"
  dbinfo > "$RESULTS/$name/db.json" 2>/dev/null || echo '{}' > "$RESULTS/$name/db.json"
  docker stats --no-stream --format '{{json .}}' dpm7-app > "$RESULTS/$name/docker_stats.json"
  docker logs dpm7-app 2>&1 | grep -ciE 'error|traceback|locked' > "$RESULTS/$name/log_error_lines.txt" || true
  docker rm -f dpm7-app >/dev/null
}

# --- scenarios --------------------------------------------------------------------------

overhead() {  # sequential latency + idle CPU + RSS, interleaved repetitions
  for rep in 1 2 3; do
    for v in off collect web; do
      local n=overhead-$v-r$rep
      log "$n"
      app "$n" "$v" 1
      sleep 20
      proc > "$RESULTS/$n/proc_idle.json"
      idle 60 > "$RESULTS/$n/idle.json"
      load "$n" --concurrency 1 --duration 30 --warmup 3 --mix health=1
      mv "$RESULTS/$n/load.json" "$RESULTS/$n/load_health.json"
      load "$n" --concurrency 1 --duration 30 --warmup 3 --mix extract=1
      mv "$RESULTS/$n/load.json" "$RESULTS/$n/load_extract.json"
      finish "$n"
    done
  done
}

matrix() {  # 50 concurrent mixed requests, 1 and 4 workers, three variants, 3 interleaved repetitions
  for rep in 1 2 3; do
  for w in 1 4; do
    for v in off collect web; do
      local n=matrix-$v-w$w-r$rep dash=0
      [ "$v" = web ] && dash=3
      log "$n"
      app "$n" "$v" "$w"
      sleep 5
      proc > "$RESULTS/$n/proc_before.json"
      load "$n" --concurrency 50 --duration 60 --dashboards "$dash" --dashboard-interval 1
      finish "$n"
    done
  done
  done
}

dash() {  # many sessions with parallel dashboard queries next to API load
  local n=dash-w4
  log "$n"
  app "$n" web 4
  sleep 5
  load "$n" --concurrency 50 --duration 60 --dashboards 8 --dashboard-interval 0.25
  finish "$n"
}

probes() {  # slow / hanging / refusing status endpoints during load
  for mode in trickle hang down; do
    local n=probes-$mode
    log "$n"
    app "$n" collect 1 probe_interval_seconds=10 flush_interval_seconds=5
    fake_mode "$mode"
    load "$n" --concurrency 50 --duration 60
    fake_mode normal
    finish "$n"
  done
}

unreachable() {  # AI/PDF upstream disappears during load
  local n=unreachable-w4
  log "$n"
  app "$n" collect 4 probe_interval_seconds=10
  ( sleep 20; docker stop -t 0 dpm7-fake >/dev/null; sleep 20; docker start dpm7-fake >/dev/null ) &
  load "$n" --concurrency 50 --duration 60
  wait
  finish "$n"
}

overflow() {  # tiny buffer + long flush interval: drops must be counted, never silent
  local n=overflow
  log "$n"
  app "$n" collect 1 buffer_max_requests=100 flush_interval_seconds=30
  load "$n" --concurrency 50 --duration 40 --warmup 0 --mix health=1
  FLUSH_WAIT=35 finish "$n"
}

pressure() {  # database above the storage limit: pruning, then writes continue or pause visibly
  local n=pressure
  log "$n"
  SEED_MIB=24 app "$n" collect 1 storage.max_bytes=16777216 maintenance_interval_seconds=10
  for i in 1 2 3 4 5 6; do
    sleep 10
    dbinfo > "$RESULTS/$n/db_t$((i * 10)).json" 2>/dev/null || true
  done
  load "$n" --concurrency 20 --duration 60
  finish "$n"
}

smoke() {  # a few seconds of everything, to check the setup
  local n=smoke
  log "$n"
  app "$n" web 1
  load "$n" --concurrency 5 --duration 5 --warmup 1 --dashboards 2 --dashboard-interval 0.5
  finish "$n"
  cat "$RESULTS/$n/load.json" "$RESULTS/$n/db.json"
}

cleanup() {
  docker rm -f dpm7-app dpm7-fake >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}

for step in "$@"; do
  case "$step" in
    build) build ;;
    cleanup) cleanup ;;
    *) net; "$step" ;;
  esac
done
