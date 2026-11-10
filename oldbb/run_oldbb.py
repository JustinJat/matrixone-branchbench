#!/usr/bin/env python3
"""Original BranchBench macrobenchmark (5 workflows) on MatrixOne, MySQL Dolt and hosted Neon (11-10, justin/oldbb).

Derived from matrixone-branchbench-mo 4.2.5/run_branchbench.py @3e9d624 and kept as close to it as possible: the same
workflow configs (oldbb/configs = 4.2.5/configs: T x S = software_dev 5x20, failure_repro 1x10, data_cleaning 10x20,
mcts 10x100, simulation 1000x1), the same seed rows (oldbb/seed.py = 4.2.5 load_seed.py), the same runner command
(macrobench.runner --strict-operations --no-progress --max-runtime-sec), the same validity checks, cold r1 (the
dedicated container is restarted before each workflow's r1). Differences:
  backends   MATRIXONE (the seed database is loaded once and cloned per rep, as 4.2.5), DOLT_MYSQL (MySQL Dolt; the seed
             SQL is loaded and committed per rep by the runner, as 4.2.5 does for DoltgreSQL) and NEON (hosted; the
             runner creates a project per rep and loads the seed SQL into it with psql).
  scale      any warehouse count W (--scale); db_scale = W as in 4.2.5. --mini = the 4.2.5 smoke shape (1 worker, 2 for
             data_cleaning and simulation, 1 step) on the same W data.
  invalid    a sample that does not complete (runtime cap, Neon's active-branch cap, operation errors) is recorded with
             its reasons and completed steps, and the next run starts (4.2.5 stops at the first invalid sample).
  hook       --hook DIR puts DIR on the runner's PYTHONPATH with BB_COUNT=1 (oldbb/rttcount: per timed operation the
             SQL statements, connections, HTTP requests and sleeps, and branch-slot waits, in verb_counts.jsonl).
Output: <output>/configs, <run>.log, results/<run>/ (parquet, e2e_stats.json, verb_counts.jsonl), summary.jsonl,
metadata.json, driver.log. Connection settings come from the environment (MO_*, DOLT_MYSQL_*, NEON_*).
"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import seed  # noqa: E402

WORKFLOWS = ("software_dev", "failure_repro", "data_cleaning", "mcts", "simulation")
BACKENDS = ("MATRIXONE", "DOLT_MYSQL", "NEON")
TABLES = tuple(seed.SCHEMA)


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise argparse.ArgumentTypeError("expected a SQL identifier")
    return value


def mysql_conn(backend, timeout=300, database=None):
    import pymysql
    p = "MO" if backend == "MATRIXONE" else "DOLT_MYSQL"
    d = ("127.0.0.1", "6001", "root", "111") if p == "MO" else ("127.0.0.1", "3306", "root", "")
    host, port, user, password = [os.environ.get(f"{p}_{k}", v) for k, v in zip(("HOST", "PORT", "USER", "PASSWORD"), d)]
    return pymysql.connect(host=host, port=int(port), user=user, password=password, database=database,
                           autocommit=True, connect_timeout=5, read_timeout=timeout, write_timeout=timeout)


def query(backend, statement, timeout=300, database=None):
    conn = mysql_conn(backend, timeout, database)
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def ready(backend, max_wait=300):
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            if query(backend, "SELECT 1", timeout=5)[0][0] == 1:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("SQL endpoint did not become ready")


def container_running(name):
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() == "true"


def stop_child(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def run_process(command, repo, log_path, timeout, env):
    started = time.monotonic()
    timed_out = False
    with log_path.open("w") as log:
        child = subprocess.Popen(command, cwd=repo, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                 start_new_session=True, env=env)
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out, code = True, 124
        finally:
            if child.poll() is None or timed_out:
                stop_child(child)
    return code, timed_out, time.monotonic() - started


def render_configs(args, tp, text_format, tag, dumps):
    configs = []
    for workflow in args.workflows:
        config = text_format.Parse((args.configs / (workflow + ".textproto")).read_text(), tp.MacroBenchConfig())
        if tp.WorkflowType.Name(config.workflow).lower() != workflow:
            raise RuntimeError("workflow differs from its template")
        config.backend = getattr(tp, args.backend)
        config.setup.db_scale = args.scale
        if args.mini:   # the 4.2.5 smoke shape
            config.setup.workers = 2 if workflow in ("data_cleaning", "simulation") else 1
            config.setup.total_steps = 1
            config.setup.root_fanout = config.setup.workers
        if args.workers is not None:
            config.setup.workers = args.workers
            if workflow == "simulation":
                config.setup.root_fanout = args.workers
        if args.steps is not None:
            config.setup.total_steps = args.steps
        if workflow == "software_dev":
            config.step.schema_changes = max(2, config.step.schema_changes)
        for rep in range(1, args.reps + 1):
            item = tp.MacroBenchConfig()
            item.CopyFrom(config)
            item.run_id = f"{args.backend.lower()}_w{args.scale}{'_mini' if args.mini else ''}_{workflow}_{tag}_r{rep}"
            item.database_setup.db_name = f"bb_{workflow}_{tag[-12:]}_r{rep}"
            item.database_setup.cleanup = True
            item.database_setup.ClearField("source")
            if args.backend == "MATRIXONE":
                item.database_setup.existing_db.branch_id = "main"
            else:
                item.database_setup.sql_dump.sql_dump_path = str(dumps[args.backend])
            configs.append((workflow, rep, item))
    return configs


def validate_result(out, config):
    from pyarrow import parquet
    files = list(out.glob("*_e2e_stats.json"))
    stats = json.loads(files[0].read_text()) if len(files) == 1 else {}
    if not isinstance(stats, dict) or not stats:
        return False, {}, ["no_stats"]
    expected = {str(i) for i in range(config.setup.workers)}
    completed = stats.get("completed_steps", {})
    statuses = stats.get("worker_status", {})
    elapsed = stats.get("elapsed_sec")
    reasons = list(stats.get("invalid_reasons") or [])
    valid = (stats.get("run_valid") is True and stats.get("timed_out") is False
             and stats.get("run_id") == config.run_id
             and stats.get("workers") == config.setup.workers
             and stats.get("total_steps") == config.setup.total_steps
             and stats.get("total_operation_errors") == 0
             and isinstance(completed, dict) and set(completed) == expected
             and all(completed[i] == config.setup.total_steps for i in expected)
             and isinstance(statuses, dict) and set(statuses) == expected
             and all(statuses[i] == "completed" for i in expected)
             and type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0)
    records = list(out.glob("*.parquet"))
    if len(records) != 1:
        valid = False
        reasons.append("parquet_count")
    else:
        table = parquet.read_table(records[0], columns=["run_id"])
        if not (table.num_rows > 0 and set(table.column("run_id").to_pylist()) == {config.run_id}):
            valid = False
            reasons.append("parquet_run_id")
    if not valid and not reasons:
        reasons.append("result_validation")
    return valid, stats, reasons


def log_failed(path):
    errors = re.compile(r"ERROR in flush_record|Traceback \(most recent call last\)|Error deleting database|"
                        r"schema commit failed|Error deleting MatrixOne databases")
    with path.open(errors="replace") as stream:
        return any(errors.search(line) for line in stream)


def slot_timeouts(path):
    with path.open(errors="replace") as stream:
        return sum("Timed out waiting for branch slot" in line for line in stream)


def mo_seed_gate(seed_db, scale):
    if seed_db not in {str(r[0]) for r in query("MATRIXONE", "SHOW DATABASES")}:
        return False
    tables = {str(r[0]) for r in query("MATRIXONE", f"SHOW TABLES FROM `{seed_db}`")}
    if not set(TABLES).issubset(tables):
        return False
    counts = {t: int(query("MATRIXONE", f"SELECT COUNT(*) FROM `{seed_db}`.`{t}`")[0][0]) for t in TABLES}
    return counts == seed.expected_counts(scale)


def mo_load_seed(seed_db, scale, log):
    """Load the seed into a new MO database once (4.2.5 load_seed.py, one warehouse per INSERT)."""
    started = time.monotonic()
    if seed_db in {str(r[0]) for r in query("MATRIXONE", "SHOW DATABASES")}:
        raise RuntimeError(f"seed database {seed_db} exists but fails the row-count gate; drop it or pick --seed-db")
    conn = mysql_conn("MATRIXONE", timeout=3600)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE `{seed_db}`")
            cur.execute(f"USE `{seed_db}`")
            for i, statement in enumerate(seed.statements(scale, "mysql"), 1):
                cur.execute(statement)
                log.write(f"seed statement {i} done\n")
                log.flush()
    finally:
        conn.close()
    if not mo_seed_gate(seed_db, scale):
        raise RuntimeError("seed row counts do not match")
    return time.monotonic() - started


def mo_clone_seed(db, seed_db, snapshot):
    conn = mysql_conn("MATRIXONE")
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SNAPSHOT `{snapshot}` FOR DATABASE `{seed_db}`")
            try:
                cur.execute(f"CREATE DATABASE `{db}` CLONE `{seed_db}` {{SNAPSHOT='{snapshot}'}}")
            finally:
                try:
                    cur.execute(f"DROP SNAPSHOT IF EXISTS `{snapshot}`")
                except Exception:
                    pass
    finally:
        conn.close()


def mysql_cleanup_owned(backend, db, keep=()):
    for row in query(backend, "SHOW DATABASES"):
        name = str(row[0])
        if name not in keep and (name == db or name.startswith(db + "_br_")):
            identifier(name)
            query(backend, f"DROP DATABASE IF EXISTS `{name}`")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=BACKENDS, required=True)
    ap.add_argument("--scale", type=int, default=1, help="warehouses W (db_scale)")
    ap.add_argument("--mini", action="store_true", help="4.2.5 smoke shape: 1-2 workers, 1 step")
    ap.add_argument("--repo", type=Path, default=HERE.parent)
    ap.add_argument("--configs", type=Path, default=HERE / "configs")
    ap.add_argument("--container", help="MO / Dolt: dedicated container, restarted before each workflow's r1")
    ap.add_argument("--seed-db", type=identifier, help="MO: seed database (default bb_seed_w<W>), loaded if missing")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=14400, help="hard cap on one runner process (seconds)")
    ap.add_argument("--max-runtime-sec", type=int, default=7200, help="worker runtime cap (the paper's 2 h)")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--workflows", nargs="+", choices=WORKFLOWS, default=list(WORKFLOWS))
    ap.add_argument("--hook", type=Path, help="counting hook directory (oldbb/rttcount)")
    ap.add_argument("--check", action="store_true", help="write configs and seed SQL only")
    args = ap.parse_args(argv)
    if min([args.scale, args.reps, args.timeout, args.max_runtime_sec]
           + [n for n in (args.workers, args.steps) if n is not None]) < 1:
        ap.error("positive values required")
    args.repo, args.configs = args.repo.resolve(), args.configs.resolve()
    args.seed_db = args.seed_db or f"bb_seed_w{args.scale}"
    sys.path.insert(0, str(args.repo))
    from google.protobuf import text_format
    from macrobench import task_pb2 as tp
    if args.backend not in tp.Backend.keys():
        ap.error(f"the runner has no {args.backend} backend (patch_oldbb_doltmysql.py, then compile the protos)")
    tag = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "_" + uuid.uuid4().hex[:12]
    root = args.output.resolve()
    if root.exists() and any(root.iterdir()):
        ap.error("output must be empty")
    root.mkdir(parents=True, exist_ok=True)
    dumps = {}
    if args.backend != "MATRIXONE":
        dialect = "pg" if args.backend == "NEON" else "mysql"
        dumps[args.backend] = root / f"seed_w{args.scale}_{dialect}.sql"
        n = seed.write_dump(dumps[args.backend], args.scale, dialect)
        print(f"seed SQL: {dumps[args.backend]} ({n} statements, {dialect})", flush=True)
    configs = render_configs(args, tp, text_format, tag, dumps)
    (root / "configs").mkdir()
    for workflow, rep, config in configs:
        (root / "configs" / f"{workflow}_r{rep}.textproto").write_text(text_format.MessageToString(config))
    if args.check:
        print(f"PASS: {len(configs)} configs; no database actions")
        return 0

    env = dict(os.environ, PYTHONUNBUFFERED="1", MO_QUERY_TIMEOUT_SEC=str(min(args.max_runtime_sec, args.timeout)))
    env["PYTHONPATH"] = str(args.repo) + (os.pathsep + str(args.hook.resolve()) if args.hook else "")
    if args.hook:
        env["BB_COUNT"] = "1"
    sqlb = "MATRIXONE" if args.backend == "MATRIXONE" else "DOLT_MYSQL"
    failures = 0
    with (root / "driver.log").open("a") as dlog:
        def note(msg):
            line = f"{time.strftime('%m-%d %H:%M:%S', time.gmtime())} {msg}"
            dlog.write(line + "\n")
            dlog.flush()
            print(line, flush=True)
        try:
            meta = {"backend": args.backend, "scale": args.scale, "mini": args.mini, "repo": str(args.repo),
                    "fork_source": (args.repo / "FORK_SOURCE").read_text().strip()
                    if (args.repo / "FORK_SOURCE").exists() else None,
                    "max_runtime_sec": args.max_runtime_sec, "timeout": args.timeout, "hook": str(args.hook or "")}
            if args.backend != "NEON":
                ready(sqlb)
                meta["server_version"] = str(query(sqlb, "SELECT VERSION()")[0][0])
                if args.backend == "DOLT_MYSQL":
                    meta["dolt_version"] = str(query(sqlb, "SELECT dolt_version()")[0][0])
                if args.container:
                    info = json.loads(subprocess.check_output(["docker", "inspect", args.container]))[0]
                    meta.update(container=args.container, container_image=info["Config"]["Image"],
                                nano_cpus=info["HostConfig"].get("NanoCpus"), memory=info["HostConfig"].get("Memory"))
            else:
                meta["neon"] = {k: os.environ.get(k) for k in ("NEON_COMPUTE_CU", "NEON_PG_VERSION", "NEON_REGION",
                                                               "NEON_MAX_ACTIVE", "NEON_KEEP_PROJECTS")}
            if args.backend == "MATRIXONE":
                if mo_seed_gate(args.seed_db, args.scale):
                    note(f"seed {args.seed_db} present, row counts match W={args.scale}")
                else:
                    secs = mo_load_seed(args.seed_db, args.scale, dlog)
                    note(f"seed {args.seed_db} loaded in {secs:.1f}s")
                meta["seed_db"] = args.seed_db
            (root / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
            for workflow, rep, config in configs:
                rep_started, t0 = time.monotonic(), time.time()
                if rep == 1 and args.container:
                    subprocess.run(["docker", "restart", "-t", "300", args.container], stdout=dlog, stderr=dlog,
                                   check=True, timeout=600)
                    ready(sqlb)
                db = config.database_setup.db_name
                if args.backend == "MATRIXONE":
                    mo_clone_seed(db, args.seed_db, f"bb_snap_{tag[-12:]}_{workflow}_r{rep}")
                out = root / "results" / f"{workflow}_r{rep}"
                out.mkdir(parents=True)
                logfile = root / f"{workflow}_r{rep}.log"
                command = [sys.executable, "-m", "macrobench.runner", "--config",
                           str(root / "configs" / f"{workflow}_r{rep}.textproto"), "--outdir", str(out),
                           "--strict-operations", "--no-progress", "--max-runtime-sec", str(args.max_runtime_sec)]
                note(f"{workflow} r{rep} start (T={config.setup.workers} S={config.setup.total_steps})")
                rc, timed_out, wall = run_process(command, args.repo, logfile, args.timeout, env)
                try:
                    valid, stats, reasons = validate_result(out, config)
                except Exception as error:
                    valid, stats, reasons = False, {}, ["result_read_failure"]
                    note(f"{type(error).__name__}: {error}")
                valid &= rc == 0 and not timed_out
                if rc != 0:
                    reasons.append(f"runner_exit_{rc}")
                if timed_out:
                    reasons.append("hard_timeout")
                if log_failed(logfile):
                    valid = False
                    reasons.append("runner_log_error")
                healthy = True
                if args.backend != "NEON":
                    try:
                        if args.container and not container_running(args.container):
                            raise RuntimeError("container not running after the run")
                        query(sqlb, "SELECT 1", timeout=30)
                        mysql_cleanup_owned(sqlb, db, keep=(args.seed_db,))
                    except Exception as error:
                        healthy, valid = False, False
                        reasons.append("health_or_cleanup")
                        note(f"health/cleanup: {type(error).__name__}: {error}")
                done = sum((stats.get("completed_steps") or {}).values())
                total = config.setup.workers * config.setup.total_steps
                row = {"workflow": workflow, "rep": rep, "phase": "cold" if rep == 1 else "warm",
                       "backend": args.backend, "scale": args.scale, "mini": args.mini, "rc": rc,
                       "elapsed_sec": stats.get("elapsed_sec"), "wall_sec": round(wall, 2),
                       "rep_wall_sec": round(time.monotonic() - rep_started, 2), "start_unix": t0,
                       "end_unix": time.time(), "workers": config.setup.workers,
                       "steps_per_worker": config.setup.total_steps, "completed_steps": done, "total_steps": total,
                       "timed_out": stats.get("timed_out"), "operation_errors": stats.get("total_operation_errors"),
                       "worker_status": sorted(set((stats.get("worker_status") or {}).values())),
                       "slot_timeouts_logged_by_t0": slot_timeouts(logfile), "valid": bool(valid),
                       "invalid_reasons": sorted(set(reasons)) if not valid else []}
                with (root / "summary.jsonl").open("a") as summary:
                    summary.write(json.dumps(row) + "\n")
                note(f"{'PASS' if valid else 'INCOMPLETE'}: {workflow} r{rep} steps {done}/{total} "
                     f"elapsed {stats.get('elapsed_sec')}s wall {wall:.0f}s"
                     + ("" if valid else f" reasons {row['invalid_reasons']}"))
                failures += not valid
                if not healthy and args.container:
                    note("restarting the container after a failed health check")
                    subprocess.run(["docker", "restart", "-t", "300", args.container], stdout=dlog, stderr=dlog,
                                   timeout=600)
                    ready(sqlb, max_wait=900)
        except (Exception, KeyboardInterrupt) as error:
            note(f"STOP {type(error).__name__}: {error}")
            return 130 if isinstance(error, KeyboardInterrupt) else 1
    print(f"done: {len(configs) - failures}/{len(configs)} complete samples", flush=True)
    return 0 if failures == 0 else 3


if __name__ == "__main__":
    def cancelled(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, cancelled)
    raise SystemExit(main())
