"""MySQL Dolt (dolt sql-server) backend for the macrobenchmark (11-10, justin/oldbb).

The paper's Dolt backend (dblib/dolt.py) is DoltgreSQL over psycopg2. This is the same backend over the MySQL
protocol (pymysql, like dblib/matrixone.py), statement for statement:

    create  = CALL DOLT_CHECKOUT('<parent>'); CALL DOLT_CHECKOUT('-b', '<name>')   (dolt.py: SELECT dolt_checkout(...))
    connect = CALL DOLT_CHECKOUT('<name>')
    delete  = CALL DOLT_BRANCH('-D', '<name>')
    current = SELECT active_branch()          (Dolt branch names are unique, so the name is the id)
    commit  = CALL DOLT_ADD('-A'); CALL DOLT_COMMIT('-m', ...)   (only when a config sets autocommit: false)

Every worker has one connection to the benchmark database; a branch switch is a DOLT_CHECKOUT on it. The workflow
SQL's INSERT ... ON CONFLICT DO NOTHING becomes INSERT IGNORE, as in the MatrixOne backend.
Setup (load_and_commit): the SQL file runs statement by statement in a new database, then DOLT_ADD('-A') +
DOLT_COMMIT, as commit_dolt_schema does for DoltgreSQL, so every branch starts from the loaded data.
Connection: DOLT_MYSQL_HOST / _PORT / _USER / _PASSWORD (defaults 127.0.0.1, 3306, root, empty);
socket timeout DOLT_MYSQL_QUERY_TIMEOUT_SEC (default MO_QUERY_TIMEOUT_SEC, else 1800 s).

DOLT_COMMIT_BEFORE_BRANCH=1 (11-10, the "same work" variant): with the configs' autocommit: true nothing is ever
Dolt-committed, so a child branch starts from its parent's last commit (the seed) and misses everything the parent
did; MatrixOne's CLONE and Neon's branches copy the parent's current state. With this flag, branch creation first
commits the parent's working set (CALL DOLT_COMMIT('-Am', ...), timed as part of BRANCH_CREATE; "nothing to commit",
e.g. on the untouched root, is fine), so the child starts from the parent's current state as on the other systems.
Unset: exactly the paper's behaviour. drop_database also purges Dolt's dropped-database copies (.dolt_dropped_databases)
so that the disk space comes back.
"""

import os
import re

import pymysql

from dblib.db_api import DBToolSuite
import dblib.result_collector as rc

DOLT_MYSQL_USER = os.environ.get("DOLT_MYSQL_USER", "root")
DOLT_MYSQL_PASSWORD = os.environ.get("DOLT_MYSQL_PASSWORD", "")
DOLT_MYSQL_HOST = os.environ.get("DOLT_MYSQL_HOST", "127.0.0.1")
DOLT_MYSQL_PORT = int(os.environ.get("DOLT_MYSQL_PORT", "3306"))
COMMIT_AUTHOR = "branchbench <branchbench@localhost>"
DOLT_COMMIT_BEFORE_BRANCH = os.environ.get("DOLT_COMMIT_BEFORE_BRANCH", "0") == "1"


def _connect(db_name: str = None, autocommit: bool = False, timeout: float = None):
    """Open a pymysql connection to the Dolt SQL server."""
    timeout = timeout or float(
        os.environ.get("DOLT_MYSQL_QUERY_TIMEOUT_SEC", os.environ.get("MO_QUERY_TIMEOUT_SEC", "1800"))
    )
    return pymysql.connect(
        host=DOLT_MYSQL_HOST,
        port=DOLT_MYSQL_PORT,
        user=DOLT_MYSQL_USER,
        password=DOLT_MYSQL_PASSWORD,
        database=db_name,
        autocommit=autocommit,
        connect_timeout=5,
        read_timeout=timeout,
        write_timeout=timeout,
    )


def _quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def load_and_commit(db_name: str, sql_path: str, message: str = "Load SQL schema") -> None:
    """Create db_name anew, run the SQL file in it (statements split on ';'), and commit it on main."""
    with open(sql_path) as f:
        statements = [s.strip() for s in f.read().split(";") if s.strip()]
    server = _connect(autocommit=True)
    try:
        with server.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
            cur.execute(f"CREATE DATABASE `{db_name}`")
    finally:
        server.close()
    conn = _connect(db_name, autocommit=True)
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            cur.execute("CALL DOLT_ADD('-A')")
            cur.execute(f"CALL DOLT_COMMIT('-m', {_quote(message)}, '--author', {_quote(COMMIT_AUTHOR)})")
            cur.execute("SELECT active_branch()")
            branch = cur.fetchone()[0]
    finally:
        conn.close()
    print(f"Dolt (MySQL) database '{db_name}' loaded from {sql_path} "
          f"({len(statements)} statements) and committed on {branch}")


def drop_database(db_name: str) -> None:
    server = _connect(autocommit=True)
    try:
        with server.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
            try:
                cur.execute("CALL DOLT_PURGE_DROPPED_DATABASES()")
            except Exception as e:
                print(f"Warning: could not purge dropped Dolt databases: {e}")
    finally:
        server.close()


class DoltMySQLToolSuite(DBToolSuite):
    """MySQL Dolt over one shared pymysql connection per worker; branches are Dolt branches."""

    @classmethod
    def get_default_connection_uri(cls) -> str:
        return f"mysql://{DOLT_MYSQL_USER}@{DOLT_MYSQL_HOST}:{DOLT_MYSQL_PORT}/"

    @classmethod
    def init_for_bench(
        cls,
        collector: rc.ResultCollector,
        db_name: str,
        autocommit: bool,
        default_branch_name: str = "main",
    ):
        conn = _connect(db_name, autocommit=autocommit)
        return cls(
            connection=conn,
            collector=collector,
            autocommit=autocommit,
            default_branch_name=default_branch_name,
            db_name=db_name,
        )

    def __init__(
        self,
        connection,
        collector: rc.ResultCollector,
        autocommit: bool,
        default_branch_name: str = "main",
        db_name: str = None,
    ):
        super().__init__(connection, result_collector=collector)
        self.autocommit = autocommit
        self.db_name = db_name
        self._connect_branch_impl(default_branch_name)

    # Postgres-flavored workflow SQL -> MySQL (the same translation as dblib/matrixone.py).
    _ON_CONFLICT_RE = re.compile(r"\s+ON\s+CONFLICT\s+DO\s+NOTHING", re.IGNORECASE)
    _INSERT_INTO_RE = re.compile(r"INSERT\s+INTO", re.IGNORECASE)

    @classmethod
    def _translate_sql(cls, query: str) -> str:
        if not query or "ON CONFLICT" not in query.upper():
            return query
        q = cls._ON_CONFLICT_RE.sub("", query)
        return cls._INSERT_INTO_RE.sub("INSERT IGNORE INTO", q, count=1)

    def execute_sql(self, query, vars=None, timed: bool = False, storage: bool = False):
        return super().execute_sql(self._translate_sql(query), vars, timed=timed, storage=storage)

    def list_branches(self) -> list[str]:
        return [r[0] for r in super().execute_sql("SELECT name FROM dolt_branches")]

    def _prepare_commit(self, message: str = "") -> None:
        try:
            super().execute_sql("CALL DOLT_ADD('-A')")
            super().execute_sql(
                f"CALL DOLT_COMMIT('-m', {_quote(message)}, '--author', {_quote(COMMIT_AUTHOR)})"
            )
        except Exception as e:
            # As dolt.py: ignore commit errors (e.g. nothing to commit).
            print(f"Commit failed: {e}")

    def _create_branch_impl(self, branch_name: str, parent_id: str = None) -> None:
        if parent_id:
            self._connect_branch_impl(parent_id)
        if DOLT_COMMIT_BEFORE_BRANCH:
            try:
                super().execute_sql(
                    f"CALL DOLT_COMMIT('-Am', {_quote('branch point of ' + branch_name)}, "
                    f"'--author', {_quote(COMMIT_AUTHOR)})"
                )
            except Exception as e:
                if "nothing to commit" not in str(e):
                    raise
        super().execute_sql(f"CALL DOLT_CHECKOUT('-b', {_quote(branch_name)})")

    def _connect_branch_impl(self, branch_name: str) -> None:
        stmt = f"CALL DOLT_CHECKOUT({_quote(branch_name)})"
        try:
            super().execute_sql(stmt)
        except Exception as e:
            # Another worker deleted the branch this session is on (the runner's prune vs. cross-branch compare
            # race). Dolt then refuses every statement on the session, even USE <db>, until it names a live branch
            # (11-10: "database not found: <db>/<branch>", "Could not load database <db>"). MatrixOne's select_db
            # has no such state, so move the session to main and try once more.
            if not self.db_name or not any(s in str(e) for s in ("database not found", "Could not load database")):
                raise
            super().execute_sql(f"USE `{self.db_name}/main`")
            super().execute_sql(stmt)

    def _get_current_branch_impl(self) -> tuple[str, str]:
        name = super().execute_sql("SELECT active_branch()")[0][0]
        return (name, name)

    def _delete_branch_impl(self, branch_name: str, branch_id: str) -> None:
        super().execute_sql(f"CALL DOLT_BRANCH('-D', {_quote(branch_name)})")

    def get_total_storage_bytes(self) -> int:
        return 0  # storage is not measured in these runs
