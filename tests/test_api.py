from datetime import UTC, datetime

from fastapi.testclient import TestClient

from temporal_memory.api import create_app


def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "memory.sqlite3")))


def payload(**overrides):
    result = {
        "subject": "profile:alex",
        "predicate": "preferred_stack",
        "value": "Python",
        "source_ref": "conversation:msg-1",
        "confidence": 0.9,
        "valid_from": "2026-01-01",
        "valid_to": None,
        "idempotency_key": "assert-1",
    }
    result.update(overrides)
    return result


def test_assert_and_query_by_valid_time(tmp_path):
    api = client(tmp_path)
    created = api.post("/facts", json=payload())
    assert created.status_code == 201
    fact = created.json()
    assert fact["value"] == "Python"
    assert api.get("/facts", params={"valid_at": "2026-05-01"}).json()[0]["id"] == fact["id"]
    assert api.get("/facts", params={"valid_at": "2025-12-31"}).json() == []


def test_valid_to_is_exclusive(tmp_path):
    api = client(tmp_path)
    api.post("/facts", json=payload(valid_to="2026-06-01"))
    assert len(api.get("/facts", params={"valid_at": "2026-05-31"}).json()) == 1
    assert api.get("/facts", params={"valid_at": "2026-06-01"}).json() == []


def test_same_idempotency_key_returns_same_fact(tmp_path):
    api = client(tmp_path)
    first = api.post("/facts", json=payload()).json()
    second = api.post("/facts", json=payload()).json()
    assert second["id"] == first["id"]
    assert len(api.get("/facts").json()) == 1


def test_idempotency_key_cannot_change_payload(tmp_path):
    api = client(tmp_path)
    api.post("/facts", json=payload())
    conflict = api.post("/facts", json=payload(value="Java"))
    assert conflict.status_code == 409


def test_conflicting_assertions_are_preserved_not_auto_resolved(tmp_path):
    api = client(tmp_path)
    api.post("/facts", json=payload())
    api.post(
        "/facts",
        json=payload(value="Java", source_ref="conversation:msg-2", idempotency_key="assert-2"),
    )
    values = {row["value"] for row in api.get("/facts").json()}
    assert values == {"Python", "Java"}


def test_retraction_is_append_only_and_applies_from_recorded_time(tmp_path):
    api = client(tmp_path)
    fact = api.post("/facts", json=payload()).json()
    retraction = api.post(
        f"/facts/{fact['id']}/retractions",
        json={"reason": "source corrected", "idempotency_key": "retract-1"},
    )
    assert retraction.status_code == 201
    before = datetime.fromisoformat(fact["recorded_at"]).replace(tzinfo=UTC)
    after = datetime.now(UTC)
    assert len(api.get("/facts", params={"recorded_before": before.isoformat()}).json()) == 1
    assert api.get("/facts", params={"recorded_before": after.isoformat()}).json() == []
    assert [event["event_type"] for event in api.get(f"/facts/{fact['id']}/history").json()] == [
        "assert",
        "retract",
    ]


def test_retract_unknown_fact_is_not_found(tmp_path):
    api = client(tmp_path)
    result = api.post(
        "/facts/does-not-exist/retractions",
        json={"reason": "wrong", "idempotency_key": "retract-missing"},
    )
    assert result.status_code == 404


def test_invalid_time_range_is_rejected(tmp_path):
    result = client(tmp_path).post("/facts", json=payload(valid_to="2025-12-31"))
    assert result.status_code == 422


def test_recorded_before_requires_timezone(tmp_path):
    result = client(tmp_path).get("/facts", params={"recorded_before": "2026-11-20T12:00:00"})
    assert result.status_code == 422
