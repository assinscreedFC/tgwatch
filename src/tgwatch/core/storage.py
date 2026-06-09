"""tgwatch core — abstraction SQLite en mode WAL.

Source de vérité unique pour l'état des clients surveillés.
Connexion par opération (évite les locks longue durée), WAL pour
accès concurrent multi-process (bots + watchdog + CLI).

Toutes les requêtes utilisent des placeholders ? (paramétrisées).
Aucune interpolation f-string ou concaténation dans le SQL (SEC-02).
"""

import logging
import sqlite3
from datetime import datetime, timezone

from tgwatch.core.models import Client, Event, Health, Status

logger = logging.getLogger(__name__)


def _now() -> str:
    """Retourne l'heure courante en ISO 8601 UTC."""
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """Abstraction SQLite WAL pour tgwatch.

    Chaque méthode ouvre sa propre connexion et la ferme dans un finally —
    pas de connexion persistante pour éviter les locks multi-process.
    WAL (Write-Ahead Logging) activé à chaque connexion pour permettre
    des lectures concurrentes pendant les écritures.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._init_schema()

    # ------------------------------------------------------------------
    # Connexion helper
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Ouvre une nouvelle connexion WAL avec row_factory."""
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Crée les tables si elles n'existent pas (idempotent)."""
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS clients(
                  id INTEGER PRIMARY KEY,
                  name TEXT UNIQUE,
                  kind TEXT,
                  status TEXT,
                  last_seen TEXT,
                  health TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events(
                  id INTEGER PRIMARY KEY,
                  client_id INTEGER,
                  type TEXT,
                  payload_json TEXT,
                  ts TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts_sent(
                  id INTEGER PRIMARY KEY,
                  client_id INTEGER,
                  type TEXT,
                  ts TEXT
                )
                """
            )
            conn.commit()
            logger.debug("Schema initialisé (WAL) sur %s", self.path)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Méthodes clients
    # ------------------------------------------------------------------

    def create_client(self, name: str, kind: str) -> int:
        """Insère un nouveau client avec statut DOWN et santé NA par défaut.

        Returns:
            L'id SQLite du client inséré.

        Raises:
            sqlite3.IntegrityError: Si un client avec ce nom existe déjà.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO clients(name, kind, status, last_seen, health) VALUES (?, ?, ?, ?, ?)",
                (name, kind, Status.DOWN.value, _now(), Health.NA.value),
            )
            conn.commit()
            logger.debug("Client créé : name=%s kind=%s id=%s", name, kind, cursor.lastrowid)
            return cursor.lastrowid
        finally:
            conn.close()

    def get_client_by_name(self, name: str) -> Client | None:
        """Retourne le client correspondant au nom, ou None s'il est absent."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, name, kind, status, last_seen, health FROM clients WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                return None
            return Client(
                id=row["id"],
                name=row["name"],
                kind=row["kind"],
                status=Status(row["status"]),
                last_seen=row["last_seen"],
                health=Health(row["health"]),
            )
        finally:
            conn.close()

    def update_last_seen(self, name: str, ts: str | None = None) -> None:
        """Met à jour last_seen pour le client donné.

        Args:
            name: Nom du client.
            ts: Timestamp ISO 8601 UTC. Si None, utilise l'heure courante.
        """
        timestamp = ts if ts is not None else _now()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE clients SET last_seen = ? WHERE name = ?",
                (timestamp, name),
            )
            conn.commit()
            logger.debug("last_seen mis à jour : name=%s ts=%s", name, timestamp)
        finally:
            conn.close()

    def update_status(self, name: str, status: Status) -> None:
        """Met à jour le statut (up/down) du client."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE clients SET status = ? WHERE name = ?",
                (status.value, name),
            )
            conn.commit()
            logger.debug("Statut mis à jour : name=%s status=%s", name, status.value)
        finally:
            conn.close()

    def set_health(self, name: str, health: Health) -> None:
        """Met à jour la santé du compte userbot."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE clients SET health = ? WHERE name = ?",
                (health.value, name),
            )
            conn.commit()
            logger.debug("Santé mise à jour : name=%s health=%s", name, health.value)
        finally:
            conn.close()

    def list_clients(self) -> list[Client]:
        """Retourne tous les clients enregistrés."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, kind, status, last_seen, health FROM clients"
            ).fetchall()
            return [
                Client(
                    id=row["id"],
                    name=row["name"],
                    kind=row["kind"],
                    status=Status(row["status"]),
                    last_seen=row["last_seen"],
                    health=Health(row["health"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Méthodes events
    # ------------------------------------------------------------------

    def record_event(
        self,
        client_id: int,
        event_type: str,
        payload_json: str,
        ts: str | None = None,
    ) -> int:
        """Insère un événement dans la table events.

        Le payload_json est reçu déjà masqué/borné par le recorder (plan 03).
        Ce storage ne fait pas de masquage — responsabilité du recorder.

        Returns:
            L'id SQLite de l'événement inséré.
        """
        timestamp = ts if ts is not None else _now()
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO events(client_id, type, payload_json, ts) VALUES (?, ?, ?, ?)",
                (client_id, event_type, payload_json, timestamp),
            )
            conn.commit()
            logger.debug(
                "Événement enregistré : client_id=%s type=%s id=%s",
                client_id,
                event_type,
                cursor.lastrowid,
            )
            return cursor.lastrowid
        finally:
            conn.close()

    def list_events(self, client_id: int) -> list[Event]:
        """Retourne tous les événements d'un client, triés par id (déterministe)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, client_id, type, payload_json, ts FROM events WHERE client_id = ? ORDER BY id",
                (client_id,),
            ).fetchall()
            return [
                Event(
                    id=row["id"],
                    client_id=row["client_id"],
                    type=row["type"],
                    payload_json=row["payload_json"],
                    ts=row["ts"],
                )
                for row in rows
            ]
        finally:
            conn.close()

    def count_events_by_type(self, client_id: int) -> dict[str, int]:
        """Retourne le nombre d'événements par type pour un client (CNT-01).

        Returns:
            Dictionnaire {type: count}, vide si aucun événement.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT type, COUNT(*) FROM events WHERE client_id = ? GROUP BY type",
                (client_id,),
            ).fetchall()
            return {row[0]: row[1] for row in rows}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Méthodes alerts_sent
    # ------------------------------------------------------------------

    def record_alert_sent(
        self,
        client_id: int,
        alert_type: str,
        ts: str | None = None,
    ) -> int:
        """Enregistre qu'une alerte a été envoyée (anti-spam dédup).

        Returns:
            L'id SQLite de l'alerte insérée.
        """
        timestamp = ts if ts is not None else _now()
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO alerts_sent(client_id, type, ts) VALUES (?, ?, ?)",
                (client_id, alert_type, timestamp),
            )
            conn.commit()
            logger.debug(
                "Alerte enregistrée : client_id=%s type=%s id=%s",
                client_id,
                alert_type,
                cursor.lastrowid,
            )
            return cursor.lastrowid
        finally:
            conn.close()

    def last_alert_sent(self, client_id: int, alert_type: str) -> str | None:
        """Retourne le ts ISO 8601 de la dernière alerte (client_id, type), ou None.

        Utilisé par l'anti-spam : la couche alert compare ce ts à la fenêtre de dédup.
        Requête paramétrée uniquement (SEC-02). Aucune f-string dans le SQL.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT ts FROM alerts_sent WHERE client_id = ? AND type = ? ORDER BY ts DESC LIMIT 1",
                (client_id, alert_type),
            ).fetchone()
            return row["ts"] if row is not None else None
        finally:
            conn.close()
