# tgwatch — Design Spec (v1)

> Date: 2026-06-06
> Statut: validé (brainstorming), prêt pour planification GSD
> Auteur: Anis Hammouche

## 1. But

Bibliothèque Python pour **surveiller des bots Telegram (aiogram) et des comptes userbot (Telethon)**.

L'utilisateur importe la lib, l'attache à son bot/client en deux lignes, et tgwatch capte automatiquement :
- l'état vivant/mort (heartbeat)
- les compteurs (messages, erreurs, FloodWait)
- la santé du compte userbot (restriction, ban, session morte) — le différenciant
- envoie une alerte dans Telegram quand un problème survient

**Zéro infrastructure** : stockage SQLite local, pas de Prometheus, pas de Grafana, pas de serveur obligatoire.

## 2. Pourquoi (niche validée)

Recherche PyPI/GitHub (2026-06-06) :
- `aiogram-prometheus` existe mais : nécessite Prometheus + Grafana (infra lourde), **bots seulement**, pas d'alerte Telegram intégrée, pas de santé compte.
- Monitoring userbot Telethon (santé compte : ban / restriction / session) : **rien n'existe**.
- Les tutos "Telegram + monitoring" font l'inverse (utiliser un bot pour surveiller des serveurs).

Trois trous non couverts : monitoring léger zéro-infra + alerte Telegram native, bot ET userbot unifiés, santé compte userbot.

## 3. Décisions verrouillées

| Décision | Choix | Raison |
|---|---|---|
| Périmètre V1 | bot aiogram + userbot Telethon | l'utilisateur veut les deux ; couche `core` partagée évite la duplication |
| Collecte | lib pure in-process, SQLite partagé | zéro serveur, zéro réseau, le plus testable ; tous les process sur la même machine |
| Détection crash | mini-guetteur CLI (`tgwatch watch`) | un process mort ne peut pas s'alerter lui-même ; un watchdog externe lit le SQLite |
| Transport alerte | bot Telegram dédié (token + chat_id) | indépendant des bots surveillés, simple HTTP, mock-able |
| Surface V1 | CLI seul (`tgwatch status`) | dashboard web reporté V2, focus livraison |
| Nom | `tgwatch` | court, libre sur PyPI |
| Python | 3.11+ | moderne |
| Packaging | `pyproject.toml`, layout `src/`, extras optionnelles | `tgwatch[telethon]`, `tgwatch[aiogram]` : on n'installe que ce qu'on utilise |
| Licence | MIT | OSS, vitrine Labs |
| Tests | pytest + pytest-asyncio, couverture ≥ 80% | règle projet |

## 4. Architecture

Modules isolés, dépendances unidirectionnelles. `core` ne connaît rien de Telegram.

```
src/tgwatch/
├── core/
│   ├── storage.py       # SQLite (WAL) : écrit/lit events + clients. AUCUNE dépendance telegram.
│   ├── models.py        # dataclasses : Client, Event, Status, Health
│   └── recorder.py      # API centrale : record_event(), heartbeat(), set_health() + masquage secrets
├── adapters/
│   ├── aiogram.py       # attach(dispatcher) → middleware capte updates/erreurs. import optionnel.
│   └── telethon.py      # attach(client) → hook capte erreurs + santé compte. import optionnel.
├── health/
│   └── telethon_health.py  # détecte restriction/ban/session morte
├── alert/
│   └── telegram.py      # envoie alerte via bot dédié (Bot API HTTP). mock-able.
├── watchdog.py          # boucle : lit storage, détecte heartbeat manqué → alerte
└── cli.py               # `tgwatch status` / `tgwatch watch`
```

**Règle de dépendance** : `adapters`, `health`, `alert`, `watchdog`, `cli` dépendent de `core`. `core` ne dépend de personne (ni aiogram, ni telethon, ni réseau). Les imports d'aiogram/telethon sont confinés aux adapters et chargés paresseusement (extras optionnelles).

## 5. API cible

```python
from tgwatch import Watch

watch = Watch(
    storage="tgwatch.db",
    alert_bot_token="123:abc",      # bot Telegram dédié aux alertes
    alert_chat_id="@mon_canal",
)

# userbot Telethon
watch.attach(telethon_client, kind="userbot", name="compte_scraping")

# bot aiogram
watch.attach(dispatcher, kind="bot", name="bot_support")
```

Après `attach`, la capture est automatique. Rien d'autre à coder côté utilisateur.

CLI :
- `tgwatch status` — affiche l'état de tous les clients (vivant/mort, compteurs, dernières erreurs/restrictions).
- `tgwatch watch` — lance le guetteur permanent (détecte heartbeat manqué, envoie alertes).

## 6. Flux de données

```
bot/userbot --(adapter capte)--> recorder --> storage (SQLite)
                                                  ↑
                              watchdog (process à part) lit
                                                  ↓
                              heartbeat manqué / restriction
                                                  ↓
                                  alert.telegram --> canal Telegram
```

- `status` (CLI) : lit storage, affiche un instantané.
- `watch` (watchdog) : boucle qui lit storage périodiquement + déclenche alertes.

## 7. Modèle SQLite

```sql
clients(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE,
  kind TEXT,            -- bot | userbot
  status TEXT,          -- up | down
  last_seen TIMESTAMP,
  health TEXT           -- sain | restreint | banni | session_morte | n/a
);

events(
  id INTEGER PRIMARY KEY,
  client_id INTEGER,
  type TEXT,            -- msg | error | floodwait | restriction | down | up
  payload_json TEXT,
  ts TIMESTAMP
);

alerts_sent(
  id INTEGER PRIMARY KEY,
  client_id INTEGER,
  type TEXT,
  ts TIMESTAMP          -- dédup anti-spam d'alertes
);
```

SQLite en mode WAL pour l'accès concurrent multi-process (bots + watchdog + CLI).

## 8. Santé compte userbot (le différenciant)

Trois couches, combinées dans `health/telethon_health.py` :

1. **Passif** (V1) : l'adapter Telethon capte les exceptions et les enregistre :
   - `PeerFloodError` → compte limité (spam)
   - `PhoneNumberBannedError`, `UserDeactivatedBanError` → compte banni
   - `AuthKeyUnregisteredError` / Unauthorized → session morte/révoquée
   - `FloodWaitError(.seconds)` → ralenti ; gros/répété = signal
   Gratuit, temps réel.

2. **Actif léger** (V1) : `get_me()` périodique → vérifie session vivante + lit `.restricted`.

3. **Actif fort** (V1.1, reporté) : interroger `@SpamBot` 1×/jour max, parser la réponse. Reporté pour débit prudent (interroger trop = ressemble à du spam).

État résultant : `sain | restreint | banni | session_morte`.

## 9. Gestion des erreurs

- Les adapters **n'avalent jamais** l'exception du bot : ils l'enregistrent PUIS la relèvent. Le comportement du bot surveillé reste inchangé.
- `storage` : SQLite WAL pour accès multi-process concurrent.
- `alert` : si l'envoi échoue, retry léger + log, jamais de crash propagé.
- Anti-spam d'alertes : `alerts_sent` empêche de réémettre la même alerte en boucle (fenêtre de dédup).

## 10. Sécurité (dès V1, non négociable)

- Token bot, session string Telethon, numéro de téléphone : **jamais** écrits dans `events`, `alerts`, ou les logs. Masquage centralisé dans `recorder`.
- `tgwatch.db` ajouté au `.gitignore` du template/README.
- Aucune entrée externe non validée (payloads bornés avant insertion SQLite, requêtes paramétrées uniquement).

## 11. Testabilité (≥ 80%)

- `core` : SQLite réel sur fichier temporaire → tests réels (pas de mock I/O).
- `adapters` : faux client/dispatcher qui lève les erreurs → 100% offline.
- `health` : mock des réponses `get_me()` / `@SpamBot`.
- `alert` : mock de l'appel HTTP Bot API.
- `watchdog` : storage avec `last_seen` ancien → assert qu'une alerte part.

## 12. Périmètre

**V1 (fini)** : heartbeat • compteurs • santé compte (couches 1+2) • alerte Telegram (bot dédié) • watchdog CLI (`watch`) • `status` CLI • adapters aiogram + telethon • packaging pip + extras • tests ≥ 80%.

**Hors V1** : dashboard web (V2) • query `@SpamBot` (V1.1) • graphes/historique long (V2) • multi-machine / collecteur HTTP (V2) • alertes email/Discord (V2).

## 13. Critères de succès

- `pip install tgwatch[telethon]` puis `watch.attach(client)` en 2 lignes capte les erreurs sans modifier le comportement du bot.
- Un userbot restreint (PeerFloodError) déclenche une alerte Telegram en < 1 cycle watchdog.
- Un process bot tué est détecté `down` par le watchdog et alerté.
- `tgwatch status` affiche l'état correct de tous les clients.
- Couverture de tests ≥ 80%, tous offline.
- Aucun secret jamais présent dans la DB ou les logs (test dédié).
