# Référence API tgwatch

Surface publique de la bibliothèque. Import unique : `from tgwatch import Watch`.

## `Watch`

```python
Watch(storage, alert_bot_token, alert_chat_id)
```

Point d'entrée unique. Construit le `Storage` + le `Recorder` (masquage secrets) en interne.

| Paramètre | Type | Rôle |
|-----------|------|------|
| `storage` | `str \| Storage` | Chemin du fichier SQLite, ou une instance `Storage` (injection pour tests) |
| `alert_bot_token` | `str` | Token du bot Telegram dédié aux alertes |
| `alert_chat_id` | `str` | Destination des alertes (`@canal` ou id numérique) |

### `Watch.attach(...)`

```python
watch.attach(client_or_dispatcher, kind=None, name="")  # -> None
```

Active la capture automatique. Idempotent (un même `name` n'est pas recréé).

| Paramètre | Type | Rôle |
|-----------|------|------|
| `client_or_dispatcher` | `Dispatcher \| TelegramClient` | L'objet aiogram ou telethon à surveiller |
| `kind` | `"bot" \| "userbot" \| None` | Type explicite ; si `None`, auto-détecté par duck-typing |
| `name` | `str` | Identifiant unique du client dans le SQLite |

**Retour** : `None`.

Pour un userbot, `Watch.attach` démarre la sonde de santé avec les **valeurs par défaut** (`probe_interval=300`, `auto_intercept=False`) et ne renvoie pas la Task. Routage par import **paresseux** selon `kind` ; `import tgwatch` fonctionne sans aiogram/telethon installés (l'`ImportError` n'est levée qu'à l'attach effectif d'un type dont l'extra manque).

```python
watch.attach(dp, kind="bot", name="bot_support")
watch.attach(client, kind="userbot", name="scraper")   # sonde 300 s, pas d'intercept
```

**Erreurs** : `ValueError` si le type est indétectable (auto-détection) ou si `kind` est inconnu.

#### Configuration avancée de la sonde (adapter bas niveau)

`Watch.attach` n'expose pas `probe_interval`/`auto_intercept` ni la Task de sonde. Pour un intervalle custom, l'interception opt-in, ou récupérer la Task (annulation propre), appelle l'adapter directement :

```python
from tgwatch.adapters.telethon import attach as telethon_attach

task = telethon_attach(
    client,
    watch._recorder,      # le Recorder construit par Watch
    "scraper",
    probe_interval=120,
    auto_intercept=True,  # enveloppe client._call (méthode privée Telethon)
)
# à la déconnexion :
if task:
    task.cancel()
```

| Paramètre (`telethon_attach`) | Type | Défaut | Rôle |
|---|---|---|---|
| `probe_interval` | `int` | `300` | Secondes entre deux sondes `get_me()` |
| `auto_intercept` | `bool` | `False` | Enveloppe `client._call` pour capter les RPCError passivement |

**Retour** : `asyncio.Task | None` (la sonde ; `None` si aucune boucle asyncio active à l'attach).

---

## Modèles (`tgwatch.core`)

Exportés pour lecture/typage. Immuables.

```python
from tgwatch.core import Client, Event, Status, Health
```

### `Status` (Enum str)
`UP` (`"up"`) · `DOWN` (`"down"`)

### `Health` (Enum str)
`SAIN` · `RESTREINT` · `BANNI` · `SESSION_MORTE` · `NA` (`"n/a"`)

### `Client` (dataclass frozen)
`id`, `name`, `kind`, `status: Status`, `last_seen: str` (ISO 8601 UTC), `health: Health`

### `Event` (dataclass frozen)
`id`, `client_id`, `type` (`msg`|`error`|`floodwait`|`down`|`up`), `payload_json: str` (masqué), `ts: str`

---

## Classification santé (`tgwatch.health.telethon_health`)

Utilisable directement si tu veux classer une exception toi-même.

### `classify_exception(exc) -> Health | None`

Mappe une exception Telethon vers un `Health`, ou `None` si non reconnue (ex. `FloodWaitError` → `None`, traité comme event `floodwait`).

Ordre de test (important — `UserDeactivatedBanError` hérite de `UnauthorizedError`) :
1. `PhoneNumberBannedError`, `UserDeactivatedBanError` → `Health.BANNI`
2. `AuthKeyUnregisteredError`, `UnauthorizedError` → `Health.SESSION_MORTE`
3. `PeerFloodError` → `Health.RESTREINT`

### `report_exception(recorder, client_name, exc) -> None`  {#report_exception}

Classifieur public à appeler dans ton `try/except`. Applique worst-state-wins via le recorder ; un `FloodWaitError` est enregistré comme event `floodwait`.

```python
from tgwatch.health.telethon_health import report_exception

try:
    await client.send_message(peer, "...")
except Exception as exc:
    report_exception(watch._recorder, "scraper", exc)  # ou via ton propre recorder
    raise
```

### `start_probe(client, recorder, name, interval=300) -> asyncio.Task`

Démarre la sonde `get_me()` en arrière-plan (appelé automatiquement par `attach` userbot). Annulation propre via `task.cancel()`.

---

## CLI (`tgwatch.cli`)

### `main(argv: list[str] | None = None) -> int`

Point d'entrée console (`tgwatch = tgwatch.cli:main`). Codes de sortie : `0` succès, `1` argument requis manquant, `2` aucune sous-commande.

Voir [guide.md](guide.md) pour les flags `status` / `watch` et les variables d'environnement `TGWATCH_*`.

---

## Recorder (`tgwatch.core.recorder.Recorder`)

API centrale d'enregistrement. Normalement utilisée en interne par les adapters ; exposée pour les cas avancés.

| Méthode | Effet |
|---------|-------|
| `record_event(client_name, event_type, payload=None)` | Enregistre un event ; masque + borne le payload avant insertion |
| `heartbeat(client_name)` | Met à jour `last_seen` |
| `set_health(client_name, health)` | Persiste l'état de santé (worst-state-wins à gérer côté appelant) |
| `get_health(client_name) -> Health` | Lit l'état persisté (lecture seule, `Health.NA` si absent) |

**Invariant** : le `Recorder` est le seul écrivain légitime de la table `events` — c'est là qu'est centralisé le masquage des secrets.
