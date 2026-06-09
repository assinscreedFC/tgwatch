"""tgwatch — monitoring bots & userbots Telegram.

Point d'entrée public unique : la classe Watch.

Usage minimal :
    from tgwatch import Watch
    watch = Watch("tgwatch.db", alert_bot_token="...", alert_chat_id="...")
    watch.attach(dispatcher, kind="bot", name="mon_bot")
    watch.attach(client, kind="userbot", name="mon_userbot")

Import tgwatch est garanti sans aiogram ni telethon installés.
Les adapters Telegram sont importés paresseusement dans Watch.attach()
uniquement au moment de leur utilisation effective.
"""

from __future__ import annotations

import logging
from typing import Union

from tgwatch.core.recorder import Recorder
from tgwatch.core.storage import Storage

logger = logging.getLogger(__name__)

__all__ = ["Watch"]


class Watch:
    """Classe publique unique pour surveiller bots et userbots Telegram.

    Construit Storage + Recorder en interne (ou réutilise un Storage injecté).
    Route vers l'adapter approprié via import paresseux dans attach().

    Args:
        storage:         Chemin SQLite (str) ou instance Storage existante (DI).
        alert_bot_token: Token du bot d'alerte Telegram (stocké, jamais loggé).
        alert_chat_id:   ID du chat de destination des alertes.
    """

    def __init__(
        self,
        storage: Union[str, Storage],
        alert_bot_token: str,
        alert_chat_id: str,
    ) -> None:
        if isinstance(storage, str):
            storage = Storage(storage)
        self._storage = storage
        self._recorder = Recorder(storage)
        # Stockés pour Phase 4 (envoi alertes) — jamais loggés (T-02-09)
        self._alert_bot_token = alert_bot_token
        self._alert_chat_id = alert_chat_id

    def attach(
        self,
        client_or_dispatcher: object,
        kind: str | None = None,
        name: str = "",
    ) -> None:
        """Enregistre un bot ou userbot et branche le monitoring automatique.

        Si kind est None, auto-détecte le type par duck-typing :
        - Dispatcher aiogram  → kind='bot'     (via .update.outer_middleware)
        - TelegramClient      → kind='userbot' (via .add_event_handler)

        Enregistre le client dans le storage (create_client si absent), puis
        importe paresseusement l'adapter adapté et l'attache.

        Args:
            client_or_dispatcher: Dispatcher aiogram ou TelegramClient Telethon.
            kind:  'bot' | 'userbot' | None (auto-détection si omis).
            name:  Nom unique du client à surveiller.

        Raises:
            ValueError: Si le type ne peut pas être déterminé (auto-détection)
                        ou si kind est une valeur inconnue.
            ImportError: Si l'adapter correspondant (aiogram/telethon) n'est
                         pas installé.
        """
        # 1. Auto-détection si kind non fourni (duck-typing, Pattern 5 RESEARCH)
        if kind is None:
            if hasattr(getattr(client_or_dispatcher, "update", None), "outer_middleware"):
                kind = "bot"
            elif hasattr(client_or_dispatcher, "add_event_handler"):
                kind = "userbot"
            else:
                raise ValueError(
                    "Impossible de détecter le type — passer kind='bot' ou kind='userbot'"
                )

        # 2. Enregistrer le client en storage AVANT de brancher l'adapter
        if self._storage.get_client_by_name(name) is None:
            self._storage.create_client(name, kind)

        # 3. Router vers l'adapter via import LAZY (Pitfall 2 RESEARCH — import circulaire)
        if kind == "bot":
            from tgwatch.adapters import aiogram as aiogram_adapter

            aiogram_adapter.attach(client_or_dispatcher, self._recorder, name)
            logger.info("Monitoring bot attaché : name=%s", name)
        elif kind == "userbot":
            from tgwatch.adapters import telethon as telethon_adapter

            telethon_adapter.attach(client_or_dispatcher, self._recorder, name)
            logger.info("Monitoring userbot attaché : name=%s", name)
        else:
            raise ValueError(
                f"kind inconnu: {kind!r} — valeurs acceptées: 'bot', 'userbot'"
            )
