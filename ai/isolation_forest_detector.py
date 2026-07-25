import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from config import IF_CONTAMINATION, IF_N_ESTIMATORS, IF_MODEL_PATH, ACTION_NO_ACTION


class IsolationForestDetector:
    def __init__(self):
        self.model = None
        self.scaler = None

    # The Isolation Forest handles ONLY continuous resource-usage anomalies
    # (CPU / memory). Discrete health signals — pod_ready==0 and restart spikes —
    # are deterministic and must not be diluted through a density model that
    # averages a single binary flip across many features, so they are gated
    # explicitly in the engine instead. See SelfHealingEngine.process_pod.
    FEATURES = ['cpu_percent', 'memory_percent']

    def train(self, df):
        normal_df = df[df['action'] == ACTION_NO_ACTION].copy()
        X = normal_df[self.FEATURES].values

        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        self.model = IsolationForest(
            n_estimators=IF_N_ESTIMATORS,
            contamination=IF_CONTAMINATION,
            random_state=42
        )
        self.model.fit(X_scaled)

        joblib.dump({'model': self.model, 'scaler': self.scaler}, IF_MODEL_PATH)
        print(f"  Isolation Forest trained on {len(normal_df)} normal samples → saved to {IF_MODEL_PATH}")

    def load(self, path=None):
        data = joblib.load(path or IF_MODEL_PATH)
        self.model = data['model']
        self.scaler = data['scaler']

    def predict(self, cpu, memory):
        X = np.array([[cpu, memory]])
        X_scaled = self.scaler.transform(X)
        prediction = self.model.predict(X_scaled)[0]
        score = self.model.score_samples(X_scaled)[0]
        return prediction == -1, round(score, 4)
