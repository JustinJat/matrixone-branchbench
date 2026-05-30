"""Reproduce BranchBench microbenchmark methodology (Figures 5 & 6) on MatrixOne.

Paper: Ang et al., "BranchBench: An Extensible Benchmark for Agentic Database
Branching" (CAIS'26 SAO Workshop). The paper's microbenchmarks isolate two
things:

  * Figure 5 -- branch *create* and *connect* latency as the number of branches
    grows. (Paper creates x branches with x threads; we use a single client
    because the target is a shared cloud free-tier, so we measure latency vs.
    branch index to expose any per-branch growth.)
  * Figure 6 -- range-read *capacity* as the number of branches grows, with
    range size = 100 rows.

Both use the same backend branch API the macrobench uses (MoToolSuite), so the
numbers are comparable to the paper's Neon/Dolt operation-latency breakdowns.

Connection comes from MO_HOST/MO_PORT/MO_USER/MO_PASSWORD env vars.
"""

import statistics as st
import time
import uuid

from dblib import matrixone as mo
from dblib.matrixone import MoToolSuite, create_database_and_load_schema
import dblib.result_collector as rc

SCHEMA = open("db_setup/mo_mini.sql").read()  # orders table + 1000 seed rows
TABLE = "orders"
PK_LO, PK_HI = 1, 1000
RANGE_SIZE = 100  # paper Figure 6


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def summary(xs):
    return {
        "n": len(xs),
        "mean": st.mean(xs),
        "p50": pct(xs, 50),
        "p90": pct(xs, 90),
        "p99": pct(xs, 99),
        "max": max(xs),
    }


def fmt(label, s):
    return (
        f"  {label:<22} n={s['n']:>4}  mean={s['mean']*1000:7.1f}ms  "
        f"p50={s['p50']*1000:7.1f}  p90={s['p90']*1000:7.1f}  "
        f"p99={s['p99']*1000:7.1f}  max={s['max']*1000:7.1f}"
    )


def new_suite(db_name):
    create_database_and_load_schema(db_name, SCHEMA, drop_if_exists=True)
    collector = rc.ResultCollector(run_id="paper_micro", output_dir="/tmp/paper_micro")
    return MoToolSuite.init_for_bench(
        collector=collector, db_name=db_name, autocommit=True, default_branch_name="main"
    )


def drop_db(db_name):
    try:
        srv = mo._connect(db_name=None, autocommit=True)
        with srv.cursor() as cur:
            cur.execute("SHOW DATABASES;")
            dbs = [r[0] for r in cur.fetchall()]
            for d in dbs:
                if d == db_name or d.startswith(f"{db_name}_br_"):
                    cur.execute(f"DROP DATABASE IF EXISTS `{d}`;")
        srv.close()
    except Exception as e:
        print(f"  cleanup warning: {e}")


# ---------------------------------------------------------------------------
# Figure 5: branch create + connect latency vs branch count
# ---------------------------------------------------------------------------
def figure5(n_branches=100, prefixes=(10, 25, 50, 100)):
    print("\n" + "=" * 72)
    print(f"FIGURE 5  branch lifecycle latency  (creating {n_branches} branches, "
          f"single client)")
    print("=" * 72)
    db = f"bb_f5_{uuid.uuid4().hex[:8]}"
    suite = new_suite(db)
    create_lat, connect_lat = [], []
    try:
        for i in range(n_branches):
            bname = f"b{i:04d}"
            suite.connect_branch("main")  # always fork from root (FAN_OUT)
            t0 = time.perf_counter()
            suite.create_branch(bname)
            create_lat.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            suite.connect_branch(bname)
            connect_lat.append(time.perf_counter() - t0)
            if (i + 1) % 25 == 0:
                print(f"  ... {i+1}/{n_branches} branches")

        print("\n  -- aggregate latency at prefixes (x branches) --")
        for p in prefixes:
            if p <= n_branches:
                print(f"  [x={p:>4}] " + fmt("BRANCH_CREATE", summary(create_lat[:p])).strip())
                print(f"          " + fmt("BRANCH_CONNECT", summary(connect_lat[:p])).strip())

        # Scaling check: does per-branch create latency grow with tree size?
        idx = list(range(len(create_lat)))
        mean = st.mean(create_lat)
        denom = sum((i - st.mean(idx)) ** 2 for i in idx) ** 0.5 * (
            sum((x - mean) ** 2 for x in create_lat) ** 0.5
        )
        corr = (
            sum((i - st.mean(idx)) * (x - mean) for i, x in zip(idx, create_lat)) / denom
            if denom
            else 0.0
        )
        print(f"\n  corr(create_latency, branch_index) = {corr:+.3f}  "
              f"({'grows with tree' if corr > 0.2 else 'flat / O(1) per branch'})")
        return {"create": summary(create_lat), "connect": summary(connect_lat), "corr": corr}
    finally:
        suite.close_connection()
        drop_db(db)
        print(f"  cleaned up {db}")


# ---------------------------------------------------------------------------
# Figure 6: range-read capacity vs number of branches (range size = 100)
# ---------------------------------------------------------------------------
def figure6(branch_levels=(1, 5, 10, 25), reads_per_branch=40):
    print("\n" + "=" * 72)
    print(f"FIGURE 6  range-read capacity  (range size = {RANGE_SIZE}, single client)")
    print("=" * 72)
    results = []
    for nb in branch_levels:
        db = f"bb_f6_{nb}_{uuid.uuid4().hex[:6]}"
        suite = new_suite(db)
        branches = []
        try:
            for i in range(nb):
                bname = f"b{i:03d}"
                suite.connect_branch("main")
                suite.create_branch(bname)
                branches.append(bname)

            lat = []
            import random
            random.seed(42)
            t_start = time.perf_counter()
            total_reads = 0
            for bname in branches:
                suite.connect_branch(bname)
                for _ in range(reads_per_branch):
                    lo = random.randint(PK_LO, PK_HI - RANGE_SIZE)
                    t0 = time.perf_counter()
                    suite.execute_sql(
                        f"SELECT * FROM {TABLE} WHERE o_id BETWEEN %s AND %s;",
                        (lo, lo + RANGE_SIZE - 1),
                    )
                    lat.append(time.perf_counter() - t0)
                    total_reads += 1
            wall = time.perf_counter() - t_start
            thr = total_reads / wall if wall else 0.0
            s = summary(lat)
            results.append((nb, thr, s))
            print(f"\n  [branches={nb:>3}] reads={total_reads:>4}  wall={wall:6.2f}s  "
                  f"throughput={thr:6.1f} reads/s")
            print("  " + fmt("RANGE_READ(100)", s).strip())
        finally:
            suite.close_connection()
            drop_db(db)
    print("\n  -- capacity vs branches --")
    print(f"  {'branches':>8} {'reads/s':>10} {'p50 ms':>8} {'p90 ms':>8}")
    for nb, thr, s in results:
        print(f"  {nb:>8} {thr:>10.1f} {s['p50']*1000:>8.1f} {s['p90']*1000:>8.1f}")
    return results


def main():
    print(f"MatrixOne @ {mo.MO_HOST}:{mo.MO_PORT}  user={mo.MO_USER!r}")
    f5 = figure5(n_branches=100)
    f6 = figure6()
    print("\n" + "=" * 72)
    print("DONE -- MatrixOne microbenchmark (BranchBench Fig 5 & 6) complete")
    print("=" * 72)


if __name__ == "__main__":
    main()
