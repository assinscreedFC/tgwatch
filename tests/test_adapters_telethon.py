"""Tests offline pour l'adapter telethon.

Utilise FakeTelethonClient — aucun réseau Telegram requis.
Les fixtures tmp_db/storage/recorder sont définies localement pour garantir
l'indépendance de ce plan (les fixtures conftest.py sont équivalentes, mais
les fixtures locales priment sur le conftest — comportement pytest standard).
"""

import asyncio
import importlib

import pytest
from unittest.mock import AsyncMock, MagicMock

from tgwatch.core.recorder import Recorder
from tgwatch.core.storage import Storage


# ---------------------------------------------------------------------------
# Fixtures locales (indépendance du plan — priment sur conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path):
    """Chemin vers une base SQLite temporaire (str)."""
    return str(tmp_path / "tgwatch_telethon.db")


@pytest.fixture()
def storage(tmp_db):
    """Storage initialisé sur DB temporaire."""
    return Storage(tmp_db)


@pytest.fixture()
def recorder(storage):
    """Recorder injecté avec le storage temporaire."""
    return Recorder(storage)


# ---------------------------------------------------------------------------
# Fake client telethon (offline — pas d'import telethon requis ici)
# ---------------------------------------------------------------------------


class FakeTelethonClient:
    """Faux TelegramClient pour tests offline — phase 2 + phase 3.

    Mémorise les handlers enregistrés via add_event_handler et permet de
    simuler la réception d'un message via simulate_message.
    Possède un get_me AsyncMock pour tester la sonde de santé (Phase 3).
    """

    def __init__(self):
        self._handlers: list = []
        fake_user = MagicMock()
        fake_user.restricted = False
        self.get_me = AsyncMock(return_value=fake_user)

    def add_event_handler(self, callback, event=None) -> None:
        self._handlers.append((event, callback))

    async def simulate_message(self, fake_event=None) -> None:
        for _, callback in self._handlers:
            await callback(fake_event or object())


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def count_events_by_type(storage: Storage, client_id: int) -> dict[str, int]:
    """Retourne un dict {event_type: count} pour un client donné."""
    events = storage.list_events(client_id)
    counts: dict[str, int] = {}
    for event in events:
        counts[event.type] = counts.get(event.type, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_handler_registered(recorder):
    """attach() enregistre exactement un handler sur le client."""
    from tgwatch.adapters.telethon import attach

    client = FakeTelethonClient()
    attach(client, recorder, "userbot_test")

    assert len(client._handlers) == 1


async def test_heartbeat(recorder, storage):
    """Chaque message simulé déclenche recorder.heartbeat — last_seen non None."""
    from tgwatch.adapters.telethon import attach

    client = FakeTelethonClient()
    attach(client, recorder, "userbot_test")

    await client.simulate_message()

    c = storage.get_client_by_name("userbot_test")
    assert c is not None
    assert c.last_seen is not None


async def test_counts_message(recorder, storage):
    """Deux messages simulés → compteur 'msg' égal à 2."""
    from tgwatch.adapters.telethon import attach

    client = FakeTelethonClient()
    attach(client, recorder, "userbot_test")

    # Première simulation
    await client.simulate_message()
    c = storage.get_client_by_name("userbot_test")
    counts = count_events_by_type(storage, c.id)
    assert counts.get("msg") == 1

    # Deuxième simulation
    await client.simulate_message()
    counts = count_events_by_type(storage, c.id)
    assert counts.get("msg") == 2


def test_lazy_import():
    """Le module s'importe sans erreur.

    Vérifie l'absence de 'from telethon' et 'from tgwatch.health' au top-level
    du module adapter en inspectant le code source.
    """
    import tgwatch.adapters.telethon as adapter_module

    # Le module est importable
    assert adapter_module is not None

    # Vérifier que l'import telethon et health ne sont pas au niveau module
    # (ils doivent être uniquement dans les fonctions attach/_install_call_interceptor)
    source_file = adapter_module.__file__
    assert source_file is not None
    with open(source_file, encoding="utf-8") as f:
        lines = f.readlines()

    top_level_telethon_imports = [
        line for line in lines
        if line.startswith("from telethon") or line.startswith("import telethon")
    ]
    assert len(top_level_telethon_imports) == 0, (
        f"Import telethon trouvé au top-level : {top_level_telethon_imports}"
    )

    top_level_health_imports = [
        line for line in lines
        if line.startswith("from tgwatch.health")
    ]
    assert len(top_level_health_imports) == 0, (
        f"Import tgwatch.health trouvé au top-level : {top_level_health_imports}"
    )


# ---------------------------------------------------------------------------
# Tests Phase 3 — Task 1 : probe wiring + régression Phase 2
# ---------------------------------------------------------------------------


async def test_attach_returns_task(recorder):
    """attach() dans un contexte async retourne une asyncio.Task (sonde démarrée)."""
    from tgwatch.adapters.telethon import attach

    client = FakeTelethonClient()
    task = attach(client, recorder, "userbot_probe")

    assert task is not None
    assert isinstance(task, asyncio.Task)

    # Cleanup : annuler la task pour ne pas la laisser pendante
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def test_attach_no_loop_returns_none(recorder):
    """attach() hors event loop retourne None, ne lève pas, handler quand même enregistré."""
    from tgwatch.adapters.telethon import attach

    client = FakeTelethonClient()
    # Appel synchrone = hors event loop
    result = attach(client, recorder, "userbot_no_loop")

    assert result is None
    # Le handler NewMessage est quand même enregistré (comportement Phase 2 intact)
    assert len(client._handlers) == 1


async def test_phase2_intact(recorder, storage):
    """Après attach(), simulate_message → heartbeat (last_seen non None) + compteur msg == 1."""
    from tgwatch.adapters.telethon import attach

    client = FakeTelethonClient()
    task = attach(client, recorder, "userbot_regress")

    await client.simulate_message()

    c = storage.get_client_by_name("userbot_regress")
    assert c is not None
    assert c.last_seen is not None
    counts = count_events_by_type(storage, c.id)
    assert counts.get("msg") == 1

    # Cleanup
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Tests Phase 3 — Task 2 : _call intercept opt-in
# ---------------------------------------------------------------------------


async def test_intercept_off_by_default(recorder):
    """attach sans auto_intercept ne touche pas client._call."""
    from tgwatch.adapters.telethon import attach

    client = FakeTelethonClient()
    original_call = AsyncMock(return_value="original")
    client._call = original_call

    task = attach(client, recorder, "userbot_intercept_off")

    # _call doit être inchangé (même objet)
    assert client._call is original_call

    # Cleanup
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_intercept_classifies_and_reraises(recorder, storage):
    """auto_intercept=True : PeerFloodError est classifiée ET re-levée."""
    from telethon.errors import PeerFloodError
    from tgwatch.adapters.telethon import attach
    from tgwatch.core.models import Health

    client = FakeTelethonClient()
    client._call = AsyncMock(side_effect=PeerFloodError(request=None))

    task = attach(client, recorder, "userbot_intercept", auto_intercept=True)

    # L'exception doit être re-levée (jamais avalée — ADPT-03)
    with pytest.raises(PeerFloodError):
        await client._call(None, None)

    # La santé doit être classifiée comme RESTREINT
    c = storage.get_client_by_name("userbot_intercept")
    assert c is not None
    assert c.health == Health.RESTREINT

    # Cleanup
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_intercept_success_passthrough(recorder):
    """_call qui réussit : wrapper retourne la valeur, aucun changement de health."""
    from tgwatch.adapters.telethon import attach
    from tgwatch.core.models import Health

    sentinel = object()
    client = FakeTelethonClient()
    client._call = AsyncMock(return_value=sentinel)

    task = attach(client, recorder, "userbot_passthrough", auto_intercept=True)

    result = await client._call(None, None)
    assert result is sentinel

    # Aucune dégradation de santé
    health = recorder.get_health("userbot_passthrough")
    assert health == Health.NA

    # Cleanup
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_intercept_missing_call_safe(recorder):
    """client sans _call + auto_intercept=True : pas de crash (hasattr guard)."""
    from tgwatch.adapters.telethon import attach

    client = FakeTelethonClient()
    # Supprimer _call pour simuler un client sans cet attribut
    if hasattr(client, "_call"):
        del client._call

    # Ne doit pas lever d'exception
    task = attach(client, recorder, "userbot_no_call", auto_intercept=True)

    # Cleanup
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Task 3 : handler try/except — recorder.heartbeat qui lève ne propage pas
# ---------------------------------------------------------------------------


async def test_handler_recorder_error_not_propagated(recorder):
    """Si recorder.heartbeat lève, le handler logge et ne propage pas l'exception."""
    from unittest.mock import MagicMock, patch
    from tgwatch.adapters.telethon import attach

    client = FakeTelethonClient()

    # Remplacer recorder par un fake dont heartbeat lève
    fake_recorder = MagicMock()
    fake_recorder.heartbeat.side_effect = RuntimeError("DB is gone")

    task = attach(client, fake_recorder, "userbot_safe")

    # simulate_message ne doit PAS propager l'exception du recorder
    try:
        await client.simulate_message()
    except Exception as exc:
        pytest.fail(f"Le handler a propagé une exception : {exc}")

    # Cleanup
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
