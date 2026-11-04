"""Users and their product entitlements, loaded from config/users.yaml."""
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent.config import CONFIG_DIR, ConfigError

_SCOPE_KEYS = {"departments": "department", "categories": "category", "brands": "brand"}


def _sql_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


@dataclass(frozen=True)
class ProductScope:
    """Which products a user may analyse. `filters` maps a products column to allowed values."""
    filters: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def unrestricted(self) -> bool:
        return not self.filters

    def to_sql_predicate(self) -> str:
        """SQL condition on the products table. Only call when the scope is restricted."""
        return " AND ".join(
            f"{column} IN ({', '.join(_sql_string(v) for v in values)})"
            for column, values in self.filters
        )

    def describe(self) -> str:
        if self.unrestricted:
            return "all products"
        return "; ".join(f"{column}: {', '.join(values)}" for column, values in self.filters)


@dataclass(frozen=True)
class User:
    id: str
    name: str
    scope: ProductScope


def _parse_scope(user_id: str, raw: object) -> ProductScope:
    if raw == "all":
        return ProductScope()
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"User '{user_id}' has no scope (use `all` or list departments/categories/brands).")
    unknown = set(raw) - set(_SCOPE_KEYS)
    if unknown:
        raise ConfigError(f"User '{user_id}' has unknown scope keys: {sorted(unknown)}.")
    filters = []
    for key, values in raw.items():
        if not values:  # an empty list must not silently widen access
            raise ConfigError(f"User '{user_id}' has an empty scope list for '{key}'.")
        filters.append((_SCOPE_KEYS[key], tuple(str(v) for v in values)))
    return ProductScope(tuple(filters))


class UserDirectory:
    def __init__(self, path: Path = CONFIG_DIR / "users.yaml") -> None:
        raw = yaml.safe_load(path.read_text())["users"]
        self._users = {
            uid: User(uid, u.get("name", uid), _parse_scope(uid, u.get("scope")))
            for uid, u in raw.items()
        }

    def get(self, user_id: str) -> User:
        try:
            return self._users[user_id]
        except KeyError:
            raise ConfigError(f"Unknown user '{user_id}'. Known users: {', '.join(sorted(self._users))}.") from None
