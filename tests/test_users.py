import pytest

from agent.config import ConfigError
from agent.users import ProductScope, UserDirectory, _parse_scope


def test_demo_users_load():
    users = UserDirectory()
    assert users.get("carol").scope.unrestricted
    assert "category IN ('Jeans', 'Swim', 'Dresses')" == users.get("alice").scope.to_sql_predicate()


def test_unknown_user_is_refused():
    with pytest.raises(ConfigError, match="Unknown user"):
        UserDirectory().get("mallory")


@pytest.mark.parametrize("raw", [None, {}, {"brands": []}, {"colours": ["red"]}, "everything"])
def test_missing_or_invalid_scope_never_means_all(raw):
    with pytest.raises(ConfigError):
        _parse_scope("x", raw)


def test_filters_combine_with_and():
    scope = _parse_scope("x", {"departments": ["Men"], "brands": ["Diesel", "Levi's"]})
    assert scope == ProductScope((("department", ("Men",)), ("brand", ("Diesel", "Levi's"))))
    assert scope.to_sql_predicate() == r"department IN ('Men') AND brand IN ('Diesel', 'Levi\'s')"
