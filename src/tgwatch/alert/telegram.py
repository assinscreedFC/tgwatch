"""alert.telegram — envoi d'alertes via l'API Bot Telegram (stdlib urllib).

Zéro dépendance externe : urllib.request POST vers api.telegram.org.
Résilient : retry + backoff, ne propage JAMAIS au watchdog (retourne bool).
Le token n'apparaît jamais dans les logs (on logge uniquement API_BASE).
"""

import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)

TELEGRAM_HOST = "api.telegram.org"
API_BASE = f"https://{TELEGRAM_HOST}"
MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE = 1.0  # secondes : backoff = base * 2**(n-1) → 1s, 2s, 4s
HTTP_TIMEOUT = 10.0

# Sender injectable : (url, data_bytes, timeout) -> status_code int. Mocké en test.
Sender = Callable[[str, bytes, float], int]


def _default_sender(url: str, data: bytes, timeout: float) -> int:
    """Sender HTTP réel via urllib. Retourne le code HTTP. Mocké en test."""
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (host fixe validé)
        return resp.status


class Alerter:
    """Envoie des alertes Telegram avec retry, backoff et anti-spam.

    Injection de dépendance : storage (lecture/écriture alerts_sent) + sender
    (appel HTTP, par défaut urllib réel, remplacé par un faux en test).
    """

    def __init__(
        self,
        storage,
        token: str,
        chat_id: str,
        *,
        sender: Sender = _default_sender,
        dedup_window: int = 3600,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
    ) -> None:
        self._storage = storage
        self._token = token
        self._chat_id = str(chat_id)
        if not re.fullmatch(r"-?\d+", self._chat_id):
            raise ValueError(
                f"chat_id doit être un entier (positif ou négatif), reçu : {chat_id!r}"
            )
        self._sender = sender
        self._dedup_window = dedup_window
        self._backoff_base = backoff_base

    def _endpoint(self) -> str:
        """URL sendMessage avec token. Host fixe api.telegram.org (anti-SSRF/host injection)."""
        return f"{API_BASE}/bot{self._token}/sendMessage"

    def send(self, text: str) -> bool:
        """Poste un message. Retry MAX_ATTEMPTS avec backoff exponentiel.

        Ne lève JAMAIS (ALRT-02) : retourne True si une tentative réussit,
        False si toutes échouent. Le token n'est jamais loggé.
        """
        data = urllib.parse.urlencode(
            {"chat_id": self._chat_id, "text": text}
        ).encode("utf-8")
        url = self._endpoint()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                status = self._sender(url, data, HTTP_TIMEOUT)
                if status == 200:
                    logger.info("Alerte envoyée (endpoint=%s)", API_BASE)
                    return True
                logger.warning(
                    "Alerte non-200 (status=%s, tentative %d/%d)",
                    status, attempt, MAX_ATTEMPTS,
                )
            except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError) as exc:
                logger.warning(
                    "Échec envoi alerte (%s, tentative %d/%d)",
                    type(exc).__name__, attempt, MAX_ATTEMPTS,
                )
            except Exception as exc:  # noqa: BLE001
                # Catch large : ALRT-02 — jamais propager au watchdog
                logger.warning(
                    "Erreur inattendue envoi alerte (%s, tentative %d/%d)",
                    type(exc).__name__, attempt, MAX_ATTEMPTS,
                )
            if attempt < MAX_ATTEMPTS:
                time.sleep(self._backoff_base * 2 ** (attempt - 1))
        logger.error(
            "Alerte abandonnée après %d tentatives (endpoint=%s)", MAX_ATTEMPTS, API_BASE
        )
        return False

    def maybe_send(self, client_id: int, alert_type: str, text: str, *, now=None) -> bool:
        """Envoie sous garde anti-spam (ALRT-03).

        Si une alerte (client_id, alert_type) a été envoyée dans dedup_window
        secondes, on n'envoie PAS (retourne False). Sinon on tente send() ;
        si succès, on enregistre via record_alert_sent.

        Args:
            client_id:  Id du client surveillé (clé dédup).
            alert_type: Type d'alerte (ex. "down", "health_degraded").
            text:       Texte du message Telegram.
            now:        datetime UTC injectable pour des tests déterministes.

        Returns:
            True si envoyée, False si bloquée par dédup OU si l'envoi a échoué.
        """
        current = now if now is not None else datetime.now(timezone.utc)
        last_ts = self._storage.last_alert_sent(client_id, alert_type)
        if last_ts is not None:
            last_dt = datetime.fromisoformat(last_ts)
            if (current - last_dt).total_seconds() < self._dedup_window:
                logger.debug(
                    "Alerte dédupliquée (client_id=%s type=%s)", client_id, alert_type
                )
                return False
        if self.send(text):
            self._storage.record_alert_sent(client_id, alert_type, current.isoformat())
            return True
        return False
