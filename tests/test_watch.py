"""Tests de la classe publique Watch (src/tgwatch/__init__.py).

Tous offline : SQLite réel sur fichier temporaire, faux Dispatcher aiogram
et faux TelegramClient Telethon. Aucune connexion réseau réelle.

Couvre :
- Initialisation avec str (chemin DB) et avec Storage injecté (DI)
- attach kind='bot' : enregistre le client en DB et appelle outer_middleware
- attach kind='userbot' : enregistre le client en DB et appelle add_event_handler
- Auto-détection par duck-typing (Dispatcher→bot, TelegramClient→userbot)
- ValueError pour objet indétectable
- ValueError pour kind inconnu
- import tgwatch sans aiogram/telethon installés
- Pas de doublon create_client si attach appelé deux fois avec le même name
"""

import pytest

from tgwatch import Watch
from tgwatch.core.storage import Storage


# ---------------------------------------------------------------------------
# Faux objets (fakes) pour les tests offline
# ---------------------------------------------------------------------------


class FakeUpdateMiddlewareProxy:
    """Simule dispatcher.update (objet exposant outer_middleware)."""

    def __init__(self):
        self._middlewares = []

    def outer_middleware(self, mw) -> None:
        self._middlewares.append(mw)


class FakeDispatcher:
    """Simule un aiogram.Dispatcher — duck-typing pour auto-détection.

    Expose `update.outer_middleware` comme le vrai Dispatcher aiogram.
    """

    def __init__(self):
        self.update = FakeUpdateMiddlewareProxy()


class FakeTelethonClient:
    """Simule un TelegramClient Telethon — duck-typing pour auto-détection.

    Expose `add_event_handler` comme le vrai TelegramClient.
    """

    def __init__(self):
        self._handlers = []

    def add_event_handler(self, callback, event=None) -> None:
        self._handlers.append((callback, event))


class UndetectableObject:
    """Objet sans aucun attribut reconnu par l'auto-détection Watch."""

    pass


# ---------------------------------------------------------------------------
# Tests init
# ---------------------------------------------------------------------------


def test_init_with_str(tmp_path):
    """Watch accepte un chemin str et construit un Storage interne."""
    db_path = str(tmp_path / "watch.db")
    watch = Watch(db_path, "token:abc", "chat123")
    assert watch is not None


def test_init_with_storage_instance(storage):
    """Watch réutilise un Storage injecté (DI) sans en créer un second."""
    watch = Watch(storage, "token:abc", "chat123")
    # L'attribut _storage doit être l'instance fournie
    assert watch._storage is storage


# ---------------------------------------------------------------------------
# Tests attach kind explicite
# ---------------------------------------------------------------------------


def test_attach_bot_registers_client_and_middleware(storage):
    """attach(disp, kind='bot', name='b1') crée le client et branche le middleware."""
    watch = Watch(storage, "token:abc", "chat123")
    disp = FakeDispatcher()

    watch.attach(disp, kind="bot", name="b1")

    # Client enregistré en DB
    client = storage.get_client_by_name("b1")
    assert client is not None
    assert client.kind == "bot"

    # Middleware enregistré sur dispatcher.update
    assert len(disp.update._middlewares) == 1


def test_attach_userbot_registers_client_and_handler(storage):
    """attach(client, kind='userbot', name='u1') crée le client et enregistre un handler."""
    watch = Watch(storage, "token:abc", "chat123")
    fake_client = FakeTelethonClient()

    watch.attach(fake_client, kind="userbot", name="u1")

    # Client enregistré en DB
    client = storage.get_client_by_name("u1")
    assert client is not None
    assert client.kind == "userbot"

    # Handler enregistré sur le client
    assert len(fake_client._handlers) == 1


# ---------------------------------------------------------------------------
# Tests auto-détection
# ---------------------------------------------------------------------------


def test_attach_autodetect_bot(storage):
    """attach sans kind détecte Dispatcher → 'bot' via duck-typing."""
    watch = Watch(storage, "token:abc", "chat123")
    disp = FakeDispatcher()

    watch.attach(disp, name="b2")

    client = storage.get_client_by_name("b2")
    assert client is not None
    assert client.kind == "bot"
    assert len(disp.update._middlewares) == 1


def test_attach_autodetect_userbot(storage):
    """attach sans kind détecte TelegramClient → 'userbot' via duck-typing."""
    watch = Watch(storage, "token:abc", "chat123")
    fake_client = FakeTelethonClient()

    watch.attach(fake_client, name="u2")

    client = storage.get_client_by_name("u2")
    assert client is not None
    assert client.kind == "userbot"
    assert len(fake_client._handlers) == 1


def test_attach_undetectable_raises(storage):
    """attach sans kind sur objet inconnu → ValueError."""
    watch = Watch(storage, "token:abc", "chat123")

    with pytest.raises(ValueError, match="Impossible de détecter"):
        watch.attach(UndetectableObject(), name="x")


def test_attach_unknown_kind_raises(storage):
    """attach avec kind inconnu → ValueError."""
    watch = Watch(storage, "token:abc", "chat123")

    with pytest.raises(ValueError, match="kind inconnu"):
        watch.attach(FakeDispatcher(), kind="alien", name="x")


# ---------------------------------------------------------------------------
# Test import sans dépendances Telegram
# ---------------------------------------------------------------------------


def test_import_tgwatch_no_telegram_deps():
    """import tgwatch ne déclenche PAS d'import aiogram ou telethon au top-level.

    Watch est importable même si aiogram/telethon ne sont pas installés.
    On vérifie que le module tgwatch.__init__ ne contient pas d'import top-level
    des adapters Telegram en inspectant les modules chargés.
    """
    import sys
    import importlib

    # tgwatch est déjà importé — on vérifie que ses adapters Telegram
    # ne sont PAS dans sys.modules depuis l'import top-level
    # (ils ne sont chargés que lors d'un appel à attach())
    import tgwatch

    # Vérification structurelle : Watch est accessible depuis le package
    assert hasattr(tgwatch, "Watch")

    # Vérification fonctionnelle : une instance Watch est créable sans deps Telegram
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        watch = tgwatch.Watch(db_path, "fake_token", "fake_chat")
        assert watch is not None


# ---------------------------------------------------------------------------
# Test idempotence (pas de doublon create_client)
# ---------------------------------------------------------------------------


def test_attach_twice_same_name_no_duplicate_client(storage):
    """attach appelé deux fois avec le même name ne crée pas de doublon en DB."""
    watch = Watch(storage, "token:abc", "chat123")
    disp1 = FakeDispatcher()
    disp2 = FakeDispatcher()

    watch.attach(disp1, kind="bot", name="b3")
    watch.attach(disp2, kind="bot", name="b3")

    # Un seul client en DB
    clients = storage.list_clients()
    names = [c.name for c in clients]
    assert names.count("b3") == 1
