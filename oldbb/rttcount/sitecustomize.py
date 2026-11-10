# Round-trip counter for the ORIGINAL BranchBench macrobenchmark (11-10, justin/oldbb; the same counters as
# mon/rttcount for the new BranchBench, attached to the old API). Python imports this file at start-up when its
# directory is on PYTHONPATH; it does nothing unless BB_COUNT=1. For every TIMED operation (every
# ResultCollector.maybe_measure_ops(timed=True): BRANCH_CREATE, BRANCH_CONNECT, BRANCH_DELETE, READ / INSERT / UPDATE /
# DDL statements, API_RETRY_WAIT) it counts, inside that operation only:
#   sql    statements sent to the server: psycopg2 cursor.execute (Neon; with their time) and pymysql
#          Connection._execute_command (MatrixOne, MySQL Dolt; every client->server command, count only)
#   conn   new connections (psycopg2.connect, pymysql Connection.connect) and their time; on Neon a connection to a
#          branch whose compute is starting waits inside connect, so a cold start shows up here
#   api    HTTP requests (requests.Session.send: the Neon control-plane API, also through neon_api) and their time
#   sleep  time.sleep calls (Neon's 404/429 retries of the connection-URI request; the runner's rate-limit back-off,
#          which is its own API_RETRY_WAIT operation) and their time
# and appends one JSON line per operation to <runner --outdir>/verb_counts.jsonl, with the operation's latency as the
# benchmark recorded it, the worker (thread_id) and step. It also records every wait for a branch slot (the runner's
# active-branch cap, 20 on Neon): {"kind": "slot_wait", "wait_s", "ok"} when the wait lasted over 1 ms or timed out.
# Nothing in the benchmark changes; the wrappers add microseconds per statement.
import os
import sys

if os.environ.get("BB_COUNT") == "1":
    import contextlib
    import importlib.abc
    import importlib.util
    import json
    import threading
    import time

    _tl = threading.local()
    _lock = threading.Lock()
    _perf = time.perf_counter
    _real_sleep = time.sleep
    _rc = {}

    def _stack():
        s = getattr(_tl, "stack", None)
        if s is None:
            s = _tl.stack = []
        return s

    def _add(key, n=1, dt=0.0):
        for sc in _stack():
            sc[key] = sc.get(key, 0) + n
            sc[key + "_s"] = sc.get(key + "_s", 0.0) + dt

    def _emit(rec):
        argv = sys.argv
        if "--outdir" not in argv:
            return
        path = os.path.join(argv[argv.index("--outdir") + 1], "verb_counts.jsonl")
        with _lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")

    def _thread_id():
        m = _rc.get("m")
        return m.get_current_thread_id() if m else -1

    def _sleep(seconds):
        t = _perf()
        try:
            return _real_sleep(seconds)
        finally:
            _add("sleep", 1, _perf() - t)

    time.sleep = _sleep

    try:
        import psycopg2
        import psycopg2.extensions as _pe

        class _CountingCursor(_pe.cursor):
            def execute(self, query, vars=None):
                t = _perf()
                try:
                    return super().execute(query, vars)
                finally:
                    _add("sql", 1, _perf() - t)

            def executemany(self, query, vars_list):
                vars_list = list(vars_list)
                t = _perf()
                try:
                    return super().executemany(query, vars_list)
                finally:
                    _add("sql", len(vars_list), _perf() - t)

        _pg_connect = psycopg2.connect

        def _connect_pg(*args, **kwargs):
            kwargs.setdefault("cursor_factory", _CountingCursor)
            t = _perf()
            try:
                return _pg_connect(*args, **kwargs)
            finally:
                _add("conn", 1, _perf() - t)

        psycopg2.connect = _connect_pg
    except ImportError:
        pass

    try:
        import pymysql.connections as _pc

        _my_cmd = _pc.Connection._execute_command
        _my_connect = _pc.Connection.connect

        def _execute_command(self, command, sql):
            _add("sql", 1)
            return _my_cmd(self, command, sql)

        def _connect_my(self, sock=None):
            t = _perf()
            try:
                return _my_connect(self, sock)
            finally:
                _add("conn", 1, _perf() - t)

        _pc.Connection._execute_command = _execute_command
        _pc.Connection.connect = _connect_my
    except ImportError:
        pass

    try:
        import requests

        _send = requests.Session.send

        def _send_counted(self, request, **kwargs):
            t = _perf()
            try:
                return _send(self, request, **kwargs)
            finally:
                _add("api", 1, _perf() - t)

        requests.Session.send = _send_counted
    except ImportError:
        pass

    def _patch_result_collector(m):
        _rc["m"] = m
        RC = m.ResultCollector
        measure = RC.maybe_measure_ops

        @contextlib.contextmanager
        def maybe_measure_ops(self, timed, op_type, storage=False):
            if not timed:
                with measure(self, timed, op_type, storage=storage):
                    yield
                return
            state = self._get_thread_state()
            try:
                from dblib import result_pb2 as rslt
                op = rslt.OpType.Name(op_type)
            except Exception:
                op = str(op_type)
            sc = {"kind": "op", "op": op, "thread": _thread_id(), "step": getattr(state, "step_id", None),
                  "t_start": time.time()}
            _stack().append(sc)
            status = "raised"
            try:
                with measure(self, timed, op_type, storage=storage):
                    yield
                status = "ok"
            finally:
                _stack().pop()
                sc["status"] = status
                sc["latency"] = getattr(state, "current_latency", None) if status == "ok" else None
                sc["wall_s"] = time.time() - sc["t_start"]
                _emit(sc)

        RC.maybe_measure_ops = maybe_measure_ops

    def _patch_branch_tree(m):
        BT = m.BranchTree
        wait = BT.wait_for_slot

        def wait_for_slot(self, timeout=30.0):
            t = _perf()
            ok = wait(self, timeout=timeout)
            dt = _perf() - t
            if dt > 0.001 or not ok:
                _emit({"kind": "slot_wait", "thread": _thread_id(), "t_start": time.time() - dt,
                       "wait_s": dt, "ok": bool(ok), "alive": self.alive_count()})
            return ok

        BT.wait_for_slot = wait_for_slot

    _PATCHES = {"dblib.result_collector": _patch_result_collector, "macrobench.branch_tree": _patch_branch_tree}

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name not in _PATCHES or getattr(_tl, "finding", False):
                return None
            _tl.finding = True
            try:
                spec = importlib.util.find_spec(name)
            finally:
                _tl.finding = False
            if spec is None or spec.loader is None:
                return spec
            exec_module, patch = spec.loader.exec_module, _PATCHES[name]

            def _exec(module):
                exec_module(module)
                patch(module)

            spec.loader.exec_module = _exec
            return spec

    sys.meta_path.insert(0, _Finder())
