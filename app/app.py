import time
import threading
import os
from functools import wraps
from flask import Flask, request, jsonify
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from metrics import (
    REQUEST_COUNT, REQUEST_LATENCY, ACTIVE_REQUESTS,
    ERROR_COUNT, TASKS_CREATED, TASKS_DELETED, TASKS_IN_STORE
)

app = Flask(__name__)

task_store = {}
task_lock = threading.Lock()
task_counter = [0]

_health_ok = True
_ready_ok = True
_stress_thread = None


def track_metrics(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        method = request.method
        endpoint = request.path
        ACTIVE_REQUESTS.inc()
        start = time.time()
        try:
            response = f(*args, **kwargs)
            status = response[1] if isinstance(response, tuple) else 200
            REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=str(status)).inc()
            return response
        except Exception as e:
            ERROR_COUNT.labels(method=method, endpoint=endpoint, error_type=type(e).__name__).inc()
            REQUEST_COUNT.labels(method=method, endpoint=endpoint, status='500').inc()
            raise
        finally:
            REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(time.time() - start)
            ACTIVE_REQUESTS.dec()
    return decorated


# ── Task CRUD ──────────────────────────────────────────────────────────────

@app.route('/tasks', methods=['POST'])
@track_metrics
def create_task():
    data = request.get_json(silent=True) or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'title is required'}), 400
    with task_lock:
        task_counter[0] += 1
        tid = task_counter[0]
        task_store[tid] = {'id': tid, 'title': title, 'done': False}
        TASKS_CREATED.inc()
        TASKS_IN_STORE.set(len(task_store))
    return jsonify(task_store[tid]), 201


@app.route('/tasks', methods=['GET'])
@track_metrics
def list_tasks():
    with task_lock:
        tasks = list(task_store.values())
    return jsonify(tasks), 200


@app.route('/tasks/<int:tid>', methods=['GET'])
@track_metrics
def get_task(tid):
    with task_lock:
        task = task_store.get(tid)
    if not task:
        return jsonify({'error': 'not found'}), 404
    return jsonify(task), 200


@app.route('/tasks/<int:tid>', methods=['PUT'])
@track_metrics
def update_task(tid):
    with task_lock:
        task = task_store.get(tid)
        if not task:
            return jsonify({'error': 'not found'}), 404
        data = request.get_json(silent=True) or {}
        if 'title' in data:
            task['title'] = data['title']
        if 'done' in data:
            task['done'] = bool(data['done'])
    return jsonify(task), 200


@app.route('/tasks/<int:tid>', methods=['DELETE'])
@track_metrics
def delete_task(tid):
    with task_lock:
        task = task_store.pop(tid, None)
        if task is None:
            return jsonify({'error': 'not found'}), 404
        TASKS_DELETED.inc()
        TASKS_IN_STORE.set(len(task_store))
    return jsonify({'deleted': tid}), 200


# ── Health & Readiness ─────────────────────────────────────────────────────

@app.route('/health')
def health():
    if _health_ok:
        return jsonify({'status': 'healthy'}), 200
    return jsonify({'status': 'unhealthy'}), 503


@app.route('/ready')
def ready():
    if _ready_ok:
        return jsonify({'status': 'ready'}), 200
    return jsonify({'status': 'not ready'}), 503


# ── Prometheus metrics ─────────────────────────────────────────────────────

@app.route('/metrics')
def metrics():
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}


# ── Stress endpoints ───────────────────────────────────────────────────────

def _burn_cpu(duration=120):
    end = time.time() + duration
    while time.time() < end:
        _ = [i * i for i in range(10000)]


def _fill_memory(mb=180):
    global _mem_block
    _mem_block = bytearray(mb * 1024 * 1024)
    time.sleep(120)
    del _mem_block


@app.route('/stress/cpu')
def stress_cpu():
    t = threading.Thread(target=_burn_cpu, args=(120,), daemon=True)
    t.start()
    return jsonify({'status': 'CPU stress started for 120s'}), 200


@app.route('/stress/memory')
def stress_memory():
    t = threading.Thread(target=_fill_memory, args=(180,), daemon=True)
    t.start()
    return jsonify({'status': 'Memory stress started — 180 MB allocated for 120s'}), 200


@app.route('/stress/crash')
def stress_crash():
    global _health_ok
    _health_ok = False
    return jsonify({'status': 'Health check will now return 503 — pod will appear crashed'}), 200


@app.route('/stress/not-ready')
def stress_not_ready():
    global _ready_ok
    _ready_ok = False
    return jsonify({'status': 'Readiness check will now return 503 — pod removed from service'}), 200


@app.route('/stress/reset')
def stress_reset():
    global _health_ok, _ready_ok
    _health_ok = True
    _ready_ok = True
    return jsonify({'status': 'All stress cleared — pod is healthy and ready'}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
