from collections import deque
import numpy as np
from config import ZSCORE_WINDOW_SIZE, ZSCORE_THRESHOLD


class ZScorePredictor:
    def __init__(self):
        self.windows = {}  # {pod: {metric: deque}}
        self.min_samples = 5

    def update_and_check(self, pod, metric_name, value):
        if pod not in self.windows:
            self.windows[pod] = {}
        if metric_name not in self.windows[pod]:
            self.windows[pod][metric_name] = deque(maxlen=ZSCORE_WINDOW_SIZE)

        window = self.windows[pod][metric_name]
        window.append(value)

        if len(window) < self.min_samples:
            return 0.0, False

        arr = np.array(window)
        mean = np.mean(arr)
        std = np.std(arr)

        if std < 0.001:
            return 0.0, False

        z_score = (value - mean) / std
        is_warning = abs(z_score) > ZSCORE_THRESHOLD
        return round(z_score, 3), is_warning

    def check_all_metrics(self, pod, cpu, memory):
        warnings = []

        z_cpu, warn_cpu = self.update_and_check(pod, 'cpu_percent', cpu)
        if warn_cpu:
            warnings.append({'metric': 'cpu_percent', 'z_score': z_cpu, 'value': cpu})

        z_mem, warn_mem = self.update_and_check(pod, 'memory_percent', memory)
        if warn_mem:
            warnings.append({'metric': 'memory_percent', 'z_score': z_mem, 'value': memory})

        return warnings
