# My Task Flow — Kubernetes Self-Healing AI Framework

A hands-on demonstration of **AI-driven self-healing on Kubernetes**. It has two
independent parts that run together:

1. **My Task Flow** — a real web application (a to-do app) that runs as pods in
   Kubernetes. This is the *workload* that gets healed.
2. **The Self-Healing Engine** — a Python control loop that continuously watches
   those pods, uses machine-learning models to detect when something is wrong,
   and automatically fixes it (scale up, rolling restart, or delete-and-recreate).

Metrics come from **Prometheus**; healing actions are performed through the
**Kubernetes API**.

**For the system architecture diagram, the healing-lifecycle sequence diagram,
and the full step-by-step guide to running every service locally (in order, with
expected output at each step) — see [`ARCHITECTURE.md`](ARCHITECTURE.md).** That
document also has the live feature-verification checklist and an explicit list
of what "production ready" does and doesn't cover here.

---

## Table of contents

1. [Repository layout](#repository-layout)
2. [Part A — The application (My Task Flow)](#part-a--the-application-my-task-flow)
3. [Part B — The self-healing engine (summary)](#part-b--the-self-healing-engine-summary)
4. [The AI models](#the-ai-models)
5. [Kubernetes manifests](#kubernetes-manifests)
6. [Quick start](#quick-start)
7. [Using the web app](#using-the-web-app)
8. [Triggering faults (demo scenarios)](#triggering-faults-demo-scenarios)
9. [Testing](#testing)
10. [Demo script (for presenting)](#demo-script-for-presenting)
11. [Configuration](#configuration)
12. [Known limitations and future work](#known-limitations-and-future-work)

---

## Repository layout

```
config.py                     # All tunables (thresholds, intervals, paths); env-overridable
main.py                       # CLI entrypoint: --train | --run | --collect

app/                          # THE WEB APPLICATION ("My Task Flow")
  app.py                      # Flask backend + embedded HTML/CSS/JS frontend (single file)
  metrics.py                  # Prometheus metric definitions used by app.py
  Dockerfile                  # Builds the app image (gunicorn, 1 worker)
  requirements.txt            # App-only deps: flask, gunicorn, prometheus-client
  .dockerignore

ai/                           # DETECTION MODELS + DATA
  data_collector.py           # Reads CPU/mem from Prometheus + restarts/ready from K8s API
  generate_training_data.py   # Creates synthetic labelled training data
  train_models.py             # Trains Isolation Forest + Random Forest, saves .pkl files
  zscore_predictor.py         # Layer 1: rolling z-score spike detector
  isolation_forest_detector.py# Layer 2: unsupervised anomaly detector (CPU/mem only)
  random_forest_classifier.py # Layer 3: chooses which healing action to take

healing/                      # THE ENGINE + ACTIONS
  self_healing_engine.py      # The poll -> detect -> decide -> act loop (the heart)
  kubernetes_actions.py       # scale_up / delete_pod / rolling_restart via K8s API
  feedback_logger.py          # Appends every decision to logs/healing_actions.csv

kubernetes/                   # Cluster manifests, applied in numeric order 01 -> 08
  01-namespace.yaml           # monitoring namespace
  02-app-deployment.yaml      # task-manager Deployment (securityContext hardened) + Service + HPA
  03-prometheus-rbac.yaml     # Prometheus ServiceAccount/ClusterRole
  04-prometheus-config.yaml   # Prometheus scrape config
  05-prometheus-deploy.yaml   # Prometheus Deployment + NodePort Service
  06-grafana-deploy.yaml      # Grafana admin-credentials Secret + Deployment + Service
  07-stress-pod.yaml          # curl helper pod to trigger /stress/* endpoints (securityContext hardened)
  08-grafana-dashboard.yaml   # Self-healing Grafana dashboard, provisioned automatically

tests/                        # pytest suite — Flask API + healing decision logic, no cluster needed
  test_app.py
  test_healing_logic.py

models/                       # Trained artifacts (.pkl) + training_data.csv  [git-ignored, regenerate with --train]
logs/                         # healing_actions.csv feedback log              [git-ignored]
requirements.txt              # Engine deps (scikit-learn, kubernetes, pandas, joblib, requests, pytest)
ARCHITECTURE.md                # Diagrams, full runbook, verification checklist, production-readiness notes
```

---

## Part A — The application (My Task Flow)

**It is a Flask app.** Everything lives in one file: **`app/app.py`** (~490 lines).
The Flask backend serves BOTH the JSON REST API and the HTML page. The frontend
is plain HTML + CSS + vanilla JavaScript embedded as a Python string — there is
no React and no separate frontend server.

### The data model

Each task is a Python dict held in an in-memory store `task_store` (`app.py:15`):

| Field         | Type   | Notes                                       |
|---------------|--------|---------------------------------------------|
| `id`          | int    | Auto-incrementing                           |
| `title`       | string | Required                                    |
| `description` | string | Optional                                    |
| `priority`    | string | `low` \| `medium` \| `high` (default medium)|
| `due_date`    | string | `YYYY-MM-DD`, optional                       |
| `category`    | string | Personal/Professional/Work/... (dropdown)   |
| `done`        | bool   | Completed flag                              |
| `created_at`  | string | UTC ISO timestamp, set on create            |

> Storage is **in-memory** (a dict), so tasks reset when a pod is replaced. This
> is intentional for a stateless demo; see "future work" for adding Redis/DB.

### Backend — the REST API (where each endpoint is written)

| Method & path        | Function        | Location (app/app.py) | What it does                          |
|----------------------|-----------------|-----------------------|---------------------------------------|
| `POST /tasks`        | `create_task`   | lines 48–70           | Add a task (all fields) -> JSON        |
| `GET /tasks`         | `list_tasks`    | lines 73–79           | Return all tasks as JSON array         |
| `GET /tasks/<id>`    | `get_task`      | lines 81–89           | Return one task                        |
| `PUT /tasks/<id>`    | `update_task`   | lines 91–112          | Edit any field(s) / toggle done        |
| `DELETE /tasks/<id>` | `delete_task`   | lines 114–126         | Remove a task                          |
| `GET /health`        | `health`        | line 128              | Liveness probe (200, or 503 if crashed)|
| `GET /ready`         | `ready`         | line 135              | Readiness probe (200, or 503)          |
| `GET /metrics`       | `metrics`       | line 144              | Prometheus metrics (see metrics.py)    |
| `GET /stress/*`      | stress handlers | lines 164–197         | Fault injection (see demo section)     |
| `GET /`              | `index`         | lines 484–489         | Serves the HTML page (the web UI)      |

All `/tasks` endpoints return JSON via Flask's `jsonify`. The API is wrapped with
a `track_metrics` decorator that records request counts/latency for Prometheus.

### Frontend — where the HTML is written

The entire web page is a Python string named **`_PAGE`** starting at
**`app.py:200`** (`_PAGE = """ ... """`). It contains:
- **HTML** — the layout (header, add form, filter bar, task list, edit modal)
- **CSS** — all styling (indigo/violet theme, pills, animations) in a `<style>` block
- **JavaScript** — all interactivity in a `<script>` block near the end

Frontend features (all client-side JavaScript over the API):
- Add tasks with **priority**, **category dropdown** (Personal, Professional,
  Work, Study, Health, Finance, Shopping, Other), **due date**, **description**
- Check off / edit (modal) / delete tasks
- **Multi-select filters**: several priorities AND several categories at once,
  plus status tabs (All/Active/Completed) and a search box — all combine
- **Sort** by Newest / Due date / Priority / A–Z
- Live **progress bar + %**, per-category colored chips, overdue/today highlights
- "Clear filters" and "Clear completed" helpers

### How the frontend and backend are integrated

Two links connect them, both inside `app/app.py`:

**Link 1 — Flask serves the page.** Visiting `/` runs `index()` (`app.py:484`)
which returns the `_PAGE` string as `text/html`. The pod's hostname is injected
in place of the `__POD__` placeholder so the page can show "served by <pod>".

**Link 2 — the page's JavaScript calls the API with `fetch()`** and exchanges
**JSON**:

```
GET  /tasks           load()      app.py:476   -> draw task list
POST /tasks           addTask()   app.py:451   -> create
PUT  /tasks/<id>      toggle()/saveEdit()       -> update
DELETE /tasks/<id>    del()       app.py:456   -> delete
```

Data flow when you use it:

```
Browser opens /           -> Flask returns _PAGE (HTML+CSS+JS)     [index, app.py:484]
Browser runs JS load()    -> fetch('/tasks')                       [app.py:476]
Flask list_tasks()        -> jsonify(all tasks)                    [app.py:73]
JS render()               -> builds the visible cards from JSON
User clicks "Add Task"    -> fetch POST /tasks {title,...}         [app.py:451]
Flask create_task()       -> writes to task_store, returns JSON    [app.py:48]
JS calls load() again     -> re-fetch -> re-render
```

So the **JavaScript is the bridge**: the Python backend only serves raw JSON
data; the browser JavaScript turns that JSON into the visible UI. This is the
classic **REST API + single-page frontend** pattern, both served by one Flask app.

### Packaging (how it becomes a container)

`app/Dockerfile` builds a `python:3.11-slim` image, installs `app/requirements.txt`,
copies `app.py` + `metrics.py`, and runs:

```
gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 --timeout 120 app:app
```

**Why 1 worker:** each gunicorn worker is a separate process with its own
`task_store`. With multiple workers, tasks added on one worker are invisible on
another (they appear to vanish on reload). One worker = one consistent in-memory
store. (Multiple threads still handle concurrency.)

The Deployment uses `image: task-manager:1.0.0` with `imagePullPolicy: Never`,
so the image must be **built into minikube's own Docker daemon** (see
[quick start](#quick-start)) — pinned to a real tag rather than a floating
`:latest`, and running under a non-root `securityContext`.

---

## Part B — The self-healing engine (summary)

Entry point: `python main.py --run` → `SelfHealingEngine.run()`
(`healing/self_healing_engine.py:118`) loops every `POLL_INTERVAL_SECONDS`
(default 30s): collect metrics for every pod → run three detectors (Z-Score,
Isolation Forest, an explicit health gate) → a Random Forest picks the healing
action → act only if some detector flagged an anomaly **and** the classifier
agrees on a confident action **and** the pod isn't in cooldown → log the
decision (and MTTR) to `logs/healing_actions.csv`.

Actions available: `SCALE_UP` (CPU overload), `ROLLING_RESTART` (memory
pressure, zero-downtime), `DELETE_AND_RECREATE` (crash/stuck pod).

**For the full detection pipeline, the decision-gate code, the `starting_up`
guard (and the real bug it fixes), worked examples from live runs, and the
comparison with Kubernetes' own native self-healing — see
[`ARCHITECTURE.md`](ARCHITECTURE.md#healing-lifecycle-sequence-diagram).**

---

## The AI models

- **Training data** is synthetic, generated by `ai/generate_training_data.py`:
  1000 labelled samples across four classes (healthy / CPU overload / memory
  pressure / crash), each with `cpu_percent, memory_percent, restart_count,
  pod_ready` and an `action` label.
- `ai/train_models.py` (run via `python main.py --train`):
  - Trains the **Isolation Forest** on the healthy (`NO_ACTION`) samples,
    CPU/mem only -> `models/isolation_forest.pkl`
  - Trains the **Random Forest** classifier on all four classes ->
    `models/random_forest.pkl`
- The `.pkl` files and `training_data.csv` are **git-ignored** and regenerated by
  `--train`.

---

## Kubernetes manifests

Applied in numeric order with `kubectl apply -f kubernetes/`:

| File | Creates |
|------|---------|
| `01-namespace.yaml` | `monitoring` namespace |
| `02-app-deployment.yaml` | `task-manager` Deployment (non-root `securityContext`, probes, 256Mi/500m limits), NodePort Service (30080), HPA (2–5 replicas, target 70% CPU) |
| `03-prometheus-rbac.yaml` | Prometheus ServiceAccount + ClusterRole/Binding |
| `04-prometheus-config.yaml` | Prometheus scrape configuration (ConfigMap) |
| `05-prometheus-deploy.yaml` | Prometheus Deployment + NodePort Service (30090) |
| `06-grafana-deploy.yaml` | `grafana-admin-credentials` Secret + Grafana Deployment (reads the Secret, no plaintext) + NodePort Service (30030) |
| `07-stress-pod.yaml` | `stress-tester` curl helper pod (non-root `securityContext`) |
| `08-grafana-dashboard.yaml` | Self-healing Grafana dashboard, provisioned automatically |

---

## Quick start

Full step-by-step with expected output at each stage lives in
[`ARCHITECTURE.md`](ARCHITECTURE.md#run-every-service-locally-in-order). Short
version:

```bash
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

minikube start

eval "$(minikube docker-env)"
docker build -t task-manager:1.0.0 app/
eval "$(minikube docker-env -u)"

kubectl apply -f kubernetes/
kubectl get pods -A -w        # wait for everything Running/Ready

python main.py --train

kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
python main.py --run

kubectl port-forward -n default svc/task-manager 8080:80 &
# open http://localhost:8080
```

---

## Using the web app

Open **http://localhost:8080**. Add tasks with a title, priority, category,
due date, and description. Check them off, edit them (pencil -> modal), delete
them. Combine filters (multiple priorities + categories + status + search) and
sort. The header shows live completion %. The footer shows which pod served you.

---

## Triggering faults (demo scenarios)

The app exposes `/stress/*` endpoints. Drive them from the in-cluster helper pod:

```bash
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/cpu
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/memory
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/crash
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/not-ready
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/reset
```

Watch the engine react and pods recover:
```bash
kubectl get pods -n default -l app=task-manager -w
tail -f logs/healing_actions.csv
```

What to expect (all verified live — see the checklist in
[`ARCHITECTURE.md`](ARCHITECTURE.md#feature-verification-checklist)):
- **memory** -> ~90% memory -> Isolation Forest flags it -> `ROLLING_RESTART`.
- **crash** -> `/health` 503 -> liveness restart + engine `DELETE_AND_RECREATE`.
- **cpu** -> capped at ~50% by the 500m CPU limit, so the AI `SCALE_UP` path may
  not trigger; the native **HPA** scales instead. (Raise the CPU limit to make
  the AI `SCALE_UP` demonstrable.)

Note: because gunicorn runs **1 worker**, `crash`/`not-ready` flip the state
in a single call (no need to repeat).

---

## Testing

```bash
python -m pytest -v
```

`tests/test_app.py` covers the Flask API (CRUD, health/ready/stress/reset,
metrics content-type) via Flask's test client — no cluster needed.
`tests/test_healing_logic.py` covers the Z-Score predictor and the engine's
decision gate (should-act, cooldown, the not-ready/`starting_up` guards) with
the ML models and Kubernetes API stubbed out — no trained `.pkl` files or live
cluster needed either.

---

## Demo script (for presenting)

A ~5-minute walkthrough that shows the app **and** the self-healing.

**Prep (before the audience):**
```bash
# 3 port-forwards + the engine (each in its own terminal so logs are visible)
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
kubectl port-forward -n default    svc/task-manager 8080:80 &
kubectl port-forward -n monitoring svc/grafana      3000:3000 &
python main.py --run          # engine terminal (keep visible)
```
Open three tabs: the app (`http://localhost:8080`), the Grafana dashboard
(`http://localhost:3000/d/selfhealing`, admin + your password), and the engine
terminal. Keep a spare terminal for stress commands and `kubectl` watch.

**Script:**
1. **Show the app.** "This is *My Task Flow*, a Flask app running as pods in
   Kubernetes." Add a task, complete one, use the filters. Point at the footer:
   "served by `<pod-name>`".
2. **Show the engine.** "A separate AI engine polls those pods every 30s." Point
   at `Collected metrics for N pod(s)`.
3. **Show Grafana.** Per-pod CPU/memory, running-pods count, request rate — all
   flat/healthy right now.
4. **Break it (memory):**
   ```bash
   kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/memory
   kubectl get pods -n default -l app=task-manager -w   # in another pane
   ```
5. **Narrate the heal (~30–60s):** the engine logs
   `ALERT ... ROLLING_RESTART`; the Grafana **memory panel spikes then drops**;
   pods **roll to new names with zero downtime**; the app stays usable; reload it
   and the "served by" pod has changed — proof it recovered.
6. **Show the evidence:** `tail -f logs/healing_actions.csv` — the row with
   `if_anomaly`, `rf_action=ROLLING_RESTART`, confidence and `mttr_seconds`.
7. **(Optional) Crash:**
   `kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/crash`
   → the engine's **health gate** fires `DELETE_AND_RECREATE`; the pod is
   recreated. Note CPU/mem stay normal here — this fault is caught by the health
   gate, not the models.
8. **Wrap up:** recap the three detectors + the two-key decision gate, and that
   the whole thing runs unattended.

**Reset between runs:**
```bash
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/reset
```

---

## Configuration

`config.py` — key values (all `PROMETHEUS_URL`/`TARGET_*` are env-overridable):

| Setting | Default | Meaning |
|---------|---------|---------|
| `PROMETHEUS_URL` | `http://localhost:9090` | Where the engine reads metrics |
| `TARGET_NAMESPACE` | `default` | Namespace to watch |
| `TARGET_DEPLOYMENT` | `task-manager` | Deployment to heal |
| `POLL_INTERVAL_SECONDS` | `30` | Loop interval |
| `COOLDOWN_SECONDS` | `180` | Per-pod cooldown after an action |
| `MAX_REPLICAS` | `5` | Cap for SCALE_UP |
| `ZSCORE_WINDOW_SIZE` / `ZSCORE_THRESHOLD` | `20` / `2.5` | Z-score detector |
| `IF_CONTAMINATION` / `IF_N_ESTIMATORS` | `0.05` / `100` | Isolation Forest |
| `RF_N_ESTIMATORS` / `RF_CONFIDENCE_MIN` | `100` / `0.70` | Random Forest + confidence floor |

---

## Known limitations and future work

- **In-memory task store** — tasks reset when a pod is replaced. Add Redis or a
  database (e.g. Postgres) for persistence across heals.
- **AI `SCALE_UP` vs. the 500m CPU limit** — CPU % is capped near 50%, below the
  model's overload profile, so the AI scale-up path rarely triggers; the native
  HPA handles CPU scaling. Raise the container CPU limit or align the metric to
  demo the AI path.
- **Single-file frontend** — the HTML/CSS/JS lives in a string in `app/app.py`.
  Splitting into `templates/`/`static/` (or a separate SPA) would be cleaner.
- **Synthetic training data** — replace with the real `logs/healing_actions.csv`
  feedback to close the learning loop.
- **Engine runs as a host process with your local kubeconfig** rather than
  in-cluster with its own least-privilege ServiceAccount — see
  [`ARCHITECTURE.md`](ARCHITECTURE.md#what-production-ready-means-here--and-what-it-deliberately-excludes)
  for why that's out of scope for now.
