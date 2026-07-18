# tgwatch

**Monitoring léger pour bots Telegram (aiogram) et comptes userbot (Telethon) — santé compte, alertes Telegram, zéro infra.**

Tu attaches tgwatch à ton bot/client en deux lignes. Il capte automatiquement l'état vivant/mort, les compteurs (messages, erreurs, FloodWait), la santé du compte userbot (restriction, ban, session morte), et envoie une alerte Telegram quand un problème survient.

**Zéro infrastructure** : stockage SQLite local, pas de Prometheus, pas de Grafana, pas de serveur obligatoire. Si tout le reste échoue, ça doit marcher.

- Python 3.11+ · SQLite (stdlib) · aiogram/telethon en extras optionnelles · 238 tests, 96 % de couverture, 100 % offline
- Licence MIT

---

## Pourquoi

| Trou | Couverture existante | tgwatch |
|------|----------------------|---------|
| Monitoring léger zéro-infra + alerte Telegram native | `aiogram-prometheus` exige Prometheus + Grafana | ✅ SQLite local + bot Telegram |
| Bot **et** userbot unifiés | bots seulement | ✅ aiogram + telethon, couche `core` partagée |
| Santé compte userbot (ban / restriction / session) | rien n'existe | ✅ le différenciant |

---

## Installation

```bash
pip install tgwatch[telethon]   # pour surveiller des userbots Telethon
pip install tgwatch[aiogram]    # pour surveiller des bots aiogram
pip install tgwatch[telethon,aiogram]   # les deux
```

La base `pip install tgwatch` n'a **aucune dépendance runtime** (sqlite3 / urllib / argparse stdlib). Les extras n'installent que ce que tu utilises.

> Ajoute `tgwatch.db` à ton `.gitignore`. tgwatch ne logge ni ne stocke **jamais** token, session string ou numéro de téléphone (masquage centralisé).

---

## Démarrage rapide

### 1. Attacher tgwatch (dans ton process bot/userbot)

```python
from tgwatch import Watch

watch = Watch(
    storage="tgwatch.db",
    alert_bot_token="123456:abc...",   # bot Telegram DÉDIÉ aux alertes
    alert_chat_id="@mon_canal_alertes",
)

# userbot Telethon
watch.attach(telethon_client, kind="userbot", name="compte_scraping")

# bot aiogram
watch.attach(dispatcher, kind="bot", name="bot_support")
```

Après `attach`, la capture est automatique. Rien d'autre à coder. Le comportement de ton bot/userbot reste **strictement inchangé** (les exceptions sont enregistrées puis relevées).

### 2. Lancer le guetteur (process séparé)

Un process mort ne peut pas s'alerter lui-même. Le watchdog tourne **à part**, lit le SQLite partagé et envoie les alertes :

```bash
tgwatch watch --storage tgwatch.db --token 123456:abc... --chat-id @mon_canal_alertes
```

### 3. Inspecter l'état

```bash
tgwatch status --storage tgwatch.db
```

```
NAME             KIND      STATUS  LAST_SEEN                   HEALTH      MSG  ERROR  FLOODWAIT
---------------------------------------------------------------------------------------------
compte_scraping  userbot   up      2026-06-07T19:12:03+00:00   restreint   842  3      1
bot_support      bot       down    2026-06-07T18:55:41+00:00   n/a         1503 12     0
```

---

## CLI

### `tgwatch status`

Instantané de tous les clients.

| Flag | Env | Défaut | Rôle |
|------|-----|--------|------|
| `--storage PATH` | `TGWATCH_STORAGE` | — (requis) | Fichier SQLite à lire |

### `tgwatch watch`

Boucle de surveillance permanente (Ctrl-C pour arrêter proprement).

| Flag | Env | Défaut | Rôle |
|------|-----|--------|------|
| `--storage PATH` | `TGWATCH_STORAGE` | — (requis) | Fichier SQLite partagé |
| `--token TOKEN` | `TGWATCH_ALERT_BOT_TOKEN` | — (requis) | Token du bot d'alerte |
| `--chat-id ID` | `TGWATCH_ALERT_CHAT_ID` | — (requis) | Destination des alertes |
| `--interval N` | — | `60` | Secondes entre deux vérifications |
| `--heartbeat-threshold N` | — | `300` | Secondes sans heartbeat → client marqué `down` |

Les flags priment sur les variables d'environnement. Exemple tout-env :

```bash
export TGWATCH_STORAGE=tgwatch.db
export TGWATCH_ALERT_BOT_TOKEN=123456:abc...
export TGWATCH_ALERT_CHAT_ID=@mon_canal_alertes
tgwatch watch --interval 30
```

---

## Santé du compte userbot (le différenciant)

tgwatch classe automatiquement l'état d'un compte userbot Telethon à partir de **deux sources** :

1. **Couche passive** — les exceptions Telethon sont classées :
   | Exception | État |
   |-----------|------|
   | `PeerFloodError` | `restreint` |
   | `PhoneNumberBannedError`, `UserDeactivatedBanError` | `banni` |
   | `AuthKeyUnregisteredError` / Unauthorized | `session_morte` |
   | `FloodWaitError` | event `floodwait` (ralenti — pas un état dégradé en soi) |

2. **Couche active légère** — sonde `get_me()` périodique qui lit `.restricted`.

**Résolution worst-state-wins** : `banni` > `session_morte` > `restreint` > `sain`. Un signal `banni` n'est jamais écrasé par un `sain` ultérieur non confirmé.

Quand le watchdog détecte un état dégradé (`restreint`/`banni`/`session_morte`) ou un heartbeat manqué, il envoie une alerte Telegram (avec anti-spam : pas de réémission de la même alerte dans la fenêtre de dédup, 1h par défaut).

---

## Sécurité

- **Aucun secret écrit en clair** : token bot, session string, numéro de téléphone — jamais dans la DB ni dans les logs. Masquage centralisé dans le `recorder` (clés sensibles rédigées + regex token bot).
- Requêtes SQLite **paramétrées uniquement**, payloads bornés (4 KB) avant insertion.
- L'envoi d'alerte est **soft-fail** : retry léger (3× backoff exponentiel) puis log, jamais de crash propagé au bot surveillé.

---

## Documentation

- **[docs/guide.md](docs/guide.md)** — guide d'usage complet (déploiement, intégration aiogram/telethon, watchdog en service).
- **[docs/api.md](docs/api.md)** — référence API (`Watch`, `attach`, CLI, états).
- **[docs/specs/2026-06-06-tgwatch-design.md](docs/specs/2026-06-06-tgwatch-design.md)** — spec de conception.

---

## Architecture (résumé)

```
src/tgwatch/
├── core/         # SQLite WAL, models, recorder (masquage secrets) — zéro dépendance Telegram
├── adapters/     # attach() aiogram + telethon, imports paresseux
├── health/       # classification santé userbot Telethon
├── alert/        # envoi alerte Telegram (urllib stdlib)
├── watchdog.py   # boucle : lit storage, détecte, alerte (process séparé)
└── cli.py        # tgwatch status / watch
```

Dépendances **unidirectionnelles** vers `core`. `core` ne connaît rien de Telegram. Multi-process safe (SQLite WAL).

---

## Licence

MIT.

## À propos

tgwatch est construit et maintenu par [SolidScale](https://solidscale.tech), l'agence IA appliquée pour les entreprises françaises. On open-source les outils qu'on utilise en interne.
