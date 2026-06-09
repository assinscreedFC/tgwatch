"""cli — point d'entrée CLI tgwatch (argparse stdlib, zéro dépendance Telegram)."""

import argparse
import logging
import os
import sys

from tgwatch.core.storage import Storage

logger = logging.getLogger(__name__)

# Variables d'environnement (fallback quand les flags CLI sont absents)
ENV_STORAGE = "TGWATCH_STORAGE"
ENV_TOKEN = "TGWATCH_ALERT_BOT_TOKEN"
ENV_CHAT_ID = "TGWATCH_ALERT_CHAT_ID"


def build_parser() -> argparse.ArgumentParser:
    """Construit et retourne le parseur CLI tgwatch."""
    parser = argparse.ArgumentParser(
        prog="tgwatch",
        description="Monitoring bots & userbots Telegram",
    )
    sub = parser.add_subparsers(dest="command")

    # --- sous-commande status ---
    status_p = sub.add_parser("status", help="Affiche l'état de tous les clients surveillés")
    status_p.add_argument(
        "--storage",
        type=str,
        default=None,
        help="Chemin vers le fichier SQLite (ou variable TGWATCH_STORAGE)",
    )

    # --- sous-commande watch ---
    watch_p = sub.add_parser("watch", help="Lance la boucle watchdog de surveillance")
    watch_p.add_argument(
        "--storage",
        type=str,
        default=None,
        help="Chemin vers le fichier SQLite (ou variable TGWATCH_STORAGE)",
    )
    watch_p.add_argument(
        "--token",
        type=str,
        default=None,
        help="Token du bot d'alerte (ou variable TGWATCH_ALERT_BOT_TOKEN)",
    )
    watch_p.add_argument(
        "--chat-id",
        dest="chat_id",
        type=str,
        default=None,
        help="Chat ID de destination pour les alertes (ou variable TGWATCH_ALERT_CHAT_ID)",
    )
    watch_p.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Intervalle en secondes entre deux vérifications (défaut : 60)",
    )
    watch_p.add_argument(
        "--heartbeat-threshold",
        dest="heartbeat_threshold",
        type=int,
        default=300,
        help="Secondes sans heartbeat avant de marquer un client DOWN (défaut : 300)",
    )

    return parser


def _resolve(value: str | None, env_name: str) -> str | None:
    """Retourne value si fourni, sinon la variable d'environnement env_name."""
    return value if value is not None else os.environ.get(env_name)


def cmd_status(args: argparse.Namespace) -> int:
    """Affiche un tableau aligné de l'état de tous les clients surveillés."""
    storage_path = _resolve(args.storage, ENV_STORAGE)
    if storage_path is None:
        print("Erreur : --storage requis (ou TGWATCH_STORAGE)", file=sys.stderr)
        return 1

    storage = Storage(storage_path)
    clients = storage.list_clients()

    if not clients:
        print("Aucun client surveillé.")
        return 0

    # Construire les lignes de données
    headers = ["NAME", "KIND", "STATUS", "LAST_SEEN", "HEALTH", "MSG", "ERROR", "FLOODWAIT"]
    rows: list[list[str]] = []
    for client in clients:
        counts = storage.count_events_by_type(client.id)
        rows.append([
            client.name,
            client.kind,
            str(client.status),
            client.last_seen,
            str(client.health),
            str(counts.get("msg", 0)),
            str(counts.get("error", 0)),
            str(counts.get("floodwait", 0)),
        ])

    # Calculer la largeur max par colonne (en-têtes + données)
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if len(cell) > col_widths[i]:
                col_widths[i] = len(cell)

    # Afficher en-têtes puis lignes
    separator = "  "
    header_line = separator.join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print(separator.join(cell.ljust(col_widths[i]) for i, cell in enumerate(row)))

    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Lance la boucle watchdog en câblant Storage, Alerter et watch_loop."""
    storage_path = _resolve(args.storage, ENV_STORAGE)
    token = _resolve(args.token, ENV_TOKEN)
    chat_id = _resolve(args.chat_id, ENV_CHAT_ID)

    # Validation — collecte tous les manquants avant d'afficher l'erreur
    missing: list[str] = []
    if storage_path is None:
        missing.append("--storage (ou TGWATCH_STORAGE)")
    if token is None:
        missing.append("--token (ou TGWATCH_ALERT_BOT_TOKEN)")
    if chat_id is None:
        missing.append("--chat-id (ou TGWATCH_ALERT_CHAT_ID)")

    if missing:
        print("Erreur : arguments requis manquants : " + ", ".join(missing), file=sys.stderr)
        return 1

    # Imports locaux (paresseux) — watch_loop et Alerter sont stdlib, mais on évite
    # de les charger lors d'un simple `import tgwatch.cli` ou `tgwatch status`.
    from tgwatch.alert.telegram import Alerter
    from tgwatch.watchdog import watch_loop

    storage = Storage(storage_path)
    alerter = Alerter(storage, token, chat_id)  # type: ignore[arg-type]
    print("watchdog démarré (Ctrl-C pour arrêter)")
    watch_loop(
        storage,
        alerter,
        heartbeat_threshold=args.heartbeat_threshold,
        poll_interval=args.interval,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée CLI tgwatch. Retourne un code de sortie entier."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        return cmd_status(args)
    if args.command == "watch":
        return cmd_watch(args)

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
