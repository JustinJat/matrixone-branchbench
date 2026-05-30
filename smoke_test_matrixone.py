"""End-to-end smoke test for the MatrixOne backend.

Drives MoToolSuite through the generic DBToolSuite API to prove that the
MySQL protocol + MatrixOne snapshot/clone branching work with the benchmark
framework. Connection params come from MO_HOST/MO_PORT/MO_USER/MO_PASSWORD.

Run:
    python smoke_test_matrixone.py
"""

import sys

from dblib import matrixone as mo
from dblib.matrixone import MoToolSuite, create_database_and_load_schema
import dblib.result_collector as rc

DB_NAME = "bb_smoke"

SCHEMA = """
CREATE TABLE warehouse (
    w_id INT PRIMARY KEY,
    w_name VARCHAR(32),
    w_balance DECIMAL(12, 2)
)
"""

PASS = 0
FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


def main():
    print(f"MatrixOne @ {mo.MO_HOST}:{mo.MO_PORT} user={mo.MO_USER!r}")

    print("\n== Setup: create database + schema ==")
    create_database_and_load_schema(DB_NAME, SCHEMA, drop_if_exists=True)
    print(f"  created database {DB_NAME} with table 'warehouse'")

    collector = rc.ResultCollector(run_id="mo_smoke", output_dir="/tmp/mo_smoke")
    suite = MoToolSuite.init_for_bench(
        collector=collector,
        db_name=DB_NAME,
        autocommit=True,
        default_branch_name="main",
    )

    try:
        print("\n== Metadata introspection (MySQL flavored) ==")
        pk = suite.get_pk_columns("warehouse")
        cols = suite.get_all_columns("warehouse")
        check("PK detected via SHOW KEYS == ['w_id']", pk == ["w_id"])
        check(
            "columns == [w_id, w_name, w_balance]",
            cols == ["w_id", "w_name", "w_balance"],
        )
        ddl = suite.get_table_schema("warehouse")
        check("get_table_schema returns CREATE TABLE", ddl.startswith("CREATE TABLE"))
        print("  schema:\n    " + ddl.replace("\n", "\n    "))

        print("\n== CRUD on main branch (dict + positional params) ==")
        for i in range(1, 6):
            suite.execute_sql(
                "INSERT INTO warehouse (w_id, w_name, w_balance) "
                "VALUES (%(w_id)s, %(w_name)s, %(w_balance)s);",
                {"w_id": i, "w_name": f"wh{i}", "w_balance": i * 100},
                timed=True,
            )
        cnt = suite.execute_sql("SELECT COUNT(*) FROM warehouse;")[0][0]
        check("5 rows inserted on main", cnt == 5)

        row = suite.execute_sql(
            "SELECT w_name FROM warehouse WHERE w_id = %s;", (3,), timed=True
        )
        check("point read returns wh3", row and row[0][0] == "wh3")

        suite.execute_sql(
            "UPDATE warehouse SET w_name = %s WHERE w_id = %s;",
            ("renamed", 3),
            timed=True,
        )
        row = suite.execute_sql("SELECT w_name FROM warehouse WHERE w_id = 3;")
        check("update applied", row and row[0][0] == "renamed")

        suite.execute_sql("DELETE FROM warehouse WHERE w_id = %s;", (5,), timed=True)
        cnt = suite.execute_sql("SELECT COUNT(*) FROM warehouse;")[0][0]
        check("delete applied (4 rows remain)", cnt == 4)
        suite.commit_changes(message="main crud")

        print("\n== Branching via snapshot + clone ==")
        suite.create_branch("dev", timed=True)
        check("dev branch tracked", "dev" in suite.list_branches())

        suite.connect_branch("dev")
        name, bid = suite.get_current_branch()
        check("current branch is dev", name == "dev")
        dev_cnt = suite.execute_sql("SELECT COUNT(*) FROM warehouse;")[0][0]
        check("dev inherits 4 rows from main", dev_cnt == 4)

        # Mutate dev; main must stay isolated.
        suite.execute_sql(
            "INSERT INTO warehouse (w_id, w_name, w_balance) VALUES (99, 'dev-only', 0);",
            timed=True,
        )
        dev_cnt = suite.execute_sql("SELECT COUNT(*) FROM warehouse;")[0][0]
        check("dev now has 5 rows", dev_cnt == 5)

        suite.connect_branch("main")
        main_cnt = suite.execute_sql("SELECT COUNT(*) FROM warehouse;")[0][0]
        check("main isolated (still 4 rows)", main_cnt == 4)

        print("\n== Branch deletion ==")
        suite.connect_branch("main")
        suite.delete_branch("dev", branch_id=bid, timed=True)
        check("dev branch removed from tracking", "dev" not in suite.list_branches())

        print("\n== Result collection ==")
        n_records = len(collector.results)
        check("timed operations recorded", n_records > 0)
        collector.write_to_parquet("mo_smoke.parquet")
        print(f"  {n_records} timed records collected")

    finally:
        suite.close_connection()
        # Cleanup: drop the test database and any leftover branch dbs.
        try:
            server = mo._connect(db_name=None, autocommit=True)
            with server.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{DB_NAME}`;")
                cur.execute(f"DROP DATABASE IF EXISTS `{DB_NAME}_br_dev`;")
            server.close()
            print("\n== Cleanup: dropped test databases ==")
        except Exception as e:
            print(f"\n== Cleanup warning: {e} ==")

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
