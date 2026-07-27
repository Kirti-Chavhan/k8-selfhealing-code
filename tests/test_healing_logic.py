"""Unit tests for the detection/decision logic — no live cluster or trained
models required. The engine's ML components and K8s actions are stubbed so
these exercise only the decision gate, cooldown, and health-gate rules in
healing/self_healing_engine.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from ai.zscore_predictor import ZScorePredictor
from config import ACTION_DELETE_RECREATE, ACTION_NO_ACTION, ACTION_ROLLING_RESTART
from healing.self_healing_engine import SelfHealingEngine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import healing.feedback_logger as feedback_logger

    monkeypatch.setattr(
        feedback_logger, "FEEDBACK_LOG_PATH", str(tmp_path / "healing_actions.csv")
    )
    eng = SelfHealingEngine()
    # Stub the ML models and the actual Kubernetes action so tests don't need
    # trained .pkl files or a live cluster.
    eng.iso.predict = lambda cpu, mem: (False, 0.0)
    eng.rf.predict = lambda cpu, mem, restarts, ready: (ACTION_NO_ACTION, 1.0)
    eng._execute = lambda action, pod: True
    return eng


def make_row(pod_name="pod-a", cpu=10.0, memory=20.0, restarts=0, ready=1):
    return {
        "pod_name": pod_name,
        "cpu_percent": cpu,
        "memory_percent": memory,
        "restart_count": restarts,
        "pod_ready": ready,
    }


def test_healthy_pod_takes_no_action(engine):
    engine.process_pod(make_row())
    assert engine.cooldowns == {}


def test_restart_spike_triggers_action_via_health_gate(engine):
    engine.rf.predict = lambda cpu, mem, restarts, ready: (ACTION_DELETE_RECREATE, 0.95)
    # First poll only establishes the baseline restart count — not a spike yet.
    engine.process_pod(make_row(restarts=0))
    assert engine.cooldowns == {}
    # Second poll: restart count increased since last poll -> restart_spike.
    engine.process_pod(make_row(restarts=1))
    assert "pod-a" in engine.cooldowns


def test_memory_anomaly_triggers_rolling_restart(engine):
    engine.iso.predict = lambda cpu, mem: (True, -0.5)
    engine.rf.predict = lambda cpu, mem, restarts, ready: (ACTION_ROLLING_RESTART, 0.96)
    engine.process_pod(make_row(memory=90.0))
    assert "pod-a" in engine.cooldowns


def test_cooldown_prevents_repeat_action(engine):
    engine.iso.predict = lambda cpu, mem: (True, -0.5)
    engine.rf.predict = lambda cpu, mem, restarts, ready: (ACTION_ROLLING_RESTART, 0.96)
    calls = []
    engine._execute = lambda action, pod: (calls.append(pod), True)[1]

    engine.process_pod(make_row(memory=90.0))
    assert len(calls) == 1

    # Anomaly persists but the pod is still in cooldown -> no second action.
    engine.process_pod(make_row(memory=90.0))
    assert len(calls) == 1


def test_new_pod_not_ready_is_not_treated_as_a_fault(engine):
    engine.rf.predict = lambda cpu, mem, restarts, ready: (ACTION_DELETE_RECREATE, 0.95)
    # A pod that has never been Ready (e.g. still starting after a scale-up)
    # must not trigger the not-ready-regression health gate.
    engine.process_pod(make_row(ready=0))
    assert engine.cooldowns == {}


def test_not_ready_after_being_ready_is_a_fault(engine):
    engine.rf.predict = lambda cpu, mem, restarts, ready: (ACTION_DELETE_RECREATE, 0.95)
    engine.process_pod(make_row(ready=1))
    assert engine.cooldowns == {}
    engine.process_pod(make_row(ready=0))
    assert "pod-a" in engine.cooldowns


def test_starting_up_pod_is_not_deleted_for_zero_metrics_anomaly(engine):
    # Regression test: a brand-new pod from a rolling restart / scale-up often
    # reports cpu=0, mem=0 (not scraped by Prometheus yet) and ready=0 before
    # it settles. The Isolation Forest flags (0, 0) as anomalous and the
    # Random Forest confidently recommends DELETE_AND_RECREATE -- but a pod
    # that has never been Ready must not be acted on for that alone, or the
    # engine ends up deleting its own still-starting pods.
    engine.iso.predict = lambda cpu, mem: (True, -0.9)
    engine.rf.predict = lambda cpu, mem, restarts, ready: (ACTION_DELETE_RECREATE, 0.89)
    engine.process_pod(make_row(cpu=0.0, memory=0.0, restarts=0, ready=0))
    assert engine.cooldowns == {}


def test_starting_up_pod_with_restart_spike_is_still_healed(engine):
    # A pod that crash-loops from birth must still be caught, even though it
    # has never been Ready -- restart_spike is a deterministic signal, not
    # suppressed by the starting_up guard.
    engine.rf.predict = lambda cpu, mem, restarts, ready: (ACTION_DELETE_RECREATE, 0.95)
    engine.process_pod(make_row(restarts=0, ready=0))
    assert engine.cooldowns == {}
    engine.process_pod(make_row(restarts=1, ready=0))
    assert "pod-a" in engine.cooldowns


def test_zscore_flags_spike_after_stable_baseline():
    z = ZScorePredictor()
    for v in [10, 11, 9, 10, 10, 11, 9, 10]:
        z.check_all_metrics("pod-a", v, 20)
    warnings = z.check_all_metrics("pod-a", 80, 20)
    assert any(w["metric"] == "cpu_percent" for w in warnings)


def test_zscore_no_warning_for_stable_metrics():
    z = ZScorePredictor()
    warnings = []
    for v in [10, 11, 9, 10, 10, 11, 9, 10, 10]:
        warnings = z.check_all_metrics("pod-a", v, 20)
    assert warnings == []


def test_zscore_no_warning_with_insufficient_samples():
    z = ZScorePredictor()
    warnings = z.check_all_metrics("pod-a", 999, 999)
    assert warnings == []
