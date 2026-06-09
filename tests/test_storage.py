"""Tests pour core/storage.py — SQLite WAL.

Tous les tests utilisent un fichier SQLite réel (tempfile), pas de mock I/O.
"""

import sqlite3
import tempfile
import os
import pytest

from tgwatch.core.models import Client, Event, Status, Health
from tgwatch.core.storage import Storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Fournit un chemin vers un fichier SQLite temporaire."""
    return str(tmp_path / "test_tgwatch.db")


@pytest.fixture
def store(db_path):
    """Instance Storage fraîche sur fichier temporaire."""
    return Storage(db_path)


# ---------------------------------------------------------------------------
# Task 1 — DDL + clients
# ---------------------------------------------------------------------------


class TestSchemaInit:
    """Vérifie que le DDL crée les tables correctement en mode WAL."""

    def test_tables_exist_after_init(self, store, db_path):
        """Les tables clients, events, alerts_sent existent après init."""
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        assert "clients" in tables
        assert "events" in tables
        assert "alerts_sent" in tables

    def test_wal_mode_active(self, store, db_path):
        """PRAGMA journal_mode retourne 'wal' après init."""
        conn = sqlite3.connect(db_path)
        row = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert row[0] == "wal"


class TestCreateClient:
    """create_client() insère et retourne un id."""

    def test_create_client_returns_id(self, store):
        client_id = store.create_client("bot_x", kind="bot")
        assert isinstance(client_id, int)
        assert client_id > 0

    def test_create_client_persisted(self, store):
        """create_client puis get_client_by_name relit le client."""
        store.create_client("bot_x", kind="bot")
        client = store.get_client_by_name("bot_x")
        assert client is not None
        assert client.name == "bot_x"
        assert client.kind == "bot"

    def test_create_client_default_status_down(self, store):
        store.create_client("bot_x", kind="bot")
        client = store.get_client_by_name("bot_x")
        assert client.status == Status.DOWN

    def test_create_client_default_health_na(self, store):
        store.create_client("bot_x", kind="bot")
        client = store.get_client_by_name("bot_x")
        assert client.health == Health.NA

    def test_create_client_duplicate_raises(self, store):
        """create_client deux fois avec le même name lève sqlite3.IntegrityError."""
        store.create_client("bot_x", kind="bot")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_client("bot_x", kind="bot")

    def test_create_userbot(self, store):
        store.create_client("ub_1", kind="userbot")
        client = store.get_client_by_name("ub_1")
        assert client.kind == "userbot"


class TestGetClientByName:
    """get_client_by_name() retourne None si absent."""

    def test_get_unknown_returns_none(self, store):
        result = store.get_client_by_name("inexistant")
        assert result is None

    def test_get_returns_client_instance(self, store):
        store.create_client("bot_x", kind="bot")
        client = store.get_client_by_name("bot_x")
        assert isinstance(client, Client)


class TestUpdateLastSeen:
    """update_last_seen() met à jour le timestamp."""

    def test_update_last_seen_persisted(self, store):
        store.create_client("bot_x", kind="bot")
        ts = "2026-06-07T10:00:00+00:00"
        store.update_last_seen("bot_x", ts)
        client = store.get_client_by_name("bot_x")
        assert client.last_seen == ts

    def test_update_last_seen_auto_now(self, store):
        """update_last_seen sans ts utilise _now() — ts non vide."""
        store.create_client("bot_x", kind="bot")
        store.update_last_seen("bot_x")
        client = store.get_client_by_name("bot_x")
        assert client.last_seen != ""
        assert "T" in client.last_seen  # ISO 8601

    def test_update_last_seen_wal_multi_connection(self, db_path):
        """WAL multi-process : écrire via instance 1, relire via instance 2."""
        store1 = Storage(db_path)
        store2 = Storage(db_path)

        store1.create_client("bot_x", kind="bot")
        ts = "2026-06-07T12:00:00+00:00"
        store1.update_last_seen("bot_x", ts)

        # Relecture depuis une connexion différente (instance 2)
        client = store2.get_client_by_name("bot_x")
        assert client is not None
        assert client.last_seen == ts


class TestUpdateStatus:
    """update_status() change le champ status."""

    def test_update_status_down_to_up(self, store):
        store.create_client("bot_x", kind="bot")
        store.update_status("bot_x", Status.UP)
        client = store.get_client_by_name("bot_x")
        assert client.status == Status.UP

    def test_update_status_up_to_down(self, store):
        store.create_client("bot_x", kind="bot")
        store.update_status("bot_x", Status.UP)
        store.update_status("bot_x", Status.DOWN)
        client = store.get_client_by_name("bot_x")
        assert client.status == Status.DOWN


class TestSetHealth:
    """set_health() change le champ health."""

    def test_set_health_restreint(self, store):
        store.create_client("ub_1", kind="userbot")
        store.set_health("ub_1", Health.RESTREINT)
        client = store.get_client_by_name("ub_1")
        assert client.health == Health.RESTREINT

    def test_set_health_banni(self, store):
        store.create_client("ub_1", kind="userbot")
        store.set_health("ub_1", Health.BANNI)
        client = store.get_client_by_name("ub_1")
        assert client.health == Health.BANNI

    def test_set_health_session_morte(self, store):
        store.create_client("ub_1", kind="userbot")
        store.set_health("ub_1", Health.SESSION_MORTE)
        client = store.get_client_by_name("ub_1")
        assert client.health == Health.SESSION_MORTE


class TestListClients:
    """list_clients() retourne tous les clients."""

    def test_list_empty(self, store):
        assert store.list_clients() == []

    def test_list_multiple(self, store):
        store.create_client("bot_a", kind="bot")
        store.create_client("ub_b", kind="userbot")
        clients = store.list_clients()
        assert len(clients) == 2
        names = {c.name for c in clients}
        assert names == {"bot_a", "ub_b"}

    def test_list_returns_client_instances(self, store):
        store.create_client("bot_x", kind="bot")
        clients = store.list_clients()
        assert all(isinstance(c, Client) for c in clients)


# ---------------------------------------------------------------------------
# Task 2 — events + alerts_sent + compteurs
# ---------------------------------------------------------------------------


class TestRecordEvent:
    """record_event() insère une ligne dans events."""

    def test_record_event_returns_id(self, store):
        cid = store.create_client("bot_x", kind="bot")
        event_id = store.record_event(cid, "msg", '{"text": "hello"}')
        assert isinstance(event_id, int)
        assert event_id > 0

    def test_record_event_persisted(self, store):
        cid = store.create_client("bot_x", kind="bot")
        store.record_event(cid, "msg", '{"text": "hello"}')
        events = store.list_events(cid)
        assert len(events) == 1
        assert events[0].type == "msg"
        assert events[0].client_id == cid

    def test_record_event_ts_auto(self, store):
        """record_event sans ts utilise _now() — ts non vide et ISO 8601."""
        cid = store.create_client("bot_x", kind="bot")
        store.record_event(cid, "msg", "{}")
        events = store.list_events(cid)
        assert events[0].ts != ""
        assert "T" in events[0].ts

    def test_record_event_custom_ts(self, store):
        cid = store.create_client("bot_x", kind="bot")
        ts = "2026-06-07T10:00:00+00:00"
        store.record_event(cid, "error", "{}", ts=ts)
        events = store.list_events(cid)
        assert events[0].ts == ts

    def test_record_event_returns_event_instance(self, store):
        cid = store.create_client("bot_x", kind="bot")
        store.record_event(cid, "msg", "{}")
        events = store.list_events(cid)
        assert isinstance(events[0], Event)


class TestListEvents:
    """list_events() filtre par client_id et ordre déterministe."""

    def test_list_events_filters_by_client(self, store):
        """Events d'un autre client ne sont pas retournés."""
        cid1 = store.create_client("bot_a", kind="bot")
        cid2 = store.create_client("bot_b", kind="bot")
        store.record_event(cid1, "msg", '{"from": "a"}')
        store.record_event(cid2, "msg", '{"from": "b"}')

        events_a = store.list_events(cid1)
        events_b = store.list_events(cid2)

        assert len(events_a) == 1
        assert events_a[0].payload_json == '{"from": "a"}'
        assert len(events_b) == 1
        assert events_b[0].payload_json == '{"from": "b"}'

    def test_list_events_ordered_by_id(self, store):
        """list_events retourne les events dans l'ordre déterministe (par id)."""
        cid = store.create_client("bot_x", kind="bot")
        for i in range(5):
            store.record_event(cid, "msg", f'{{"i": {i}}}')
        events = store.list_events(cid)
        ids = [e.id for e in events]
        assert ids == sorted(ids)

    def test_list_events_empty(self, store):
        cid = store.create_client("bot_x", kind="bot")
        assert store.list_events(cid) == []


class TestCountEventsByType:
    """count_events_by_type() retourne les compteurs par type."""

    def test_count_events_multiple_types(self, store):
        """3 msg + 2 error + 1 floodwait → compteurs corrects."""
        cid = store.create_client("bot_x", kind="bot")
        for _ in range(3):
            store.record_event(cid, "msg", "{}")
        for _ in range(2):
            store.record_event(cid, "error", "{}")
        store.record_event(cid, "floodwait", "{}")

        counts = store.count_events_by_type(cid)
        assert counts == {"msg": 3, "error": 2, "floodwait": 1}

    def test_count_events_empty(self, store):
        cid = store.create_client("bot_x", kind="bot")
        assert store.count_events_by_type(cid) == {}

    def test_count_events_single_type(self, store):
        cid = store.create_client("bot_x", kind="bot")
        store.record_event(cid, "msg", "{}")
        store.record_event(cid, "msg", "{}")
        counts = store.count_events_by_type(cid)
        assert counts == {"msg": 2}

    def test_count_events_isolates_by_client(self, store):
        """Les compteurs d'un autre client ne contaminent pas."""
        cid1 = store.create_client("bot_a", kind="bot")
        cid2 = store.create_client("bot_b", kind="bot")
        store.record_event(cid1, "msg", "{}")
        store.record_event(cid1, "msg", "{}")
        store.record_event(cid2, "error", "{}")

        counts1 = store.count_events_by_type(cid1)
        counts2 = store.count_events_by_type(cid2)
        assert counts1 == {"msg": 2}
        assert counts2 == {"error": 1}


class TestRecordAlertSent:
    """record_alert_sent() insère dans alerts_sent."""

    def test_record_alert_sent_returns_id(self, store):
        cid = store.create_client("bot_x", kind="bot")
        alert_id = store.record_alert_sent(cid, "down")
        assert isinstance(alert_id, int)
        assert alert_id > 0

    def test_record_alert_sent_persisted(self, store, db_path):
        """Relecture directe SQL confirme la ligne avec un ts non vide."""
        cid = store.create_client("bot_x", kind="bot")
        store.record_alert_sent(cid, "down")

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM alerts_sent").fetchall()
        conn.close()

        assert len(rows) == 1
        row = rows[0]
        # row: (id, client_id, type, ts)
        assert row[1] == cid
        assert row[2] == "down"
        assert row[3] != ""  # ts non vide

    def test_record_alert_sent_custom_ts(self, store, db_path):
        cid = store.create_client("bot_x", kind="bot")
        ts = "2026-06-07T09:00:00+00:00"
        store.record_alert_sent(cid, "down", ts=ts)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT ts FROM alerts_sent").fetchall()
        conn.close()
        assert rows[0][0] == ts

    def test_record_alert_sent_multiple(self, store):
        cid = store.create_client("bot_x", kind="bot")
        store.record_alert_sent(cid, "down")
        store.record_alert_sent(cid, "restriction")
        id2 = store.record_alert_sent(cid, "down")
        assert id2 == 3  # 3 insertions
