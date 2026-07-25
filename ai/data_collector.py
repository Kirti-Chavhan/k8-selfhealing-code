import logging
import requests
import pandas as pd
from kubernetes import client, config as k8s_config
from config import PROMETHEUS_URL, TARGET_NAMESPACE, TARGET_DEPLOYMENT

logger = logging.getLogger(__name__)


def _query_prometheus(query):
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={'query': query},
            timeout=5
        )
        resp.raise_for_status()
        return resp.json().get('data', {}).get('result', [])
    except Exception as e:
        logger.warning(f"Prometheus query failed: {e}")
        return []


def _load_k8s():
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()


def _get_k8s_pod_info():
    """Get restart_count and pod_ready for each pod via Kubernetes API."""
    try:
        _load_k8s()
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(
            namespace=TARGET_NAMESPACE,
            label_selector=f"app={TARGET_DEPLOYMENT}"
        )
        info = {}
        for pod in pods.items:
            name = pod.metadata.name
            restarts = 0
            ready = 0

            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    restarts += cs.restart_count

            if pod.status.conditions:
                for cond in pod.status.conditions:
                    if cond.type == 'Ready' and cond.status == 'True':
                        ready = 1

            info[name] = {'restart_count': restarts, 'pod_ready': ready}
        return info
    except Exception as e:
        logger.warning(f"Kubernetes API failed: {e}")
        return {}


MEMORY_LIMIT_BYTES = 256 * 1024 * 1024  # 256Mi as set in deployment YAML


def collect_all_metrics():
    """
    Returns a DataFrame with columns:
      pod_name, cpu_percent, memory_percent, restart_count, pod_ready
    """
    # cadvisor on Kubernetes 1.28+ uses pod= label, not container=
    pod_selector = f'namespace="{TARGET_NAMESPACE}",pod=~"{TARGET_DEPLOYMENT}.*"'

    cpu_q = f'rate(container_cpu_usage_seconds_total{{{pod_selector}}}[2m]) * 100'
    mem_q = f'container_memory_working_set_bytes{{{pod_selector}}}'

    cpu_map = {r['metric'].get('pod', ''): float(r['value'][1])
               for r in _query_prometheus(cpu_q)}
    # Convert raw bytes to percentage of the 256Mi limit
    mem_map = {r['metric'].get('pod', ''): float(r['value'][1]) / MEMORY_LIMIT_BYTES * 100
               for r in _query_prometheus(mem_q)}

    k8s_info = _get_k8s_pod_info()

    rows = []
    for pod_name, info in k8s_info.items():
        rows.append({
            'pod_name':       pod_name,
            'cpu_percent':    round(cpu_map.get(pod_name, 0.0), 2),
            'memory_percent': round(mem_map.get(pod_name, 0.0), 2),
            'restart_count':  info['restart_count'],
            'pod_ready':      info['pod_ready'],
        })

    cols = ['pod_name', 'cpu_percent', 'memory_percent', 'restart_count', 'pod_ready']
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
