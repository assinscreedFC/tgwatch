"""Tests pour tgwatch.cli — parseur argparse, status, watch, sécurité token.

100% offline : Storage SQLite temp réel, watch_loop/Alerter mockés.
Couvre : sous-commandes, fallback env, validation erreurs, tableau status,
sécurité token (jamais en stdout/stderr), PKG-01 (sys.modules propre).
"""

from __future__ import annotations

import logging
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from tgwatch.cli import _resolve, build_parser, cmd_status, cmd_watch, main
from tgwatch.core.models import Health, Status
from tgwatch.core.storage import Storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def populated_storage(tmp_db: str) -> Storage:
    """Storage avec deux clients et quelques événements."""
    s = Storage(tmp_db)
    cid1 = s.create_client("mybot", "bot")
    s.update_status("mybot", Status.UP)
    s.record_event(cid1, "msg", "{}")
    s.record_event(cid1, "msg", "{}")
    s.record_event(cid1, "error", "{}")

    cid2 = s.create_client("myuserbot", "userbot")
    s.set_health("myuserbot", Health.RESTREINT)
    s.record_event(cid2, "floodwait", "{}")
    return s


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_status_storage(self) -> None:
        p = build_parser()
        ns = p.parse_args(["status", "--storage", "foo.db"])
        assert ns.command == "status"
        assert ns.storage == "foo.db"

    def test_status_no_storage_default_none(self) -> None:
        p = build_parser()
        ns = p.parse_args(["status"])
        assert ns.storage is None

    def test_watch_all_flags(self) -> None:
        p = build_parser()
        ns = p.parse_args([
            "watch",
            "--storage", "x.db",
            "--token", "SECRET",
            "--chat-id", "42",
            "--interval", "5",
            "--heartbeat-threshold", "30",
        ])
        assert ns.command == "watch"
        assert ns.storage == "x.db"
        assert ns.token == "SECRET"
        assert ns.chat_id == "42"
        assert ns.interval == 5
        assert ns.heartbeat_threshold == 30

    def test_watch_defaults(self) -> None:
        p = build_parser()
        ns = p.parse_args(["watch"])
        assert ns.interval == 60
        assert ns.heartbeat_threshold == 300
        assert ns.token is None
        assert ns.chat_id is None

    def test_no_subcommand_returns_namespace(self) -> None:
        p = build_parser()
        ns = p.parse_args([])
        assert ns.command is None


# ---------------------------------------------------------------------------
# _resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_flag_has_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "env_value")
        assert _resolve("cli_value", "MY_VAR") == "cli_value"

    def test_fallback_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "env_value")
        assert _resolve(None, "MY_VAR") == "env_value"

    def test_both_absent_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_VAR", raising=False)
        assert _resolve(None, "MY_VAR") is None


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def test_missing_storage_returns_1(self, capsys: pytest.CaptureFixture) -> None:
        p = build_parser()
        ns = p.parse_args(["status"])
        ret = cmd_status(ns)
        assert ret == 1
        assert "TGWATCH_STORAGE" in capsys.readouterr().err

    def test_empty_db_returns_0(self, tmp_db: str, capsys: pytest.CaptureFixture) -> None:
        ret = main(["status", "--storage", tmp_db])
        assert ret == 0
        assert "Aucun client" in capsys.readouterr().out

    def test_table_headers_present(
        self, populated_storage: Storage, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["status", "--storage", populated_storage.path])
        assert ret == 0
        out = capsys.readouterr().out
        for header in ["NAME", "KIND", "STATUS", "LAST_SEEN", "HEALTH", "MSG", "ERROR", "FLOODWAIT"]:
            assert header in out

    def test_table_contains_client_data(
        self, populated_storage: Storage, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["status", "--storage", populated_storage.path])
        assert ret == 0
        out = capsys.readouterr().out
        assert "mybot" in out
        assert "myuserbot" in out
        assert "bot" in out
        assert "userbot" in out

    def test_counters_in_output(
        self, populated_storage: Storage, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["status", "--storage", populated_storage.path])
        assert ret == 0
        out = capsys.readouterr().out
        # mybot a 2 msg et 1 error
        lines = [l for l in out.splitlines() if "mybot" in l]
        assert lines, "ligne mybot absente"
        assert "2" in lines[0]  # msg count

    def test_env_fallback_storage(
        self,
        tmp_db: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("TGWATCH_STORAGE", tmp_db)
        ret = main(["status"])
        assert ret == 0

    def test_no_secrets_in_output(
        self, populated_storage: Storage, capsys: pytest.CaptureFixture
    ) -> None:
        """T-05-02 : status ne rend aucun payload_json ni secret."""
        ret = main(["status", "--storage", populated_storage.path])
        assert ret == 0
        out = capsys.readouterr().out
        # payload_json est toujours '{}' mais ne doit pas leaker de vraies données
        # On vérifie que le token (absent ici) ne figurerait jamais
        assert "token" not in out.lower()


# ---------------------------------------------------------------------------
# cmd_watch — validation
# ---------------------------------------------------------------------------


class TestCmdWatchValidation:
    def test_missing_all_returns_1(self, capsys: pytest.CaptureFixture) -> None:
        ret = main(["watch"])
        assert ret == 1
        err = capsys.readouterr().err
        assert "TGWATCH_STORAGE" in err
        assert "TGWATCH_ALERT_BOT_TOKEN" in err
        assert "TGWATCH_ALERT_CHAT_ID" in err

    def test_missing_token_returns_1(
        self, tmp_db: str, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["watch", "--storage", tmp_db, "--chat-id", "42"])
        assert ret == 1
        err = capsys.readouterr().err
        assert "TGWATCH_ALERT_BOT_TOKEN" in err

    def test_missing_chat_id_returns_1(
        self, tmp_db: str, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["watch", "--storage", tmp_db, "--token", "tok"])
        assert ret == 1
        err = capsys.readouterr().err
        assert "TGWATCH_ALERT_CHAT_ID" in err

    def test_token_never_in_error_output(
        self, tmp_db: str, capsys: pytest.CaptureFixture
    ) -> None:
        """T-05-01 : le token ne doit JAMAIS apparaître dans stderr."""
        ret = main(["watch", "--storage", tmp_db, "--token", "MY_SECRET_TOKEN"])
        assert ret == 1
        err = capsys.readouterr().err
        assert "MY_SECRET_TOKEN" not in err

    def test_env_fallback_all_three(
        self,
        tmp_db: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("TGWATCH_STORAGE", tmp_db)
        monkeypatch.setenv("TGWATCH_ALERT_BOT_TOKEN", "envtok")
        monkeypatch.setenv("TGWATCH_ALERT_CHAT_ID", "111")
        with patch("tgwatch.watchdog.watch_loop") as mock_loop:
            ret = main(["watch"])
        assert ret == 0
        mock_loop.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_watch — câblage watch_loop
# ---------------------------------------------------------------------------


class TestCmdWatchWiring:
    def test_watch_loop_called_with_correct_kwargs(
        self, tmp_db: str, capsys: pytest.CaptureFixture
    ) -> None:
        with patch("tgwatch.watchdog.watch_loop") as mock_loop:
            ret = main([
                "watch",
                "--storage", tmp_db,
                "--token", "tok",
                "--chat-id", "999",
                "--interval", "10",
                "--heartbeat-threshold", "60",
            ])
        assert ret == 0
        mock_loop.assert_called_once()
        _, kwargs = mock_loop.call_args
        assert kwargs["poll_interval"] == 10
        assert kwargs["heartbeat_threshold"] == 60

    def test_watch_startup_message_no_token(
        self, tmp_db: str, capsys: pytest.CaptureFixture
    ) -> None:
        """T-05-01 : le message 'watchdog démarré' ne contient pas le token."""
        with patch("tgwatch.watchdog.watch_loop"):
            main(["watch", "--storage", tmp_db, "--token", "SUPER_SECRET", "--chat-id", "1"])
        out, err = capsys.readouterr()
        assert "SUPER_SECRET" not in out
        assert "SUPER_SECRET" not in err

    def test_alerter_receives_token_not_echoed(
        self, tmp_db: str, capsys: pytest.CaptureFixture
    ) -> None:
        """Le token est passé à Alerter mais jamais loggé/affiché."""
        captured_alerter: list = []

        def fake_watch_loop(storage, alerter, **kw) -> None:
            captured_alerter.append(alerter)

        with patch("tgwatch.watchdog.watch_loop", fake_watch_loop):
            main(["watch", "--storage", tmp_db, "--token", "MY_TOKEN", "--chat-id", "1"])

        out, err = capsys.readouterr()
        assert "MY_TOKEN" not in out
        assert "MY_TOKEN" not in err
        # L'alerter a bien reçu le token (vérification interne)
        assert captured_alerter[0]._token == "MY_TOKEN"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# main — comportements généraux
# ---------------------------------------------------------------------------


class TestMain:
    def test_no_command_returns_2(self, capsys: pytest.CaptureFixture) -> None:
        ret = main([])
        assert ret == 2
        assert capsys.readouterr().err  # help affiché sur stderr

    def test_dunder_main_entry_point(self) -> None:
        """Vérifie que main() est importable sans crash."""
        from tgwatch.cli import main as cli_main
        assert callable(cli_main)


# ---------------------------------------------------------------------------
# PKG-01 : cli.py ne contient aucun import aiogram/telethon au niveau module
# ---------------------------------------------------------------------------


class TestPkg01:
    def test_cli_source_no_top_level_aiogram_import(self) -> None:
        """PKG-01 : le source cli.py ne contient pas d'import aiogram au niveau module."""
        import inspect
        import tgwatch.cli as cli_mod
        source = inspect.getsource(cli_mod)
        # Les imports locaux (dans cmd_watch) sont autorisés,
        # mais aucun 'import aiogram' ou 'import telethon' ne doit figurer
        # en dehors d'une fonction (top-level).
        top_lines = [
            line for line in source.splitlines()
            if (line.startswith("import ") or line.startswith("from "))
            and ("aiogram" in line or "telethon" in line)
        ]
        assert top_lines == [], f"Import top-level interdit trouvé : {top_lines}"

    def test_status_runs_without_watch_imports(
        self, tmp_db: str, capsys: pytest.CaptureFixture
    ) -> None:
        """PKG-01 : status n'importe pas watch_loop/Alerter (imports locaux dans cmd_watch)."""
        # On vérifie qu'avant d'appeler cmd_watch, watch_loop n'est pas dans les globals cli
        import tgwatch.cli as cli_mod
        cli_globals_before = set(vars(cli_mod).keys())

        main(["status", "--storage", tmp_db])

        # watch_loop ne doit pas être importé dans l'espace global du module cli
        assert "watch_loop" not in cli_globals_before
        assert "Alerter" not in cli_globals_before


# ---------------------------------------------------------------------------
# Plan 05-02 : tests avec noms canoniques requis par les critères d'acceptance
# (complémentaires aux tests ci-dessus — couvrent les mêmes comportements mais
#  avec les noms exacts prescrits par le plan afin que grep les trouve)
# ---------------------------------------------------------------------------


def test_build_parser_status() -> None:
    """parse ['status','--storage','x.db'] → command=='status', storage=='x.db'."""
    ns = build_parser().parse_args(["status", "--storage", "x.db"])
    assert ns.command == "status"
    assert ns.storage == "x.db"


def test_status_empty(tmp_db: str, capsys: pytest.CaptureFixture) -> None:
    """DB temp vide → exit 0, stdout contient 'Aucun client'."""
    ret = main(["status", "--storage", tmp_db])
    assert ret == 0
    assert "Aucun client" in capsys.readouterr().out


def test_status_table_headers(tmp_db: str, capsys: pytest.CaptureFixture) -> None:
    """Avec ≥1 client, stdout contient les en-têtes et le nom du client."""
    s = Storage(tmp_db)
    s.create_client("bot_alpha", "bot")
    ret = main(["status", "--storage", tmp_db])
    assert ret == 0
    out = capsys.readouterr().out
    assert "NAME" in out
    assert "KIND" in out
    assert "STATUS" in out
    assert "HEALTH" in out
    assert "bot_alpha" in out


def test_status_counters(tmp_db: str, capsys: pytest.CaptureFixture) -> None:
    """client + 2 msg + 1 error → stdout contient '2' et '1' sur la ligne du client."""
    s = Storage(tmp_db)
    cid = s.create_client("count_bot", "bot")
    s.record_event(cid, "msg", "{}")
    s.record_event(cid, "msg", "{}")
    s.record_event(cid, "error", "{}")
    ret = main(["status", "--storage", tmp_db])
    assert ret == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "count_bot" in line]
    assert lines, "ligne count_bot absente"
    assert "2" in lines[0]
    assert "1" in lines[0]


def test_status_missing_storage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Sans --storage ni env → exit 1, stderr non vide."""
    monkeypatch.delenv("TGWATCH_STORAGE", raising=False)
    ret = main(["status"])
    assert ret == 1
    assert capsys.readouterr().err != ""


def test_status_no_secret_in_output(tmp_db: str, capsys: pytest.CaptureFixture) -> None:
    """T-05-02 : status ne rend pas de payload_json ni de chaîne ressemblant à un token."""
    s = Storage(tmp_db)
    cid = s.create_client("secure_bot", "bot")
    # Même si un payload masqué est enregistré, il ne doit jamais apparaître dans status
    s.record_event(cid, "msg", '{"data": "safe"}')
    ret = main(["status", "--storage", tmp_db])
    assert ret == 0
    out = capsys.readouterr().out
    # status n'affiche que le tableau (colonnes fixes) — jamais payload_json
    assert "payload" not in out.lower()
    assert "token" not in out.lower()


def test_watch_wires_watch_loop(
    tmp_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """watch_loop mocké appelé 1× avec poll_interval et heartbeat_threshold corrects."""
    calls: list[dict] = []

    def fake_loop(storage, alerter, **kwargs) -> None:  # type: ignore[no-untyped-def]
        calls.append(kwargs)

    monkeypatch.setattr("tgwatch.watchdog.watch_loop", fake_loop)
    ret = main([
        "watch",
        "--storage", tmp_db,
        "--token", "T",
        "--chat-id", "42",
        "--interval", "5",
        "--heartbeat-threshold", "30",
    ])
    assert ret == 0
    assert len(calls) == 1
    assert calls[0]["poll_interval"] == 5
    assert calls[0]["heartbeat_threshold"] == 30


def test_watch_missing_token(
    tmp_db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Token absent (flag + env) → exit 1, stderr mentionne --token."""
    monkeypatch.delenv("TGWATCH_ALERT_BOT_TOKEN", raising=False)
    ret = main(["watch", "--storage", tmp_db, "--chat-id", "42"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "TGWATCH_ALERT_BOT_TOKEN" in err


def test_watch_env_fallback(
    tmp_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """TGWATCH_* env seuls (sans flags) → watch_loop mocké appelé."""
    monkeypatch.setenv("TGWATCH_STORAGE", tmp_db)
    monkeypatch.setenv("TGWATCH_ALERT_BOT_TOKEN", "T_ENV")
    monkeypatch.setenv("TGWATCH_ALERT_CHAT_ID", "99")
    monkeypatch.setattr("tgwatch.watchdog.watch_loop", lambda *a, **kw: None)
    ret = main(["watch"])
    assert ret == 0


def test_watch_flag_overrides_env(
    tmp_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Flag --token T_FLAG + env token T_ENV → Alerter reçoit T_FLAG."""
    monkeypatch.setenv("TGWATCH_ALERT_BOT_TOKEN", "T_ENV")
    monkeypatch.setenv("TGWATCH_ALERT_CHAT_ID", "1")

    captured: list = []

    def fake_loop(storage, alerter, **kw) -> None:  # type: ignore[no-untyped-def]
        captured.append(alerter)

    monkeypatch.setattr("tgwatch.watchdog.watch_loop", fake_loop)
    ret = main(["watch", "--storage", tmp_db, "--token", "T_FLAG", "--chat-id", "1"])
    assert ret == 0
    # L'alerter doit avoir reçu T_FLAG, pas T_ENV
    assert captured[0]._token == "T_FLAG"  # type: ignore[union-attr]


def test_token_never_in_output(
    tmp_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """T-05-01 : le token sentinelle 'SUPERSECRET123' n'apparaît jamais dans stdout/stderr."""
    monkeypatch.setattr("tgwatch.watchdog.watch_loop", lambda *a, **kw: None)
    # Cas 1 : watch réussi
    main(["watch", "--storage", tmp_db, "--token", "SUPERSECRET123", "--chat-id", "1"])
    out1, err1 = capsys.readouterr()
    assert "SUPERSECRET123" not in out1
    assert "SUPERSECRET123" not in err1
    # Cas 2 : validation échoue (token fourni mais chat-id absent)
    monkeypatch.delenv("TGWATCH_ALERT_CHAT_ID", raising=False)
    main(["watch", "--storage", tmp_db, "--token", "SUPERSECRET123"])
    out2, err2 = capsys.readouterr()
    assert "SUPERSECRET123" not in out2
    assert "SUPERSECRET123" not in err2


def test_main_no_command(capsys: pytest.CaptureFixture) -> None:
    """main([]) → exit 2, aide affichée sur stderr."""
    ret = main([])
    assert ret == 2
    assert capsys.readouterr().err != ""


# ---------------------------------------------------------------------------
# Task 3 : _positive_int — validation --interval et --heartbeat-threshold
# ---------------------------------------------------------------------------


class TestPositiveInt:
    """_positive_int rejette les valeurs <= 0 (SystemExit code 2 via argparse)."""

    def test_interval_zero_rejected(self, capsys: pytest.CaptureFixture) -> None:
        """--interval 0 → SystemExit(2)."""
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args(["watch", "--interval", "0",
                                       "--storage", "x.db", "--token", "t", "--chat-id", "1"])
        assert exc_info.value.code == 2

    def test_interval_negative_rejected(self, capsys: pytest.CaptureFixture) -> None:
        """--interval -5 → SystemExit(2)."""
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args(["watch", "--interval", "-5",
                                       "--storage", "x.db", "--token", "t", "--chat-id", "1"])
        assert exc_info.value.code == 2

    def test_interval_positive_accepted(self) -> None:
        """--interval 60 → accepté, valeur == 60."""
        ns = build_parser().parse_args(["watch", "--interval", "60",
                                        "--storage", "x.db", "--token", "t", "--chat-id", "1"])
        assert ns.interval == 60

    def test_heartbeat_threshold_zero_rejected(self, capsys: pytest.CaptureFixture) -> None:
        """--heartbeat-threshold 0 → SystemExit(2)."""
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args(["watch", "--heartbeat-threshold", "0",
                                       "--storage", "x.db", "--token", "t", "--chat-id", "1"])
        assert exc_info.value.code == 2

    def test_heartbeat_threshold_negative_rejected(self, capsys: pytest.CaptureFixture) -> None:
        """--heartbeat-threshold -5 → SystemExit(2)."""
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args(["watch", "--heartbeat-threshold", "-5",
                                       "--storage", "x.db", "--token", "t", "--chat-id", "1"])
        assert exc_info.value.code == 2

    def test_heartbeat_threshold_positive_accepted(self) -> None:
        """--heartbeat-threshold 300 → accepté, valeur == 300."""
        ns = build_parser().parse_args(["watch", "--heartbeat-threshold", "300",
                                        "--storage", "x.db", "--token", "t", "--chat-id", "1"])
        assert ns.heartbeat_threshold == 300


# ---------------------------------------------------------------------------
# Task 3 : warning quand token/chat_id passés en flag
# ---------------------------------------------------------------------------


class TestFlagSecretWarning:
    """logger.warning émis quand --token ou --chat-id passés en flag CLI."""

    def test_token_flag_triggers_warning(
        self, tmp_db: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Passer --token en flag → warning dans les logs."""
        with patch("tgwatch.watchdog.watch_loop"):
            with caplog.at_level(logging.WARNING, logger="tgwatch.cli"):
                main(["watch", "--storage", tmp_db, "--token", "tok", "--chat-id", "1"])
        assert any("TGWATCH_ALERT_BOT_TOKEN" in r.message for r in caplog.records)

    def test_chat_id_flag_triggers_warning(
        self, tmp_db: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Passer --chat-id en flag → warning dans les logs."""
        with patch("tgwatch.watchdog.watch_loop"):
            with caplog.at_level(logging.WARNING, logger="tgwatch.cli"):
                main(["watch", "--storage", tmp_db, "--token", "tok", "--chat-id", "1"])
        assert any("TGWATCH_ALERT_CHAT_ID" in r.message for r in caplog.records)

    def test_env_only_no_warning(
        self,
        tmp_db: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Variables d'env uniquement → aucun warning token/chat_id."""
        monkeypatch.setenv("TGWATCH_ALERT_BOT_TOKEN", "tok")
        monkeypatch.setenv("TGWATCH_ALERT_CHAT_ID", "1")
        with patch("tgwatch.watchdog.watch_loop"):
            with caplog.at_level(logging.WARNING, logger="tgwatch.cli"):
                main(["watch", "--storage", tmp_db])
        token_warnings = [
            r for r in caplog.records
            if "TGWATCH_ALERT_BOT_TOKEN" in r.message or "TGWATCH_ALERT_CHAT_ID" in r.message
        ]
        assert token_warnings == []
