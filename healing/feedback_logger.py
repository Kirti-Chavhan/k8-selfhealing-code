import csv
import os
import logging
from datetime import datetime
from config import FEEDBACK_LOG_PATH

logger = logging.getLogger(__name__)

FIELDS = [
    'timestamp', 'pod_name', 'cpu_percent', 'memory_percent',
    'restart_count', 'pod_ready',
    'zscore_cpu', 'zscore_memory', 'zscore_warning',
    'if_anomaly', 'if_score',
    'rf_action', 'rf_confidence',
    'action_taken', 'mttr_seconds'
]


class FeedbackLogger:
    def __init__(self):
        os.makedirs(os.path.dirname(FEEDBACK_LOG_PATH), exist_ok=True)
        if not os.path.exists(FEEDBACK_LOG_PATH):
            with open(FEEDBACK_LOG_PATH, 'w', newline='') as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def log(self, pod_name, cpu, memory, restart_count, pod_ready,
            zscore_cpu, zscore_memory, zscore_warning,
            if_anomaly, if_score, rf_action, rf_confidence,
            action_taken, mttr_seconds=None):
        row = {
            'timestamp':      datetime.now().isoformat(),
            'pod_name':       pod_name,
            'cpu_percent':    cpu,
            'memory_percent': memory,
            'restart_count':  restart_count,
            'pod_ready':      pod_ready,
            'zscore_cpu':     zscore_cpu,
            'zscore_memory':  zscore_memory,
            'zscore_warning': zscore_warning,
            'if_anomaly':     if_anomaly,
            'if_score':       if_score,
            'rf_action':      rf_action,
            'rf_confidence':  rf_confidence,
            'action_taken':   action_taken,
            'mttr_seconds':   mttr_seconds or '',
        }
        try:
            with open(FEEDBACK_LOG_PATH, 'a', newline='') as f:
                csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
        except Exception as e:
            logger.error(f"FeedbackLogger write failed: {e}")
