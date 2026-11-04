from datetime import UTC, datetime, timedelta

import pytest

from agent.storage.reports_store import ReportStore


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 4, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return ReportStore(tmp_path / "sub" / "app.db", clock)


def test_save_and_find(store):
    saved = store.save("alice", "t1", "Q1 review", "Revenue grew.")
    assert [r.id for r in store.find("alice")] == [saved.id]


def test_data_survives_reopening(tmp_path):
    path = tmp_path / "app.db"
    ReportStore(path).save("alice", "t1", "A", "x")
    assert len(ReportStore(path).find("alice")) == 1


def test_users_cannot_see_each_others_reports(store):
    theirs = store.save("bob", "t1", "Bob's secret", "Client X")
    assert store.find("alice") == []
    assert store.find("alice", search="Client X") == []
    assert store.find("alice", report_ids=[theirs.id]) == []


def test_users_cannot_delete_each_others_reports(store):
    theirs = store.save("bob", "t1", "Bob's", "x")
    assert store.soft_delete("alice", [theirs.id]) == []
    assert len(store.find("bob")) == 1


def test_search_matches_title_and_content_ignoring_case(store):
    a = store.save("alice", "t", "About client x", "...")
    b = store.save("alice", "t", "Other", "Mentions CLIENT X here")
    store.save("alice", "t", "Unrelated", "Nothing")
    assert {r.id for r in store.find("alice", search="Client X")} == {a.id, b.id}


@pytest.mark.parametrize("search", ["%", "_", "\\"])
def test_search_wildcards_are_literal(store, search):
    store.save("alice", "t", "plain", "plain")
    assert store.find("alice", search=search) == []


def test_search_is_not_sql_injectable(store):
    store.save("alice", "t", "A", "x")
    store.save("bob", "t", "B", "x")
    assert store.find("alice", search="' OR 1=1 --") == []
    assert [r.title for r in store.find("alice")] == ["A"]


def test_filters_combine(store):
    keep = store.save("alice", "other", "Client X old", "x")
    match = store.save("alice", "this", "Client X new", "x")
    store.save("alice", "this", "Different", "x")
    assert [r.id for r in store.find("alice", search="Client X", conversation_id="this")] == [match.id]
    assert keep.id not in [r.id for r in store.find("alice", conversation_id="this")]


def test_delete_only_given_ids_and_hide_them(store):
    a, b = store.save("alice", "t", "A", "x"), store.save("alice", "t", "B", "x")
    assert [r.id for r in store.soft_delete("alice", [a.id])] == [a.id]
    assert [r.id for r in store.find("alice")] == [b.id]


def test_delete_twice_reports_nothing_second_time(store):
    a = store.save("alice", "t", "A", "x")
    store.soft_delete("alice", [a.id])
    assert store.soft_delete("alice", [a.id]) == []


def test_restore_brings_back_only_the_latest_batch(store, clock):
    a, b = store.save("alice", "t", "A", "x"), store.save("alice", "t", "B", "x")
    store.soft_delete("alice", [a.id])
    clock.now += timedelta(minutes=1)
    store.soft_delete("alice", [b.id])
    assert [r.id for r in store.restore_last_delete("alice", timedelta(minutes=10))] == [b.id]
    assert [r.id for r in store.find("alice")] == [b.id]


def test_restore_window_expires(store, clock):
    a = store.save("alice", "t", "A", "x")
    store.soft_delete("alice", [a.id])
    clock.now += timedelta(minutes=11)
    assert store.restore_last_delete("alice", timedelta(minutes=10)) == []
    assert store.find("alice") == []


def test_restore_never_touches_other_users(store):
    a = store.save("bob", "t", "Bob's", "x")
    store.soft_delete("bob", [a.id])
    assert store.restore_last_delete("alice", timedelta(minutes=10)) == []
    assert store.find("bob") == []


def test_actions_are_audited(store):
    a = store.save("alice", "t1", "A", "x")
    store.soft_delete("alice", [a.id], "t1")
    store.restore_last_delete("alice", timedelta(minutes=10), "t1")
    entries = store.audit_entries("alice")
    assert [e["action"] for e in entries] == ["save", "delete", "restore"]
    assert all(e["report_ids"] == [a.id] for e in entries)


def test_failed_attempt_on_someone_elses_report_is_not_logged_as_a_delete(store):
    theirs = store.save("bob", "t", "Bob's", "x")
    store.soft_delete("alice", [theirs.id])
    assert store.audit_entries("alice") == []
