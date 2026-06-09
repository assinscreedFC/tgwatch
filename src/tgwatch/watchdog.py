"""watchdog — process autonome de détection (heartbeat manqué + santé dégradée).

Lit le SQLite partagé (WAL), compare last_seen au seuil et la santé aux états
dégradés, déclenche les alertes via Alerter (anti-spam délégué). Synchrone et
standalone : time.sleep acceptable ici (PAS un contexte async).

Architecture : watchdog importe core.models + core.recorder — pas d'import
circulaire (core ne dépend pas de watchdog). Les events down/up passent par
Recorder.record_event (masquage centralisé, invariant CORE-03 : Recorder est le
seul écrivain de la table events).
"""

import logging
import time
from datetime import datetime, timezone

from tgwatch.core.models import Health, Status
from tgwatch.core.recorder import Recorder

logger = logging.getLogger(__name__)

HEARTBEAT_THRESHOLD = 300  # s — au-delà sans last_seen → down
DEDUP_WINDOW = 3600  # s — fenêtre anti-spam (passée à l'Alerter)
POLL_INTERVAL = 60  # s — entre deux run_once

# États santé qui déclenchent une alerte (sain et n_a sont OK)
DEGRADED_HEALTH = (Health.RESTREINT, Health.BANNI, Health.SESSION_MORTE)


def _seconds_since(last_seen: str, now: datetime) -> float:
    """Secondes écoulées depuis last_seen (ISO 8601 UTC). Robuste au parse."""
    last_dt = datetime.fromisoformat(last_seen)
    # Assurer la compatibilité si last_dt est naïf (sans tzinfo)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (now - last_dt).total_seconds()


def _alert_text(name: str, kind: str, what: str, now: datetime) -> str:
    """Message humain SANS secret (name/kind/évènement/timestamp uniquement).

    T-04-08 : jamais de payload_json ni de champ pouvant contenir un token.
    """
    return f"[tgwatch] {kind} '{name}' : {what} (à {now.isoformat()})"


def run_once(
    storage,
    alerter,
    *,
    heartbeat_threshold: int = HEARTBEAT_THRESHOLD,
    dedup_window: int = DEDUP_WINDOW,
    now: datetime | None = None,
) -> None:
    """Une itération : détecte down/recovery + santé dégradée, déclenche alertes.

    Conçu pour être appelé en boucle par watch_loop ou directement en test.

    Args:
        storage: Storage (list_clients, update_status). Les events down/up
            sont écrits via Recorder(storage) pour respecter le masquage centralisé.
        alerter: Alerter avec maybe_send (anti-spam interne).
        heartbeat_threshold: Secondes sans heartbeat avant de marquer DOWN.
        dedup_window: Fenêtre anti-spam en secondes (passée à l'alerter).
        now: datetime UTC injectable pour tests déterministes. Si None, utilise
             datetime.now(UTC).

    Notes:
        - update_status/record_event uniquement sur transition (évite les doublons).
        - L'alerte 'down' est tentée à chaque run — l'Alerter.maybe_send dédup.
        - T-04-06 : last_seen invalide → client ignoré + log WARNING, pas de crash.
    """
    current = now if now is not None else datetime.now(timezone.utc)
    recorder = Recorder(storage)  # events down/up via masquage centralisé (CORE-03)

    for client in storage.list_clients():
        # --- Parse last_seen (T-04-06 : robuste au parse) ---
        try:
            elapsed = _seconds_since(client.last_seen, current)
        except (ValueError, TypeError):
            logger.warning(
                "last_seen illisible pour %s ('%s'), client ignoré",
                client.name,
                client.last_seen,
            )
            continue

        # --- WDOG-02 : heartbeat manqué / récupération ---
        if elapsed > heartbeat_threshold:
            # Transition UP→DOWN : une seule fois (évite de polluer les events)
            if client.status != Status.DOWN:
                storage.update_status(client.name, Status.DOWN)
                recorder.record_event(client.name, "down")
                logger.info(
                    "Client %s marqué DOWN (%.0fs sans heartbeat)", client.name, elapsed
                )
            # L'alerte est tentée à chaque itération ; l'Alerter.maybe_send dédup.
            alerter.maybe_send(
                client.id,
                "down",
                _alert_text(client.name, client.kind, "ne répond plus (down)", current),
                now=current,
            )
        elif client.status == Status.DOWN:
            # Récupération : était DOWN, à nouveau actif (last_seen récent)
            storage.update_status(client.name, Status.UP)
            recorder.record_event(client.name, "up")
            logger.info("Client %s redevenu UP", client.name)

        # --- WDOG-03 : santé dégradée ---
        if client.health in DEGRADED_HEALTH:
            # Clé distincte par état : "health_restreint" / "health_banni" / "health_session_morte"
            alert_type = f"health_{client.health.value}"
            alerter.maybe_send(
                client.id,
                alert_type,
                _alert_text(
                    client.name,
                    client.kind,
                    f"santé dégradée : {client.health.value}",
                    current,
                ),
                now=current,
            )


def watch_loop(
    storage,
    alerter,
    *,
    heartbeat_threshold: int = HEARTBEAT_THRESHOLD,
    dedup_window: int = DEDUP_WINDOW,
    poll_interval: int = POLL_INTERVAL,
) -> None:
    """Boucle de surveillance : run_once puis sleep, jusqu'à KeyboardInterrupt.

    Process standalone synchrone (time.sleep acceptable, PAS de contexte async).
    Arrêt propre sur Ctrl-C : log et retour normal, sans propagation.

    Args:
        storage: Storage partagé (WAL multi-process safe).
        alerter: Alerter configuré (token + chat_id + anti-spam).
        heartbeat_threshold: Secondes sans heartbeat avant DOWN.
        dedup_window: Fenêtre anti-spam en secondes.
        poll_interval: Secondes entre deux run_once.
    """
    logger.info(
        "watchdog démarré (poll=%ds, seuil=%ds)", poll_interval, heartbeat_threshold
    )
    try:
        while True:
            run_once(
                storage,
                alerter,
                heartbeat_threshold=heartbeat_threshold,
                dedup_window=dedup_window,
            )
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("watchdog arrêté proprement (KeyboardInterrupt)")
