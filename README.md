# Discord Slowmode Bot

Ein modularer Discord-Bot, der benutzerdefinierte Cooldowns pro Kanal durchsetzt. Inklusive Web-Interface zur Konfiguration, Audit-Log, Benutzerverwaltung und Slash-Commands.

---

## Funktionen

- **Kanal-Cooldowns**: Pro Kanal einstellbar (1–2880 Minuten / 48 Stunden), oder Kanäle ohne Cooldown zur späteren Erweiterung
- **Rollenausnahmen**: Bestimmte Rollen können vom Cooldown ausgenommen werden
- **Slash-Commands**: Konfiguration direkt aus Discord heraus (nur für berechtigte Rollen + Serverbesitzer)
- **Web-Interface**: Vollständiges Admin-Panel mit Discord-ähnlichem Design
- **Benutzerverwaltung**: Mehrere Admin-Benutzer, Passwort-Management
- **Audit-Log**: Alle Aktionen werden protokolliert und sind im Web-Interface einsehbar
- **DM-Benachrichtigung**: Betroffene Nutzer erhalten eine Nachricht mit verbleibender Wartezeit
- **Docker-Support**: Einfaches Deployment mit Docker Compose
- **Modular**: Erweiterbar durch das discord.py Cog-System

---

## Voraussetzungen

- **Docker** und **Docker Compose** auf dem Server installiert
- Ein **Discord Bot Token** (siehe unten)

---

## Schritt 1: Discord Bot erstellen

1. Gehe zu [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Klicke auf **„New Application"** und vergib einen Namen
3. Gehe zu **„Bot"** in der linken Seitenleiste
4. Klicke auf **„Reset Token"** und kopiere den Token — **diesen sicher aufbewahren!**
5. Aktiviere unter **„Privileged Gateway Intents“** folgende Intents:
   - ✅ **SERVER MEMBERS INTENT** (Zugriff auf Servermitglieder-Informationen)
   - ✅ **MESSAGE CONTENT INTENT** (Zugriff auf Nachrichteninhalte)
6. Gehe zu **„OAuth2“ → „URL Generator“**:
   - **Anwendungsbereiche (Scopes)**: `bot`, `applications.commands`
   - **Bot-Berechtigungen**: `Nachrichten senden`, `Nachrichten verwalten`, `Nachrichtenverlauf lesen`, `Kanäle ansehen`
7. Kopiere die generierte URL und öffne sie im Browser, um den Bot auf deinen Server einzuladen

---

## Schritt 2: Server-ID herausfinden

1. Öffne Discord und gehe zu **Einstellungen → Erweitert → Entwicklermodus aktivieren**
2. Rechtsklick auf deinen Server → **„Server-ID kopieren"**

---

## Schritt 3: Konfiguration

1. Repository klonen oder die Dateien auf den Server kopieren:

```bash
git clone <repository-url> slowmode-bot
cd slowmode-bot
```

2. `.env`-Datei erstellen:

```bash
cp .env.example .env
```

3. `.env`-Datei bearbeiten:

```bash
nano .env
```

Folgende Werte anpassen:

| Variable | Beschreibung | Beispiel |
|---|---|---|
| `DISCORD_TOKEN` | Bot-Token aus Schritt 1 | `MTIz...abc` |
| `GUILD_ID` | Server-ID aus Schritt 2 | `123456789012345678` |
| `SECRET_KEY` | Zufälliger String für Session-Verschlüsselung | `mein-geheimer-schluessel-123` |
| `ADMIN_USERNAME` | Benutzername für den ersten Admin | `admin` |
| `ADMIN_PASSWORD` | Initialpasswort (muss beim ersten Login geändert werden) | `changeme` |
| `WEB_PORT` | Port für das Web-Interface | `5000` |
| `BOT_NAME` | Anzeigename des Bots | `Slowmode Bot` |

> **Wichtig:** Das Initialpasswort (`ADMIN_PASSWORD`) wird nur beim allerersten Start verwendet, um den Admin-Account zu erstellen. Es muss danach über das Web-Interface geändert werden.

---

## Schritt 4: Bot starten

Bot im Hintergrund (detached) starten:

```bash
docker compose up -d --build
```

> Das `-d` Flag steht für **detached** — der Bot läuft im Hintergrund und blockiert das Terminal nicht.

Logs prüfen:

```bash
docker compose logs -f
```

Bot stoppen:

```bash
docker compose down
```

---

## Schritt 5: Erstes Login im Web-Interface

1. Öffne im Browser: `http://<server-ip>:5000`
2. Melde dich mit den in der `.env` konfigurierten Zugangsdaten an
3. Du wirst aufgefordert, das Passwort zu ändern — **dies ist verpflichtend**
4. Nach der Passwortänderung gelangst du zum Dashboard

---

## Web-Interface Übersicht

### Dashboard
Übersicht über Bot-Status, verbundener Server, überwachte Kanäle und Audit-Statistiken.

### Channels (Kanäle)
- Kanäle hinzufügen (Dropdown wenn Bot verbunden, sonst manuelle ID-Eingabe)
- Cooldown pro Kanal einstellen (0–2880 Minuten, 0 = kein Cooldown)
- Aktive Cooldowns pro Kanal einsehen mit Reset-Button pro Nutzer
- Kanäle aktivieren/deaktivieren
- Kanäle entfernen

### Roles (Rollen)
- **Exempt Roles**: Rollen, die vom Cooldown ausgenommen sind
- **Command Roles**: Rollen, die Slash-Commands nutzen dürfen (Serverbesitzer können immer)

### Users (Benutzer)
- Weitere Admin-Benutzer für das Web-Interface erstellen
- Passwörter zurücksetzen
- Benutzer löschen

### Audit Log
Protokoll aller Aktionen: gelöschte Nachrichten, Konfigurationsänderungen, Benutzerverwaltung.

### Listening Party
- Input-Kanal (muss ein überwachter Kanal sein) und Output-Kanal definieren
- Zeitraum in Stunden konfigurieren (wie weit zurück nach Songs gesucht wird)
- Per `/random-song` Slash-Command wird ein zufälliger Suno-Song aus dem Zeitraum in den Output-Kanal gepostet
- Unterstützte URL-Formate: `https://suno.com/s/...` und `https://suno.com/song/...`

### Playlist Search
- Kanäle definieren, in denen Suno-Playlist-Links gepostet werden
- Nutzer können mit `/find-list <Suchbegriff>` nach Playlists suchen
- Ergebnisse werden nur dem suchenden Nutzer angezeigt (ephemeral)
- Unterstütztes URL-Format: `https://suno.com/playlist/...`

### Song Stats
- Übersicht über gepostete Songs pro überwachtem Kanal
- Aufschlüsselung nach Jahr, Monat, Woche und Tag mit Balkendiagrammen
- Filter nach einzelnem Kanal oder alle Kanäle zusammen
- "Scan History" Button zum Importieren aller bisherigen Song-Posts aus der Kanalhistorie
- Neue Songs werden automatisch in Echtzeit erfasst

### Settings (Einstellungen)
- Bot-Name ändern
- Guild-ID ändern (erfordert Neustart)

---

## Slash-Commands

| Command | Beschreibung |
|---|---|
| `/cooldown-set #kanal 120` | Cooldown für einen Kanal setzen (in Minuten, 0–2880) |
| `/cooldown-info #kanal` | Aktuelle Konfiguration eines Kanals anzeigen |
| `/cooldown-reset @user [#kanal]` | Cooldown eines Nutzers zurücksetzen |
| `/cooldown-clear [#kanal]` | Alle Cooldowns in einem/allen Kanal/Kanälen löschen |
| `/cooldown-toggle #kanal true/false` | Monitoring für einen Kanal ein-/ausschalten |
| `/random-song [#input-kanal]` | Zufälligen Suno-Song aus dem Input-Kanal posten |
| `/find-list <Suchbegriff>` | Suno-Playlists nach Interpret/Keyword durchsuchen (nur für dich sichtbar) |
| `/song-stats [#kanal]` | Song-Posting-Statistiken anzeigen (nur für dich sichtbar) |

> Cooldown- und Toggle-Commands können nur von Serverbesitzern und Mitgliedern mit einer **Command Role** genutzt werden. `/random-song`, `/find-list` und `/song-stats` sind für **alle** Servermitglieder verfügbar.

---

## Architektur

```
discord-slowmode-bot/
├── bot/
│   ├── main.py              # Bot-Klasse und Initialisierung
│   ├── database.py          # SQLite Datenbankmanager (async)
│   └── cogs/
│       ├── slowmode.py      # Cooldown-Überwachung (on_message)
│       └── commands.py      # Slash-Commands
├── web/
│   ├── app.py               # Quart Web-Server (alle Routen)
│   └── templates/           # HTML Templates (Jinja2 + TailwindCSS)
├── config.py                # Konfiguration aus Umgebungsvariablen
├── run.py                   # Startet Bot + Web-Server gleichzeitig
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

### Erweiterbarkeit

Der Bot verwendet das **Cog-System** von discord.py. Neue Funktionen können als eigene Cogs unter `bot/cogs/` hinzugefügt werden:

1. Neue Datei erstellen: `bot/cogs/mein_feature.py`
2. Cog-Klasse mit `commands.Cog` erstellen
3. In `bot/main.py` laden: `await self.load_extension("bot.cogs.mein_feature")`

---

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| Bot kommt nicht online | `DISCORD_TOKEN` und `GUILD_ID` in `.env` prüfen |
| Slash-Commands erscheinen nicht | Bot neu einladen mit `applications.commands` Scope |
| Nachrichten werden nicht gelöscht | Bot benötigt die Permission `Manage Messages` |
| Web-Interface nicht erreichbar | Firewall-Port 5000 freigeben, `WEB_HOST=0.0.0.0` prüfen |
| „Must change password"-Schleife | Mit dem Initial-Passwort einloggen und neues Passwort setzen |

---

## Sicherheitshinweise

- **`.env`-Datei niemals in Git committen** — sie enthält den Bot-Token!
- `SECRET_KEY` sollte ein langer, zufälliger String sein
- Für Produktivbetrieb: Web-Interface hinter einem Reverse-Proxy (z.B. nginx) mit HTTPS betreiben
- Regelmäßig Passwörter ändern
