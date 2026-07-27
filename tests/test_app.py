"""Flask test-client coverage for app/app.py — no cluster required."""
import importlib.util
import os
import sys

_APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)  # so app.py's `from metrics import ...` resolves

_spec = importlib.util.spec_from_file_location(
    "task_manager_app", os.path.join(_APP_DIR, "app.py")
)
app_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app_module)

import pytest


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    app_module.task_store.clear()
    app_module.task_counter[0] = 0
    app_module._health_ok = True
    app_module._ready_ok = True
    with app_module.app.test_client() as c:
        yield c


def test_create_task_requires_title(client):
    resp = client.post("/tasks", json={})
    assert resp.status_code == 400


def test_create_and_list_task(client):
    resp = client.post("/tasks", json={"title": "Buy milk", "priority": "high"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["title"] == "Buy milk"
    assert body["priority"] == "high"
    assert body["done"] is False

    resp = client.get("/tasks")
    assert resp.status_code == 200
    tasks = resp.get_json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Buy milk"


def test_get_task_not_found(client):
    resp = client.get("/tasks/999")
    assert resp.status_code == 404


def test_update_task(client):
    created = client.post("/tasks", json={"title": "Original"}).get_json()
    tid = created["id"]
    resp = client.put(f"/tasks/{tid}", json={"done": True, "title": "Updated"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["done"] is True
    assert body["title"] == "Updated"


def test_update_task_not_found(client):
    resp = client.put("/tasks/999", json={"done": True})
    assert resp.status_code == 404


def test_delete_task(client):
    created = client.post("/tasks", json={"title": "Temp"}).get_json()
    tid = created["id"]
    resp = client.delete(f"/tasks/{tid}")
    assert resp.status_code == 200
    resp = client.get(f"/tasks/{tid}")
    assert resp.status_code == 404


def test_invalid_priority_defaults_to_medium(client):
    resp = client.post("/tasks", json={"title": "X", "priority": "urgent"})
    assert resp.get_json()["priority"] == "medium"


def test_health_and_ready_default_ok(client):
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200


def test_stress_crash_and_reset(client):
    assert client.get("/stress/crash").status_code == 200
    assert client.get("/health").status_code == 503

    assert client.get("/stress/reset").status_code == 200
    assert client.get("/health").status_code == 200


def test_stress_not_ready_and_reset(client):
    assert client.get("/stress/not-ready").status_code == 200
    assert client.get("/ready").status_code == 503

    client.get("/stress/reset")
    assert client.get("/ready").status_code == 200


def test_metrics_endpoint_content_type(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.content_type


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"My Task Flow" in resp.data
