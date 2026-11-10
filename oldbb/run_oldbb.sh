#!/bin/bash
# The ORIGINAL BranchBench macrobenchmark (the paper's 5 workflows: software_dev, failure_repro, data_cleaning, mcts,
# simulation; 4.2.5 configs T x S = 5x20, 1x10, 10x20, 10x100, 1000x1) on MatrixOne, MySQL Dolt and hosted Neon, 11-10.
# Tree: this file's tree (JustinJat/matrixone-branchbench, branch justin/oldbb = dengn/matrixone-branchbench @95c18b7
# + the 4.2.5 branchbench.patch + Neon settings from env + the dolt_mysql backend + oldbb/), set up by
# oldbb/setup_env.sh. Driver oldbb/run_oldbb.py (4.2.5 run_branchbench.py's lifecycle; a partial run is recorded and
# the next one starts). Same seed rows everywhere (oldbb/seed.py = 4.2.5 load_seed.py, W warehouses, generated inside
# each database). Counting hook oldbb/rttcount on every runner (verb_counts.jsonl: per timed operation the SQL
# statements, connections, Neon API requests, sleeps, and every wait for a branch slot).
#   mo    Zuyu's MO (MOBIN, default src/ZUYU_MO_SRC_DIR) on the nightly-1362ffba image with the settings of
#         mo-main-latest (mon/final_run_cmd.py), new container mo-oldbb on an empty data dir, --cpus 16 --memory 64g
#   dolt  dolthub/dolt-sql-server:$DOLT_VER, new container dolt-oldbb on an empty data dir, --cpus 16 --memory 64g,
#         127.0.0.1:13306, --max-connections=2000 (simulation opens 1000 worker connections; default 1000)
#   neon  hosted (Launch plan): NEON_COMPUTE_CU=16 fixed (root and every branch), PG 17 and aws-us-east-1 (the
#         backend's own), active-branch cap NEON_MAX_ACTIVE=20 (the runner's own), one project per run; with
#         NEON_KEEP_PROJECTS=1 the project is kept (child branches deleted) so that oldbb/neon_usage.py can read its
#         billed CU-hours an hour later and then delete it. Export NEON_API_KEY_ORG first (never printed).
# Dolt variants (11-10, second version): "fair" (DOLT_WORKFLOWS, default all) commits the parent's working set before
# branching (DOLT_COMMIT_BEFORE_BRANCH=1), so a branch starts from its parent's current state as on MO and Neon; "orig"
# (DOLT_ORIG_WORKFLOWS, default none) is the paper's behaviour (autocommit: true, never Dolt-committed, so every branch
# starts from the seed commit). The Dolt seed is loaded and committed once (bb_seed_w<W>) and copied per run.
# Per system: a mini pass first (the 4.2.5 smoke shape: 1-2 workers, 1 step, that system's workflows, same W data; MO_MINI
# / DOLT_MINI / NEON_MINI, default MINI=1; a workflow that does not finish its steps skips that system's full runs),
# then the full runs, REPS each, 2 h worker cap (MAXRT, the paper's), TIMEOUT per runner process. Results:
# newbb/results/oldbb_<time>/{mo,dolt_fair,dolt,neon}_{mini,w<W>}/ (summary.jsonl, logs, parquet, verb_counts.jsonl),
# packed into /ssd_root/wang7693/branchbench/oldbb_<time>.tar.gz. Stop: touch mon/STOP_OLDBB.
# Usage (from the work dir): nohup bash $(cat ../newbb/OLDBB_DIR)/oldbb/run_oldbb.sh > mon/oldbb.log 2>&1 &
#   env: SYSTEMS="mo dolt neon" W=1 WORKFLOWS (default all 5) MO_WORKFLOWS DOLT_WORKFLOWS DOLT_ORIG_WORKFLOWS NEON_WORKFLOWS
#        REPS=1 MAXRT=7200 TIMEOUT=14400 MINI=1 MO_MINI DOLT_MINI NEON_MINI AVAIL_MIN (G free on /ssd_root to start;
#        default 100, 300 for W >= 50) MOBIN DOLT_VER=2.5.1 CPUS=16 MEMORY=64g NEON_COMPUTE_CU=16 NEON_REGION
#        NEON_MAX_ACTIVE NEON_KEEP_PROJECTS=1
cd /ssd_root/wang7693/branchbench/matrixone-branchbench-mo-bench-both || exit 1
B=/ssd_root/wang7693/branchbench
T=$B/newbb
D=$(cd "$(dirname "$0")/.." && pwd)
PY=$D/.venv/bin/python
SYSTEMS=${SYSTEMS:-"mo dolt neon"}
W=${W:-1}
ALLWF="software_dev failure_repro data_cleaning mcts simulation"
WORKFLOWS=${WORKFLOWS:-$ALLWF}
MO_WORKFLOWS=${MO_WORKFLOWS-$WORKFLOWS}
DOLT_WORKFLOWS=${DOLT_WORKFLOWS-$WORKFLOWS}
DOLT_ORIG_WORKFLOWS=${DOLT_ORIG_WORKFLOWS-}
NEON_WORKFLOWS=${NEON_WORKFLOWS-$WORKFLOWS}
REPS=${REPS:-1}
MAXRT=${MAXRT:-7200}
TIMEOUT=${TIMEOUT:-14400}
MINI=${MINI:-1}
MO_MINI=${MO_MINI:-$MINI}; DOLT_MINI=${DOLT_MINI:-$MINI}; NEON_MINI=${NEON_MINI:-$MINI}
AVAIL_MIN=${AVAIL_MIN:-$([ $W -ge 50 ] && echo 300 || echo 100)}
CPUS=${CPUS:-16}; MEMORY=${MEMORY:-64g}
MOBIN=${MOBIN:-$(cat $B/src/ZUYU_MO_SRC_DIR 2>/dev/null)}
TEMPLATE=${TEMPLATE:-mo-main-latest}
IMG=${IMG:-$(docker inspect -f '{{.Config.Image}}' $TEMPLATE 2>/dev/null)}
DOLT_VER=${DOLT_VER:-2.5.1}; DIMG=dolthub/dolt-sql-server:$DOLT_VER
MC=mo-oldbb; MDATA=$B/mo-data-oldbb
DC=dolt-oldbb; DDATA=$B/dolt-data-oldbb; DPORT=13306
GIMG=matrixorigin/golang:1.26.4-ubuntu22.04
export NEON_COMPUTE_CU=${NEON_COMPUTE_CU:-16} NEON_PG_VERSION=${NEON_PG_VERSION:-17} NEON_REGION=${NEON_REGION:-aws-us-east-1}
export NEON_MAX_ACTIVE=${NEON_MAX_ACTIVE:-20} NEON_KEEP_PROJECTS=${NEON_KEEP_PROJECTS:-1}
export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-60}   # psql seed load into Neon (the runner's default 5 s < a 16 CU cold start)
export MO_HOST=127.0.0.1 MO_USER=root MO_PASSWORD=111
export DOLT_MYSQL_HOST=127.0.0.1 DOLT_MYSQL_PORT=$DPORT DOLT_MYSQL_USER=root DOLT_MYSQL_PASSWORD=111
OUT=$T/results/oldbb_$(date -u +%Y%m%dT%H%M%SZ)
log() { echo "== [oldbb] $(date -u '+%m-%d %T') $*"; }
running() { docker ps -q -f name=^$1$ -f status=running | grep -q .; }
dock() { docker run --rm --log-driver none -v $B:/b $GIMG bash -c "$1"; }
avail() { df --output=avail -BG /ssd_root | tail -1 | tr -dc 0-9; }
rtt() {   # median / min / max of 20 TCP handshakes, ms
  python3 - "$1" "$2" <<'EOF'
import socket, statistics, sys, time
host, port = sys.argv[1], int(sys.argv[2])
try:
    ip = socket.gethostbyname(host)
    xs = []
    for _ in range(20):
        t = time.perf_counter()
        s = socket.create_connection((ip, port), 5)
        xs.append((time.perf_counter() - t) * 1000)
        s.close()
        time.sleep(0.2)
    print(f"{host} {ip} median_ms {statistics.median(xs):.2f} min_ms {min(xs):.2f} max_ms {max(xs):.2f}")
except Exception as e:
    print(f"{host} failed: {type(e).__name__}: {e}")
EOF
}
stop_all() { for c in mo-branchbench mo-425c mo-main-b6789851 mo-main-latest mo-proto mo-425-default mo-fin-main mo-fin-proto \
                      mo-main-sf1000 mo-micro-main mo-micro-proto mo-vsd-main dolt-micro dolt-macro $MC $DC; do
  if running $c; then log "stopping $c"; docker stop -t 300 $c >/dev/null || return 1; fi; done; }
fresh_mo() {
  stop_all || return 1
  docker inspect $MC >/dev/null 2>&1 && { docker rm $MC >/dev/null || return 1; }
  dock "rm -rf /b/mo-data-oldbb && mkdir -p /b/mo-data-oldbb" || return 1
  python3 mon/final_run_cmd.py $TEMPLATE $MC "$IMG" $MDATA --bin "$MOBIN" --cpus $CPUS --memory $MEMORY > $OUT/docker_run_mo.txt || return 1
  bash $OUT/docker_run_mo.txt > /dev/null || return 1
  MO_PORT=$(docker port $MC 6001/tcp 2>/dev/null | head -1 | sed 's/.*://'); export MO_PORT=${MO_PORT:-6001}
  local i; for i in $(seq 900); do mysql -h127.0.0.1 -P$MO_PORT -uroot -p111 -N -e "select 1" >/dev/null 2>&1 && break; sleep 1; done
  sleep 60
  mysql -h127.0.0.1 -P$MO_PORT -uroot -p111 -N -e "select 1" >/dev/null 2>&1 || { log "$MC does not answer"; docker logs --tail 30 $MC 2>&1 | cut -c1-300; return 1; }
  [ "$(docker exec $MC md5sum /mo-service 2>/dev/null | cut -c1-32)" = "$(md5sum $MOBIN/mo-service | cut -c1-32)" ] || { log "$MC does not run $MOBIN/mo-service"; return 1; }
  log "mo up: port $MO_PORT, version $(mysql -h127.0.0.1 -P$MO_PORT -uroot -p111 -N -e 'select version()' 2>/dev/null), image $IMG, mo-service md5 $(md5sum $MOBIN/mo-service | cut -c1-12), cpus $(docker inspect -f '{{.HostConfig.NanoCpus}}' $MC), memory $(docker inspect -f '{{.HostConfig.Memory}}' $MC)"
}
fresh_dolt() {
  stop_all || return 1
  docker inspect $DC >/dev/null 2>&1 && { docker rm -f $DC >/dev/null || return 1; }
  dock "rm -rf /b/dolt-data-oldbb && mkdir -p /b/dolt-data-oldbb" || return 1
  docker run -d --name $DC --cpus $CPUS --memory $MEMORY --log-driver json-file --log-opt max-size=256m --log-opt max-file=4 \
      -p 127.0.0.1:$DPORT:3306 -e DOLT_ROOT_PASSWORD=111 -e DOLT_ROOT_HOST=% \
      -v $DDATA:/var/lib/dolt $DIMG --max-connections=2000 > /dev/null || return 1
  local i; for i in $(seq 300); do mysql -h127.0.0.1 -P$DPORT -uroot -p111 -N -e "select 1" >/dev/null 2>&1 && break; sleep 1; done
  mysql -h127.0.0.1 -P$DPORT -uroot -p111 -N -e "select 1" >/dev/null 2>&1 || { log "$DC does not answer"; docker logs --tail 30 $DC 2>&1 | cut -c1-300; return 1; }
  log "dolt up: $DIMG, version $(mysql -h127.0.0.1 -P$DPORT -uroot -p111 -N -e 'select dolt_version()' 2>/dev/null), max_connections $(mysql -h127.0.0.1 -P$DPORT -uroot -p111 -N -e 'select @@max_connections' 2>/dev/null), cpus $(docker inspect -f '{{.HostConfig.NanoCpus}}' $DC), memory $(docker inspect -f '{{.HostConfig.Memory}}' $DC)"
}
drive() {   # $1 mo|dolt_fair|dolt|neon  $2 mini|full  $3 workflows ; returns the driver's exit code
  local s=$1 mode=$2 wf=$3 be ctr=() extra=() od fair=0
  case $s in
    mo)        be=MATRIXONE; ctr=(--container $MC) ;;
    dolt_fair) be=DOLT_MYSQL; ctr=(--container $DC --dolt-data-dir $DDATA); fair=1 ;;
    dolt)      be=DOLT_MYSQL; ctr=(--container $DC --dolt-data-dir $DDATA) ;;
    neon)      be=NEON ;;
  esac
  if [ $mode = mini ]; then od=$OUT/${s}_mini; extra=(--mini --reps 1); else od=$OUT/${s}_w$W; extra=(--reps $REPS); fi
  log "$s $mode: workflows $wf"
  ( cd $D && DOLT_COMMIT_BEFORE_BRANCH=$fair $PY oldbb/run_oldbb.py --backend $be --scale $W "${ctr[@]}" "${extra[@]}" \
      --workflows $wf --max-runtime-sec $MAXRT --timeout $TIMEOUT --hook $D/oldbb/rttcount --output $od ) > $OUT/logs/${s}_$mode.log 2>&1
  local rc=$?
  log "$s $mode: driver rc=$rc (0 all samples complete, 3 some incomplete, 1 driver stopped)"
  python3 - $od/summary.jsonl <<'EOF' 2>/dev/null | sed 's/^/   /'
import json, sys
for line in open(sys.argv[1]):
    r = json.loads(line)
    print(f"{r['workflow']:<14} r{r['rep']} {'PASS' if r['valid'] else 'INCOMPLETE':<10} steps {r['completed_steps']}/{r['total_steps']}"
          f"  elapsed {r['elapsed_sec']}s  wall {r['wall_sec']}s  op_errors {r['operation_errors']}  {r['invalid_reasons']}")
EOF
  [ $mode = mini ] || return $rc
  # Mini gate: the driver ran to the end and every workflow finished all its steps. Operation errors alone do not
  # fail it: the runner's prune vs. cross-branch-compare race fails a compare now and then on every system.
  [ $rc = 0 ] || [ $rc = 3 ] || return 1
  python3 - $od/summary.jsonl "$wf" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
ok = {r["workflow"] for r in rows if r["completed_steps"] == r["total_steps"] and not r["timed_out"]}
sys.exit(0 if set(sys.argv[2].split()) <= ok else 1)
EOF
}

# ---- checks ----
mkdir -p $T/results
exec 9> $T/results/.run_oldbb.lock
flock -n 9 || { log "another run_oldbb.sh is running, stop"; exit 1; }
pgrep -f "run_branchbenc[h]\.py|macrobench\.runne[r]|microbench\.runner[2]|oldbb/run_oldb[b]\.py" >/dev/null && { log "a benchmark is running, stop"; exit 1; }
[ -e mon/STOP_OLDBB ] && { log "mon/STOP_OLDBB exists: remove it and start again"; exit 1; }
[ -x $PY ] && [ -f $D/oldbb/run_oldbb.py ] || { log "$D has no .venv (run oldbb/setup_env.sh first), stop"; exit 1; }
[ "$(avail)" -ge $AVAIL_MIN ] || { log "/ssd_root has $(avail)G free, less than AVAIL_MIN=${AVAIL_MIN}G, stop"; exit 1; }
for s in $SYSTEMS; do case $s in
  mo)   [ -f mon/final_run_cmd.py ] && grep -q -- '--cpus' mon/final_run_cmd.py || { log "mon/final_run_cmd.py (with --cpus) missing, stop"; exit 1; }
        [ -n "$MOBIN" ] && [ -x "$MOBIN/mo-service" ] || { log "MO binary '$MOBIN/mo-service' missing (MOBIN / src/ZUYU_MO_SRC_DIR), stop"; exit 1; }
        docker inspect $TEMPLATE >/dev/null 2>&1 && docker image inspect "$IMG" >/dev/null 2>&1 || { log "template $TEMPLATE or image '$IMG' missing, stop"; exit 1; } ;;
  dolt) docker image inspect $DIMG >/dev/null 2>&1 || docker pull -q $DIMG >/dev/null || { log "cannot pull $DIMG, stop"; exit 1; }
        ss -ltn 2>/dev/null | grep -q ":$DPORT " && ! running $DC && ! running dolt-macro && ! running dolt-micro && { log "port $DPORT in use, stop"; exit 1; } ;;
  neon) [ -n "$NEON_API_KEY_ORG" ] || { log "export NEON_API_KEY_ORG in this shell first, stop"; exit 1; }
        code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $NEON_API_KEY_ORG" "https://console.neon.tech/api/v2/projects?limit=1")
        log "Neon API from this server: HTTP $code"; [ "$code" = 200 ] || { log "Neon API did not answer 200 (key?), stop"; exit 1; }
        if ! command -v psql >/dev/null; then   # the runner loads the seed SQL into Neon with psql
          mkdir -p $T/tools/psqlshim
          printf '%s\n' '#!/bin/bash' 'f=; p=; for a in "$@"; do [ "$p" = -f ] && f=$a; p=$a; done' \
            'exec docker run --rm -i --network host --log-driver none -e PGPASSWORD -e PGCONNECT_TIMEOUT ${f:+-v "$(dirname "$f"):$(dirname "$f"):ro"} postgres:17 psql "$@"' \
            > $T/tools/psqlshim/psql && chmod +x $T/tools/psqlshim/psql && export PATH=$T/tools/psqlshim:$PATH
          docker image inspect postgres:17 >/dev/null 2>&1 || docker pull -q postgres:17 >/dev/null || { log "no psql and cannot pull postgres:17, stop"; exit 1; }
          log "psql: not installed, using $T/tools/psqlshim/psql (postgres:17 in docker)"
        fi ;;
  *) log "unknown system $s, stop"; exit 1 ;;
esac; done
for s in mo dolt; do case " $SYSTEMS " in *" $s "*)
  e=$(docker run --rm --cpus $CPUS --memory $MEMORY $GIMG true 2>&1) || { log "docker refuses --cpus $CPUS --memory $MEMORY: $(echo "$e" | tail -n 1 | cut -c1-200), stop"; exit 1; }; break ;; esac; done
mkdir -p $OUT/logs $OUT/neon
export NEON_USAGE_LOG=$OUT/neon/neon_projects.jsonl
{ echo "started        $(date -u '+%F %T') UTC"
  echo "tree           $D ($(cat $D/FORK_SOURCE 2>/dev/null)); script md5 $(md5sum $0 | cut -c1-32)"
  echo "systems        $SYSTEMS ; W=$W ; reps $REPS ; mini pass mo $MO_MINI, dolt $DOLT_MINI, neon $NEON_MINI ; /ssd_root free $(avail)G"
  echo "workflows      mo: $MO_WORKFLOWS | dolt fair: $DOLT_WORKFLOWS | dolt orig (paper): ${DOLT_ORIG_WORKFLOWS:-none} | neon: $NEON_WORKFLOWS"
  echo "limits         worker cap ${MAXRT}s (paper 2 h), runner process cap ${TIMEOUT}s"
  echo "mo             $MOBIN/mo-service ($( [ -n "$MOBIN" ] && md5sum $MOBIN/mo-service 2>/dev/null | cut -c1-12)), image $IMG, template $TEMPLATE, cpus $CPUS, memory $MEMORY"
  echo "dolt           $DIMG, cpus $CPUS, memory $MEMORY, max_connections 2000, port $DPORT"
  echo "neon           ${NEON_COMPUTE_CU} CU fixed, PG $NEON_PG_VERSION, $NEON_REGION, active-branch cap $NEON_MAX_ACTIVE, keep projects $NEON_KEEP_PROJECTS"
  echo "hook           $(md5sum $D/oldbb/rttcount/sitecustomize.py | cut -c1-32)"
} > $OUT/manifest.txt
log "results under $OUT"; sed 's/^/   /' $OUT/manifest.txt
( exec 9>&-; while sleep 60; do [ "$(avail)" -lt 30 ] && { log "/ssd_root < 30G free, touching mon/STOP_OLDBB"; touch mon/STOP_OLDBB; break; }; done ) &
WATCH=$!
trap 'kill $WATCH 2>/dev/null' EXIT

for s in $SYSTEMS; do
  [ -e mon/STOP_OLDBB ] && { log "STOP file"; break; }
  case $s in
    mo)   fresh_mo   || { log "mo did not start, skipping mo"; continue; } ;;
    dolt) fresh_dolt || { log "dolt did not start, skipping dolt"; continue; } ;;
    neon) echo "before $(rtt console.neon.tech 443)" >> $OUT/neon/rtt.txt
          echo "before $(rtt rtt-probe.${NEON_REGION#aws-}.aws.neon.tech 5432)" >> $OUT/neon/rtt.txt
          sed 's/^/   rtt /' $OUT/neon/rtt.txt ;;
  esac
  case $s in mo) mini=$MO_MINI ;; dolt) mini=$DOLT_MINI ;; neon) mini=$NEON_MINI ;; esac
  first=1
  for spec in "mo|$MO_WORKFLOWS" "dolt_fair|$DOLT_WORKFLOWS" "dolt|$DOLT_ORIG_WORKFLOWS" "neon|$NEON_WORKFLOWS"; do
    v=${spec%%|*}; wf=${spec#*|}
    case $v in mo) [ $s = mo ] ;; dolt_fair|dolt) [ $s = dolt ] ;; neon) [ $s = neon ] ;; esac || continue
    [ -n "$wf" ] || continue
    [ -e mon/STOP_OLDBB ] && { log "STOP file"; break 2; }
    if [ "$mini" = 1 ] && [ $first = 1 ]; then   # one mini pass per system, on its first variant
      drive $v mini "$wf" || { log "$v: the mini pass did not complete every workflow (log $OUT/logs/${v}_mini.log), skipping $s"; continue 2; }
    fi
    first=0
    [ -e mon/STOP_OLDBB ] && { log "STOP file"; break 2; }
    drive $v full "$wf"
  done
  case $s in
    mo)   docker logs --tail 50000 $MC 2>&1 | gzip > $OUT/logs/server_mo.log.gz ;;
    dolt) docker logs --tail 50000 $DC 2>&1 | gzip > $OUT/logs/server_dolt.log.gz ;;
    neon) echo "after $(rtt console.neon.tech 443)" >> $OUT/neon/rtt.txt ;;
  esac
done
stop_all; docker start mo-branchbench >/dev/null 2>&1 && log "mo-branchbench started again"
case " $SYSTEMS " in *" neon "*)
  log "Neon projects (kept=$NEON_KEEP_PROJECTS): $(grep -c '"created"' $NEON_USAGE_LOG 2>/dev/null); read their billed CU-hours in an hour or more:"
  log "   $PY $D/oldbb/neon_usage.py $NEON_USAGE_LOG --out $OUT/neon/neon_usage.json --delete" ;;
esac
PACK=$B/$(basename $OUT).tar.gz
tar czf $PACK -C $T/results $(basename $OUT)
log "all done; results in $OUT, packed $PACK ($(du -h $PACK | cut -f1))"
