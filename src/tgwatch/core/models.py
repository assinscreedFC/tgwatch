"""tgwatch core — modèles de données (sans dépendance Telegram)."""

from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    """État vivant/mort d'un client surveillé."""

    UP = "up"
    DOWN = "down"

    # Python 3.11+ : str() sur une str-Enum retourne "ClassName.MEMBER" par défaut.
    # On surcharge __str__ pour garantir la valeur brute (compatibilité JSON/SQLite).
    def __str__(self) -> str:
        return self.value


class Health(str, Enum):
    """Santé du compte userbot Telegram.

    NA s'applique aussi aux bots (pas de santé compte).
    """

    SAIN = "sain"
    RESTREINT = "restreint"
    BANNI = "banni"
    SESSION_MORTE = "session_morte"
    # "/" n'est pas un identifiant Python valide — on utilise "n_a" (valeur stockée telle quelle)
    NA = "n_a"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Client:
    """Représente un bot ou userbot surveillé.

    Immuable après création (frozen=True) pour éviter la corruption d'état partagé.
    """

    id: int
    name: str
    kind: str  # "bot" | "userbot"
    status: Status
    last_seen: str  # ISO 8601 UTC text
    health: Health


@dataclass(frozen=True)
class Event:
    """Représente un événement discret capturé par un adapter.

    Immuable après création — le payload_json est masqué par le recorder avant insertion.
    """

    id: int
    client_id: int
    type: str  # msg | error | floodwait | restriction | down | up
    payload_json: str
    ts: str  # ISO 8601 UTC text
