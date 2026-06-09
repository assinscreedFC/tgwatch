"""Tests pour tgwatch.health.telethon_health — classifieur passif + worst-state-wins.

TDD RED : ces tests échouent tant que telethon_health.py n'est pas implémenté.
Tous offline : SQLite temp, vraies exceptions construites en mémoire (telethon installé).
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from tgwatch.core.models import Health
from tgwatch.health.telethon_health import (
    HEALTH_PRIORITY,
    _resolve_health,
    classify_exception,
    report_exception,
)

# ---------------------------------------------------------------------------
# Imports exceptions telethon (installé en dev)
# ---------------------------------------------------------------------------

from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PeerFloodError,
    PhoneNumberBannedError,
    UserDeactivatedBanError,
)


# ---------------------------------------------------------------------------
# Tests classify_exception — mapping exhaustif
# ---------------------------------------------------------------------------


class TestClassifyException:
    """Mapping exception Telethon -> état de santé."""

    def test_classify_peer_flood(self):
        """PeerFloodError -> RESTREINT."""
        exc = PeerFloodError(request=None)
        assert classify_exception(exc) == Health.RESTREINT

    def test_classify_phone_banned(self):
        """PhoneNumberBannedError -> BANNI."""
        exc = PhoneNumberBannedError(request=None)
        assert classify_exception(exc) == Health.BANNI

    def test_classify_user_deactivated(self):
        """UserDeactivatedBanError -> BANNI (pas SESSION_MORTE, bien que sous-classe UnauthorizedError)."""
        exc = UserDeactivatedBanError(request=None)
        assert classify_exception(exc) == Health.BANNI

    def test_classify_auth_key_unregistered(self):
        """AuthKeyUnregisteredError -> SESSION_MORTE."""
        exc = AuthKeyUnregisteredError(request=None)
        assert classify_exception(exc) == Health.SESSION_MORTE

    def test_classify_flood_wait_is_none(self):
        """FloodWaitError -> None (event floodwait, pas un état dégradé)."""
        exc = FloodWaitError(request=None, capture=30)
        assert classify_exception(exc) is None

    def test_classify_unknown_returns_none(self):
        """Exception inconnue -> None."""
        assert classify_exception(ValueError("inconnu")) is None

    def test_classify_generic_exception_returns_none(self):
        """Exception générique -> None."""
        assert classify_exception(RuntimeError("boom")) is None


# ---------------------------------------------------------------------------
# Tests _resolve_health — table de priorité worst-state-wins
# ---------------------------------------------------------------------------


class TestResolveHealth:
    """Tests de la fonction _resolve_health (worst-state-wins)."""

    def test_priority_banni_highest(self):
        """BANNI a la priorité maximale."""
        assert HEALTH_PRIORITY[Health.BANNI] > HEALTH_PRIORITY[Health.SESSION_MORTE]
        assert HEALTH_PRIORITY[Health.SESSION_MORTE] > HEALTH_PRIORITY[Health.RESTREINT]
        assert HEALTH_PRIORITY[Health.RESTREINT] > HEALTH_PRIORITY[Health.SAIN]
        assert HEALTH_PRIORITY[Health.SAIN] > HEALTH_PRIORITY[Health.NA]

    def test_resolve_banni_not_overwritten_by_session_morte(self):
        """BANNI ne rétrograde pas vers SESSION_MORTE."""
        assert _resolve_health(Health.BANNI, Health.SESSION_MORTE) == Health.BANNI

    def test_resolve_banni_not_overwritten_by_sain(self):
        """BANNI ne rétrograde pas vers SAIN."""
        assert _resolve_health(Health.BANNI, Health.SAIN) == Health.BANNI

    def test_resolve_session_morte_upgraded_to_banni(self):
        """SESSION_MORTE upgrade vers BANNI."""
        assert _resolve_health(Health.SESSION_MORTE, Health.BANNI) == Health.BANNI

    def test_resolve_sain_upgraded_to_restreint(self):
        """SAIN upgrade vers RESTREINT."""
        assert _resolve_health(Health.SAIN, Health.RESTREINT) == Health.RESTREINT

    def test_resolve_na_upgraded_to_banni(self):
        """NA upgrade vers BANNI."""
        assert _resolve_health(Health.NA, Health.BANNI) == Health.BANNI

    def test_resolve_same_state_unchanged(self):
        """Même état -> inchangé."""
        assert _resolve_health(Health.RESTREINT, Health.RESTREINT) == Health.RESTREINT


# ---------------------------------------------------------------------------
# Tests report_exception — couche passive API publique
# ---------------------------------------------------------------------------


class TestReportException:
    """Tests de report_exception (worst-state-wins + event floodwait)."""

    def test_report_peer_flood_sets_restreint(self, recorder, storage):
        """PeerFloodError persiste Health.RESTREINT."""
        exc = PeerFloodError(request=None)
        report_exception("userbot1", exc, recorder)
        client = storage.get_client_by_name("userbot1")
        assert client is not None
        assert client.health == Health.RESTREINT

    def test_report_phone_banned_sets_banni(self, recorder, storage):
        """PhoneNumberBannedError persiste Health.BANNI."""
        exc = PhoneNumberBannedError(request=None)
        report_exception("userbot1", exc, recorder)
        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.BANNI

    def test_report_flood_wait_records_event_not_health(self, recorder, storage):
        """FloodWaitError enregistre un event 'floodwait' et ne change PAS health."""
        exc = FloodWaitError(request=None, capture=30)
        report_exception("userbot1", exc, recorder)
        client = storage.get_client_by_name("userbot1")
        assert client is not None
        # Health inchangée (NA car client créé par record_event)
        assert client.health == Health.NA
        # Event floodwait enregistré
        counts = storage.count_events_by_type(client.id)
        assert counts.get("floodwait", 0) == 1

    def test_report_flood_wait_payload_has_seconds(self, recorder, storage):
        """L'event floodwait contient {'seconds': 30} dans payload_json."""
        exc = FloodWaitError(request=None, capture=30)
        report_exception("userbot1", exc, recorder)
        client = storage.get_client_by_name("userbot1")
        events = storage.list_events(client.id)
        payload = json.loads(events[0].payload_json)
        assert payload["seconds"] == 30

    def test_report_unknown_exc_does_nothing(self, recorder, storage):
        """Exception inconnue n'écrit rien en DB."""
        report_exception("userbot1", ValueError("x"), recorder)
        client = storage.get_client_by_name("userbot1")
        assert client is None

    def test_worst_state_wins_banni_not_overwritten_by_session_morte(
        self, recorder, storage
    ):
        """Après BANNI, report_exception(AuthKeyUnregisteredError) laisse health == BANNI."""
        # Couche passive: ban
        ban_exc = UserDeactivatedBanError(request=None)
        report_exception("userbot1", ban_exc, recorder)
        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.BANNI

        # Tentative de rétrogradation vers session_morte
        session_exc = AuthKeyUnregisteredError(request=None)
        report_exception("userbot1", session_exc, recorder)
        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.BANNI  # toujours BANNI

    def test_worst_state_wins_restreint_upgraded_to_banni(self, recorder, storage):
        """RESTREINT est upgradé vers BANNI si exception de ban arrive."""
        flood_exc = PeerFloodError(request=None)
        report_exception("userbot1", flood_exc, recorder)
        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.RESTREINT

        ban_exc = PhoneNumberBannedError(request=None)
        report_exception("userbot1", ban_exc, recorder)
        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.BANNI

    def test_worst_state_wins_sain_not_overwrite_restreint(self, recorder, storage):
        """SAIN n'écrase pas un RESTREINT existant (via set_health direct)."""
        recorder.set_health("userbot1", Health.RESTREINT)
        # report_exception avec PeerFloodError depuis RESTREINT -> reste RESTREINT
        exc = PeerFloodError(request=None)
        report_exception("userbot1", exc, recorder)
        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.RESTREINT


# ---------------------------------------------------------------------------
# Tests _run_probe + _apply_health — couche active get_me() (HLTH-02 + HLTH-03)
# ---------------------------------------------------------------------------


class TestRunProbe:
    """Tests de _run_probe (sonde itération unique) + _apply_health worst-state-wins."""

    async def test_probe_sain(self, recorder, storage):
        """get_me() -> User(.restricted=False) => Health.SAIN."""
        from tgwatch.health.telethon_health import _run_probe

        fake_user = MagicMock()
        fake_user.restricted = False
        fake_client = MagicMock()
        fake_client.get_me = AsyncMock(return_value=fake_user)

        await _run_probe(fake_client, "userbot1", recorder)

        client = storage.get_client_by_name("userbot1")
        assert client is not None
        assert client.health == Health.SAIN

    async def test_probe_restreint(self, recorder, storage):
        """get_me() -> User(.restricted=True) => Health.RESTREINT."""
        from tgwatch.health.telethon_health import _run_probe

        fake_user = MagicMock()
        fake_user.restricted = True
        fake_client = MagicMock()
        fake_client.get_me = AsyncMock(return_value=fake_user)

        await _run_probe(fake_client, "userbot1", recorder)

        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.RESTREINT

    async def test_probe_session_morte(self, recorder, storage):
        """get_me() -> None => Health.SESSION_MORTE (UnauthorizedError capturé en interne)."""
        from tgwatch.health.telethon_health import _run_probe

        fake_client = MagicMock()
        fake_client.get_me = AsyncMock(return_value=None)

        await _run_probe(fake_client, "userbot1", recorder)

        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.SESSION_MORTE

    async def test_probe_restricted_none_is_sain(self, recorder, storage):
        """User(.restricted=None) => Health.SAIN (None traité comme non-restreint, DC2)."""
        from tgwatch.health.telethon_health import _run_probe

        fake_user = MagicMock()
        fake_user.restricted = None
        fake_client = MagicMock()
        fake_client.get_me = AsyncMock(return_value=fake_user)

        await _run_probe(fake_client, "userbot1", recorder)

        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.SAIN

    async def test_banni_not_overwritten(self, recorder, storage):
        """BANNI persisté ne doit pas être écrasé par get_me()->None (worst-state-wins)."""
        from tgwatch.health.telethon_health import _run_probe

        # Couche passive: banni
        recorder.set_health("userbot1", Health.BANNI)

        # Couche active: get_me() retourne None -> candidat SESSION_MORTE
        fake_client = MagicMock()
        fake_client.get_me = AsyncMock(return_value=None)

        await _run_probe(fake_client, "userbot1", recorder)

        # worst-state-wins: BANNI(4) > SESSION_MORTE(3) => reste BANNI
        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.BANNI

    async def test_sain_no_overwrite_restreint(self, recorder, storage):
        """get_me()->User(.restricted=False) ne rétrograde pas un RESTREINT existant.

        Décision V1 : recovery automatique vers sain désactivée (worst-state-wins strict).
        Un RESTREINT persiste jusqu'à confirmation manuelle.
        """
        from tgwatch.health.telethon_health import _run_probe

        recorder.set_health("userbot1", Health.RESTREINT)

        fake_user = MagicMock()
        fake_user.restricted = False
        fake_client = MagicMock()
        fake_client.get_me = AsyncMock(return_value=fake_user)

        await _run_probe(fake_client, "userbot1", recorder)

        # worst-state-wins: RESTREINT(2) > SAIN(1) => reste RESTREINT
        client = storage.get_client_by_name("userbot1")
        assert client.health == Health.RESTREINT

    async def test_probe_unexpected_exception(self, recorder, storage):
        """Exception inattendue dans get_me() => pas de propagation, health inchangée."""
        from tgwatch.health.telethon_health import _run_probe

        fake_client = MagicMock()
        fake_client.get_me = AsyncMock(side_effect=RuntimeError("erreur inattendue"))

        # Ne doit pas lever d'exception
        await _run_probe(fake_client, "userbot1", recorder)

        # Health inchangée (client pas encore créé => None)
        client = storage.get_client_by_name("userbot1")
        assert client is None  # aucun set_health appelé


# ---------------------------------------------------------------------------
# Tests start_probe + _probe_loop — boucle périodique annulable (HLTH-02)
# ---------------------------------------------------------------------------


class TestStartProbe:
    """Tests de start_probe (retourne Task) et _probe_loop (CancelledError reraisé)."""

    async def test_start_probe_returns_task(self, recorder):
        """start_probe() retourne un asyncio.Task."""
        from tgwatch.health.telethon_health import start_probe

        fake_user = MagicMock()
        fake_user.restricted = False
        fake_client = MagicMock()
        fake_client.get_me = AsyncMock(return_value=fake_user)

        task = start_probe(fake_client, recorder, "userbot1", interval=9999)
        try:
            assert isinstance(task, asyncio.Task)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_probe_task_cancels_cleanly(self, recorder):
        """task.cancel() => CancelledError reraisée, task.done() True (T-03-05)."""
        from tgwatch.health.telethon_health import start_probe

        fake_user = MagicMock()
        fake_user.restricted = False
        fake_client = MagicMock()
        fake_client.get_me = AsyncMock(return_value=fake_user)

        task = start_probe(fake_client, recorder, "userbot1", interval=9999)
        await asyncio.sleep(0)  # laisser la tâche démarrer

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()

    async def test_probe_loop_runs_iteration(self, recorder, storage):
        """La boucle appelle _run_probe au moins 1 fois (health mis à jour)."""
        from tgwatch.health.telethon_health import start_probe

        fake_user = MagicMock()
        fake_user.restricted = False
        fake_client = MagicMock()

        # asyncio.sleep levant CancelledError après la 1re itération garantit
        # exactement 1 appel _run_probe puis arrêt propre.
        sleep_called = 0

        async def fake_sleep(interval):
            nonlocal sleep_called
            sleep_called += 1
            raise asyncio.CancelledError()

        # Patcher asyncio.sleep dans le module health
        import tgwatch.health.telethon_health as health_mod
        original_sleep = health_mod.asyncio.sleep
        health_mod.asyncio.sleep = fake_sleep

        fake_client.get_me = AsyncMock(return_value=fake_user)

        try:
            task = start_probe(fake_client, recorder, "userbot1", interval=1)
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            health_mod.asyncio.sleep = original_sleep

        # _run_probe a été appelé => health persisté
        client = storage.get_client_by_name("userbot1")
        assert client is not None
        assert client.health == Health.SAIN
        assert sleep_called == 1

    def test_start_probe_no_loop_raises(self, recorder):
        """start_probe() hors event loop lève RuntimeError explicite (Pitfall 4)."""
        from tgwatch.health.telethon_health import start_probe

        fake_client = MagicMock()

        # Test NON-async : pas de loop active => RuntimeError attendu
        with pytest.raises(RuntimeError):
            start_probe(fake_client, recorder, "userbot1")
