# K8s Self-Healing AI Framework

An AI-driven control loop that watches a demo application running in Kubernetes,
detects when pods misbehave, and automatically remediates — scaling up, rolling
a deployment, or deleting and recreating a stuck pod. Metrics come from
Prometheus; healing actions go through the Kubernetes API.

## How it works

Every `POLL_INTERVAL_SECONDS` (default 30s) the engine collects per-pod metrics
and runs a three-layer decision pipeline:

| Layer | Component | Role |
|-------|-----------|------|
| 1 | **Z-Score** (`ai/zscore_predictor.py`) | Flags statistical spikes in CPU/memory vs. each pod's own rolling window. |
| 2 | **Isolation Forest** (`ai/isolation_forest_detector.py`) | Unsupervised anomaly detection on **CPU/memory** (continuous resource usage). |
| 2b | **Explicit health gate** (`healing/self_healing_engine.py`) | Deterministic check for discrete faults: `pod_ready == 0` or a restart-count increase since the last poll. |
| 3 | **Random Forest** (`ai/random_forest_classifier.py`) | Given the metrics, classifies **which** action to take, and only acts above `RF_CONFIDENCE_MIN` (70%). |

An action fires only when an anomaly is detected (Layer 1, 2, **or** 2b) **and**
the Random Forest recommends a non-`NO_ACTION` remedy, subject to a per-pod
cooldown. Actions and their outcomes (including MTTR) are appended to
`logs/healing_actions.csv` as a feedback dataset.

> **Why the Isolation Forest only sees CPU/memory:** a binary signal like
> `pod_ready` gets averaged away across many features in a density model, making
> crash detection unreliable. Discrete health faults are therefore gated
> explicitly (Layer 2b) rather than left to the Isolation Forest.

### Healing actions

- `SCALE_UP` — add a replica (up to `MAX_REPLICAS`) for CPU overload.
- `ROLLING_RESTART` — roll the deployment to clear a memory leak.
- `DELETE_AND_RECREATE` — delete a stuck/crashed pod and let Kubernetes recreate it.

## Repository layout

```
config.py                 # Tunables: thresholds, intervals, model paths (env-overridable)
main.py                   # CLI entrypoint: --train | --run | --collect
ai/
  data_collector.py       # Pulls CPU/mem from Prometheus + restarts/ready from the K8s API
  generate_training_data.py
  train_models.py         # Generates synthetic data and trains both models
  zscore_predictor.py
  isolation_forest_detector.py
  random_forest_classifier.py
healing/
  self_healing_engine.py  # The poll → detect → decide → act loop
  kubernetes_actions.py    # scale_up / delete_pod / rolling_restart via the K8s API
  feedback_logger.py       # Writes logs/healing_actions.csv
app/                      # Demo "task-manager" Flask app (the target workload)
  app.py                   # Task CRUD + /health, /ready, /metrics, /stress/* endpoints
  metrics.py               # Prometheus client metrics
  Dockerfile               # gunicorn, 2 workers
kubernetes/               # Manifests, applied in numeric order (01 → 07)
models/                   # Trained .pkl artifacts (git-ignored; regenerate with --train)
```

## Prerequisites

- Python 3.11
- [minikube](https://minikube.sigs.k8s.io/) with the Docker driver
- `kubectl`
- Docker

## Setup

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

### 3. Build the app image *into* minikube

The app deployment uses `image: task-manager:latest` with
`imagePullPolicy: Never`, so the image must exist inside minikube's Docker
daemon:

```bash
eval $(minikube docker-env)     # point docker at minikube
docker build -t task-manager:latest app/
eval $(minikube docker-env -u)  # (optional) revert your shell
```

### 4. Deploy the app + monitoring stack

```bash
kubectl apply -f kubernetes/
```

This creates the `task-manager` Deployment/Service/HPA (namespace `default`),
Prometheus + Grafana (namespace `monitoring`), and the `stress-tester` helper
pod. Wait for everything to be ready:

```bash
kubectl get pods -A -w
```

### 5. Train the AI models

```bash
python main.py --train
```

Writes `models/isolation_forest.pkl` and `models/random_forest.pkl` (both
git-ignored and regenerable).

## Running the engine

The engine reads metrics from Prometheus at `PROMETHEUS_URL` (default
`http://localhost:9090`), so expose Prometheus first:

```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
python main.py --run
```

You should see it load the models, connect to the cluster, and begin polling.
Detected anomalies are logged with the chosen action and its confidence.

## Triggering stress scenarios

The demo app exposes `/stress/*` endpoints that induce faults. Drive them from
the in-cluster `stress-tester` pod:

```bash
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/cpu
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/memory
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/crash
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/not-ready
kubectl exec -n default stress-tester -- curl -s http://task-manager/stress/reset
```

`/stress/crash` and `/stress/not-ready` flip a per-worker flag; since the app
runs 2 gunicorn workers behind the service, call them a few times to flip every
worker before the liveness/readiness probe fails consistently.

Then watch the engine react and the pods recover:

```bash
kubectl get pods -n default -l app=task-manager -w
```

## Observability

On the Docker driver these NodePorts aren't directly routable from the host;
use `minikube service <name> -n monitoring --url` or `kubectl port-forward`.

| Service | NodePort | Notes |
|---------|----------|-------|
| task-manager app | 30080 | REST API + `/metrics` |
| Prometheus | 30090 | |
| Grafana | 30030 | login `admin` / `admin123` |

## CLI reference

```bash
python main.py --train     # Generate data, train models, save .pkl files
python main.py --run       # Start the self-healing engine loop
python main.py --collect   # Print current pod metrics once, then exit
```

## Configuration

`config.py` values are overridable via environment variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROMETHEUS_URL` | `http://localhost:9090` | Where to read metrics |
| `TARGET_NAMESPACE` | `default` | Namespace to watch |
| `TARGET_DEPLOYMENT` | `task-manager` | Deployment to heal |

Non-env tunables in `config.py` include the poll interval, cooldown, max
replicas, z-score window/threshold, Isolation Forest contamination, and the
Random Forest confidence floor.

## Troubleshooting

- **`--run` says "No pods found" / Prometheus errors** — confirm the
  `kubectl port-forward` to Prometheus is active and `minikube status` is
  healthy.
- **App pod `ImagePullBackOff` / `ErrImageNeverPull`** — the image wasn't built
  into minikube's daemon; redo step 3 (`eval $(minikube docker-env)` before
  `docker build`).
- **`stress-tester` `ImagePullBackOff`** — this minikube can't reach Docker Hub;
  run `minikube image load curlimages/curl:8.11.1`, or use a host
  `kubectl port-forward svc/task-manager 8080:80` and curl the endpoints locally.
- **HPA shows `unknown` CPU / `metrics-server` failing** — the AI engine uses
  Prometheus, not `metrics-server`, so healing still works; the HPA is
  independent and optional.
