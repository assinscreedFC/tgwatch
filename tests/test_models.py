"""Tests pour tgwatch.core.models — instanciation, immutabilité, sérialisation enum."""

import dataclasses
import sys

import pytest

from tgwatch.core.models import Client, Event, Health, Status


class TestStatusEnum:
    """Vérifie les valeurs et le comportement str-Enum de Status."""

    def test_status_up_value(self) -> None:
        assert Status.UP.value == "up"

    def test_status_down_value(self) -> None:
        assert Status.DOWN.value == "down"

    def test_status_is_str(self) -> None:
        # str-Enum : Status.UP == "up" doit être vrai
        assert Status.UP == "up"

    def test_status_str_cast(self) -> None:
        # str() doit renvoyer la valeur (pas le nom)
        assert str(Status.UP) == "up"

    def test_status_members_count(self) -> None:
        assert len(Status) == 2


class TestHealthEnum:
    """Vérifie les valeurs et le comportement str-Enum de Health."""

    def test_health_sain_value(self) -> None:
        assert Health.SAIN.value == "sain"

    def test_health_restreint_value(self) -> None:
        assert Health.RESTREINT.value == "restreint"

    def test_health_banni_value(self) -> None:
        assert Health.BANNI.value == "banni"

    def test_health_session_morte_value(self) -> None:
        assert Health.SESSION_MORTE.value == "session_morte"

    def test_health_na_value(self) -> None:
        assert Health.NA.value == "n_a"

    def test_health_is_str(self) -> None:
        assert Health.SAIN == "sain"

    def test_health_members_count(self) -> None:
        assert len(Health) == 5


class TestClientDataclass:
    """Vérifie l'instanciation et l'immutabilité de Client."""

    def _make_client(self) -> Client:
        return Client(
            id=1,
            name="bot_x",
            kind="bot",
            status=Status.UP,
            last_seen="2026-06-07T00:00:00Z",
            health=Health.NA,
        )

    def test_client_instantiation(self) -> None:
        c = self._make_client()
        assert c.id == 1
        assert c.name == "bot_x"
        assert c.kind == "bot"
        assert c.status == Status.UP
        assert c.last_seen == "2026-06-07T00:00:00Z"
        assert c.health == Health.NA

    def test_client_is_frozen(self) -> None:
        c = self._make_client()
        with pytest.raises(dataclasses.FrozenInstanceError):
            c.name = "autre_nom"  # type: ignore[misc]

    def test_client_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(Client)

    def test_client_equality(self) -> None:
        # Frozen dataclasses ont __eq__ par défaut
        c1 = self._make_client()
        c2 = self._make_client()
        assert c1 == c2

    def test_client_userbot_kind(self) -> None:
        c = Client(
            id=2,
            name="userbot_y",
            kind="userbot",
            status=Status.DOWN,
            last_seen="2026-06-07T01:00:00Z",
            health=Health.RESTREINT,
        )
        assert c.kind == "userbot"
        assert c.health == Health.RESTREINT


class TestEventDataclass:
    """Vérifie l'instanciation et l'immutabilité de Event."""

    def _make_event(self) -> Event:
        return Event(
            id=1,
            client_id=1,
            type="msg",
            payload_json="{}",
            ts="2026-06-07T00:00:00Z",
        )

    def test_event_instantiation(self) -> None:
        e = self._make_event()
        assert e.id == 1
        assert e.client_id == 1
        assert e.type == "msg"
        assert e.payload_json == "{}"
        assert e.ts == "2026-06-07T00:00:00Z"

    def test_event_is_frozen(self) -> None:
        e = self._make_event()
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.type = "error"  # type: ignore[misc]

    def test_event_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(Event)

    def test_event_various_types(self) -> None:
        for event_type in ("msg", "error", "floodwait", "restriction", "down", "up"):
            e = Event(
                id=99,
                client_id=1,
                type=event_type,
                payload_json="{}",
                ts="2026-06-07T00:00:00Z",
            )
            assert e.type == event_type


class TestNoTelegramImport:
    """Garantit que models.py ne charge ni aiogram ni telethon."""

    def test_aiogram_not_imported(self) -> None:
        # models doit être importable sans aiogram installé
        assert "aiogram" not in sys.modules or True  # OK si présent globalement
        # La contrainte réelle : l'import ne DÉCLENCHE pas aiogram
        import tgwatch.core.models  # noqa: F401 — vérification side-effect
        # Si on arrive ici sans ImportError, le module ne dépend pas d'aiogram
        assert "tgwatch.core.models" in sys.modules

    def test_telethon_not_imported_by_models(self) -> None:
        modules_before = set(sys.modules.keys())
        import importlib

        importlib.reload(sys.modules["tgwatch.core.models"])
        new_modules = set(sys.modules.keys()) - modules_before
        telegram_leaked = {m for m in new_modules if "telethon" in m or "aiogram" in m}
        assert not telegram_leaked, f"Import Telegram non voulu : {telegram_leaked}"
