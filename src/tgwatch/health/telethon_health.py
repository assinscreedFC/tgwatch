"""tgwatch.health.telethon_health — couche passive de classification santé userbot.

Ce module est le SEUL point d'import des exceptions Telethon dans la couche health.
Tous les imports telethon sont LAZY (à l'intérieur des fonctions) pour que le package
tgwatch reste importable sans telethon installé.

Aucun import de Recorder au top-level (évite cycle health -> core -> health).
Le type 'Recorder' est référencé en commentaire uniquement.
"""

from __future__ import annotations

import asyncio
import logging

from tgwatch.core.models import Health

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table de priorité worst-state-wins
# ---------------------------------------------------------------------------

HEALTH_PRIORITY: dict[Health, int] = {
    Health.BANNI: 4,
    Health.SESSION_MORTE: 3,
    Health.RESTREINT: 2,
    Health.SAIN: 1,
    Health.NA: 0,
}


def _resolve_health(current: Health, new: Health) -> Health:
    """worst-state-wins : ne régresse jamais vers un état moins grave.

    Retourne l'état le plus grave entre current et new.
    Si current est déjà plus grave ou égal, current est retourné inchangé.
    """
    return (
        current
        if HEALTH_PRIORITY.get(current, 0) >= HEALTH_PRIORITY.get(new, 0)
        else new
    )


# ---------------------------------------------------------------------------
# Classifieur pur — testable isolément, pas d'effet de bord
# ---------------------------------------------------------------------------


def classify_exception(exc: BaseException) -> Health | None:
    """Classifie une exception Telethon en état de santé userbot.

    Retourne None si l'exception n'est pas reconnue ou si telethon n'est pas
    installé. FloodWaitError retourne None (c'est un event, pas un état dégradé).

    ORDRE CRITIQUE (MRO Pitfall) :
    UserDeactivatedBanError hérite de UnauthorizedError — tester BANNI
    (sous-classes) AVANT SESSION_MORTE (classe parente UnauthorizedError).

    Args:
        exc: Exception à classifier.

    Returns:
        Health correspondant, ou None si non reconnu.
    """
    try:
        from telethon.errors import (
            AuthKeyUnregisteredError,
            FloodWaitError,
            PeerFloodError,
            PhoneNumberBannedError,
            UnauthorizedError,
            UserDeactivatedBanError,
        )
    except ImportError:
        return None  # telethon non installé — pas d'état classifiable

    # ORDRE OBLIGATOIRE : sous-classes AVANT classe parente (Pitfall MRO)
    # PhoneNumberBannedError hérite de BadRequestError (pas de problème MRO ici)
    # UserDeactivatedBanError hérite de UnauthorizedError → DOIT être avant UnauthorizedError
    if isinstance(exc, (PhoneNumberBannedError, UserDeactivatedBanError)):
        return Health.BANNI

    if isinstance(exc, (AuthKeyUnregisteredError, UnauthorizedError)):
        return Health.SESSION_MORTE

    if isinstance(exc, PeerFloodError):
        return Health.RESTREINT

    if isinstance(exc, FloodWaitError):
        return None  # event floodwait, pas un état de santé dégradé

    return None


# ---------------------------------------------------------------------------
# API publique couche passive — HLTH-01
# ---------------------------------------------------------------------------


def report_exception(
    client_name: str,
    exc: BaseException,
    recorder,  # type: Recorder — pas d'import pour éviter le cycle health -> core
) -> None:
    """Classifie une exception et met à jour la santé worst-state-wins.

    Pour FloodWaitError : enregistre l'event 'floodwait' uniquement.
      Payload limité à {"seconds": int} — jamais str(exc) ni l'objet complet
      (T-03-01 : Information Disclosure mitigation).
    Pour les exceptions reconnues : upgrade l'état si plus grave que l'état courant.
    Pour les exceptions inconnues : aucune action.

    Logger uniquement type(exc).__name__ et health.value — jamais str(exc)
    (T-03-02 : Information Disclosure mitigation).

    Args:
        client_name: Nom unique du client surveillé.
        exc:         Exception Telethon à analyser.
        recorder:    Instance Recorder (DI — pas d'import direct pour éviter cycle).
    """
    try:
        from telethon.errors import FloodWaitError
    except ImportError:
        return  # telethon non installé

    # FloodWaitError : event uniquement, pas d'état de santé dégradé
    if isinstance(exc, FloodWaitError):
        recorder.record_event(
            client_name,
            "floodwait",
            {"seconds": exc.seconds},  # payload minimal, jamais str(exc)
        )
        return

    health = classify_exception(exc)
    if health is None:
        return

    # Lire l'état courant via API publique recorder (pas recorder._storage direct)
    current = recorder.get_health(client_name)

    # worst-state-wins : n'écraser que si le nouvel état est plus grave
    resolved = _resolve_health(current, health)
    if resolved != current:
        recorder.set_health(client_name, resolved)
        logger.info(
            "Santé dégradée (passif) : client=%s %s -> %s via %s",
            client_name,
            current.value,
            resolved.value,
            type(exc).__name__,  # jamais str(exc) — T-03-02
        )


# ---------------------------------------------------------------------------
# Couche active — sonde get_me() (HLTH-02 + HLTH-03)
# ---------------------------------------------------------------------------


def _apply_health(
    client_name: str,
    new: Health,
    recorder,  # type: Recorder
    source: str,
) -> None:
    """Applique worst-state-wins et persiste si l'état est plus grave.

    Décision V1 — recovery sain désactivée :
    _apply_health applique worst-state-wins SANS exception de recovery.
    Un get_me() sain (.restricted=False) ne rétrograde PAS un état plus grave
    (RESTREINT, SESSION_MORTE, BANNI) déjà persisté. La recovery automatique
    n'est pas active en V1 (CONTEXT.md : "un banni ne doit pas être écrasé
    par un sain ultérieur tant que non confirmé").

    Args:
        client_name: Nom unique du client surveillé.
        new:         Nouvel état santé proposé par la sonde.
        recorder:    Instance Recorder (DI).
        source:      Description de la source pour debug (jamais de secret).
    """
    current = recorder.get_health(client_name)
    resolved = _resolve_health(current, new)
    recorder.set_health(client_name, resolved)
    logger.debug(
        "Sonde %s : %s -> %s (source: %s)",
        client_name,
        current.value,
        resolved.value,
        source,
    )


async def _run_probe(client, client_name: str, recorder) -> None:
    """Exécute une itération de sonde get_me() et met à jour la santé.

    Fonction séparée (pas inlinée dans la boucle) pour testabilité directe :
    les tests appellent _run_probe() une seule fois sans dormir 300s.

    Comportement get_me() VÉRIFIÉ [VERIFIED: source telethon/client/users.py] :
    - Attrape UnauthorizedError (dont AuthKeyUnregisteredError et UserDeactivatedBanError)
      et retourne None — PAS d'exception levée pour ces deux cas.
    - PeerFloodError / PhoneNumberBannedError : non catchées -> remontent.
    - User.restricted : Optional[bool]. None ou False = non restreint, True = restreint.

    Exception inattendue dans get_me() : loggée (type uniquement, jamais str(exc))
    et ignorée — la boucle continue à la prochaine itération (T-03-04 mitigation).

    Args:
        client:      Client Telethon (duck-typed pour testabilité).
        client_name: Nom unique du client surveillé.
        recorder:    Instance Recorder (DI).
    """
    try:
        from telethon.errors import PhoneNumberBannedError, PeerFloodError
    except ImportError:
        return  # telethon non installé

    try:
        me = await client.get_me()
    except PhoneNumberBannedError:
        _apply_health(client_name, Health.BANNI, recorder, "get_me(PhoneNumberBanned)")
        return
    except PeerFloodError:
        _apply_health(client_name, Health.RESTREINT, recorder, "get_me(PeerFlood)")
        return
    except Exception as exc:
        # Exception inattendue (NetworkError transitoire, etc.) — ne pas crasher la boucle.
        # Logger uniquement type(exc).__name__ : str(exc) peut contenir session/phone (T-03-06).
        logger.warning(
            "get_me() exception inattendue pour %s : %s",
            client_name,
            type(exc).__name__,
        )
        return

    if me is None:
        # None signifie UnauthorizedError capturée par get_me() en interne :
        # - AuthKeyUnregisteredError (session_morte)
        # - UserDeactivatedBanError (banni — indistinguable ici seul)
        # La couche passive peut avoir déjà enregistré BANNI.
        # _apply_health -> _resolve_health honore worst-state-wins (T-03-07).
        _apply_health(client_name, Health.SESSION_MORTE, recorder, "get_me(None)")
        return

    # me.restricted : Optional[bool]. Truthy = True uniquement ; None/False -> sain (DC2).
    if me.restricted:
        _apply_health(client_name, Health.RESTREINT, recorder, "get_me(restricted=True)")
    else:
        _apply_health(client_name, Health.SAIN, recorder, "get_me(sain)")


# ---------------------------------------------------------------------------
# Boucle périodique annulable — HLTH-02
# ---------------------------------------------------------------------------

DEFAULT_PROBE_INTERVAL = 300  # secondes


def start_probe(
    client,
    recorder,
    name: str,
    interval: int = DEFAULT_PROBE_INTERVAL,
) -> asyncio.Task:
    """Démarre la tâche de sonde périodique et retourne la Task pour annulation.

    DOIT être appelé depuis un contexte async (boucle asyncio en cours).
    Avec Telethon, c'est toujours le cas : l'utilisateur appelle attach()
    après await client.start() ou dans une coroutine principale.

    Si appelé hors d'un event loop, asyncio.get_running_loop() lève RuntimeError —
    comportement attendu et documenté (Pitfall 4).

    L'appelant est responsable de l'annulation propre :
        probe_task = start_probe(client, recorder, name)
        # ... plus tard (ex: au disconnect) ...
        probe_task.cancel()
        await probe_task  # attend la fin propre (CancelledError absorbée ici)

    Args:
        client:   Client Telethon (duck-typed pour testabilité).
        recorder: Instance Recorder (DI).
        name:     Nom unique du client surveillé.
        interval: Intervalle entre deux sondes en secondes (défaut 300).

    Returns:
        asyncio.Task annulable.

    Raises:
        RuntimeError: Si appelé hors d'un event loop actif.
    """
    loop = asyncio.get_running_loop()  # RuntimeError si pas de loop — Pitfall 4
    task = loop.create_task(_probe_loop(client, recorder, name, interval))
    return task


async def _probe_loop(client, recorder, name: str, interval: int) -> None:
    """Boucle de sonde périodique avec gestion propre de CancelledError.

    Appelle _run_probe() à chaque itération puis dort `interval` secondes.
    CancelledError est reraisée obligatoirement (T-03-05) pour que task.cancel()
    fonctionne proprement. Le bloc finally assure le cleanup de la tâche.

    Ne jamais utiliser time.sleep() ici — asyncio.sleep() uniquement.

    Args:
        client:   Client Telethon.
        recorder: Instance Recorder.
        name:     Nom unique du client surveillé.
        interval: Intervalle en secondes entre chaque sonde.
    """
    logger.info("Sonde démarrée pour %s (interval=%ds)", name, interval)
    try:
        while True:
            await _run_probe(client, name, recorder)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Sonde arrêtée proprement pour %s", name)
        raise  # OBLIGATOIRE — reraise CancelledError (T-03-05)
    finally:
        logger.debug("Sonde cleanup pour %s", name)
