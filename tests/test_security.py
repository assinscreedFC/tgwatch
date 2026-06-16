"""Tests de sécurité dédiés SEC-01 — aucun secret n'atteint la base de données.

Ce fichier teste que le masquage centralisé dans recorder.record_event
empêche tout token/session/phone/api_hash d'apparaître dans la table events,
quelle que soit la profondeur d'imbrication du payload.

Méthode : on lit les données en SQL brut (sqlite3 standard) après insertion
via Recorder — on n'inspecte pas le model Event, on lit la colonne payload_json
directement pour s'assurer qu'aucune transformation ultérieure n'est impliquée.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from tgwatch.core.recorder import Recorder
from tgwatch.core.storage import Storage


# ---------------------------------------------------------------------------
# Fixture commune : DB temporaire réelle + Recorder
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Chemin vers une base SQLite temporaire."""
    return tmp_path / "sec_test.db"


@pytest.fixture()
def rec(db_path: Path) -> Recorder:
    """Recorder avec Storage sur DB temporaire."""
    storage = Storage(str(db_path))
    return Recorder(storage)


def _read_all_payloads(db_path: Path) -> list[str]:
    """Lit toutes les lignes payload_json en SQL brut, sans ORM."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT payload_json FROM events").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SEC-01 : Aucun secret n'atteint la DB — test principal
# ---------------------------------------------------------------------------


class TestSecretNeverReachesDb:
    """Vérifie que les secrets ne sont jamais écrits dans events.payload_json."""

    def test_bot_token_never_in_db(self, rec, db_path):
        """Un token bot Telegram n'apparaît jamais dans payload_json."""
        bot_token = "8123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        rec.record_event("userbot1", "error", {"token": bot_token})

        payloads = _read_all_payloads(db_path)
        assert len(payloads) == 1
        assert bot_token not in payloads[0], (
            f"Token bot trouvé dans payload_json : {payloads[0]}"
        )

    def test_session_string_never_in_db(self, rec, db_path):
        """Une session string Telethon n'apparaît jamais dans payload_json."""
        session_string = "1ApWapABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"
        rec.record_event("userbot1", "init", {"session_string": session_string})

        payloads = _read_all_payloads(db_path)
        assert session_string not in payloads[0], (
            f"session_string trouvé dans payload_json : {payloads[0]}"
        )

    def test_phone_number_never_in_db(self, rec, db_path):
        """Un numéro de téléphone n'apparaît jamais dans payload_json."""
        phone = "+33612345678"
        rec.record_event("userbot1", "auth", {"phone": phone})

        payloads = _read_all_payloads(db_path)
        assert phone not in payloads[0], (
            f"phone trouvé dans payload_json : {payloads[0]}"
        )

    def test_api_hash_never_in_db(self, rec, db_path):
        """Un api_hash n'apparaît jamais dans payload_json."""
        api_hash = "deadbeef0123456789abcdef01234567"
        rec.record_event("userbot1", "config", {"api_hash": api_hash})

        payloads = _read_all_payloads(db_path)
        assert api_hash not in payloads[0], (
            f"api_hash trouvé dans payload_json : {payloads[0]}"
        )

    def test_multiple_secrets_all_redacted(self, rec, db_path):
        """Un payload bourré de secrets — tous doivent être rédigés en DB."""
        bot_token = "8123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        session_string = "1ApWapABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"
        phone = "+33612345678"
        api_hash = "deadbeef0123456789abcdef01234567"

        payload = {
            "token": bot_token,
            "session_string": session_string,
            "phone": phone,
            "nested": {"api_hash": api_hash},
        }
        rec.record_event("userbot_all", "error", payload)

        payloads = _read_all_payloads(db_path)
        raw = payloads[0]

        # Aucun secret littéral ne doit apparaître
        assert bot_token not in raw, f"bot_token dans DB : {raw}"
        assert session_string not in raw, f"session_string dans DB : {raw}"
        assert phone not in raw, f"phone dans DB : {raw}"
        assert api_hash not in raw, f"api_hash dans DB : {raw}"

        # Le marqueur de masquage doit être présent
        assert "***" in raw, f"*** absent du payload masqué : {raw}"

    def test_token_regex_in_message_value_never_in_db(self, rec, db_path):
        """Un token embarqué dans une valeur string est rédigé même hors denylist."""
        bot_token = "8123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        payload = {"message": f"Erreur lors de la connexion avec le token {bot_token}"}
        rec.record_event("bot_msg", "error", payload)

        payloads = _read_all_payloads(db_path)
        assert bot_token not in payloads[0], (
            f"Token bot dans payload_json (valeur embarquée) : {payloads[0]}"
        )

    def test_nested_list_secrets_never_in_db(self, rec, db_path):
        """Les secrets dans une liste imbriquée sont rédigés."""
        phone = "+33699887766"
        payload = {"contacts": [{"phone": phone}, {"name": "Alice"}]}
        rec.record_event("bot_contacts", "sync", payload)

        payloads = _read_all_payloads(db_path)
        assert phone not in payloads[0], (
            f"phone (liste imbriquée) dans payload_json : {payloads[0]}"
        )

    def test_case_insensitive_key_never_in_db(self, rec, db_path):
        """Les clés sensibles en majuscules sont aussi rédigées."""
        session_val = "MySecretSession12345"
        payload = {"SESSION_STRING": session_val}
        rec.record_event("bot_case", "init", payload)

        payloads = _read_all_payloads(db_path)
        assert session_val not in payloads[0], (
            f"SESSION_STRING (maj) trouvé dans payload_json : {payloads[0]}"
        )

    def test_no_secret_in_multiple_events(self, rec, db_path):
        """Plusieurs insertions successives — aucun secret dans aucun event."""
        bot_token = "8123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        rec.record_event("bot_multi", "msg", {"token": bot_token, "text": "hello"})
        rec.record_event("bot_multi", "error", {"session": "s3cr3t_sess10n"})
        rec.record_event("bot_multi", "heartbeat", None)

        payloads = _read_all_payloads(db_path)
        for raw in payloads:
            assert bot_token not in raw
            assert "s3cr3t_sess10n" not in raw


# ---------------------------------------------------------------------------
# SEC-02 : Payload borné à 4096 octets — vérifié en DB
# ---------------------------------------------------------------------------


class TestPayloadBoundedInDb:
    """Vérifie que les gros payloads sont tronqués avant insertion."""

    def test_large_payload_truncated_in_db(self, rec, db_path):
        """Un payload > 4096 octets est tronqué dans la DB."""
        big_payload = {"data": "X" * 5000}
        rec.record_event("bot_big", "msg", big_payload)

        payloads = _read_all_payloads(db_path)
        raw = payloads[0]
        assert len(raw.encode("utf-8")) <= 4096, (
            f"payload_json dépasse 4096 octets en DB : {len(raw.encode())} octets"
        )
        assert "[truncated]" in raw

    def test_small_payload_not_truncated_in_db(self, rec, db_path):
        """Un petit payload reste intact en DB."""
        payload = {"key": "value", "count": 42}
        rec.record_event("bot_small", "msg", payload)

        payloads = _read_all_payloads(db_path)
        assert "[truncated]" not in payloads[0]
        assert "value" in payloads[0]


# ---------------------------------------------------------------------------
# S2 : account_id / user_id / tg_id / id masqués (nouvelles clés sensibles)
# ---------------------------------------------------------------------------


class TestAccountIdMasking:
    """S2 — Les clés account_id/user_id/tg_id/id sont masquées dans les payloads."""

    def test_account_id_masked(self, rec, db_path):
        """account_id est masqué dans le payload."""
        rec.record_event("bot1", "event", {"account_id": 123456})
        raw = _read_all_payloads(db_path)[0]
        assert "123456" not in raw
        assert "***" in raw

    def test_user_id_masked(self, rec, db_path):
        """user_id est masqué dans le payload."""
        rec.record_event("bot1", "event", {"user_id": 456789})
        raw = _read_all_payloads(db_path)[0]
        assert "456789" not in raw
        assert "***" in raw

    def test_tg_id_masked(self, rec, db_path):
        """tg_id est masqué dans le payload."""
        rec.record_event("bot1", "event", {"tg_id": 789012})
        raw = _read_all_payloads(db_path)[0]
        assert "789012" not in raw
        assert "***" in raw

    def test_id_masked(self, rec, db_path):
        """id est masqué dans le payload."""
        rec.record_event("bot1", "event", {"id": 1})
        raw = _read_all_payloads(db_path)[0]
        # "1" alone is too short to assert absence, check *** present
        assert "***" in raw

    def test_all_account_keys_masked_together(self, rec, db_path):
        """Toutes les clés account sont masquées ensemble."""
        rec.record_event(
            "bot1",
            "event",
            {"account_id": 123, "user_id": 456, "tg_id": 789, "id": 1},
        )
        raw = _read_all_payloads(db_path)[0]
        assert "123" not in raw
        assert "456" not in raw
        assert "789" not in raw
        assert "***" in raw


# ---------------------------------------------------------------------------
# S3 : session string Telethon masquée dans les valeurs string libres
# ---------------------------------------------------------------------------


class TestSessionStringMaskingInValue:
    """S3 — Une session string Telethon dans une valeur libre est masquée."""

    def test_session_string_in_free_value_masked(self, rec, db_path):
        """Une fausse session string (>=30 chars base64 après '1') est masquée."""
        fake_session = "1A" + "b" * 40
        rec.record_event("bot1", "event", {"info": fake_session})
        raw = _read_all_payloads(db_path)[0]
        assert fake_session not in raw
        assert "***" in raw

    def test_short_normal_text_not_masked(self, rec, db_path):
        """Un texte court normal reste intact."""
        rec.record_event("bot1", "event", {"msg": "hello world"})
        raw = _read_all_payloads(db_path)[0]
        assert "hello world" in raw

    def test_bot_token_still_masked_by_token_re(self, rec, db_path):
        """Un token bot (TOKEN_RE) reste masqué — pas de régression."""
        bot_token = "8123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        rec.record_event("bot1", "event", {"info": bot_token})
        raw = _read_all_payloads(db_path)[0]
        assert bot_token not in raw
        assert "***" in raw


# ---------------------------------------------------------------------------
# S1 : payload error aiogram ne contient pas "msg" (str(exc) supprimé)
# ---------------------------------------------------------------------------


class TestAiogramErrorPayloadNoMsg:
    """S1 — Le payload error aiogram contient uniquement {"type": "<ExcName>"}."""

    def test_error_payload_has_type_not_msg(self, rec, db_path):
        """Le payload error n'a que 'type', jamais 'msg'."""
        import asyncio
        import json

        from tgwatch.adapters.aiogram import TgwatchMiddleware
        from tgwatch.core.storage import Storage

        storage = Storage(str(db_path))

        async def _run():
            mw = TgwatchMiddleware(rec, "bot_s1")

            async def failing_handler(event, data):
                raise RuntimeError("secret info that must not leak")

            from aiogram.types import Update

            update = Update.model_validate({"update_id": 1})
            try:
                await mw(failing_handler, update, {})
            except RuntimeError:
                pass

        asyncio.run(_run())

        payloads = _read_all_payloads(db_path)
        error_payloads = [p for p in payloads if "RuntimeError" in p or "error" in p]
        # Find error event
        all_raw = " ".join(payloads)
        assert "msg" not in all_raw or all(['"msg"' not in p for p in payloads if "RuntimeError" in p])
        # Specifically: payload should have type=RuntimeError
        error_raw = [p for p in payloads if "RuntimeError" in p]
        assert len(error_raw) >= 1
        for raw in error_raw:
            parsed = json.loads(raw)
            assert "type" in parsed
            assert parsed["type"] == "RuntimeError"
            assert "msg" not in parsed
