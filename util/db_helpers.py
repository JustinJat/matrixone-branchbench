"""
Database helper utilities.

Standalone utility functions for common database operations. They work with
both Postgres-protocol connections (psycopg2) and MySQL-protocol connections
(pymysql, used by the MatrixOne backend). The protocol is detected from the
connection object, so callers don't need to care which one they hold.
"""

from typing import Optional


def _is_mysql(conn) -> bool:
    """True if ``conn`` is a MySQL-protocol (pymysql) connection."""
    return type(conn).__module__.split(".")[0] == "pymysql"


def _run_sql_query(conn, query: str, params: tuple = None) -> list[tuple]:
    """
    Execute a SQL query and return all results (internal helper).

    Args:
        conn: Active database connection (psycopg2 or pymysql)
        query: SQL query to execute
        params: Optional query parameters for parameterized queries

    Returns:
        List of result tuples

    Raises:
        Exception: If query execution fails
    """
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            # cursor.description is None for statements that produce no result
            # set (INSERT/UPDATE/DDL). This check is protocol-agnostic.
            if cur.description is None:
                return []
            return cur.fetchall()
    except Exception as e:
        raise Exception(f"Error executing SQL query: {query}; {params}; {e}")


def initialize_schema(conn, schema_ddl: str) -> None:
    """
    Initialize the database schema using the provided DDL statements.

    Args:
        conn: Active database connection
        schema_ddl: DDL statements separated by semicolons
    """
    print("Initializing database schema...")
    sql_statements = [
        stmt.strip() for stmt in schema_ddl.split(";") if stmt.strip()
    ]
    with conn.cursor() as cur:
        for stmt in sql_statements:
            cur.execute(stmt)

    conn.commit()


def _get_primary_key_columns(conn, table_name: str) -> list[tuple[str, int]]:
    """
    Get the primary key columns for a table.

    Returns:
        List of (column_name, ordinal_position) tuples
    """
    if _is_mysql(conn):
        # SHOW KEYS columns: Table, Non_unique, Key_name, Seq_in_index,
        # Column_name, ... -> filter to PRIMARY, return (name, seq).
        rows = _run_sql_query(conn, f"SHOW KEYS FROM `{table_name}`;")
        pk = [(r[4], int(r[3])) for r in rows if r[2] == "PRIMARY"]
        pk.sort(key=lambda x: x[1])
        return pk

    query = """
        SELECT
            column_name, ordinal_position
        FROM
            information_schema.key_column_usage
        WHERE
            table_schema = 'public'
            AND table_name = %s
            AND constraint_name = (
                SELECT constraint_name
                FROM information_schema.table_constraints
                WHERE table_schema = 'public'
                AND table_name = %s
                AND constraint_type = 'PRIMARY KEY'
            )
        ORDER BY ordinal_position DESC;
    """
    pk_columns = _run_sql_query(conn, query, (table_name, table_name))
    return [(col[0], col[1]) for col in pk_columns]


def get_pk_column_names(conn, table_name: str) -> list[str]:
    """
    Get the primary key column names for a table.

    Raises:
        ValueError: If table has no primary key
    """
    all_columns = [col[0] for col in _get_primary_key_columns(conn, table_name)]
    if not all_columns:
        raise ValueError(f"Table {table_name} has no primary key.")
    return all_columns


def get_pk_values(
    conn,
    table_name: str,
    pk_columns: Optional[list[str]] = None,
) -> set[tuple]:
    """
    Get all primary key values for a table.

    This should be reasonably fast since it's an index-only scan.

    Returns:
        Set of primary key value tuples
    """
    if not pk_columns:
        pk_columns = get_pk_column_names(conn, table_name)

    if _is_mysql(conn):
        cols = ", ".join(f"`{c}`" for c in pk_columns)
        sql = f"SELECT {cols} FROM `{table_name}`;"
        return _run_sql_query(conn, sql)

    # Ensure we're using the public schema (Postgres only).
    _run_sql_query(conn, "SET search_path TO public")
    sql = f"SELECT {', '.join(pk_columns)} FROM {table_name};"
    return _run_sql_query(conn, sql)


def get_all_tables(conn) -> list[str]:
    """
    Get all base-table names in the current database/schema.
    """
    if _is_mysql(conn):
        query = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
        AND table_type = 'BASE TABLE';
        """
        tables = _run_sql_query(conn, query)
        return [table[0] for table in tables]

    _run_sql_query(conn, "SET search_path TO public")
    query = """
    SELECT table_name
    FROM information_schema.tables
    WHERE table_type = 'BASE TABLE'
    AND table_schema NOT IN ('pg_catalog', 'information_schema');
    """
    tables = _run_sql_query(conn, query)
    return [table[0] for table in tables]


def get_db_size(conn) -> int:
    """
    Get the current database size in bytes, or 0 if unable to determine.
    """
    if _is_mysql(conn):
        # MatrixOne/MySQL: sum logical data + index length for the current db.
        query = """
        SELECT COALESCE(SUM(data_length + index_length), 0)
        FROM information_schema.tables
        WHERE table_schema = DATABASE();
        """
        res = _run_sql_query(conn, query)
        if res and res[0] and res[0][0] is not None:
            return int(res[0][0])
        return 0

    # Get the current database name
    db_name_query = "SELECT current_database();"
    db_name_result = _run_sql_query(conn, db_name_query)
    db_name = db_name_result[0][0] if db_name_result else None

    _run_sql_query(conn, "SET search_path TO public")

    if not db_name:
        print("Warning: Could not determine database name, returning 0")
        return 0

    # Query the size of the current database using pg_database_size
    size_query = "SELECT pg_database_size(%s);"
    size_result = _run_sql_query(conn, size_query, (db_name,))

    if size_result and size_result[0][0] is not None:
        return int(size_result[0][0])

    return 0


def get_all_columns(conn, table_name: str) -> list[str]:
    """
    Get all column names for a table.
    """
    if _is_mysql(conn):
        query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = %s
        ORDER BY ordinal_position;
        """
        columns = _run_sql_query(conn, query, (table_name,))
        # Exclude MatrixOne-internal columns (e.g. __mo_cpkey_col).
        return [col[0] for col in columns if not str(col[0]).startswith("__mo_")]

    _run_sql_query(conn, "SET search_path TO public")
    query = """
    SELECT column_name
    FROM information_schema.columns
    WHERE table_name = %s
    """
    columns = _run_sql_query(conn, query, (table_name,))
    return [col[0] for col in columns]
