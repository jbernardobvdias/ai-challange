import pytest
import sqlglot
from sqlglot import exp

from agent.safety.sql_guard import SqlGuard, SqlRejected
from agent.schema import Schema
from agent.users import ProductScope

DS = "bigquery-public-data.thelook_ecommerce"
USERS = f"`{DS}.users`"
ORDERS = f"`{DS}.orders`"
ITEMS = f"`{DS}.order_items`"
PRODUCTS = f"`{DS}.products`"

ALL = ProductScope()
JEANS = ProductScope((("category", ("Jeans",)),))


@pytest.fixture(scope="module")
def guard() -> SqlGuard:
    return SqlGuard(Schema(), max_rows=1000)


def reads_pii(sql: str) -> bool:
    """True if the final SQL could read a PII column from the raw users table."""
    pii = {"first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude", "user_geom"}
    return any(i.name.lower() in pii for i in sqlglot.parse_one(sql, dialect="bigquery").find_all(exp.Identifier))


@pytest.mark.parametrize("sql, code", [
    (f"SELECT email FROM {USERS}", "pii_column"),
    (f"SELECT u.first_name, u.last_name FROM {USERS} u", "pii_column"),
    (f"SELECT EMAIL FROM {USERS}", "pii_column"),
    (f"SELECT COUNT(*) FROM {USERS} WHERE email LIKE '%@gmail.com'", "pii_column"),
    (f"SELECT x FROM (SELECT street_address AS x FROM {USERS})", "pii_column"),
    (f"SELECT AVG(latitude) FROM {USERS}", "pii_column"),
    (f"DELETE FROM {USERS} WHERE TRUE", "not_read_only"),
    (f"DROP TABLE {USERS}", "not_read_only"),
    (f"UPDATE {ORDERS} SET status = 'x' WHERE TRUE", "not_read_only"),
    (f"INSERT INTO {ORDERS} (order_id) VALUES (1)", "not_read_only"),
    ("EXPORT DATA OPTIONS(uri='gs://x/*') AS SELECT 1", "not_read_only"),
    ("EXECUTE IMMEDIATE 'DROP TABLE x'", "not_read_only"),
    (f"SELECT 1; DROP TABLE {USERS}", "multiple_statements"),
    ("SELECT 1; SELECT 2", "multiple_statements"),
    ("CREATE TEMP FUNCTION f() AS (1); SELECT f()", "multiple_statements"),
    ("SELECT * FROM `other-project.secrets.passwords`", "table_not_allowed"),
    ("SELECT * FROM `region-us.INFORMATION_SCHEMA.TABLES`", "table_not_allowed"),
    (f"SELECT * FROM `{DS}.INFORMATION_SCHEMA.COLUMNS`", "table_not_allowed"),
    (f"SELECT * FROM `{DS}.inventory_items`", "table_not_allowed"),
    (f"SELECT * FROM `{DS}.*`", "table_not_allowed"),
    ("SELECT * FROM users", "table_not_allowed"),
    ("SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')", "blocked_function"),
    ("SELECT * FROM ML.PREDICT(MODEL m, TABLE t)", "table_not_allowed"),
    ("SELECT AI.GENERATE('x')", "blocked_function"),
    ("SELEC nonsense FROM", "syntax"),
    ("", "multiple_statements"),
])
def test_rejected(guard, sql, code):
    with pytest.raises(SqlRejected) as e:
        guard.check(sql, ALL)
    assert e.value.code == code


@pytest.mark.parametrize("sql", [
    f"SELECT * FROM {USERS}",
    f"SELECT u.* FROM {USERS} u",
    f"SELECT u FROM {USERS} u",
    f"SELECT TO_JSON_STRING(u) FROM {USERS} u",
    f"SELECT TO_JSON_STRING(users) FROM {USERS}",
    f"WITH a AS (SELECT * FROM {USERS}) SELECT * FROM a",
    f"SELECT * FROM (SELECT * FROM {USERS})",
    f"SELECT * FROM {ORDERS} o JOIN {USERS} u ON u.id = o.user_id",
    "SELECT * FROM `bigquery-public-data`.thelook_ecommerce.users",
    f"SELECT 1 UNION ALL SELECT id FROM {USERS}",
    f"WITH users AS (SELECT * FROM {USERS}) SELECT * FROM users",
    f"SELECT state FROM {USERS} WHERE id IN (SELECT id FROM {USERS})",
])
@pytest.mark.parametrize("scope", [ALL, JEANS])
def test_whole_row_tricks_cannot_reach_pii(guard, sql, scope):
    assert not reads_pii(guard.check(sql, scope))


def test_raw_tables_appear_only_inside_views(guard):
    safe = guard.check(f"SELECT u.state FROM {USERS} u", ALL)
    tree = sqlglot.parse_one(safe, dialect="bigquery")
    assert tree.args["from_"].this.alias == "u"
    assert isinstance(tree.args["from_"].this, exp.Subquery)


@pytest.mark.parametrize("sql", [
    f"SELECT SUM(sale_price) FROM {ITEMS}",
    f"SELECT COUNT(*) FROM {ORDERS}",
    f"SELECT COUNT(*) FROM {USERS}",
    f"SELECT brand FROM {PRODUCTS}",
])
def test_every_table_is_limited_to_the_users_products(guard, sql):
    assert "category IN ('Jeans')" in guard.check(sql, JEANS)
    assert "category IN" not in guard.check(sql, ALL)


def test_scope_values_are_escaped(guard):
    scope = ProductScope((("brand", ("Levi's", "x') OR TRUE --")),))
    tree = sqlglot.parse_one(guard.check(f"SELECT brand FROM {PRODUCTS}", scope), dialect="bigquery")
    literals = {lit.this for lit in tree.find_all(exp.Literal)}
    assert {"Levi's", "x') OR TRUE --"} <= literals
    assert not list(tree.find_all(exp.Or))


def test_user_filter_cannot_widen_scope(guard):
    safe = guard.check(f"SELECT name FROM {PRODUCTS} WHERE category = 'Swim' OR TRUE", JEANS)
    assert "category IN ('Jeans')" in safe


def test_cte_named_like_a_table_is_kept(guard):
    safe = guard.check(f"WITH orders AS (SELECT status FROM {ORDERS}) SELECT status FROM orders", ALL)
    assert "SELECT status FROM orders" in safe


@pytest.mark.parametrize("sql, limit", [
    (f"SELECT status FROM {ORDERS}", 1000),
    (f"SELECT status FROM {ORDERS} LIMIT 5", 5),
    (f"SELECT status FROM {ORDERS} LIMIT 99999", 1000),
    (f"SELECT status FROM {ORDERS} UNION ALL SELECT status FROM {ORDERS}", 1000),
])
def test_rows_are_capped(guard, sql, limit):
    assert guard.check(sql, ALL).rstrip().endswith(f"LIMIT {limit}")


def test_comments_are_not_passed_on(guard):
    assert "secret" not in guard.check(f"SELECT status /* secret */ FROM {ORDERS}", ALL)


@pytest.mark.parametrize("sql", [
    f"SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) m, SUM(sale_price) r FROM {ITEMS} GROUP BY 1 ORDER BY 1",
    f"SELECT p.category, SAFE_DIVIDE(SUM(i.sale_price), COUNT(DISTINCT i.order_id)) FROM {ITEMS} i JOIN {PRODUCTS} p ON p.id = i.product_id GROUP BY 1",
    f"SELECT DATE_TRUNC(DATE(created_at), MONTH) d FROM {ORDERS} QUALIFY ROW_NUMBER() OVER (PARTITION BY status ORDER BY created_at) = 1",
    "SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2024-01-01', '2024-02-01')) d",
])
def test_normal_analytics_queries_pass(guard, sql):
    assert guard.check(sql, JEANS)


def test_customer_keys_never_leave_bigquery_raw(guard):
    safe = guard.check(f"SELECT id AS x FROM {USERS}", ALL)
    assert "SHA256" in safe
    assert "SHA256" in guard.check(f"SELECT user_id FROM {ORDERS}", ALL)
    assert "SHA256" in guard.check(f"SELECT user_id FROM {ITEMS}", ALL)


def test_other_ids_stay_usable(guard):
    assert "SHA256" not in guard.check(f"SELECT id, category FROM {PRODUCTS}", ALL)


def test_keys_are_salted_per_guard():
    schema = Schema()
    first = SqlGuard(schema, key_salt="one").check(f"SELECT id FROM {USERS}", ALL)
    assert "'one'" in first
    assert "'one'" not in SqlGuard(schema, key_salt="two").check(f"SELECT id FROM {USERS}", ALL)
    assert SqlGuard(schema)._key_salt != SqlGuard(schema)._key_salt


@pytest.mark.parametrize("sql", [
    "SELECT @@project_id AS p",
    "SELECT @@dataset_id",
    "SELECT SESSION_USER() AS su",
    "SELECT CURRENT_USER()",
    f"SELECT status FROM {ORDERS} WHERE status = @@location",
])
def test_system_information_is_not_available(guard, sql):
    with pytest.raises(SqlRejected) as e:
        guard.check(sql, ALL)
    assert e.value.code == "blocked_function"
