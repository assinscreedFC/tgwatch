"""tgwatch core — point d'entrée unique pour tout enregistrement d'événement.

Ce module est le SEUL chemin légitime vers la table `events`.
Il applique le masquage des secrets et le bornage du payload AVANT
toute insertion en base de données (SEC-01, SEC-02).

Aucun appelant n'a besoin de masquer : c'est fait ici, de façon centralisée.
"""

import json
import logging
import re
from datetime import datetime, timezone

from tgwatch.core.models import Health
from tgwatch.core.storage import Storage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes de masquage et bornage
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = frozenset(
    {
        "token",
        "session",
        "session_string",
        "phone",
        "phone_number",
        "api_id",
        "api_hash",
    }
)

REDACTED = "***"

# Regex : token bot Telegram (6+ chiffres : 30+ chars word/tiret)
TOKEN_RE = re.compile(r"\d{6,}:[\w-]{30,}")

MAX_PAYLOAD_BYTES = 4096
TRUNCATION_MARKER = "...[truncated]"


# ---------------------------------------------------------------------------
# Fonctions privées — masquage et bornage
# ---------------------------------------------------------------------------


def _mask(value: object) -> object:
    """Masque récursivement les secrets dans un payload.

    Règles :
    - dict  : redige les clés dans SENSITIVE_KEYS (insensible à la casse),
              applique _mask récursivement sur les valeurs non-sensibles.
    - list  : applique _mask sur chaque élément.
    - str   : remplace tout token bot (TOKEN_RE) par REDACTED.
    - autre : retourne tel quel (int, float, bool, None…).
    """
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in SENSITIVE_KEYS:
                result[k] = REDACTED
            else:
                result[k] = _mask(v)
        return result
    if isinstance(value, list):
        return [_mask(item) for item in value]
    if isinstance(value, str):
        return TOKEN_RE.sub(REDACTED, value)
    # int, float, bool, None — retournés tels quels
    return value


def _to_bounded_json(payload: dict | None) -> str:
    """Sérialise un payload en JSON masqué, borné à MAX_PAYLOAD_BYTES octets.

    - Si payload est None, retourne "{}".
    - Les secrets sont masqués (via _mask) AVANT la sérialisation.
    - Si le JSON dépasse MAX_PAYLOAD_BYTES, le texte est tronqué et
      TRUNCATION_MARKER est ajouté en suffixe.
    """
    if payload is None:
        return "{}"
    masked = _mask(payload)
    text = json.dumps(masked, ensure_ascii=False)
    data = text.encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        marker_bytes = TRUNCATION_MARKER.encode("utf-8")
        cut = MAX_PAYLOAD_BYTES - len(marker_bytes)
        text = data[:cut].decode("utf-8", errors="ignore") + TRUNCATION_MARKER
    return text


def _now() -> str:
    """Retourne l'heure courante en ISO 8601 UTC."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Classe principale — API publique du recorder
# ---------------------------------------------------------------------------


class Recorder:
    """Point d'entrée unique pour tout enregistrement vers tgwatch.

    Injection de dépendance : reçoit un Storage à la construction.
    Aucun état global — les instances sont auto-suffisantes et testables.

    Responsabilités :
    - Masquer les secrets du payload avant insert (SEC-01).
    - Borner le payload à MAX_PAYLOAD_BYTES (SEC-02).
    - Créer le client s'il n'existe pas encore (_ensure_client).
    - Déléguer la persistance à Storage (CORE-03).
    """

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    def _ensure_client(self, name: str, kind: str = "bot") -> int:
        """Retourne l'id du client, en le créant s'il est absent."""
        client = self._storage.get_client_by_name(name)
        if client is None:
            return self._storage.create_client(name, kind)
        return client.id

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def record_event(
        self,
        client_name: str,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        """Enregistre un événement pour un client.

        Le payload est masqué et borné AVANT insertion.
        Le client est créé automatiquement s'il n'existe pas.

        Args:
            client_name: Nom unique du client (bot ou userbot).
            event_type:  Type d'événement (msg, error, floodwait, …).
            payload:     Données libres ; peut contenir des secrets —
                         ils seront rédigés avant toute persistance.
        """
        client_id = self._ensure_client(client_name)
        payload_json = _to_bounded_json(payload)  # masquage + bornage
        self._storage.record_event(client_id, event_type, payload_json, _now())
        logger.debug(
            "Événement enregistré : client=%s type=%s", client_name, event_type
        )

    def heartbeat(self, client_name: str) -> None:
        """Met à jour le timestamp last_seen du client.

        Crée le client s'il n'existe pas encore.

        Args:
            client_name: Nom unique du client.
        """
        self._ensure_client(client_name)
        self._storage.update_last_seen(client_name, _now())
        logger.debug("Heartbeat : client=%s", client_name)

    def set_health(self, client_name: str, health: Health) -> None:
        """Met à jour la santé du compte userbot.

        Crée le client s'il n'existe pas encore.

        Args:
            client_name: Nom unique du client.
            health:      Nouvelle valeur Health (SAIN, RESTREINT, BANNI, …).
        """
        self._ensure_client(client_name)
        self._storage.set_health(client_name, health)
        logger.debug(
            "Santé mise à jour : client=%s health=%s", client_name, health.value
        )

    def get_health(self, client_name: str) -> Health:
        """Retourne la santé persistée du client, ou Health.NA s'il est absent.

        Lecture seule — ne crée PAS le client (contrairement à set_health).
        Utilisé par la couche health pour le calcul worst-state-wins sans
        accès direct à _storage depuis le package health/.
        """
        client = self._storage.get_client_by_name(client_name)
        return client.health if client is not None else Health.NA
