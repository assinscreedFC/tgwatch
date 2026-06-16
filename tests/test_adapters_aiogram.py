"""Tests offline du middleware aiogram pour tgwatch.

Couvre ADPT-01 (capture updates/heartbeat/msg, import lazy) et ADPT-03 (re-raise).
Toutes les I/O sont offline (SQLite temp, pas de vrai Dispatcher ni Bot).
asyncio_mode = "auto" dans pyproject.toml — pas de @pytest.mark.asyncio nécessaire.
"""

import json
import pytest

from tgwatch.adapters.aiogram import TgwatchMiddleware, attach


# ---------------------------------------------------------------------------
# Helpers locaux
# ---------------------------------------------------------------------------


def _make_fake_update(with_message: bool = False):
    """Retourne un vrai objet aiogram.types.Update minimal.

    Utilise model_validate pour construire un Update sans Dispatcher réel.
    with_message=True → Update avec un message non-None (champ message présent).
    """
    from aiogram.types import Update

    if with_message:
        # Update minimal avec message (chat_id et from requis)
        return Update.model_validate(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 100, "type": "private"},
                    "date": 1700000000,
                    "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                    "text": "hello",
                },
            }
        )
    else:
        # Update minimal sans message
        return Update.model_validate({"update_id": 1})


class FakeDispatcher:
    """Dispatcher minimal pour tester attach() sans aiogram réel."""

    def __init__(self):
        self._registered = []

        class _UpdateRouter:
            def __init__(self, parent):
                self._parent = parent

            def outer_middleware(self, mw) -> None:
                self._parent._registered.append(mw)

        self.update = _UpdateRouter(self)


# ---------------------------------------------------------------------------
# Tests TgwatchMiddleware
# ---------------------------------------------------------------------------


async def test_middleware_heartbeat(recorder, storage):
    """Un update traversant le middleware met à jour last_seen du client."""
    mw = TgwatchMiddleware(recorder, "bot_test")

    async def dummy_handler(event, data):
        return "ok"

    update = _make_fake_update()
    await mw(dummy_handler, update, {})

    client = storage.get_client_by_name("bot_test")
    assert client is not None
    assert client.last_seen is not None


async def test_middleware_counts_message(recorder, storage):
    """Un Update avec .message non None incrémente le compteur 'msg'."""
    mw = TgwatchMiddleware(recorder, "bot_msg")

    async def dummy_handler(event, data):
        return "ok"

    update = _make_fake_update(with_message=True)
    await mw(dummy_handler, update, {})

    client = storage.get_client_by_name("bot_msg")
    assert client is not None
    counts = storage.count_events_by_type(client.id)
    assert counts.get("msg", 0) == 1


async def test_middleware_reraises_and_records(recorder, storage):
    """Handler qui lève RuntimeError → exception re-raised ET event 'error' enregistré."""
    import json

    mw = TgwatchMiddleware(recorder, "bot_err")

    async def failing_handler(event, data):
        raise RuntimeError("boom")

    update = _make_fake_update()
    with pytest.raises(RuntimeError, match="boom"):
        await mw(failing_handler, update, {})

    client = storage.get_client_by_name("bot_err")
    assert client is not None
    events = storage.list_events(client.id)
    error_events = [e for e in events if e.type == "error"]
    assert len(error_events) == 1
    # S1 : payload contient {"type": "RuntimeError"} et pas de clé "msg"
    payload = json.loads(error_events[0].payload_json)
    assert payload.get("type") == "RuntimeError"
    assert "msg" not in payload


async def test_middleware_floodwait(recorder, storage):
    """Handler qui lève TelegramRetryAfter → re-raised ET event 'floodwait' avec retry_after."""
    from aiogram.exceptions import TelegramRetryAfter

    mw = TgwatchMiddleware(recorder, "bot_flood")

    async def flood_handler(event, data):
        raise TelegramRetryAfter(retry_after=42, method=None, message="Flood control exceeded")

    update = _make_fake_update()
    with pytest.raises(TelegramRetryAfter):
        await mw(flood_handler, update, {})

    client = storage.get_client_by_name("bot_flood")
    assert client is not None
    events = storage.list_events(client.id)
    flood_events = [e for e in events if e.type == "floodwait"]
    assert len(flood_events) == 1
    payload = json.loads(flood_events[0].payload_json)
    assert payload.get("retry_after") == 42


async def test_attach_registers_outer_middleware(recorder):
    """attach() enregistre bien le middleware sur dispatcher.update.outer_middleware."""
    dispatcher = FakeDispatcher()
    attach(dispatcher, recorder, "bot_attach")
    assert len(dispatcher._registered) == 1
    assert isinstance(dispatcher._registered[0], TgwatchMiddleware)


async def test_no_secret_in_error_event(recorder, storage):
    """Un token bot dans le message d'exception ne doit pas apparaître en DB (SEC-01)."""
    bot_token = "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    mw = TgwatchMiddleware(recorder, "bot_sec")

    async def leaky_handler(event, data):
        raise RuntimeError(f"Auth failed with token={bot_token}")

    update = _make_fake_update()
    with pytest.raises(RuntimeError):
        await mw(leaky_handler, update, {})

    client = storage.get_client_by_name("bot_sec")
    assert client is not None
    events = storage.list_events(client.id)
    error_events = [e for e in events if e.type == "error"]
    assert len(error_events) == 1
    # Le token ne doit pas apparaître dans le payload stocké
    assert bot_token not in error_events[0].payload_json
