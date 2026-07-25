import numpy as np
import pandas as pd
from config import ACTION_SCALE_UP, ACTION_DELETE_RECREATE, ACTION_ROLLING_RESTART, ACTION_NO_ACTION


def generate_training_data(n_samples=1000):
    np.random.seed(42)
    n = n_samples // 4

    def make_samples(cpu_mean, cpu_std, mem_mean, mem_std,
                     restart_mean, restart_std, ready, label, count):
        return pd.DataFrame({
            'cpu_percent':    np.clip(np.random.normal(cpu_mean, cpu_std, count), 0, 100),
            'memory_percent': np.clip(np.random.normal(mem_mean, mem_std, count), 0, 100),
            'restart_count':  np.clip(np.random.normal(restart_mean, restart_std, count), 0, 20).astype(int),
            'pod_ready':      [ready] * count,
            'action':         [label] * count
        })

    # CPU overload — scale up more replicas
    scale_up = make_samples(88, 5, 55, 15, 1, 1, 1, ACTION_SCALE_UP, n)

    # Crash loop — delete the stuck pod and let Kubernetes recreate it
    delete_recreate = make_samples(40, 15, 50, 20, 6, 2, 0, ACTION_DELETE_RECREATE, n)

    # Memory pressure — rolling restart clears memory leak
    rolling_restart = make_samples(40, 15, 89, 5, 2, 1, 1, ACTION_ROLLING_RESTART, n)

    # Healthy — realistic idle-to-moderate load, do nothing.
    # Drawn uniformly so the near-idle range (~0% CPU) real pods actually sit in
    # is represented densely, not treated as a low-tail outlier. A single restart
    # on an otherwise-healthy pod is still healthy, so allow restart_count 0 or 1.
    no_action = pd.DataFrame({
        'cpu_percent':    np.round(np.random.uniform(0, 50, n), 2),
        'memory_percent': np.round(np.random.uniform(10, 60, n), 2),
        'restart_count':  np.random.choice([0, 1], size=n, p=[0.85, 0.15]),
        'pod_ready':      [1] * n,
        'action':         [ACTION_NO_ACTION] * n
    })

    df = pd.concat([scale_up, delete_recreate, rolling_restart, no_action], ignore_index=True)
    df = df.sample(frac=1, random_state=42).reset_index(drop=True)
    return df
