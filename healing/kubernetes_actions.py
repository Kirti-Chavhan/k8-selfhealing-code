import logging
from datetime import datetime, timezone
from kubernetes import client, config as k8s_config
from config import TARGET_NAMESPACE, MAX_REPLICAS

logger = logging.getLogger(__name__)


def load_kube_config():
    try:
        k8s_config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except Exception:
        k8s_config.load_kube_config()
        logger.info("Loaded local kubeconfig")


def scale_up_deployment(deployment_name):
    try:
        apps = client.AppsV1Api()
        deploy = apps.read_namespaced_deployment(deployment_name, TARGET_NAMESPACE)
        current = deploy.spec.replicas or 1
        new_count = min(current + 1, MAX_REPLICAS)

        if new_count == current:
            logger.info(f"Already at max replicas ({MAX_REPLICAS}) — skipping scale up")
            return False

        deploy.spec.replicas = new_count
        apps.patch_namespaced_deployment(deployment_name, TARGET_NAMESPACE, deploy)
        logger.info(f"Scaled {deployment_name}: {current} → {new_count} replicas")
        return True
    except Exception as e:
        logger.error(f"scale_up_deployment failed: {e}")
        return False


def delete_pod(pod_name):
    try:
        v1 = client.CoreV1Api()
        v1.delete_namespaced_pod(
            pod_name, TARGET_NAMESPACE,
            body=client.V1DeleteOptions(grace_period_seconds=0)
        )
        logger.info(f"Deleted pod {pod_name}")
        return True
    except Exception as e:
        logger.error(f"delete_pod failed: {e}")
        return False


def rolling_restart_deployment(deployment_name):
    try:
        apps = client.AppsV1Api()
        now = datetime.now(timezone.utc).isoformat()
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now
                        }
                    }
                }
            }
        }
        apps.patch_namespaced_deployment(deployment_name, TARGET_NAMESPACE, patch)
        logger.info(f"Rolling restart triggered for {deployment_name}")
        return True
    except Exception as e:
        logger.error(f"rolling_restart_deployment failed: {e}")
        return False


def get_pod_names_for_deployment(deployment_name):
    try:
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(
            TARGET_NAMESPACE, label_selector=f"app={deployment_name}"
        )
        return [p.metadata.name for p in pods.items]
    except Exception as e:
        logger.error(f"get_pod_names failed: {e}")
        return []
