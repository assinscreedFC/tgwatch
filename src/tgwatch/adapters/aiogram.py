"""tgwatch adapter aiogram — middleware outer sur dispatcher.update.

Capture heartbeat + compteur message à chaque update, enregistre les exceptions
(record-then-reraise), et classe les FloodWait aiogram.

Import aiogram paresseux : ce module est importable sans aiogram installé.
L'import aiogram se produit uniquement dans attach() et dans __call__().
"""

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class TgwatchMiddleware:
    """Middleware outer aiogram pour tgwatch.

    Enregistre heartbeat à chaque update, compteur 'msg' si l'update contient
    un message, et capture les exceptions du handler (record-then-reraise).

    Instanciable directement pour les tests offline sans Dispatcher réel.
    """

    def __init__(self, recorder: Any, name: str) -> None:
        self._recorder = recorder
        self._name = name

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        """Intercepte chaque update : heartbeat, comptage message, capture exception."""
        # Imports lazy : aiogram est forcément installé à ce stade (on reçoit un event)
        from aiogram.exceptions import TelegramRetryAfter
        from aiogram.types import Update

        self._recorder.heartbeat(self._name)

        if isinstance(event, Update) and event.message is not None:
            self._recorder.record_event(self._name, "msg", None)

        try:
            return await handler(event, data)
        except TelegramRetryAfter as exc:
            # DOIT précéder except Exception (TelegramRetryAfter < Exception)
            self._recorder.record_event(
                self._name,
                "floodwait",
                {"retry_after": exc.retry_after},
            )
            raise
        except Exception as exc:
            self._recorder.record_event(
                self._name,
                "error",
                {"type": type(exc).__name__, "msg": str(exc)[:200]},
            )
            raise


def attach(dispatcher: Any, recorder: Any, name: str) -> None:
    """Attache le middleware de monitoring sur le dispatcher aiogram.

    Import lazy : aiogram est chargé uniquement ici, pas au niveau module.
    Lève ImportError avec message d'aide si aiogram n'est pas installé.

    Args:
        dispatcher: Instance de aiogram.Dispatcher (ou objet avec .update.outer_middleware).
        recorder:   Instance de tgwatch.core.recorder.Recorder.
        name:       Nom unique du client à surveiller.
    """
    try:
        from aiogram import BaseMiddleware  # noqa: F401 — vérifie que aiogram est disponible
    except ImportError as exc:
        raise ImportError(
            "aiogram est requis pour kind='bot'.\n"
            "Installez avec: pip install 'tgwatch[aiogram]'"
        ) from exc

    mw = TgwatchMiddleware(recorder, name)
    dispatcher.update.outer_middleware(mw)
    logger.info("TgwatchMiddleware enregistré sur dispatcher.update : client=%s", name)
