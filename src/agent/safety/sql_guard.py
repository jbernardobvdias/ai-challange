"""Deterministic SQL guard: nothing reaches BigQuery without passing through here.

1. Parse with sqlglot. Anything unparsable or not a single read-only query is rejected.
2. Reject references to personal-data columns, tables outside the allowlist, and risky functions.
3. Rewrite every table reference into a secure view that exposes only non-PII columns and only
   rows within the user's product scope. Tricks that read a whole row (`SELECT u`,
   `TO_JSON_STRING(u)`, `SELECT *`) therefore cannot reach personal data.
4. Cap the number of rows.

Only the SQL rendered from the checked syntax tree is executed, never the model's original text.
"""
import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from agent.schema import Schema
from agent.users import ProductScope

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # rejected input is handled below

_BLOCKED_FUNCTIONS = {"EXTERNAL_QUERY", "VECTOR_SEARCH"}
_BLOCKED_NAMESPACES = {"ML", "AI"}  # billable model calls


class SqlRejected(Exception):
    """A query was refused. `code` is for metrics; `message` and `hint` go back to the model."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


class SqlGuard:
    def __init__(self, schema: Schema, max_rows: int = 1000) -> None:
        self.schema = schema
        self.max_rows = max_rows
        catalog, dataset = schema.dataset.split(".")
        self._catalog, self._dataset = catalog.lower(), dataset.lower()
        self._pii = {c for cols in schema.pii_columns.values() for c in cols}

    def check(self, sql: str, scope: ProductScope) -> str:
        """Return the safe SQL to execute, or raise SqlRejected."""
        tree = self._parse_single_query(sql)
        self._reject_pii_columns(tree)
        self._reject_blocked_functions(tree)
        self._apply_secure_views(tree, scope)
        self._cap_rows(tree)
        return tree.sql(dialect="bigquery", comments=False)

    def _parse_single_query(self, sql: str) -> exp.Query:
        try:
            statements = [s for s in sqlglot.parse(sql, dialect="bigquery") if s is not None]
        except SqlglotError as e:
            raise SqlRejected("syntax", f"Could not parse the SQL: {str(e)[:300]}",
                              "Write a single valid BigQuery Standard SQL SELECT.") from e
        if len(statements) != 1:
            raise SqlRejected("multiple_statements", "Only one statement can be run at a time.",
                              "Send a single SELECT; use CTEs for intermediate steps.")
        tree = statements[0]
        if not isinstance(tree, exp.Query):
            raise SqlRejected("not_read_only", "Only read-only SELECT queries are allowed.",
                              "This data is read-only and cannot be changed.")
        return tree

    def _reject_pii_columns(self, tree: exp.Expression) -> None:
        for identifier in tree.find_all(exp.Identifier):
            if identifier.name.lower() in self._pii:
                raise SqlRejected(
                    "pii_column", f"Column '{identifier.name}' contains personal data and is not available.",
                    "Tell the user personal data cannot be accessed; offer aggregates or demographics instead.",
                )

    def _reject_blocked_functions(self, tree: exp.Expression) -> None:
        for fn in tree.find_all(exp.Anonymous):
            if fn.name.upper() in _BLOCKED_FUNCTIONS:
                raise SqlRejected("blocked_function", f"Function {fn.name.upper()} is not allowed.")
        for dot in tree.find_all(exp.Dot):
            if dot.this.name.upper() in _BLOCKED_NAMESPACES:
                raise SqlRejected("blocked_function", f"{dot.this.name.upper()} functions are not allowed.")

    def _apply_secure_views(self, tree: exp.Expression, scope: ProductScope) -> None:
        cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
        replacements = []
        for table in tree.find_all(exp.Table):
            if not table.db and not table.catalog and table.name.lower() in cte_names:
                continue
            replacements.append((table, self._secure_view(table, scope)))
        # Validate everything first, then replace: the views must not be walked themselves.
        for table, view in replacements:
            table.replace(view)

    def _secure_view(self, table: exp.Table, scope: ProductScope) -> exp.Expression:
        name = table.name.lower()
        allowed = (
            isinstance(table.this, exp.Identifier)
            and table.catalog.lower() == self._catalog
            and table.db.lower() == self._dataset
            and name in self.schema.tables
        )
        if not allowed:
            raise SqlRejected(
                "table_not_allowed", f"Table '{table.sql(dialect='bigquery')}' is not available.",
                f"Use only these tables, fully qualified: {', '.join(self.schema.fqn(t) for t in self.schema.tables)}.",
            )
        view = sqlglot.parse_one(self._view_sql(name, scope), dialect="bigquery")
        return view.subquery(alias=table.alias or table.name, copy=False)

    def _view_sql(self, name: str, scope: ProductScope) -> str:
        columns = ", ".join(c.name for c in self.schema.tables[name].public_columns)
        select = f"SELECT {columns} FROM {self.schema.fqn(name)}"
        if scope.unrestricted:
            return select

        predicate = scope.to_sql_predicate()
        scoped_products = f"SELECT id FROM {self.schema.fqn('products')} WHERE {predicate}"
        scoped_items = f"SELECT {{key}} FROM {self.schema.fqn('order_items')} WHERE product_id IN ({scoped_products})"
        conditions = {
            "products": predicate,
            "order_items": f"product_id IN ({scoped_products})",
            "orders": f"order_id IN ({scoped_items.format(key='order_id')})",
            "users": f"id IN ({scoped_items.format(key='user_id')})",
        }
        return f"{select} WHERE {conditions[name]}"

    def _cap_rows(self, tree: exp.Query) -> None:
        limit = tree.args.get("limit")
        current = limit.expression if limit is not None else None
        if isinstance(current, exp.Literal) and current.is_int and int(current.this) <= self.max_rows:
            return
        tree.limit(self.max_rows, copy=False)
