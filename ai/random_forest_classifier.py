import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score, f1_score
from config import RF_N_ESTIMATORS, RF_CONFIDENCE_MIN, RF_MODEL_PATH, ACTION_NO_ACTION


class RandomForestActionClassifier:
    FEATURES = ['cpu_percent', 'memory_percent', 'restart_count', 'pod_ready']

    def __init__(self):
        self.model = None

    def train(self, df):
        X = df[self.FEATURES].values
        y = df['action'].values

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )

        self.model = RandomForestClassifier(n_estimators=RF_N_ESTIMATORS, random_state=42)
        self.model.fit(X_train, y_train)

        y_pred = self.model.predict(X_test)
        acc = accuracy_score(y_test, y_pred)
        f1 = f1_score(y_test, y_pred, average='macro')

        print(f"\n  Accuracy  : {acc:.3f}")
        print(f"  Macro F1  : {f1:.3f}")
        print("\n  Classification Report:")
        print(classification_report(y_test, y_pred))
        print("  Feature Importances:")
        for name, imp in sorted(
            zip(self.FEATURES, self.model.feature_importances_), key=lambda x: -x[1]
        ):
            print(f"    {name}: {imp:.3f}")

        joblib.dump(self.model, RF_MODEL_PATH)
        print(f"\n  Model saved to {RF_MODEL_PATH}")

    def load(self, path=None):
        self.model = joblib.load(path or RF_MODEL_PATH)

    def predict(self, cpu, memory, restart_count, pod_ready):
        X = np.array([[cpu, memory, restart_count, pod_ready]])
        proba = self.model.predict_proba(X)[0]
        confidence = float(max(proba))

        if confidence < RF_CONFIDENCE_MIN:
            return ACTION_NO_ACTION, round(confidence, 4)

        action = self.model.classes_[int(np.argmax(proba))]
        return action, round(confidence, 4)
