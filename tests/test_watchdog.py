"""Tests pour tgwatch.watchdog — run_once et watch_loop.

100% offline : storage SQLite temp réel, fake_alerter sans réseau, now injecté.
Couvre : détection down, récupération, santé dégradée, anti-spam, parse robuste, loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tgwatch.core.models import Health, Status
from tgwatch.core.storage import Storage


# ---------------------------------------------------------------------------
# Helpers / fake objects
# ---------------------------------------------------------------------------


class FakeAlerter:
    """Alerter bouchon : capture les appels maybe_send sans réseau."""

    def __init__(self, return_value: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.return_value = return_value

    def maybe_send(
        self, client_id: int, alert_type: str, text: str, *, now=None
    ) -> bool:
        self.calls.append(
            {"client_id": client_id, "alert_type": alert_type, "text": text, "now": now}
        )
        return self.return_value

    def calls_of_type(self, alert_type: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["alert_type"] == alert_type]


def _ts_ago(now: datetime, seconds: int) -> str:
    """Retourne un timestamp ISO 8601 antérieur de `seconds` secondes à `now`."""
    return (now - timedelta(seconds=seconds)).isoformat()


def _ts_from_now(now: datetime, seconds: int) -> str:
    """Retourne un timestamp ISO 8601 récent (seconds avant now)."""
    return (now - timedelta(seconds=seconds)).isoformat()


FROZEN_NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
THRESHOLD = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Tests run_once — détection heartbeat manqué (WDOG-02)
# ---------------------------------------------------------------------------


class TestRunOnceHeartbeatDown:
    """Client actif dont le heartbeat dépasse le seuil → marqué DOWN."""

    def test_client_marked_down_in_db(self, storage: Storage) -> None:
        """Client last_seen il y a 400s (>300s) → status=DOWN persisté en DB."""
        from tgwatch.watchdog import run_once

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 400))
        # update_status UP pour partir d'un état UP initial
        storage.update_status("bot1", Status.UP)

        alerter = FakeAlerter()
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        client = storage.get_client_by_name("bot1")
        assert client is not None
        assert client.status == Status.DOWN

    def test_down_event_recorded(self, storage: Storage) -> None:
        """Transition vers DOWN enregistre un event de type 'down'."""
        from tgwatch.watchdog import run_once

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 400))
        storage.update_status("bot1", Status.UP)

        client_before = storage.get_client_by_name("bot1")
        assert client_before is not None
        run_once(storage, FakeAlerter(), heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        events = storage.list_events(client_before.id)
        types = [e.type for e in events]
        assert "down" in types

    def test_maybe_send_called_with_down_type(self, storage: Storage) -> None:
        """La transition DOWN déclenche un appel maybe_send de type 'down'."""
        from tgwatch.watchdog import run_once

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 400))
        storage.update_status("bot1", Status.UP)

        alerter = FakeAlerter()
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        assert len(alerter.calls_of_type("down")) >= 1

    def test_already_down_no_new_event(self, storage: Storage) -> None:
        """Client déjà DOWN → pas de nouvel event 'down' (pas de double transition)."""
        from tgwatch.watchdog import run_once

        cid = storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 400))
        # Déjà DOWN (état par défaut à la création)

        alerter = FakeAlerter()
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        events = storage.list_events(cid)
        down_events = [e for e in events if e.type == "down"]
        # Aucun event "down" car pas de transition (était déjà DOWN)
        assert len(down_events) == 0

    def test_already_down_alert_still_attempted(self, storage: Storage) -> None:
        """Client déjà DOWN → maybe_send 'down' quand même tenté (anti-spam côté Alerter)."""
        from tgwatch.watchdog import run_once

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 400))
        # Déjà DOWN (default)

        alerter = FakeAlerter()
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        # L'alerte est tentée même si pas de transition
        assert len(alerter.calls_of_type("down")) >= 1

    def test_fresh_client_no_action(self, storage: Storage) -> None:
        """Client avec last_seen récent (50s) + status UP → aucune action."""
        from tgwatch.watchdog import run_once

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 50))
        storage.update_status("bot1", Status.UP)

        alerter = FakeAlerter()
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        assert len(alerter.calls) == 0
        client = storage.get_client_by_name("bot1")
        assert client is not None
        assert client.status == Status.UP


# ---------------------------------------------------------------------------
# Tests run_once — récupération (WDOG-02 recovery)
# ---------------------------------------------------------------------------


class TestRunOnceRecovery:
    """Client DOWN avec last_seen récent → repassé UP."""

    def test_client_marked_up_in_db(self, storage: Storage) -> None:
        """Client était DOWN, last_seen récent (50s) → status=UP persisté en DB."""
        from tgwatch.watchdog import run_once

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 50))
        # Forcer DOWN manuellement (état par défaut)

        run_once(storage, FakeAlerter(), heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        client = storage.get_client_by_name("bot1")
        assert client is not None
        assert client.status == Status.UP

    def test_up_event_recorded(self, storage: Storage) -> None:
        """Récupération (DOWN→UP) enregistre un event de type 'up'."""
        from tgwatch.watchdog import run_once

        cid = storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 50))

        run_once(storage, FakeAlerter(), heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        events = storage.list_events(cid)
        types = [e.type for e in events]
        assert "up" in types

    def test_no_down_alert_on_recovery(self, storage: Storage) -> None:
        """Récupération : aucune alerte 'down' envoyée (last_seen récent)."""
        from tgwatch.watchdog import run_once

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 50))

        alerter = FakeAlerter()
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        assert len(alerter.calls_of_type("down")) == 0


# ---------------------------------------------------------------------------
# Tests run_once — santé dégradée (WDOG-03)
# ---------------------------------------------------------------------------


class TestRunOnceHealth:
    """Santé dégradée → alerte typée."""

    @pytest.mark.parametrize(
        "health, expected_type",
        [
            (Health.RESTREINT, "health_restreint"),
            (Health.BANNI, "health_banni"),
            (Health.SESSION_MORTE, "health_session_morte"),
        ],
    )
    def test_degraded_health_triggers_alert(
        self, storage: Storage, health: Health, expected_type: str
    ) -> None:
        """Client avec santé dégradée → maybe_send avec le type correspondant."""
        from tgwatch.watchdog import run_once

        storage.create_client("ub1", "userbot")
        storage.update_last_seen("ub1", _ts_ago(FROZEN_NOW, 50))
        storage.update_status("ub1", Status.UP)
        storage.set_health("ub1", health)

        alerter = FakeAlerter()
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        assert len(alerter.calls_of_type(expected_type)) == 1

    @pytest.mark.parametrize(
        "health",
        [Health.SAIN, Health.NA],
    )
    def test_healthy_client_no_health_alert(
        self, storage: Storage, health: Health
    ) -> None:
        """Client health=sain ou n/a → aucune alerte santé."""
        from tgwatch.watchdog import run_once

        storage.create_client("ub1", "userbot")
        storage.update_last_seen("ub1", _ts_ago(FROZEN_NOW, 50))
        storage.update_status("ub1", Status.UP)
        storage.set_health("ub1", health)

        alerter = FakeAlerter()
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        health_calls = [c for c in alerter.calls if c["alert_type"].startswith("health_")]
        assert len(health_calls) == 0


# ---------------------------------------------------------------------------
# Tests run_once — robustesse (T-04-06)
# ---------------------------------------------------------------------------


class TestRunOnceRobustness:
    """run_once ne crashe jamais sur des données corrompues."""

    def test_bad_last_seen_no_crash(self, storage: Storage) -> None:
        """last_seen illisible → client ignoré, pas de crash, pas d'exception."""
        from tgwatch.watchdog import run_once

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", "pas-une-date")

        alerter = FakeAlerter()
        # Ne doit pas lever
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        # Aucune action sur ce client
        assert len(alerter.calls) == 0

    def test_empty_storage_no_crash(self, storage: Storage) -> None:
        """Storage vide → aucune action, pas de crash."""
        from tgwatch.watchdog import run_once

        alerter = FakeAlerter()
        run_once(storage, alerter, now=FROZEN_NOW)

        assert len(alerter.calls) == 0


# ---------------------------------------------------------------------------
# Test anti-spam intégration (ALRT-03 + WDOG-02)
# ---------------------------------------------------------------------------


class TestAntiSpamIntegration:
    """2 run_once successifs → le sender HTTP n'est appelé qu'une fois (dédup Alerter)."""

    def test_sender_called_once_on_double_run_once(self, storage: Storage) -> None:
        """Vrai Alerter avec sender mocké : 2 run_once → sender appelé 1 fois."""
        from tgwatch.alert.telegram import Alerter
        from tgwatch.watchdog import run_once

        http_calls: list[int] = []

        def fake_sender(url: str, data: bytes, timeout: float) -> int:
            http_calls.append(1)
            return 200

        alerter = Alerter(
            storage,
            token="fake-token",
            chat_id="123456",
            sender=fake_sender,
            backoff_base=0,
            dedup_window=3600,
        )

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 400))
        storage.update_status("bot1", Status.UP)

        # 1er run_once : marque DOWN + envoie alerte (sender appelé 1 fois)
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)
        # 2e run_once : déjà DOWN, alerte tentée mais bloquée par dédup
        run_once(storage, alerter, heartbeat_threshold=THRESHOLD, now=FROZEN_NOW)

        assert len(http_calls) == 1


# ---------------------------------------------------------------------------
# Tests watch_loop — arrêt propre sur KeyboardInterrupt
# ---------------------------------------------------------------------------


class TestWatchLoop:
    """watch_loop s'arrête proprement sur KeyboardInterrupt sans propager."""

    def test_loop_stops_on_keyboard_interrupt(
        self, storage: Storage, monkeypatch
    ) -> None:
        """Monkeypatch time.sleep → lève KeyboardInterrupt au 1er appel → loop retourne."""
        import tgwatch.watchdog as wdog
        from tgwatch.watchdog import watch_loop

        sleep_calls: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            raise KeyboardInterrupt

        monkeypatch.setattr(wdog.time, "sleep", fake_sleep)

        storage.create_client("bot1", "bot")
        storage.update_last_seen("bot1", _ts_ago(FROZEN_NOW, 50))
        storage.update_status("bot1", Status.UP)

        alerter = FakeAlerter()
        # Ne doit pas lever KeyboardInterrupt
        watch_loop(
            storage,
            alerter,
            heartbeat_threshold=THRESHOLD,
            poll_interval=30,
        )

        # sleep a bien été appelé (run_once a tourné au moins une fois)
        assert len(sleep_calls) >= 1

    def test_loop_runs_run_once_before_sleep(
        self, storage: Storage, monkeypatch
    ) -> None:
        """run_once est exécuté avant le 1er sleep (ordre garanti)."""
        import tgwatch.watchdog as wdog
        from tgwatch.watchdog import watch_loop

        execution_order: list[str] = []
        original_run_once = wdog.run_once

        def traced_run_once(*args, **kwargs) -> None:  # type: ignore[override]
            execution_order.append("run_once")
            original_run_once(*args, **kwargs)

        def fake_sleep(seconds: float) -> None:
            execution_order.append("sleep")
            raise KeyboardInterrupt

        monkeypatch.setattr(wdog, "run_once", traced_run_once)
        monkeypatch.setattr(wdog.time, "sleep", fake_sleep)

        alerter = FakeAlerter()
        watch_loop(storage, alerter, poll_interval=30)

        assert execution_order == ["run_once", "sleep"]
