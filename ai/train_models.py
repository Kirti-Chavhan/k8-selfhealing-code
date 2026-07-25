import os
from ai.generate_training_data import generate_training_data
from ai.isolation_forest_detector import IsolationForestDetector
from ai.random_forest_classifier import RandomForestActionClassifier


def train_all():
    os.makedirs('models', exist_ok=True)

    print("=" * 55)
    print("Step 1: Generating 1,000 synthetic training samples...")
    df = generate_training_data(n_samples=1000)
    df.to_csv('models/training_data.csv', index=False)
    print(f"  Total samples : {len(df)}")
    print(f"  Distribution  :\n{df['action'].value_counts().to_string()}")

    print("\nStep 2: Training Isolation Forest Anomaly Detector...")
    iso = IsolationForestDetector()
    iso.train(df)

    print("\nStep 3: Training Random Forest Action Classifier...")
    rf = RandomForestActionClassifier()
    rf.train(df)

    print("\n" + "=" * 55)
    print("All models trained and saved to models/")
