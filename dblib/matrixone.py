"""MatrixOne backend for the branchable-database benchmark.

MatrixOne speaks the MySQL wire protocol, so this suite uses ``pymysql``
instead of psycopg2. MatrixOne has no git-style branches; instead it
provides snapshots and zero-copy database clones. We model a "branch" as a
separate physical database created by cloning the parent at a snapshot:

    CREATE SNAPSHOT <snap> FOR DATABASE <parent>;
    CREATE DATABASE <child> CLONE <parent> {SNAPSHOT = '<snap>'};

The default ("main") branch is the physical database ``db_name`` itself.
Connecting to a branch switches the active database via ``USE``/``select_db``.
"""

import os
import re
import uuid

import pymysql

from dblib.db_api import DBToolSuite
import dblib.result_collector as rc

MO_USER = os.environ.get("MO_USER", "root")
MO_PASSWORD = os.environ.get("MO_PASSWORD", "111")
MO_HOST = os.environ.get("MO_HOST", "127.0.0.1")
MO_PORT = int(os.environ.get("MO_PORT", "6001"))


def _connect(db_name: str = None, autocommit: bool = False) -> pymysql.connections.Connection:
    """Open a pymysql connection to MatrixOne."""
    return pymysql.connect(
        host=MO_HOST,
        port=MO_PORT,
        user=MO_USER,
        password=MO_PASSWORD,
        database=db_name,
        autocommit=autocommit,
        local_infile=True,
    )


def _sanitize_db_ident(name: str) -> str:
    """Make a string safe to use as a MatrixOne database identifier."""
    safe = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    if safe and safe[0].isdigit():
        safe = f"b_{safe}"
    return safe


def create_database_and_load_schema(
    db_name: str, schema_sql: str, drop_if_exists: bool = True
) -> None:
    """Create ``db_name`` (optionally dropping it first) and load a schema.

    The schema is split on ``;`` and executed statement by statement, which
    matches how the benchmark's other backends bootstrap a database.
    """
    server_conn = _connect(db_name=None, autocommit=True)
    try:
        with server_conn.cursor() as cur:
            if drop_if_exists:
                cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`;")
            cur.execute(f"CREATE DATABASE `{db_name}`;")
    finally:
        server_conn.close()

    if not schema_sql or not schema_sql.strip():
        return

    db_conn = _connect(db_name=db_name, autocommit=True)
    try:
        with db_conn.cursor() as cur:
            for stmt in (s.strip() for s in schema_sql.split(";")):
                if stmt:
                    cur.execute(stmt)
    finally:
        db_conn.close()


class MoToolSuite(DBToolSuite):
    """Tools for interacting with a MatrixOne database over a shared connection."""

    @classmethod
    def get_default_connection_uri(cls) -> str:
        return f"mysql://{MO_USER}@{MO_HOST}:{MO_PORT}/mysql"

    @classmethod
    def get_initial_connection_uri(cls, db_name: str) -> str:
        return f"mysql://{MO_USER}@{MO_HOST}:{MO_PORT}/{db_name}"

    @classmethod
    def init_for_bench(
        cls,
        collector: rc.ResultCollector,
        db_name: str,
        autocommit: bool,
        default_branch_name: str = "main",
    ):
        conn = _connect(db_name=db_name, autocommit=autocommit)
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
        # The root branch is always the base physical database. Named branches
        # are separate clone databases (microbench_br_<name>). Note that the
        # runner may pass a *setup* branch as ``default_branch_name`` (it
        # overwrites it with the last branch created), so we register the root
        # explicitly rather than assuming ``default_branch_name`` is the root.
        self._root_branch = "main"
        self._branch_to_db = {self._root_branch: db_name}
        self._current_branch = self._root_branch
        # Connect to whichever branch the caller asked to start on; for a
        # named branch this resolves to its clone database.
        self._connect_branch_impl(default_branch_name)

    # ------------------------------------------------------------------
    # Branch model helpers
    # ------------------------------------------------------------------

    def _physical_db_for(self, branch_name: str) -> str:
        """Physical database name backing a branch."""
        if branch_name in self._branch_to_db:
            return self._branch_to_db[branch_name]
        return _sanitize_db_ident(f"{self.db_name}_br_{branch_name}")

    def list_branches(self) -> list[str]:
        return list(self._branch_to_db.keys())

    # ------------------------------------------------------------------
    # Abstract/overridable hooks from DBToolSuite
    # ------------------------------------------------------------------

    def _prepare_commit(self, message: str = "") -> None:
        # MatrixOne commits via the standard connection commit(); nothing to
        # stage explicitly the way Dolt needs dolt_add/dolt_commit.
        pass

    # ------------------------------------------------------------------
    # SQL dialect translation (Postgres-flavored workflow SQL -> MySQL)
    # ------------------------------------------------------------------
    # The shared macrobench workflow SQL is written for Postgres and uses
    # ``INSERT ... ON CONFLICT DO NOTHING``. MatrixOne speaks MySQL, whose
    # equivalent is ``INSERT IGNORE``. We translate at execution time so the
    # workflow definitions stay backend-neutral.
    _ON_CONFLICT_RE = re.compile(r"\s+ON\s+CONFLICT\s+DO\s+NOTHING", re.IGNORECASE)
    _INSERT_INTO_RE = re.compile(r"INSERT\s+INTO", re.IGNORECASE)

    @classmethod
    def _translate_sql(cls, query: str) -> str:
        if not query or "ON CONFLICT" not in query.upper():
            return query
        q = cls._ON_CONFLICT_RE.sub("", query)
        q = cls._INSERT_INTO_RE.sub("INSERT IGNORE INTO", q, count=1)
        return q

    def execute_sql(self, query, vars=None, timed: bool = False, storage: bool = False):
        return super().execute_sql(
            self._translate_sql(query), vars, timed=timed, storage=storage
        )

    def _create_branch_impl(self, branch_name: str, parent_id: str = None) -> None:
        """Create a branch by cloning the parent database at a fresh snapshot."""
        parent_db = parent_id or self._branch_to_db[self._current_branch]
        child_db = self._physical_db_for(branch_name)
        snap = f"bb_snap_{uuid.uuid4().hex}"

        super().execute_sql(f"CREATE SNAPSHOT {snap} FOR DATABASE `{parent_db}`;")
        try:
            super().execute_sql(
                f"CREATE DATABASE `{child_db}` CLONE `{parent_db}` "
                f"{{SNAPSHOT = '{snap}'}};"
            )
        finally:
            # The clone is a full materialized copy; the snapshot is no longer
            # needed. Best-effort cleanup so snapshots don't accumulate.
            try:
                super().execute_sql(f"DROP SNAPSHOT {snap};")
            except Exception:
                pass

        self._branch_to_db[branch_name] = child_db

    def _connect_branch_impl(self, branch_name: str) -> None:
        physical = self._branch_to_db.get(branch_name) or self._physical_db_for(
            branch_name
        )
        self.conn.select_db(physical)
        self._branch_to_db.setdefault(branch_name, physical)
        self._current_branch = branch_name

    def _get_current_branch_impl(self) -> tuple[str, str]:
        return (self._current_branch, self._branch_to_db[self._current_branch])

    def _delete_branch_impl(self, branch_name: str, branch_id: str) -> None:
        physical = branch_id or self._branch_to_db.get(branch_name)
        if physical is None:
            return
        # Must not be connected to the database being dropped.
        if self._current_branch == branch_name:
            main_db = self._branch_to_db.get(self._root_branch, self.db_name)
            self.conn.select_db(main_db)
            self._current_branch = self._root_branch
        super().execute_sql(f"DROP DATABASE IF EXISTS `{physical}`;")
        self._branch_to_db.pop(branch_name, None)

    # ------------------------------------------------------------------
    # Storage / schema introspection (MySQL flavored)
    # ------------------------------------------------------------------

    def get_total_storage_bytes(self) -> int:
        """Best-effort logical size of the current branch's database.

        MatrixOne Cloud doesn't expose on-disk byte usage to tenants, so we
        sum ``information_schema.tables.data_length`` for the current database.
        Returns 0 when unavailable.
        """
        try:
            physical = self._branch_to_db[self._current_branch]
            res = super().execute_sql(
                "SELECT COALESCE(SUM(data_length), 0) "
                "FROM information_schema.tables WHERE table_schema = %s;",
                (physical,),
            )
            if res and res[0] and res[0][0] is not None:
                return int(res[0][0])
        except Exception:
            pass
        return 0

    def get_table_schema(self, table_name: str) -> str:
        """Return a CREATE TABLE string built from MySQL information_schema.

        Overrides the Postgres implementation in DBToolSuite, which relies on
        ``udt_name`` (a Postgres-only column).
        """
        physical = self._branch_to_db[self._current_branch]
        query = """
        SELECT column_name, data_type, is_nullable,
               character_maximum_length, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position;
        """
        columns = super().execute_sql(query, (physical, table_name))
        if not columns:
            raise Exception(f"Error: Table '{table_name}' not found.")

        defs = []
        for col_name, data_type, is_nullable, char_len, num_prec, num_scale in columns:
            # Skip MatrixOne-internal columns (e.g. the hidden composite-PK
            # column __mo_cpkey_col); they aren't part of the user schema.
            if str(col_name).startswith("__mo_"):
                continue
            dtype = data_type
            if char_len is not None:
                dtype += f"({char_len})"
            elif data_type.lower() in ("decimal", "numeric") and num_prec is not None:
                dtype += f"({num_prec}, {num_scale})"
            line = f"  {col_name} {dtype}"
            if is_nullable == "NO":
                line += " NOT NULL"
            defs.append(line)
        return "CREATE TABLE {} (\n{}\n);".format(table_name, ",\n".join(defs))

    def get_pk_columns(self, table_name: str) -> list[str]:
        """Primary-key column names, in order.

        Uses ``SHOW KEYS`` because MatrixOne leaves
        ``information_schema.key_column_usage`` empty and represents composite
        keys with a hidden ``__mo_cpkey_col`` column in
        ``information_schema.columns``.
        """
        physical = self._branch_to_db[self._current_branch]
        rows = super().execute_sql(
            f"SHOW KEYS FROM `{table_name}` IN `{physical}`;"
        )
        # SHOW KEYS columns: Table, Non_unique, Key_name, Seq_in_index,
        # Column_name, ... -> filter to the PRIMARY key, ordered by seq.
        pk = [
            (int(r[3]), r[4]) for r in (rows or []) if r[2] == "PRIMARY"
        ]
        pk.sort(key=lambda x: x[0])
        return [name for _, name in pk]

    def get_all_columns(self, table_name: str) -> list[str]:
        """User-facing column names for a table (excludes MatrixOne internals)."""
        physical = self._branch_to_db[self._current_branch]
        rows = super().execute_sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position;",
            (physical, table_name),
        )
        return [r[0] for r in (rows or []) if not str(r[0]).startswith("__mo_")]
