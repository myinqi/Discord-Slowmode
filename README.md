# Corax Discord Bot

Corax is a modular Discord community bot with a permission-aware web administration panel. The project started as a channel slowmode manager and has grown into a collection of tools for Discord moderation, Suno music communities, Twitch radio streams, translations, community games, reminders, and media management.

The application uses Discord.py, Quart, SQLite, FFmpeg, Caddy, and an optional local Ollama service. It is designed to run through Docker Compose.

## Highlights

- Per-channel cooldowns with role exemptions and moderator controls
- Multi-user web administration with granular page permissions and audit logging
- Suno song discovery, statistics, playlist players, reactions, and promotion tools
- Two independent Twitch radio managers with Suno submissions and stream overlays
- Twitch EventSub alerts posted directly to Twitch chat
- Automatic chat translation through Google Translate, Ollama, OpenAI, or DeepL
- Raven's Nest: Relic Hunt with the Hrafnathorp village extension
- Collectible cards with weighted daily draws and private collection browsing
- Birthday calendar and personal one-time or recurring reminders
- Polls, quizzes, reaction roles, image posting, RPG adventures, and moderation tools

## Requirements

- Docker Engine with Docker Compose
- A Discord application and bot token
- A Discord server where the bot can register slash commands
- A public HTTPS hostname for Discord Player OAuth and Twitch OAuth callbacks
- Twitch credentials when using either radio manager or Twitch Alerts
- Optional OpenAI and DeepL API keys for their translation engines

FFmpeg is installed inside the application image. The Dockerfile intentionally uses **Python 3.12 on Debian 12 Bookworm** to retain the stable FFmpeg version used by the radio streams.

## Discord Application Setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create or reset the bot token on the **Bot** page.
3. Enable these privileged gateway intents:
   - **Server Members Intent**
   - **Message Content Intent**
4. Invite the bot with the `bot` and `applications.commands` scopes.
5. Grant the permissions required by the enabled features. The usual set includes:
   - View Channels
   - Send Messages
   - Embed Links
   - Attach Files
   - Read Message History
   - Add Reactions
   - Manage Messages
   - Create Public Threads and Send Messages in Threads
   - Manage Roles when reaction roles are used

For Suno Player identity linking, add these exact OAuth2 redirects for your hostname:

```text
https://your-domain.example/player/discord/callback
https://your-domain.example/public/player/discord/callback
```

The Client ID and Client Secret can be supplied through `.env` or managed from the Settings page.

## Configuration

Create the environment file:

```bash
cp .env.example .env
```

Core variables:

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `DISCORD_CLIENT_ID` | Discord application Client ID used as the Player OAuth fallback |
| `DISCORD_CLIENT_SECRET` | Discord application Client Secret used as the Player OAuth fallback |
| `GUILD_ID` | Discord server ID used for guild command synchronization |
| `WEB_HOST` | Quart bind address, normally `0.0.0.0` |
| `WEB_PORT` | Internal web port, normally `5000` |
| `SECRET_KEY` | Random secret used to protect web sessions |
| `ADMIN_USERNAME` | Initial Admin UI username |
| `ADMIN_PASSWORD` | Initial password, used only when the first account is created |
| `DATABASE_PATH` | SQLite database path |
| `BOT_NAME` | Initial web display name |

The Compose stack additionally configures the internal Ollama URL, model name, public web URL, Hugging Face cache, and `Europe/Berlin` timezone.

## Deployment

Build and start the complete stack:

```bash
docker compose up -d --build
```

Update the complete stack after pulling changes so Caddy and MediaMTX also reload
routing and media configuration:

```bash
git pull
docker compose up -d --build
```

Rebuilding only `slowmode-bot` is safe only when the update does not modify
`Caddyfile`, `mediamtx.yml`, or `docker-compose.yml`. In particular, TrYa DCS
media-route changes require the complete command above.

Inspect logs:

```bash
docker compose logs -f slowmode-bot
```

Stop the stack:

```bash
docker compose down
```

Docker Compose runs three services:

- `slowmode-bot`: Discord bot, background workers, FFmpeg stream managers, and Quart Admin UI
- `slowmode-caddy`: HTTPS reverse proxy
- `slowmode-ollama`: isolated local LLM service

SQLite data, uploaded assets, model caches, and Ollama data are stored in named Docker volumes. The Admin UI is reached through Caddy over HTTPS; port `5000` is not published directly by the provided Compose file.

## Administration Panel

The first login uses `ADMIN_USERNAME` and `ADMIN_PASSWORD`. The initial administrator must change the password before using the panel.

Each web user has an independent permission set. Navigation visibility can also be configured globally without removing the underlying permissions. Dashboard and Settings remain reachable so administrators cannot hide all management access accidentally.

### Core Administration

- **Dashboard**: bot connection state, guild information, monitored channels, and activity overview
- **Channels**: monitored channel configuration, cooldown duration, active timers, enable state, and resets
- **Roles**: cooldown exemptions and roles allowed to use administrative slash commands
- **Users**: Admin UI accounts, passwords, and page permissions
- **Welcome**: configurable welcome behavior
- **Audit Log**: searchable record of administrative and bot actions
- **Settings**: bot name and icon, guild, Player OAuth, menu visibility, Listening Party settings, database/full-data backup and validated restore, and general integration settings

### Moderation and Automation

- **Channel Moderation**: configurable moderation rules for selected channels
- **Executioner**: administrative cleanup tools
- **Submission Bans**: block selected Discord users from `/twitch-submit` and `/twitch-replace` for a defined number of Experimental Radio streams
- **Reaction Roles**: role assignment through Discord reactions
- **Auto Translate**: monitored channel, target languages, skip markers, and output formatting
  - Google Translate
  - Local Ollama LLM
  - ChatGPT through the OpenAI API, including a daily token limit
  - DeepL Free or Pro API
  - Separate messages or one combined multilingual response
  - Monthly request, character, and token usage statistics

### Suno and Community Tools

- **Suno Player**: Admin and public players with filtering, shuffle, audio visualization, lyrics, covers, and Discord-linked reactions
- **Player Reactions**: linked users react under their own Discord display name; Corax maintains one summary message in a song thread
- **Suno Playlist Player**: plays Suno playlists, Discord channel songs, party playlists, and historical Experimental Radio playlists
- **Suno Promotion**: maintains profiles and reliably resolves recent non-pinned Suno songs
- **Suno Analyzer**: resolves song metadata and related Suno information
- **Songripper**: downloads Suno audio as MP3 or WAV; MP3 output is re-encoded to repair misleading duration headers
- **Playlist Search**: indexes and searches playlist links from configured Discord channels
- **Song, User, and Reaction Stats**: historical scans, filters, leaderboards, charts, reaction breakdowns, and data maintenance
- **Image Posting**: categorized image library used by `/imageposting`
- **Polls and Quiz**: poll management, configurable quiz categories, questions, scoring, and highscores

### Listening Party

Two related workflows are available:

1. **Random Song** scans a configured input channel for recent Suno links and posts a random result to an output channel. It can be globally disabled in Settings; when disabled, `/random-song` is blocked while channel configurations are preserved.
2. **Party Playlist** accepts Suno or YouTube submissions, tracks heard songs, and provides a private Discord carousel plus a web-based playlist view.

The maximum number of Party Playlist submissions per member is configurable.

### Card Collection

Administrators create collectible cards with an image, name, optional deck label, rarity, draw weight, and availability state. Users receive one public draw per day and privately browse their own or another member's collection.

Duplicate cards are stored as quantities. Draw probability uses the configured rarity base weight multiplied by the card's individual draw weight. The Admin UI displays the resulting probability for every available card.

### Birthday Calendar and Reminders

- Members can save or remove only their own birthday.
- `/birthdays` privately shows upcoming, complete, or month-specific calendars.
- The Admin UI can edit all entries and configure reminder messages two days before and on the birthday.
- Automated birthday notices suppress user pings.
- Personal reminders can be one-time, daily, weekly, or monthly and are delivered by DM.
- Members can delete only their own reminders; administrators can manage all reminders in the web panel.

## Twitch Radio

The project contains two independent radio managers. They share parts of the Twitch integration but cannot run at the same time. The classic radio is additionally blocked on configured Experimental Radio stream days.

### Classic Twitch Radio

- Public Suno submission form with configurable stream name
- Automatic MP3 download from Suno
- Configurable submissions per Suno user and maximum playable duration
- Decoded-audio duration validation to handle broken MP3 headers
- Optional shuffled playback
- Background image or looped background video
- Configurable lyrics box and scrolling cleaned Suno lyrics
- Local or RTMP picture-in-picture input
- Song video PiP with cover fallback
- Configurable persistent multi-line disclaimer rendered in the bottom-left corner
- Twitch OAuth chat bot and delayed Now Playing announcements
- Song expiry notifications that include the configured stream name
- Stable 2000 kbps H.264 Twitch output

### Experimental Radio

- Discord submission, replacement, deletion, and playlist commands
- Two-step consent and server-side Suno audio transfer
- Real decoded duration validation at submission and stream-build time
- Separate submission and admin playlists plus intro and outro material
- Expiry handling, consent records, moderation state, cover normalization, and manual duration checks
- Whisper transcription, timestamps, lyrics preparation, and ASS subtitle overlays
- Optional Suno Hook videos supplied by administrators or submitters
- Hook video cleanup when a song expires, is deleted, or is replaced
- Configurable Song Video PiP, progress display, persistent bottom-right disclaimer, and early-play boost
- Manual, safe-stop, and scheduled stream controls with live logs
- Current-song status and Suno links in the Admin UI
- Historical stream snapshots used by `/twitch-to-suno` and the Suno Playlist Player

## Twitch Alerts

Twitch Alerts uses EventSub and can post customizable messages directly into Twitch chat for:

- New followers
- New subscriptions
- Resubscription messages
- Gift subscriptions
- Bits and cheers
- Incoming raids
- Subscription streak and milestone notifications when Twitch supplies them

Event reception uses the broadcaster account while chat output can use the separate Corax bot account. The Admin UI shows feature, EventSub, chat sender, event account, scope, and subscription status. Twitch-native emotes can be used by writing their exact emote name, provided the sending account has access to them.

## Raven's Nest: Relic Hunt

Raven's Nest is a Twitch chat collection game backed by SQLite. It includes relic rarity, points, XP, ranks, daily rewards, combining, a community ritual, a phrase puzzle, custom commands, configurable game access, and the Hrafnathorp village extension.

Viewer commands:

```text
!raven !nest !items !top !rank !daily !ritual !combine
!village !entertain !teach !trade !invest !nextVillage
!phrase !solve !relichelp
```

Village investments consume Shinies. Completed village rewards are summarized into a single chat message to avoid flooding the channel.

## Slash Commands

Commands shown by Discord depend on command synchronization and permissions. Commands marked as administrative require the server owner or a configured Command Role.

### Cooldowns and Moderation

| Command | Description |
|---|---|
| `/cooldown-set` | Set a channel cooldown from 0 to 2880 minutes (admin) |
| `/cooldown-info` | Show a channel's cooldown configuration |
| `/cooldown-reset` | Reset a member's cooldown (admin) |
| `/cooldown-clear` | Clear channel cooldown state (admin) |
| `/cooldown-toggle` | Enable or disable monitoring (admin) |
| `/timer` | Privately show the caller's active cooldown timers |

### Songs and Listening Parties

| Command | Description |
|---|---|
| `/random-song` | Pick from a configured recent-song input channel; blocked when the feature is disabled |
| `/find-list` | Search indexed Suno playlists |
| `/find-song` | Find a song by member, title, or random selection |
| `/find-usersongs` | Browse a member's recent songs in a private carousel |
| `/song-stats` | Show song posting statistics |
| `/user-stats` | Show statistics for a member |
| `/user-score` | Post the song contribution leaderboard |
| `/top` | Privately show the most reacted songs for a period |
| `/new` | Browse recent songs the caller has not reacted to |
| `/party-submit` | Submit a Suno or YouTube song to the Party Playlist |
| `/party-songs` | View the caller's Party Playlist submissions |
| `/party-remove` | Remove one of the caller's submissions |
| `/party-list` | Privately list all Party Playlist submissions |
| `/party` | Open the unheard-song carousel |
| `/party-reset` | Reset the Party Playlist (admin) |
| `/player` | Post the configured public Suno Player link |

### Experimental Radio

| Command | Description |
|---|---|
| `/twitch-submit` | Start a new Experimental Radio submission, optionally with a Hook video |
| `/twitch-replace` | Validate a new song and replace the caller's oldest active submission |
| `/twitch-delete` | Delete one of the caller's active submissions |
| `/twitch-hook` | Add or replace a Hook video on an active submission |
| `/twitch-hook-remove` | Remove a Hook video from an active submission |
| `/twitch-playlist` | Privately show the current submission playlist |
| `/twitch-to-suno` | Return Suno URLs from the running or most recent stream snapshot |

### Community and Utilities

| Command | Description |
|---|---|
| `/cards-draw` | Publicly draw one collectible card per day |
| `/cards-collection` | Privately browse the caller's or another member's collection |
| `/birthday-set` | Save or update the caller's birthday |
| `/birthday-remove` | Remove the caller's birthday |
| `/birthdays` | Privately browse the server birthday calendar |
| `/reminder-set` | Create a one-time, daily, weekly, or monthly DM reminder |
| `/reminder-delete` | Delete one of the caller's reminders |
| `/quiz` | Post a random configured quiz question |
| `/quiz-highscore` | Privately show the quiz top ten |
| `/poll-create` | Create a poll |
| `/poll-edit` | Edit an existing poll |
| `/imageposting` | Post an image from the configured library |
| `/talk` | Ask the bot to post text, optionally translated |
| `/translate` | Privately translate text |
| `/dice` | Roll configurable W6, W10, or W20 dice |
| `/help` | Privately show the in-Discord command overview |

The message context action **Translate Message** translates an existing Discord message.

### RPG Adventures

The `/rpg` command group provides character and party-based adventures:

```text
/rpg classes
/rpg create
/rpg sheet
/rpg delete-character
/rpg party-create
/rpg party-join
/rpg party-leave
/rpg party-list
/rpg party-status
/rpg adventures
/rpg start
/rpg scene
/rpg choose
/rpg say
/rpg attack
/rpg ability
/rpg status
```

## Architecture

```text
Discord-Slowmode/
├── bot/
│   ├── main.py                 Discord bot initialization and cog loading
│   ├── database.py             Async SQLite schema, migrations, and repositories
│   ├── stream_manager.py       Classic Twitch Radio FFmpeg manager
│   ├── exp_stream_manager.py   Experimental Radio scheduler and FFmpeg manager
│   ├── exp_radio_worker.py     Suno download, metadata, transcription, and analysis jobs
│   ├── twitch_bot.py           Twitch OAuth and rate-limited chat sender
│   ├── twitch_event_alerts.py  Twitch EventSub listener
│   ├── relic_hunt.py           Raven's Nest game logic
│   ├── rpg_engine.py           RPG rules and models
│   └── cogs/                   Discord listeners and slash-command modules
├── web/
│   ├── app.py                  Quart routes, APIs, OAuth callbacks, and Admin UI logic
│   ├── templates/              Jinja2 templates
│   └── static/                 Static branding assets
├── data/                       Runtime data in non-container development
├── config.py                   Environment configuration
├── run.py                      Starts Discord and Quart together
├── Dockerfile                  Debian 12 application image
├── docker-compose.yml          Bot, Caddy, Ollama, and persistent volumes
└── Caddyfile                   HTTPS reverse proxy configuration
```

Database migrations run automatically during startup. Feature settings and sensitive integration credentials saved through the Admin UI are stored in SQLite; the database and uploaded runtime files must therefore be backed up together.

### Admin Backups

Settings provides two online exports while Corax remains available:

- **Database Backup** creates a transactionally consistent SQLite file.
- **Full Data Archive** packages that SQLite snapshot together with persistent uploaded assets. Rebuildable model caches and previous restore safety copies are omitted.

Database restore accepts the SQLite export, runs an integrity and schema check, and requires the confirmation text `RESTORE`. Both radio streams must be stopped. Before replacing the active database, Corax stores a safety copy in `data/restore_backups/` and keeps the five newest copies. The process then exits cleanly so the Docker `unless-stopped` policy starts it with the restored database.

## Troubleshooting

| Problem | Check |
|---|---|
| Bot is offline | Validate `DISCORD_TOKEN`, guild access, and container logs |
| Slash commands are missing | Confirm the `applications.commands` scope, `GUILD_ID`, and command synchronization logs |
| Messages cannot be managed | Grant Manage Messages and verify the bot's role position |
| Player OAuth returns `invalid_client` | Ensure Client ID and Client Secret belong to the same Discord application and rebuild after changing `.env` |
| OAuth reports a redirect mismatch | Add the exact HTTPS callback shown in Settings to the provider application |
| Twitch Alerts are connected to only some events | Re-authorize the broadcaster with all displayed scopes, then restart the listener |
| Twitch chat returns HTTP 429 | Inspect the sender queue and Twitch rate-limit logs |
| Stream duration differs from the audible song | Run the decoded-duration check; Suno MP3 headers can contain incorrect duration metadata |
| Admin UI is unavailable | Check Caddy and bot logs, DNS, HTTPS certificates, and the internal `slowmode-bot:5000` connection |

## Security and Operations

- Never commit `.env`, API keys, OAuth secrets, refresh tokens, stream keys, or the SQLite database.
- Use a long random `SECRET_KEY`.
- Keep the Admin UI behind HTTPS.
- Grant only the Admin UI permissions each account requires.
- Back up the `bot-data` Docker volume regularly.
- Treat database backups as sensitive because settings may contain integration credentials.
- Test radio and OAuth changes on the server where the production network and provider callbacks are available.
