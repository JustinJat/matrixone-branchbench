"""The 4.2.5 BranchBench seed (matrixone-branchbench-mo 4.2.5/load_seed.py @3e9d624), for any warehouse count W.

The rows are generated inside the database by INSERT ... SELECT over small number tables (n10, n100, n1000, n3, nw),
one warehouse per statement, so every system gets the same rows from a few KB of SQL (nothing large crosses the
network, which matters for hosted Neon). Two dialects: "mysql" (MatrixOne, MySQL Dolt; the 4.2.5 "mo" dialect) and
"pg" (Neon; the 4.2.5 "dolt" dialect for DoltgreSQL: DATETIME -> TIMESTAMP). Statements and schema are copied
unchanged; only the scale is free (4.2.5 allows 100 and 1000).
Rows for W warehouses: warehouse W, district 10 W, customer 30,000 W (2% with NULL c_balance), item 100,000,
stock 100,000 W, orders 30,000 W, new_order 9,000 W, order_line 300,000 W.
"""

SCHEMA = {
    "warehouse": "w_id INT PRIMARY KEY, w_name VARCHAR(32), w_ytd DECIMAL(12,2)",
    "district": "d_id INT, d_w_id INT, d_next_o_id INT, PRIMARY KEY(d_w_id, d_id)",
    "customer": "c_id INT, c_d_id INT, c_w_id INT, c_credit_lim DECIMAL(12,2), c_ytd_payment DECIMAL(12,2), c_balance DECIMAL(12,2), PRIMARY KEY(c_w_id, c_d_id, c_id)",
    "item": "i_id INT PRIMARY KEY, i_price DECIMAL(12,2), i_name VARCHAR(32)",
    "stock": "s_i_id INT, s_w_id INT, s_quantity INT, PRIMARY KEY(s_w_id, s_i_id)",
    "orders": "o_id INT, o_d_id INT, o_w_id INT, o_c_id INT, o_carrier_id INT NULL, o_ol_cnt INT, o_all_local INT, o_entry_d DATETIME, PRIMARY KEY(o_w_id, o_d_id, o_id)",
    "new_order": "no_o_id INT, no_d_id INT, no_w_id INT, PRIMARY KEY(no_w_id, no_d_id, no_o_id)",
    "order_line": "ol_o_id INT, ol_d_id INT, ol_w_id INT, ol_number INT, ol_i_id INT, ol_supply_w_id INT, ol_delivery_d DATETIME NULL, ol_quantity INT, ol_amount DECIMAL(12,2), ol_dist_info VARCHAR(32), PRIMARY KEY(ol_w_id, ol_d_id, ol_o_id, ol_number)",
}
INSERTS = {
    "customer": "SELECT c2.n * 1000 + c1.n + 1, d.n + 1, w.w, 50000, (c1.n % 1000) * 10, CASE WHEN c1.n % 50 = 0 THEN NULL ELSE (c1.n % 2000) - 1000 END FROM {warehouses} w CROSS JOIN n10 d CROSS JOIN n3 c2 CROSS JOIN n1000 c1",
    "stock": "SELECT a.n * 100 + b.n + 1, w.w, 100 + ((a.n + b.n) % 50) FROM {warehouses} w CROSS JOIN n1000 a CROSS JOIN n100 b",
    "orders": "SELECT c2.n * 1000 + c1.n + 1, d.n + 1, w.w, c2.n * 1000 + c1.n + 1, NULL, 10, 1, CURRENT_TIMESTAMP FROM {warehouses} w CROSS JOIN n10 d CROSS JOIN n3 c2 CROSS JOIN n1000 c1",
    "new_order": "SELECT c1.n + 2101, d.n + 1, w.w FROM {warehouses} w CROSS JOIN n10 d CROSS JOIN n1000 c1 WHERE c1.n < 900",
    "order_line": "SELECT c2.n * 1000 + c1.n + 1, d.n + 1, w.w, l.n + 1, ((c1.n * 10 + l.n) % 100000) + 1, w.w, NULL, 5, ((c1.n + l.n) % 500) + 1, 'dist_info' FROM {warehouses} w CROSS JOIN n10 d CROSS JOIN n3 c2 CROSS JOIN n1000 c1 CROSS JOIN n10 l",
}
DIALECTS = ("mysql", "pg")


def expected_counts(warehouses: int) -> dict:
    factors = dict(warehouse=1, district=10, customer=30000, stock=100000,
                   orders=30000, new_order=9000, order_line=300000)
    return {"item": 100000, **{table: factor * warehouses for table, factor in factors.items()}}


def statements(warehouses: int, dialect: str = "mysql", batch: int = 1):
    if warehouses < 1 or batch < 1 or dialect not in DIALECTS:
        raise ValueError("positive warehouses and batch, dialect mysql or pg")
    yield "CREATE TABLE n10 (n INT PRIMARY KEY)"
    yield "INSERT INTO n10 VALUES " + ",".join(f"({n})" for n in range(10))
    yield "CREATE TABLE n100 (n INT PRIMARY KEY)"
    yield "INSERT INTO n100 SELECT a.n * 10 + b.n FROM n10 a CROSS JOIN n10 b"
    yield "CREATE TABLE n1000 (n INT PRIMARY KEY)"
    yield "INSERT INTO n1000 SELECT a.n * 10 + b.n FROM n100 a CROSS JOIN n10 b"
    yield "CREATE TABLE n3 (n INT PRIMARY KEY)"
    yield "INSERT INTO n3 VALUES (0),(1),(2)"
    yield "CREATE TABLE nw (w INT PRIMARY KEY)"
    yield "INSERT INTO nw VALUES " + ",".join(f"({n})" for n in range(1, warehouses + 1))
    for table, schema in SCHEMA.items():
        if dialect == "pg":
            schema = schema.replace("DATETIME", "TIMESTAMP")
        yield f"CREATE TABLE {table} ({schema})"
    yield "INSERT INTO warehouse SELECT w, CONCAT('w', w), 0 FROM nw"
    yield "INSERT INTO district SELECT d.n + 1, w.w, 3001 FROM nw w CROSS JOIN n10 d"
    yield "INSERT INTO item SELECT a.n * 100 + b.n + 1, ((a.n * 100 + b.n) % 1000) / 10.0, CONCAT('item', a.n * 100 + b.n + 1) FROM n1000 a CROSS JOIN n100 b"
    # Restrict the warehouse input before the cross joins, bounding each INSERT.
    for low in range(1, warehouses + 1, batch):
        high = min(low + batch, warehouses + 1)
        subset = f"(SELECT w FROM nw WHERE w >= {low} AND w < {high})"
        for table, select in INSERTS.items():
            yield f"INSERT INTO {table} " + select.format(warehouses=subset)


def write_dump(path, warehouses: int, dialect: str) -> int:
    """Write the statements, one per line ending in ';' (psql -f, or split on ';'). Returns the statement count."""
    n = 0
    with open(path, "x") as dump:
        for statement in statements(warehouses, dialect):
            dump.write(statement + ";\n")
            n += 1
    return n
