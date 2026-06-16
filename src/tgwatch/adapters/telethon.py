"""tgwatch adapter — Telethon userbot.

Attache un handler NewMessage sur un TelegramClient Telethon pour enregistrer
le heartbeat et comptabiliser les messages reçus. Phase 3 : démarre également
la sonde get_me() périodique et installe optionnellement un intercepteur _call.

Import telethon paresseux : ce module est importable même si telethon n'est
pas installé. L'ImportError est levée uniquement à l'appel de attach().
Import tgwatch.health paresseux : confiné dans attach() et _install_call_interceptor.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _install_call_interceptor(client, recorder, name: str) -> None:
    """Wrappe client._call pour capter passivement les RPCError (opt-in).

    _call est une méthode privée Telethon (stabilité MEDIUM, RESEARCH DC3).
    Activé uniquement si auto_intercept=True dans attach(). TOUJOURS re-raise
    l'exception (comportement du userbot inchangé — contrainte ADPT-03).

    hasattr guard : si le client n'a pas de _call, log warning et retourne
    sans planter (T-03-11 mitigation).

    Args:
        client:   Instance TelegramClient (duck-typed).
        recorder: Instance Recorder (DI).
        name:     Nom unique du userbot surveillé.
    """
    if not hasattr(client, "_call"):
        logger.warning(
            "auto_intercept demandé mais client._call absent pour %s", name
        )
        return

    from tgwatch.health.telethon_health import report_exception  # lazy import

    original_call = client._call

    async def _intercepting_call(sender, request, ordered=False, flood_sleep_threshold=None):
        try:
            return await original_call(sender, request, ordered, flood_sleep_threshold)
        except Exception as exc:
            report_exception(name, exc, recorder)
            raise  # OBLIGATOIRE — ne jamais avaler l'exception du userbot (ADPT-03)

    client._call = _intercepting_call


def attach(
    client,
    recorder,
    name: str,
    probe_interval: int | None = None,
    auto_intercept: bool = False,
) -> "Optional[asyncio.Task]":
    """Attache le monitoring sur le client Telethon.

    Phase 2 : enregistre un handler catch-all events.NewMessage.
      À chaque message reçu : heartbeat + compteur 'msg'.

    Phase 3 : démarre la sonde get_me() périodique (HLTH-02) et, si
      auto_intercept=True, installe un wrapper sur client._call pour capter
      passivement les RPCError (HLTH-01).

    DOIT être appelé depuis un contexte async pour que la sonde démarre.
    Si appelé hors event loop, la sonde n'est pas démarrée (retourne None +
    warning), mais le handler NewMessage est quand même enregistré.

    Args:
        client:         Instance TelegramClient Telethon.
        recorder:       Instance Recorder (injection de dépendance).
        name:           Nom unique du userbot surveillé.
        probe_interval: Intervalle entre sondes get_me() en secondes (défaut 300).
        auto_intercept: Si True, wrappe client._call pour capter les RPCError
                        automatiquement (opt-in, MEDIUM stability — RESEARCH DC3).

    Returns:
        asyncio.Task de la sonde (annulable via task.cancel()), ou None si
        attach() est appelé hors event loop actif.

    Raises:
        ImportError: Si telethon n'est pas installé.
    """
    try:
        from telethon import events
    except ImportError as exc:
        raise ImportError(
            "telethon est requis pour kind='userbot'.\n"
            "Installez avec: pip install 'tgwatch[telethon]'"
        ) from exc

    if probe_interval is None:
        from tgwatch.health.telethon_health import DEFAULT_PROBE_INTERVAL
        probe_interval = DEFAULT_PROBE_INTERVAL

    # --- Phase 2 : handler NewMessage heartbeat + compteur msg ---
    async def _tgwatch_handler(event) -> None:
        try:
            recorder.heartbeat(name)
            recorder.record_event(name, "msg", None)
        except Exception:
            logger.warning(
                "Échec monitoring handler telethon pour %s", name, exc_info=True
            )

    client.add_event_handler(_tgwatch_handler, events.NewMessage)
    logger.debug("Handler NewMessage enregistré pour : %s", name)

    # --- Phase 3 : intercept _call opt-in (HLTH-01) ---
    if auto_intercept:
        _install_call_interceptor(client, recorder, name)

    # --- Phase 3 : démarrage sonde get_me() périodique (HLTH-02) ---
    from tgwatch.health.telethon_health import start_probe  # lazy import

    probe_task = None
    try:
        probe_task = start_probe(client, recorder, name, interval=probe_interval)
        logger.debug("Sonde de santé démarrée pour : %s", name)
    except RuntimeError:
        # Hors event loop : heartbeat reste actif, sonde non démarrée (T-03-09)
        logger.warning(
            "Sonde non démarrée pour %s : pas de boucle asyncio active. "
            "Appelez attach() depuis un contexte async (après await client.start()).",
            name,
        )

    return probe_task
