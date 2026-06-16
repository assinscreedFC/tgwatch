"""Tests offline pour alert/telegram.py — send/retry/anti-spam/no-secret.

Tous les tests sont 100% offline :
- Sender HTTP injecté (pas de vrai réseau).
- Storage réel sur SQLite temporaire (fixture conftest).
- Backoff_base=0 pour éviter tout sleep réel dans les tests retry.
"""

import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

import pytest

from tgwatch.alert.telegram import Alerter, TELEGRAM_HOST, API_BASE, MAX_ATTEMPTS
from tgwatch.core.storage import Storage


# ---------------------------------------------------------------------------
# Helpers / Faux senders
# ---------------------------------------------------------------------------


def make_ok_sender():
    """Retourne un sender qui simule une réponse 200 (succès)."""
    calls = []

    def sender(url: str, data: bytes, timeout: float) -> int:
        calls.append({"url": url, "data": data, "timeout": timeout})
        return 200

    sender.calls = calls
    return sender


def make_fail_sender(exc=None):
    """Retourne un sender qui lève URLError (échec réseau)."""
    calls = []
    error = exc or urllib.error.URLError("connexion refusée")

    def sender(url: str, data: bytes, timeout: float) -> int:
        calls.append({"url": url, "data": data, "timeout": timeout})
        raise error

    sender.calls = calls
    return sender


def make_non_200_sender(status: int = 400):
    """Retourne un sender qui retourne un code HTTP non-200."""
    calls = []

    def sender(url: str, data: bytes, timeout: float) -> int:
        calls.append({"url": url, "data": data, "timeout": timeout})
        return status

    sender.calls = calls
    return sender


# ---------------------------------------------------------------------------
# Tests Storage.last_alert_sent
# ---------------------------------------------------------------------------


class TestLastAlertSent:
    def test_returns_none_when_no_alerts(self, storage):
        """Aucune ligne → last_alert_sent retourne None."""
        # Arrange
        client_id = storage.create_client("bot_a", "bot")

        # Act
        result = storage.last_alert_sent(client_id, "down")

        # Assert
        assert result is None

    def test_returns_most_recent_ts(self, storage):
        """2 alertes pour (client, type) → retourne le ts le plus récent."""
        # Arrange
        client_id = storage.create_client("bot_b", "bot")
        ts_old = "2026-01-01T00:00:00+00:00"
        ts_new = "2026-06-07T10:00:00+00:00"
        storage.record_alert_sent(client_id, "down", ts_old)
        storage.record_alert_sent(client_id, "down", ts_new)

        # Act
        result = storage.last_alert_sent(client_id, "down")

        # Assert
        assert result == ts_new

    def test_ignores_other_type(self, storage):
        """last_alert_sent filtre par type — une alerte d'un autre type doit être ignorée."""
        # Arrange
        client_id = storage.create_client("bot_c", "bot")
        storage.record_alert_sent(client_id, "health_degraded", "2026-06-07T10:00:00+00:00")

        # Act
        result = storage.last_alert_sent(client_id, "down")

        # Assert
        assert result is None

    def test_ignores_other_client(self, storage):
        """last_alert_sent filtre par client_id — alerte d'un autre client ignorée."""
        # Arrange
        cid1 = storage.create_client("bot_d", "bot")
        cid2 = storage.create_client("bot_e", "bot")
        storage.record_alert_sent(cid2, "down", "2026-06-07T10:00:00+00:00")

        # Act
        result = storage.last_alert_sent(cid1, "down")

        # Assert
        assert result is None


# ---------------------------------------------------------------------------
# Tests Alerter.send — succès, échec, no-secret
# ---------------------------------------------------------------------------


class TestAlerterSend:
    def test_send_success_returns_true(self, storage):
        """Sender retourne 200 → send() retourne True."""
        # Arrange
        sender = make_ok_sender()
        alerter = Alerter(storage, token="tok", chat_id="42", sender=sender, backoff_base=0)

        # Act
        result = alerter.send("test message")

        # Assert
        assert result is True

    def test_send_success_calls_sender_once(self, storage):
        """Sender 200 → appelé exactement 1 fois."""
        # Arrange
        sender = make_ok_sender()
        alerter = Alerter(storage, token="tok", chat_id="42", sender=sender, backoff_base=0)

        # Act
        alerter.send("hello")

        # Assert
        assert len(sender.calls) == 1

    def test_send_url_contains_api_host(self, storage):
        """L'URL envoyée au sender contient api.telegram.org."""
        # Arrange
        sender = make_ok_sender()
        alerter = Alerter(storage, token="my_token", chat_id="99", sender=sender, backoff_base=0)

        # Act
        alerter.send("msg")

        # Assert
        url = sender.calls[0]["url"]
        assert TELEGRAM_HOST in url

    def test_send_data_contains_chat_id_and_text(self, storage):
        """Le payload HTTP contient chat_id et text."""
        # Arrange
        sender = make_ok_sender()
        alerter = Alerter(storage, token="tok", chat_id="777", sender=sender, backoff_base=0)

        # Act
        alerter.send("alerte critique")

        # Assert
        data = sender.calls[0]["data"].decode("utf-8")
        assert "777" in data
        assert "alerte" in data

    def test_send_failure_returns_false(self, storage):
        """Sender lève URLError 3 fois → send() retourne False (jamais raise)."""
        # Arrange
        sender = make_fail_sender()
        alerter = Alerter(storage, token="tok", chat_id="42", sender=sender, backoff_base=0)

        # Act
        result = alerter.send("test")

        # Assert
        assert result is False

    def test_send_failure_retries_exactly_3_times(self, storage):
        """Sender échoue → appelé exactement MAX_ATTEMPTS (3) fois."""
        # Arrange
        sender = make_fail_sender()
        alerter = Alerter(storage, token="tok", chat_id="42", sender=sender, backoff_base=0)

        # Act
        alerter.send("retry test")

        # Assert
        assert len(sender.calls) == MAX_ATTEMPTS
        assert MAX_ATTEMPTS == 3

    def test_send_never_raises(self, storage):
        """send() ne propage jamais une exception, peu importe le sender."""
        # Arrange
        sender = make_fail_sender(exc=RuntimeError("inattendu"))
        alerter = Alerter(storage, token="tok", chat_id="42", sender=sender, backoff_base=0)

        # Act / Assert (ne doit pas lever)
        try:
            result = alerter.send("test")
        except Exception:
            pytest.fail("send() a levé une exception — ne doit jamais propager")
        assert result is False

    def test_send_no_secret_in_logs(self, storage, caplog):
        """Après un échec, le token bot n'apparaît PAS dans les logs."""
        # Arrange
        token = "123456789:AAEsecrettokenvalueXXXXXXXXXXXXXXXXXXX"
        sender = make_fail_sender()
        alerter = Alerter(storage, token=token, chat_id="42", sender=sender, backoff_base=0)

        # Act
        with caplog.at_level(logging.DEBUG, logger="tgwatch.alert.telegram"):
            alerter.send("test no secret")

        # Assert — le token (ou des fragments reconnaissables) ne doit pas apparaître
        assert "123456789:AAE" not in caplog.text
        assert "secrettoken" not in caplog.text
        assert token not in caplog.text


# ---------------------------------------------------------------------------
# Tests Alerter.maybe_send — anti-spam + record_alert_sent conditionnel
# ---------------------------------------------------------------------------


class TestAlerterMaybeSend:
    def test_maybe_send_first_alert_sends_and_records(self, storage):
        """Premier envoi (aucune alerte antérieure) → send appelé, record_alert_sent enregistré, retourne True."""
        # Arrange
        client_id = storage.create_client("bot_x", "bot")
        sender = make_ok_sender()
        alerter = Alerter(storage, token="tok", chat_id="42", sender=sender, backoff_base=0)

        # Act
        result = alerter.maybe_send(client_id, "down", "bot_x est tombé")

        # Assert
        assert result is True
        assert len(sender.calls) == 1
        # record_alert_sent a été enregistré
        last_ts = storage.last_alert_sent(client_id, "down")
        assert last_ts is not None

    def test_maybe_send_dedup_blocks_within_window(self, storage):
        """Alerte (client, type) il y a 10s, window=3600 → maybe_send bloque (False), sender jamais appelé."""
        # Arrange
        client_id = storage.create_client("bot_y", "bot")
        now = datetime.now(timezone.utc)
        ts_recent = (now - timedelta(seconds=10)).isoformat()
        storage.record_alert_sent(client_id, "down", ts_recent)
        sender = make_ok_sender()
        alerter = Alerter(
            storage, token="tok", chat_id="42", sender=sender, dedup_window=3600, backoff_base=0
        )

        # Act
        result = alerter.maybe_send(client_id, "down", "doublon", now=now)

        # Assert
        assert result is False
        assert len(sender.calls) == 0  # sender JAMAIS appelé

    def test_maybe_send_outside_window_resends(self, storage):
        """Alerte il y a 4000s, window=3600 → maybe_send envoie (True)."""
        # Arrange
        client_id = storage.create_client("bot_z", "bot")
        now = datetime.now(timezone.utc)
        ts_old = (now - timedelta(seconds=4000)).isoformat()
        storage.record_alert_sent(client_id, "down", ts_old)
        sender = make_ok_sender()
        alerter = Alerter(
            storage, token="tok", chat_id="42", sender=sender, dedup_window=3600, backoff_base=0
        )

        # Act
        result = alerter.maybe_send(client_id, "down", "réenvoi", now=now)

        # Assert
        assert result is True
        assert len(sender.calls) == 1

    def test_maybe_send_http_failure_does_not_record(self, storage):
        """Sender échoue → maybe_send retourne False ET n'enregistre PAS record_alert_sent (pas de faux positif anti-spam)."""
        # Arrange
        client_id = storage.create_client("bot_w", "bot")
        sender = make_fail_sender()
        alerter = Alerter(storage, token="tok", chat_id="42", sender=sender, backoff_base=0)

        # Act
        result = alerter.maybe_send(client_id, "down", "échec réseau")

        # Assert
        assert result is False
        # Aucune alerte enregistrée (pas de faux marqueur anti-spam)
        last_ts = storage.last_alert_sent(client_id, "down")
        assert last_ts is None

    def test_maybe_send_different_types_independent(self, storage):
        """Deux types d'alerte différents (down / health_degraded) sont indépendants."""
        # Arrange
        client_id = storage.create_client("bot_v", "bot")
        now = datetime.now(timezone.utc)
        ts_recent = (now - timedelta(seconds=10)).isoformat()
        storage.record_alert_sent(client_id, "down", ts_recent)
        sender = make_ok_sender()
        alerter = Alerter(
            storage, token="tok", chat_id="42", sender=sender, dedup_window=3600, backoff_base=0
        )

        # Act — alerte de type différent ne doit pas être bloquée
        result = alerter.maybe_send(client_id, "health_degraded", "restriction", now=now)

        # Assert
        assert result is True
        assert len(sender.calls) == 1


# ---------------------------------------------------------------------------
# Task 3 : Alerter chat_id validation (MEDIUM)
# ---------------------------------------------------------------------------


class TestAlerterChatIdValidation:
    """Alerter.__init__ lève ValueError si chat_id n'est pas un entier."""

    def test_non_numeric_chat_id_raises(self, storage):
        """chat_id='abc' → ValueError."""
        with pytest.raises(ValueError):
            Alerter(storage, token="tok", chat_id="abc")

    def test_empty_chat_id_raises(self, storage):
        """chat_id='' → ValueError."""
        with pytest.raises(ValueError):
            Alerter(storage, token="tok", chat_id="")

    def test_float_string_chat_id_raises(self, storage):
        """chat_id='1.5' → ValueError (non entier)."""
        with pytest.raises(ValueError):
            Alerter(storage, token="tok", chat_id="1.5")

    def test_positive_numeric_chat_id_accepted(self, storage):
        """chat_id='12345' → accepté sans erreur."""
        alerter = Alerter(storage, token="tok", chat_id="12345", sender=make_ok_sender())
        assert alerter._chat_id == "12345"

    def test_negative_numeric_chat_id_accepted(self, storage):
        """chat_id='-100123' → accepté (groupes/canaux négatifs)."""
        alerter = Alerter(storage, token="tok", chat_id="-100123", sender=make_ok_sender())
        assert alerter._chat_id == "-100123"

    def test_integer_chat_id_accepted(self, storage):
        """chat_id=42 (int) → converti en str et accepté."""
        alerter = Alerter(storage, token="tok", chat_id=42, sender=make_ok_sender())
        assert alerter._chat_id == "42"
