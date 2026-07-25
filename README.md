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

This README is written as a **handover document**: it explains what the code
does, how each part works, exactly *where* each thing is written (file and line),
and the reasoning behind the key design decisions.

---

## Table of contents

1. [Architecture at a glance](#architecture-at-a-glance)
2. [Repository layout](#repository-layout)
3. [Part A — The application (My Task Flow)](#part-a--the-application-my-task-flow)
4. [Part B — The self-healing engine](#part-b--the-self-healing-engine)
5. [Self-healing in depth](#self-healing-in-depth)
5. [The AI models](#the-ai-models)
6. [Kubernetes manifests](#kubernetes-manifests)
7. [Setup and run (end to end)](#setup-and-run-end-to-end)
8. [Using the web app](#using-the-web-app)
9. [Triggering faults (demo scenarios)](#triggering-faults-demo-scenarios)
10. [Observability](#observability)
11. [Configuration](#configuration)
12. [Environment notes (corporate network / Zscaler)](#environment-notes-corporate-network--zscaler)
13. [Troubleshooting](#troubleshooting)
14. [Known limitations and future work](#known-limitations-and-future-work)

---

## Architecture at a glance

```
                          YOUR MACHINE (host)
  ┌──────────────────────────────────────────────────────────────┐
  │  Browser  ──HTTP──►  http://localhost:8080  (kubectl port-fwd)│
  │                                                               │
  │  Self-Healing Engine  (python main.py --run)                  │
  │     reads metrics ◄── http://localhost:9090 (Prometheus pf)   │
  │     sends actions ──► Kubernetes API (kubeconfig)             │
  └───────────────┬───────────────────────────────┬──────────────┘
                  │                                │
                  ▼                                ▼
                       minikube CLUSTER
  ┌──────────────────────────────────────────────────────────────┐
  │  namespace: default                                           │
  │     Deployment task-manager  (2+ pods, "My Task Flow" app)    │
  │     Service task-manager (ClusterIP/NodePort)                 │
  │     HorizontalPodAutoscaler (native k8s CPU autoscaling)      │
  │     Pod stress-tester (helper to trigger faults)              │
  │                                                               │
  │  namespace: monitoring                                        │
  │     Prometheus  (scrapes pod CPU/mem via cAdvisor)            │
  │     Grafana     (dashboards)                                  │
  └──────────────────────────────────────────────────────────────┘
```

Two control loops act on the workload:
- **The AI engine** (this project) — detects anomalies and heals.
- **Kubernetes itself** — liveness/readiness probes restart/again-route pods, and
  the HPA scales on CPU. The AI engine is designed to *complement* these, not
  fight them (see the health-gate design note below).

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

kubernetes/                   # Cluster manifests, applied in numeric order 01 -> 07
  01-namespace.yaml           # monitoring namespace
  02-app-deployment.yaml      # task-manager Deployment + Service + HPA
  03-prometheus-rbac.yaml     # Prometheus ServiceAccount/ClusterRole
  04-prometheus-config.yaml   # Prometheus scrape config
  05-prometheus-deploy.yaml   # Prometheus Deployment + NodePort Service
  06-grafana-deploy.yaml      # Grafana Deployment + NodePort Service
  07-stress-pod.yaml          # curl helper pod to trigger /stress/* endpoints

models/                       # Trained artifacts (.pkl) + training_data.csv  [git-ignored, regenerate with --train]
logs/                         # healing_actions.csv feedback log              [git-ignored]
requirements.txt              # Engine deps (scikit-learn, kubernetes, pandas, flask, ...)
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

The Deployment uses `image: task-manager:latest` with `imagePullPolicy: Never`,
so the image must be **built into minikube's own Docker daemon** (see setup).

---

## Part B — The self-healing engine

Entry point: `python main.py --run` -> constructs `SelfHealingEngine`
(`healing/self_healing_engine.py:24`) and calls `.run()`.

### The control loop

`SelfHealingEngine.run()` (`self_healing_engine.py:118`) loops forever, every
`POLL_INTERVAL_SECONDS` (default 30):

1. `collect_all_metrics()` gathers per-pod metrics.
2. For each pod, `process_pod(row)` runs the detection pipeline and, if needed,
   executes a healing action.
3. Sleeps and repeats.

### Metric collection

`ai/data_collector.py` builds one row per pod with:
`pod_name, cpu_percent, memory_percent, restart_count, pod_ready`.

- **CPU / memory** come from **Prometheus** (cAdvisor metrics):
  `rate(container_cpu_usage_seconds_total[2m]) * 100` and
  `container_memory_working_set_bytes` (converted to % of the 256Mi limit).
- **restart_count / pod_ready** come from the **Kubernetes API** directly.

### The detection pipeline (in `process_pod`, `self_healing_engine.py:53`)

| Layer | Where | What it decides |
|-------|-------|-----------------|
| **1. Z-Score** (`ai/zscore_predictor.py`) | called at line 60 | Is CPU/mem a spike vs. this pod's own recent history? Sets `zscore_warn`. |
| **2. Isolation Forest** (`ai/isolation_forest_detector.py`) | line 67 `iso.predict(cpu, memory)` | Is resource usage anomalous? Sets `if_anomaly`. **CPU/mem only.** |
| **2b. Explicit health gate** | lines 73–86 | Deterministic fault check: a restart-count **increase** since last poll (`restart_spike`), OR a pod that **regressed** from Ready to not-ready (`not_ready_regression`). Sets `health_degraded`. |
| **3. Random Forest** (`ai/random_forest_classifier.py`) | line 88 `rf.predict(...)` | Which action fits? Returns `rf_action` + confidence. |

### The decision rule (`self_healing_engine.py:92`)

```python
should_act = zscore_warn or if_anomaly or health_degraded
if should_act and rf_action != NO_ACTION and not in_cooldown(pod):
    execute(rf_action, pod)
```

An action fires only when **(some detector flags an anomaly) AND (the Random
Forest recommends a real action, above RF_CONFIDENCE_MIN) AND (the pod is not in
cooldown)**. This "two-key" design stops a single noisy signal from acting alone.

### Healing actions (`healing/kubernetes_actions.py`)

| Action                | Function                     | Effect |
|-----------------------|------------------------------|--------|
| `SCALE_UP`            | `scale_up_deployment`        | +1 replica up to `MAX_REPLICAS` (CPU overload) |
| `ROLLING_RESTART`     | `rolling_restart_deployment` | Patches a restart annotation -> zero-downtime roll (memory leak) |
| `DELETE_AND_RECREATE` | `delete_pod`                 | Deletes the pod; the Deployment recreates it (crash/stuck) |

After acting, the pod enters a `COOLDOWN_SECONDS` (default 180s) window so the
engine does not repeatedly hit the same pod.

### Feedback log

Every poll, `healing/feedback_logger.py` appends one row per pod to
`logs/healing_actions.csv`. Columns:

```
timestamp, pod_name, cpu_percent, memory_percent, restart_count, pod_ready,
zscore_cpu, zscore_memory, zscore_warning, if_anomaly, if_score,
rf_action, rf_confidence, action_taken, mttr_seconds
```

This is both an audit trail and a dataset for future model retraining.

### Key design decisions (the "why")

**1. The Isolation Forest only looks at CPU/memory.**
Originally it was fed all four features (incl. `pod_ready`, `restart_count`) and
flagged *every* pod as anomalous. Two reasons: (a) the healthy training data
centered CPU around 35% while real pods idle near 0%, so idle pods looked like
outliers; (b) a single binary signal like `pod_ready` gets **averaged away**
across four features in a density model — a grid search showed crash detection
capped around ~54% (a coin flip). Fix: the Isolation Forest now models only the
continuous resource metrics it is actually good at (CPU/mem), and the healthy
training class was widened to cover the realistic idle->moderate range.

**2. Discrete health faults are gated explicitly (Layer 2b), not by ML.**
`pod_ready == 0` and restart spikes are deterministic, so they are checked with
plain logic instead of a model.

**3. Not-ready is only a fault after a pod has *been* ready.**
`self_healing_engine.py:84-86` tracks a `seen_ready` set. A brand-new pod that is
still starting up (e.g. one the HPA just created) is not-ready but has never been
ready — treating that as a crash caused the engine to **delete healthy starting
pods and fight the autoscaler**. Now not-ready only counts as `health_degraded`
once the pod has regressed from a previously-Ready state; a pod that crash-loops
from birth is still caught by the restart-count spike.

---

## Self-healing in depth

Self-healing is the core of this project: the system **observes** the workload,
**detects** abnormal behaviour on its own, **decides** the right remedy, **acts**
through the Kubernetes API, and **records** the outcome — all without a human in
the loop. The goal is to minimise **MTTR** (mean time to recovery).

### The healing lifecycle (one full cycle, every 30s)

```
        ┌────────────┐   Prometheus (CPU/mem)  +  K8s API (restarts/ready)
        │  OBSERVE   │◄───────────────────────────────────────────────────┐
        └─────┬──────┘                                                     │
              ▼                                                            │
        ┌────────────┐   Layer 1 Z-Score  ─┐                              │
        │   DETECT   │   Layer 2 Iso.Forest ├─► anomaly?                   │
        │            │   Layer 2b Health gate┘                            │
        └─────┬──────┘                                                     │
              ▼                                                            │
        ┌────────────┐   Random Forest picks an action (>= 70% confidence) │
        │   DECIDE   │   act only if: anomaly AND action != NO_ACTION      │
        │            │                AND pod not in cooldown              │
        └─────┬──────┘                                                     │
              ▼                                                            │
        ┌────────────┐   SCALE_UP | ROLLING_RESTART | DELETE_AND_RECREATE  │
        │    ACT     │   via the Kubernetes API                            │
        └─────┬──────┘                                                     │
              ▼                                                            │
        ┌────────────┐   append row to logs/healing_actions.csv (+ MTTR)   │
        │  LOG/LEARN │───────────────────────────────────────────────────►┘
        └────────────┘   (feedback dataset for future retraining)
```

Code path: `main.py --run` -> `SelfHealingEngine.run()`
(`healing/self_healing_engine.py:118`) -> for each pod `process_pod(row)`
(`self_healing_engine.py:53`).

### Why three detectors plus a rule (defence in depth)

No single detector catches every failure mode, so the engine combines
complementary signals. Each answers a different question:

**Layer 1 — Z-Score (`ai/zscore_predictor.py`)**
Keeps a rolling window (size `ZSCORE_WINDOW_SIZE`, default 20) of each pod's CPU
and memory. For a new reading `x` it computes `z = (x - mean) / std`; if
`|z| > ZSCORE_THRESHOLD` (default 2.5) it raises a warning. It detects a **spike
relative to that pod's own recent baseline** — e.g. a pod that normally sits at
5% CPU suddenly jumping to 45%. It reacts fast and is pod-specific, but needs a
few samples of history to be meaningful.

**Layer 2 — Isolation Forest (`ai/isolation_forest_detector.py`)**
An unsupervised anomaly detector trained only on *healthy* samples. It isolates
outliers by random partitioning (fewer splits to isolate = more anomalous), with
`IF_CONTAMINATION` (0.05) setting the outlier threshold. It detects **absolute
resource anomalies** — e.g. memory at 90% is abnormal regardless of the pod's
own history. It looks at **CPU and memory only** (see design note below).

**Layer 2b — Explicit health gate (`self_healing_engine.py:73-86`)**
A deterministic rule, not a model. It fires on:
- `restart_spike` — `restart_count` increased since the previous poll (the
  container was restarted, e.g. by a failing liveness probe), or
- `not_ready_regression` — the pod was Ready before and is now not-ready.

These are the crash / hang / stuck signals that do **not** show up in CPU/mem.

**Layer 3 — Random Forest (`ai/random_forest_classifier.py`)**
A supervised classifier trained on four labelled classes (healthy, CPU overload,
memory pressure, crash). Given `cpu, memory, restart_count, pod_ready` it outputs
**which action** to take and a **confidence**. It only counts if confidence
>= `RF_CONFIDENCE_MIN` (0.70).

### The decision gate (`self_healing_engine.py:92`)

```python
should_act = zscore_warn or if_anomaly or health_degraded      # "is something wrong?"
if should_act and rf_action != NO_ACTION and not in_cooldown:  # "and what do we do?"
    execute(rf_action, pod)
```

This is a deliberate **two-key rule**: one set of signals decides *that* a pod is
unhealthy (any of Z-Score / Isolation Forest / health gate), and a separate model
decides *what* to do (Random Forest). A single noisy reading cannot cause an
action on its own — the classifier must also agree with a concrete, confident
remedy. The per-pod `COOLDOWN_SECONDS` (180s) then prevents repeated action on
the same pod while a fix takes effect.

### The three healing actions (`healing/kubernetes_actions.py`)

| Action | Trigger (typical) | What the engine does | K8s effect |
|--------|-------------------|----------------------|-----------|
| **SCALE_UP** | Sustained high CPU | `scale_up_deployment()` patches `spec.replicas` +1 (capped at `MAX_REPLICAS`) | A new replica is scheduled to share load |
| **ROLLING_RESTART** | Memory pressure / leak | `rolling_restart_deployment()` patches a `kubectl.kubernetes.io/restartedAt` annotation on the pod template | Deployment does a **zero-downtime** rolling update (`maxUnavailable: 0, maxSurge: 1`) — new pods come up before old ones go |
| **DELETE_AND_RECREATE** | Crash / stuck / not-ready | `delete_pod()` deletes the pod (grace period 0) | The Deployment's ReplicaSet immediately recreates a fresh pod |

### MTTR and the feedback loop

When an action succeeds, the engine records **MTTR** (time to execute the
remedy) and appends a full row to `logs/healing_actions.csv`
(`healing/feedback_logger.py`). Every poll logs one row per pod — including
`NO_ACTION` — so the CSV is both an audit trail and a labelled dataset that can be
fed back into `--train` to improve the models over time.

### Worked examples (from real demo runs)

**1) Memory pressure -> ROLLING_RESTART.** A pod's memory was driven to ~90%.
The Isolation Forest flagged it; the Random Forest chose `ROLLING_RESTART`; the
healthy sibling pod was untouched:
```
pod    cpu    mem     rst rdy  if_anomaly  rf_action        conf  taken            mttr
q2t25  0.32   90.41   1   1    True        ROLLING_RESTART  0.96  ROLLING_RESTART  0.02s   <- healed
fp6m2  0.20   20.58   0   1    False       NO_ACTION        1.00  NO_ACTION        -       <- left alone
```
New pods from a fresh ReplicaSet replaced the stressed pods with **zero
downtime**.

**2) Crash -> DELETE_AND_RECREATE.** `/health` was forced to 503. Kubernetes'
liveness probe restarted the container (restart_count 0->1, briefly not-ready).
Note the resource metrics are **normal** — the Isolation Forest correctly stayed
silent; the **health gate** drove the action:
```
pod    cpu    mem     rst rdy  if_anomaly  rf_action            conf  taken                mttr
7jdnp  0.21   20.59   1   0    False       DELETE_AND_RECREATE  0.95  DELETE_AND_RECREATE  0.02s
```

**3) CPU overload -> HPA (not the AI).** Under CPU stress the pod's `cpu_percent`
plateaued near ~46% because the container's CPU **limit is 500m** (`rate*100`
caps around 50%), which is below the Random Forest's overload profile (~88%). So
the AI `SCALE_UP` did not fire; the native **HorizontalPodAutoscaler** scaled the
Deployment 2 -> 5 instead. To demo the AI scale-up path, raise the CPU limit so
`cpu_percent` can exceed the overload threshold.

### AI self-healing vs. Kubernetes' native self-healing

Kubernetes already self-heals in basic ways; this engine **adds a layer on top**:

| Concern | Kubernetes native | This AI engine adds |
|---------|-------------------|---------------------|
| Dead container | Liveness probe restarts it | Detects the restart and can escalate to `DELETE_AND_RECREATE` |
| Not-ready pod | Removed from Service endpoints | Treats a *regression* to not-ready as a fault to remediate |
| High CPU | HPA scales on CPU vs. request | Multi-signal detection; chooses among several remedies |
| Memory leak | (no native remedy) | `ROLLING_RESTART` to clear it before OOM |
| Which fix to apply | n/a | Random Forest classifier picks the action |
| Audit / MTTR / learning | limited | Full CSV log + feedback dataset |

Because both loops act on the same pods, the engine is intentionally designed
**not to fight** Kubernetes — e.g. it does not treat freshly-created (still
starting) pods as crashed (see the health-gate design note).

### Tuning the healing behaviour (`config.py`)

| Knob | Effect on healing |
|------|-------------------|
| `POLL_INTERVAL_SECONDS` | How quickly faults are detected (lower = faster, noisier) |
| `COOLDOWN_SECONDS` | Minimum gap between actions on the same pod |
| `ZSCORE_THRESHOLD` / `ZSCORE_WINDOW_SIZE` | Sensitivity of the spike detector |
| `IF_CONTAMINATION` | How aggressively the Isolation Forest flags anomalies |
| `RF_CONFIDENCE_MIN` | Minimum confidence before an action is taken |
| `MAX_REPLICAS` | Upper bound for `SCALE_UP` |


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
| `02-app-deployment.yaml` | `task-manager` Deployment (probes, 256Mi/500m limits), NodePort Service (30080), HPA (2–5 replicas, target 70% CPU) |
| `03-prometheus-rbac.yaml` | Prometheus ServiceAccount + ClusterRole/Binding |
| `04-prometheus-config.yaml` | Prometheus scrape configuration (ConfigMap) |
| `05-prometheus-deploy.yaml` | Prometheus Deployment + NodePort Service (30090) |
| `06-grafana-deploy.yaml` | Grafana Deployment + NodePort Service (30030), admin/admin123 |
| `07-stress-pod.yaml` | `stress-tester` curl helper pod |

---

## Setup and run (end to end)

### Prerequisites
Python 3.11, minikube (Docker driver), kubectl, Docker.

### 1. Python environment
```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Start the cluster
```bash
minikube start
```

### 3. Build the app image INTO minikube
The Deployment uses `imagePullPolicy: Never`, so the image must live in
minikube's Docker daemon:
```bash
eval "$(minikube docker-env)"        # point docker CLI at minikube
docker build -t task-manager:latest app/
eval "$(minikube docker-env -u)"     # (optional) revert your shell
```
Rebuild + roll after any change to `app/`:
```bash
eval "$(minikube docker-env)"; docker build -t task-manager:latest app/
kubectl rollout restart deployment/task-manager -n default
```

### 4. Deploy everything
```bash
kubectl apply -f kubernetes/
kubectl get pods -A -w        # wait until all are Running/Ready
```

### 5. Train the models
```bash
python main.py --train
```

### 6. Run the engine (needs Prometheus reachable)
```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
python main.py --run
```

### 7. Open the web app
```bash
kubectl port-forward -n default svc/task-manager 8080:80 &
# then open http://localhost:8080
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

What to expect:
- **memory** -> ~90% memory -> Isolation Forest flags it -> `ROLLING_RESTART`.
- **crash** -> `/health` 503 -> liveness restart + engine `DELETE_AND_RECREATE`.
- **cpu** -> capped at ~50% by the 500m CPU limit, so the AI `SCALE_UP` path may
  not trigger; the native **HPA** scales instead. (Raise the CPU limit to make
  the AI `SCALE_UP` demonstrable.)

Note: because gunicorn now runs **1 worker**, `crash`/`not-ready` flip the state
in a single call (no need to repeat).

---

## Observability

On the Docker driver these NodePorts are not directly routable from the host;
use `kubectl port-forward` (or `minikube service <name> -n monitoring --url`).

| Service | NodePort | Access |
|---------|----------|--------|
| task-manager (web app) | 30080 | `kubectl port-forward -n default svc/task-manager 8080:80` |
| Prometheus | 30090 | `kubectl port-forward -n monitoring svc/prometheus 9090:9090` |
| Grafana | 30030 | `kubectl port-forward -n monitoring svc/grafana 3000:3000` (admin/admin123) |

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

## Environment notes (corporate network / Zscaler)

This project was set up on a corporate machine behind **Zscaler** (a TLS-
intercepting proxy). Two consequences worth knowing for a fresh setup:

1. **The minikube node could not pull `registry.k8s.io` images** (e.g.
   `metrics-server`) — error: `x509: certificate signed by unknown authority`.
   Zscaler re-signs HTTPS with its own root CA, which the minikube node did not
   trust.
   **Durable fix applied:** export the Zscaler Root CA from the macOS keychain to
   `~/.minikube/certs/` and reprovision so minikube installs it into the node:
   ```bash
   security find-certificate -a -c "Zscaler" -p /Library/Keychains/System.keychain \
     > ~/.minikube/certs/zscaler-root-ca.pem
   minikube start   # installs certs from ~/.minikube/certs into the node
   ```
   minikube re-syncs that folder on every `minikube start`, so it stays fixed.
   After this, `registry.k8s.io` / Docker Hub / gcr.io pulls all work.

2. The AI engine itself does **not** depend on `metrics-server` (it uses
   Prometheus). `metrics-server` only powers `kubectl top` and the HPA.

---

## Troubleshooting

- **`--run` says "No pods found" / Prometheus errors** — ensure the Prometheus
  `kubectl port-forward` is running and `minikube status` is healthy.
- **App pod `ErrImageNeverPull` / `ImagePullBackOff`** — the image is not in
  minikube's Docker daemon; redo `eval "$(minikube docker-env)"; docker build`.
- **Tasks vanish on reload** — a symptom of multiple gunicorn workers; the
  Dockerfile is set to `--workers 1` to prevent this. Rebuild if you changed it.
- **Web page pod name never changes on reload** — `kubectl port-forward svc/...`
  pins to one pod; it changes when that pod is actually replaced by a heal. If a
  heal deletes the pinned pod, re-run the port-forward.
- **`metrics-server` `ImagePullBackOff`** — see the Zscaler note above.
- **HPA shows `unknown` CPU** — `metrics-server` isn't ready; healing still works
  regardless (engine uses Prometheus).

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
