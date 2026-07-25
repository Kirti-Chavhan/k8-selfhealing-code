import time
import logging
from config import (
    TARGET_DEPLOYMENT, POLL_INTERVAL_SECONDS, COOLDOWN_SECONDS,
    ACTION_SCALE_UP, ACTION_DELETE_RECREATE, ACTION_ROLLING_RESTART, ACTION_NO_ACTION
)
from ai.data_collector import collect_all_metrics
from ai.zscore_predictor import ZScorePredictor
from ai.isolation_forest_detector import IsolationForestDetector
from ai.random_forest_classifier import RandomForestActionClassifier
from healing.kubernetes_actions import (
    load_kube_config, scale_up_deployment, delete_pod, rolling_restart_deployment
)
from healing.feedback_logger import FeedbackLogger

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class SelfHealingEngine:
    def __init__(self):
        self.zscore = ZScorePredictor()
        self.iso = IsolationForestDetector()
        self.rf = RandomForestActionClassifier()
        self.feedback = FeedbackLogger()
        self.cooldowns = {}       # {pod_name: unix_timestamp_of_last_action}
        self.last_restarts = {}   # {pod_name: restart_count seen on previous poll}

    def load_models(self):
        logger.info("Loading AI models from disk...")
        self.iso.load()
        self.rf.load()
        logger.info("Models loaded.")

    def _in_cooldown(self, pod_name):
        last = self.cooldowns.get(pod_name, 0)
        return (time.time() - last) < COOLDOWN_SECONDS

    def _execute(self, action, pod_name):
        if action == ACTION_SCALE_UP:
            return scale_up_deployment(TARGET_DEPLOYMENT)
        if action == ACTION_DELETE_RECREATE:
            return delete_pod(pod_name)
        if action == ACTION_ROLLING_RESTART:
            return rolling_restart_deployment(TARGET_DEPLOYMENT)
        return False

    def process_pod(self, row):
        pod          = row['pod_name']
        cpu          = row['cpu_percent']
        memory       = row['memory_percent']
        restarts     = row['restart_count']
        ready        = row['pod_ready']

        # ── Layer 1: Z-Score ──────────────────────────────────────────
        warnings     = self.zscore.check_all_metrics(pod, cpu, memory)
        zscore_cpu   = next((w['z_score'] for w in warnings if w['metric'] == 'cpu_percent'), 0.0)
        zscore_mem   = next((w['z_score'] for w in warnings if w['metric'] == 'memory_percent'), 0.0)
        zscore_warn  = len(warnings) > 0

        # ── Layer 2: Isolation Forest (resource anomalies only) ───────
        if_anomaly, if_score = self.iso.predict(cpu, memory)

        # ── Layer 2b: explicit health signals ─────────────────────────
        # pod_ready==0 and a fresh restart are deterministic fault signals;
        # gate them directly rather than through the density model. A pod's
        # accumulated restart count on first sight is not a new fault, so only
        # an *increase* over the previous poll counts as a spike.
        prev_restarts  = self.last_restarts.get(pod)
        restart_spike  = prev_restarts is not None and restarts > prev_restarts
        self.last_restarts[pod] = restarts
        health_degraded = (ready == 0) or restart_spike

        # ── Layer 3: Random Forest ────────────────────────────────────
        rf_action, rf_conf = self.rf.predict(cpu, memory, restarts, ready)

        # ── Decision ─────────────────────────────────────────────────
        should_act   = zscore_warn or if_anomaly or health_degraded
        action_taken = ACTION_NO_ACTION
        mttr         = None

        if should_act and rf_action != ACTION_NO_ACTION and not self._in_cooldown(pod):
            logger.info(
                f"ALERT [{pod}] CPU:{cpu:.1f}% MEM:{memory:.1f}% "
                f"RST:{restarts} RDY:{ready} → {rf_action} ({rf_conf:.0%} conf)"
            )
            t0 = time.time()
            if self._execute(rf_action, pod):
                action_taken = rf_action
                mttr = round(time.time() - t0, 2)
                self.cooldowns[pod] = time.time()
                logger.info(f"  ✓ {rf_action} done in {mttr}s")
        else:
            if not should_act:
                logger.debug(f"OK   [{pod}] CPU:{cpu:.1f}% MEM:{memory:.1f}% — no anomaly")

        self.feedback.log(
            pod, cpu, memory, restarts, ready,
            zscore_cpu, zscore_mem, zscore_warn,
            if_anomaly, if_score, rf_action, rf_conf,
            action_taken, mttr
        )

    def run(self):
        load_kube_config()
        logger.info(f"Self-Healing Engine running — polling every {POLL_INTERVAL_SECONDS}s")
        logger.info(f"Cooldown: {COOLDOWN_SECONDS}s | Max replicas: {TARGET_DEPLOYMENT}")

        while True:
            try:
                df = collect_all_metrics()
                if df.empty:
                    logger.warning("No pods found — is the cluster running?")
                else:
                    logger.info(f"Collected metrics for {len(df)} pod(s)")
                    for _, row in df.iterrows():
                        self.process_pod(row)
            except KeyboardInterrupt:
                logger.info("Engine stopped by user.")
                break
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)

            time.sleep(POLL_INTERVAL_SECONDS)
