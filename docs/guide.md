# Guide d'usage tgwatch

Guide complet : intégration, déploiement du watchdog, exploitation.

## Sommaire

1. [Concepts](#concepts)
2. [Intégration aiogram (bot)](#intégration-aiogram-bot)
3. [Intégration telethon (userbot)](#intégration-telethon-userbot)
4. [Déployer le watchdog](#déployer-le-watchdog)
5. [Lire l'état](#lire-létat)
6. [Configuration](#configuration)
7. [FAQ](#faq)

---

## Concepts

tgwatch sépare **trois rôles** qui partagent un seul fichier SQLite (mode WAL, multi-process safe) :

| Rôle | Où | Quoi |
|------|-----|------|
| **Capture** | dans ton process bot/userbot, via `Watch.attach` | écrit events + heartbeat dans le SQLite |
| **Watchdog** | process séparé (`tgwatch watch`) | lit le SQLite, détecte down/dégradation, alerte |
| **Inspection** | terminal (`tgwatch status`) | lit le SQLite, affiche un instantané |

Un process bot mort ne peut pas s'alerter lui-même : c'est pourquoi le watchdog tourne **à part**.

États possibles :
- **Status** : `up` | `down` (vivant/mort par heartbeat)
- **Health** (userbot) : `sain` | `restreint` | `banni` | `session_morte` | `n/a` (bots = `n/a`)

---

## Intégration aiogram (bot)

```python
import asyncio
from aiogram import Bot, Dispatcher
from tgwatch import Watch

dp = Dispatcher()
bot = Bot(token="TON_BOT_TOKEN")

watch = Watch(
    storage="tgwatch.db",
    alert_bot_token="TOKEN_BOT_ALERTE",   # un AUTRE bot, dédié aux alertes
    alert_chat_id="@canal_alertes",
)
watch.attach(dp, kind="bot", name="bot_support")

# ... tes handlers ...

async def main():
    await dp.start_polling(bot)

asyncio.run(main())
```

Sous le capot : un middleware *outer* sur `dp.update` compte chaque update (heartbeat + compteur `msg`) et, sur exception d'un handler, enregistre un event `error` **puis relève l'exception** — ton bot se comporte exactement comme avant. Les `TelegramRetryAfter` (FloodWait) sont classés en event `floodwait`.

---

## Intégration telethon (userbot)

```python
import asyncio
from telethon import TelegramClient
from tgwatch import Watch

client = TelegramClient("session", api_id, api_hash)

watch = Watch(
    storage="tgwatch.db",
    alert_bot_token="TOKEN_BOT_ALERTE",
    alert_chat_id="@canal_alertes",
)

async def main():
    await client.start()
    watch.attach(client, kind="userbot", name="compte_scraping")
    await client.run_until_disconnected()

asyncio.run(main())
```

`attach` (userbot) fait deux choses :
1. Enregistre un handler `events.NewMessage` → heartbeat + compteur `msg`.
2. Démarre une **sonde de santé** en arrière-plan (`get_me()` toutes les 300 s par défaut) qui lit `.restricted` et capte session morte / ban.

> **Important (limite Telethon)** : Telethon avale les exceptions levées *dans* les handlers. La détection passive de `PeerFloodError`/`PhoneNumberBannedError` ne peut donc pas passer par un handler. Deux options :
> - laisser la **sonde active** détecter (suffit pour la plupart des cas) ;
> - appeler explicitement le classifieur dans ton propre `try/except` autour des appels risqués (voir [api.md](api.md#report_exception)) ;
> - activer l'interception opt-in via l'**adapter bas niveau** (`Watch.attach` ne l'expose pas) :
>   ```python
>   from tgwatch.adapters.telethon import attach as telethon_attach
>   task = telethon_attach(client, watch._recorder, "scraper", auto_intercept=True)
>   ```
>   (enveloppe `client._call` — méthode privée Telethon, à utiliser en connaissance de cause). Voir [api.md](api.md#configuration-avancée-de-la-sonde-adapter-bas-niveau).

---

## Déployer le watchdog

Le watchdog est un process synchrone autonome. Lance-le **en parallèle** de tes bots, sur la même machine (V1).

### En direct

```bash
tgwatch watch \
  --storage tgwatch.db \
  --token TOKEN_BOT_ALERTE \
  --chat-id @canal_alertes \
  --interval 60 \
  --heartbeat-threshold 300
```

### En service systemd

```ini
# /etc/systemd/system/tgwatch.service
[Unit]
Description=tgwatch watchdog
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/monbot
Environment=TGWATCH_STORAGE=/opt/monbot/tgwatch.db
Environment=TGWATCH_ALERT_BOT_TOKEN=123456:abc...
Environment=TGWATCH_ALERT_CHAT_ID=@canal_alertes
ExecStart=/opt/monbot/.venv/bin/tgwatch watch --interval 30
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now tgwatch
journalctl -u tgwatch -f
```

### Anti-spam

Une même alerte `(client, type)` n'est pas réémise dans la fenêtre de dédup (1 h par défaut, table `alerts_sent`). Si l'envoi HTTP échoue, retry 3× (backoff 1/2/4 s) puis log — jamais de crash.

---

## Lire l'état

```bash
tgwatch status --storage tgwatch.db
```

Colonnes : `NAME KIND STATUS LAST_SEEN HEALTH MSG ERROR FLOODWAIT`.

Le même fichier SQLite peut être lu pendant que bots et watchdog écrivent (WAL).

---

## Configuration

Tout passe par flags CLI ou variables d'environnement (les flags priment) :

| Variable | Équivalent flag | Défaut |
|----------|-----------------|--------|
| `TGWATCH_STORAGE` | `--storage` | — |
| `TGWATCH_ALERT_BOT_TOKEN` | `--token` | — |
| `TGWATCH_ALERT_CHAT_ID` | `--chat-id` | — |
| — | `--interval` | 60 s |
| — | `--heartbeat-threshold` | 300 s |

Côté code, `Watch(storage, alert_bot_token, alert_chat_id)` accepte un chemin `str` ou une instance `Storage`.

---

## FAQ

**Le bot d'alerte doit-il être le bot surveillé ?**
Non — utilise un bot **dédié** aux alertes. Indépendant des bots surveillés.

**Plusieurs bots/userbots dans le même SQLite ?**
Oui. Donne un `name` unique à chaque `attach`. Le watchdog les surveille tous.

**Le watchdog peut-il tourner sur une autre machine ?**
V1 suppose la même machine (SQLite partagé). Le multi-machine/collecteur HTTP est prévu V2.

**Et `@SpamBot` (santé forte) ?**
Reporté V1.1 (débit prudent — interroger trop ressemble à du spam).

**Mes secrets fuient-ils ?**
Non. Le `recorder` masque tokens/sessions/numéros avant toute écriture DB ou log. Un test dédié vérifie qu'aucun secret n'atteint le SQLite.
