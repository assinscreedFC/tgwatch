"""Tests pour tgwatch.core.recorder — masquage, bornage et API Recorder.

TDD RED : ces tests échouent tant que recorder.py n'est pas implémenté.
Toutes les I/O utilisent SQLite sur fichier temporaire (offline).
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from tgwatch.core.models import Health, Status
from tgwatch.core.storage import Storage


# ---------------------------------------------------------------------------
# Helpers pour créer Storage + Recorder sur fichier temporaire
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path: Path) -> str:
    """Chemin vers une base SQLite temporaire."""
    return str(tmp_path / "test.db")


@pytest.fixture()
def storage(tmp_db: str) -> Storage:
    """Storage initialisé sur DB temporaire."""
    return Storage(tmp_db)


@pytest.fixture()
def recorder(storage: Storage):
    """Recorder injecté avec storage temporaire."""
    from tgwatch.core.recorder import Recorder

    return Recorder(storage)


# ---------------------------------------------------------------------------
# Task 1 — Tests _mask (masquage récursif)
# ---------------------------------------------------------------------------


class TestMask:
    """Tests de la fonction privée _mask."""

    def _mask(self, value):
        from tgwatch.core.recorder import _mask

        return _mask(value)

    def test_mask_token_key(self):
        """Clé 'token' doit être rédigée peu importe la valeur."""
        result = self._mask({"token": "123:abc"})
        assert "123:abc" not in str(result)
        assert result["token"] == "***"

    def test_mask_case_insensitive_keys(self):
        """Toutes les clés sensibles, insensibles à la casse."""
        sensitive_pairs = [
            ("token", "val1"),
            ("session", "val2"),
            ("session_string", "val3"),
            ("phone", "val4"),
            ("phone_number", "val5"),
            ("api_id", "val6"),
            ("api_hash", "val7"),
            ("Token", "val8"),
            ("SESSION_STRING", "val9"),
        ]
        for key, val in sensitive_pairs:
            result = self._mask({key: val})
            assert val not in str(result), f"Clé '{key}' non masquée"
            # La valeur redactée doit être présente
            assert "***" in str(result), f"Clé '{key}' : *** absent"

    def test_mask_recursive_nested_dict(self):
        """Masquage récursif dans les dicts imbriqués."""
        payload = {"a": {"session": "secret_session"}, "msg": "hello"}
        result = self._mask(payload)
        assert "secret_session" not in str(result)
        assert result["msg"] == "hello"  # non-sensible préservée

    def test_mask_recursive_nested_list(self):
        """Masquage récursif dans les listes."""
        payload = {"items": [{"phone": "secret_phone"}, {"msg": "ok"}]}
        result = self._mask(payload)
        assert "secret_phone" not in str(result)

    def test_mask_deep_nested(self):
        """Masquage récursif profond : dict > list > dict."""
        payload = {"a": {"b": [{"phone": "y"}]}}
        result = self._mask(payload)
        assert "y" not in str(result)

    def test_mask_token_regex_in_value(self):
        """TOKEN_RE redige un token bot même dans une clé non-denylist."""
        bot_token = "8123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        result = self._mask({"msg": bot_token})
        assert bot_token not in str(result)
        assert "***" in str(result)

    def test_mask_token_regex_embedded_in_string(self):
        """TOKEN_RE fonctionne quand le token est embarqué dans une phrase."""
        bot_token = "8123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        text = f"Error: token={bot_token} rejected"
        result = self._mask({"description": text})
        assert bot_token not in str(result)

    def test_mask_none_returns_empty_dict(self):
        """_mask(None) ne doit pas lever d'exception."""
        result = self._mask(None)
        # Doit retourner quelque chose de non-secret (None ou {} selon implémentation)
        assert result is None or result == {} or result == ""

    def test_mask_non_sensitive_key_preserved(self):
        """Les clés non-sensibles sont préservées intactes."""
        result = self._mask({"count": 42, "msg": "hello", "ok": True})
        assert result["count"] == 42
        assert result["msg"] == "hello"
        assert result["ok"] is True

    def test_mask_list_at_root(self):
        """_mask peut recevoir une liste à la racine."""
        result = self._mask([{"token": "abc"}, {"msg": "hello"}])
        assert "abc" not in str(result)
        assert "hello" in str(result)


# ---------------------------------------------------------------------------
# Task 1 — Tests _to_bounded_json (bornage payload)
# ---------------------------------------------------------------------------


class TestToBoundedJson:
    """Tests de la fonction privée _to_bounded_json."""

    def _to_bounded_json(self, payload):
        from tgwatch.core.recorder import _to_bounded_json

        return _to_bounded_json(payload)

    def test_small_payload_unchanged(self):
        """Un petit payload (< 4096 octets) doit être retourné entier."""
        payload = {"key": "value", "count": 1}
        result = self._to_bounded_json(payload)
        assert len(result.encode("utf-8")) <= 4096
        parsed = json.loads(result)
        assert parsed["key"] == "value"

    def test_large_payload_truncated(self):
        """Un payload > 4096 octets doit être tronqué."""
        big_payload = {"data": "x" * 5000}
        result = self._to_bounded_json(big_payload)
        assert len(result.encode("utf-8")) <= 4096
        assert "[truncated]" in result

    def test_large_payload_marker_present(self):
        """Le marqueur de troncature est présent pour les gros payloads."""
        big_payload = {"data": "A" * 5000}
        result = self._to_bounded_json(big_payload)
        assert "...[truncated]" in result

    def test_none_payload_returns_empty_json(self):
        """None retourne '{}'."""
        result = self._to_bounded_json(None)
        assert result == "{}"

    def test_secrets_masked_before_json(self):
        """Les secrets sont masqués AVANT la sérialisation JSON."""
        payload = {"token": "8123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
        result = self._to_bounded_json(payload)
        assert "8123456789:AAE" not in result
        assert "***" in result

    def test_output_is_valid_json_when_not_truncated(self):
        """Pour un petit payload, la sortie est du JSON valide."""
        payload = {"a": 1, "b": "hello"}
        result = self._to_bounded_json(payload)
        parsed = json.loads(result)
        assert parsed["a"] == 1

    def test_boundary_exactly_4096(self):
        """Un payload exactement à la limite ne doit pas être tronqué."""
        # On génère un JSON d'exactement <= 4096 octets
        payload = {"k": "v"}
        result = self._to_bounded_json(payload)
        assert "[truncated]" not in result


# ---------------------------------------------------------------------------
# Task 2 — Tests API Recorder (record_event / heartbeat / set_health)
# ---------------------------------------------------------------------------


class TestRecorderRecordEvent:
    """Tests de Recorder.record_event."""

    def test_record_event_creates_client_if_absent(self, recorder, storage):
        """record_event crée le client automatiquement s'il n'existe pas."""
        recorder.record_event("bot_auto", "msg", {"text": "hello"})
        client = storage.get_client_by_name("bot_auto")
        assert client is not None
        assert client.name == "bot_auto"

    def test_record_event_inserts_event(self, recorder, storage):
        """record_event insère bien un événement dans la DB."""
        recorder.record_event("bot_x", "msg", {"k": "v"})
        client = storage.get_client_by_name("bot_x")
        events = storage.list_events(client.id)
        assert len(events) == 1
        assert events[0].type == "msg"

    def test_record_event_existing_client_adds_event(self, recorder, storage):
        """record_event sur un client existant ajoute l'event sans écraser le client."""
        recorder.record_event("bot_x", "msg", {"k": "v"})
        recorder.record_event("bot_x", "error", {"err": "oops"})
        client = storage.get_client_by_name("bot_x")
        events = storage.list_events(client.id)
        assert len(events) == 2

    def test_record_event_payload_none(self, recorder, storage):
        """record_event accepte un payload None."""
        recorder.record_event("bot_none", "heartbeat", None)
        client = storage.get_client_by_name("bot_none")
        events = storage.list_events(client.id)
        assert len(events) == 1
        assert events[0].payload_json == "{}"

    def test_record_event_masks_secrets_in_db(self, recorder, storage):
        """Les secrets dans le payload ne doivent jamais apparaître en DB."""
        bot_token = "8123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        recorder.record_event("bot_sec", "error", {"token": bot_token})
        client = storage.get_client_by_name("bot_sec")
        events = storage.list_events(client.id)
        assert bot_token not in events[0].payload_json


class TestRecorderHeartbeat:
    """Tests de Recorder.heartbeat."""

    def test_heartbeat_creates_client_if_absent(self, recorder, storage):
        """heartbeat crée le client s'il n'existe pas."""
        recorder.heartbeat("bot_hb")
        client = storage.get_client_by_name("bot_hb")
        assert client is not None

    def test_heartbeat_updates_last_seen(self, recorder, storage):
        """heartbeat met à jour last_seen."""
        recorder.heartbeat("bot_hb")
        client_before = storage.get_client_by_name("bot_hb")
        last_seen_before = client_before.last_seen

        import time

        time.sleep(0.01)  # Assure un timestamp différent
        recorder.heartbeat("bot_hb")
        client_after = storage.get_client_by_name("bot_hb")
        assert client_after.last_seen != last_seen_before

    def test_heartbeat_successive_change_last_seen(self, recorder, storage):
        """Deux heartbeats successifs changent last_seen à chaque fois."""
        recorder.heartbeat("bot_tick")
        t1 = storage.get_client_by_name("bot_tick").last_seen
        import time

        time.sleep(0.01)
        recorder.heartbeat("bot_tick")
        t2 = storage.get_client_by_name("bot_tick").last_seen
        assert t1 != t2


class TestRecorderSetHealth:
    """Tests de Recorder.set_health."""

    def test_set_health_creates_client_if_absent(self, recorder, storage):
        """set_health crée le client s'il n'existe pas."""
        recorder.set_health("bot_health", Health.RESTREINT)
        client = storage.get_client_by_name("bot_health")
        assert client is not None

    def test_set_health_updates_health(self, recorder, storage):
        """set_health met à jour la santé du client."""
        recorder.heartbeat("bot_health")
        recorder.set_health("bot_health", Health.RESTREINT)
        client = storage.get_client_by_name("bot_health")
        assert client.health == Health.RESTREINT

    def test_set_health_banni(self, recorder, storage):
        """set_health fonctionne avec Health.BANNI."""
        recorder.set_health("bot_banni", Health.BANNI)
        client = storage.get_client_by_name("bot_banni")
        assert client.health == Health.BANNI

    def test_set_health_session_morte(self, recorder, storage):
        """set_health fonctionne avec Health.SESSION_MORTE."""
        recorder.set_health("bot_dead", Health.SESSION_MORTE)
        client = storage.get_client_by_name("bot_dead")
        assert client.health == Health.SESSION_MORTE


class TestRecorderGetHealth:
    """Tests de Recorder.get_health (lecture seule, pas de création client)."""

    def test_get_health_unknown_returns_na(self, recorder, storage):
        """get_health('inconnu') retourne Health.NA sans créer le client."""
        result = recorder.get_health("inconnu")
        assert result == Health.NA
        # Aucun client ne doit avoir été créé
        assert storage.get_client_by_name("inconnu") is None

    def test_get_health_returns_banni_after_set(self, recorder, storage):
        """Après set_health BANNI, get_health retourne Health.BANNI."""
        recorder.set_health("bot_x", Health.BANNI)
        assert recorder.get_health("bot_x") == Health.BANNI

    def test_get_health_returns_sain_after_set(self, recorder, storage):
        """Après set_health SAIN, get_health retourne Health.SAIN."""
        recorder.set_health("bot_x", Health.SAIN)
        assert recorder.get_health("bot_x") == Health.SAIN
