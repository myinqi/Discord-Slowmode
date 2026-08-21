# TrYa DCS deployment notes

TrYa DCS is the private Discord Community Stream. It is deliberately separate
from TrYa Stream and Experimental Radio, even though later milestones can reuse
their proven media-processing components.

The implementation checklist and progress log live in [`plan.md`](../plan.md).

## Current state

The current implementation includes:

- an independent Admin UI permission and configuration page;
- dedicated SQLite tables for songs, consent evidence, playlist snapshots and
  short-lived stream tokens;
- a separate persistent media directory;
- Discord OAuth with `identify guilds` and a server-side guild membership check;
- renewable, hashed HLS access tokens stored in an HttpOnly cookie;
- Caddy authorization for every protected HLS request;
- an internal MediaMTX service and a responsive HLS player shell.
- private `/trya-dcs-submit`, `/trya-dcs-replace` and `/trya-dcs-delete`
  Discord commands;
- a one-time web upload form with versioned rights confirmations and documented
  Free/Paid plus Original/Cover/Remix status;
- immutable original evidence, normalized work audio, real decoded duration,
  Suno metadata, Whisper timestamps and ASS subtitles;
- optional local-LLM lyric moderation that routes flagged, uncertain or failed
  reviews to manual approval without considering Free/Paid or content kind.
- a dedicated FFmpeg publisher targeting MediaMTX, with safe stop, playlist
  snapshots and configurable playlist-end behaviour;
- manual Admin UI approval/rejection controls;
- live player metadata and a Discord-backed WebSocket chat with guild emojis,
  attachments, replies, edits and deletions;
- managed webhook posting for authenticated web users, marked with `· Web`;
- independent DCS background uploads and media-frame corner/border settings;
- independent DCS top-right overlay uploads with fixed, single-shuffle,
  ordered/random concat-all and rotating random-subset modes plus validated
  cached 720p CFR30 combined videos;
- admin-assigned intro/outro pools, with intro played once and outro on stop-mode
  completion or safe stop;
- optional validated WeLoveMusic track links alongside canonical Suno links in the
  private player, editable during submission or later from the Admin UI;
- separate Admin UI lists for intro songs followed by outro songs, excluded from
  the normal submission table;
- permanent Admin UI cleanup for failed or unfinished uploads, including their
  database rows and associated files;
- evidence-preserving removal of active submission, intro and outro songs;
- compact Admin actions for Whisper and moderation retries plus validated external
  word-timestamp JSON imports that regenerate ASS subtitles;
- a central live activity log for publishing, uploads, Whisper, moderation and
  Admin actions, with polling and clipboard copy;
- independently collapsible Admin UI sections whose state persists per browser;
- a configurable, validated offline player image stored outside the public web
  root and served only to authenticated guild members;
- a live dashboard below the player with previous/next-song links, playlist
  position, song/rotation remaining time and in-memory active-listener presence;
- a configurable rotating community panel and live Raven's Nest panel with
  leaderboard, commands, finds, combines, ritual, phrase and event state;
- transport-neutral Raven's Nest command dispatch from native Discord and the
  authenticated web chat into the existing shared `relic_*` database tables;
- a dedicated OBS contribution listener at `rtmp://HOST:1937/live` with a
  per-installation stream key and automatic local-overlay fallback.

DCS intro/outro pools and a dedicated OBS RTMP overlay contribution are available.
Reaction synchronisation remains an optional later milestone.

## Submission commands

All command responses and upload links are private to the invoking member:

- `/trya-dcs-submit` creates a one-time upload slot;
- `/trya-dcs-replace` keeps the old song active until the replacement file has
  passed validation and its evidence transaction succeeds;
- `/trya-dcs-delete` removes an owned song from the active playlist or cancels
  an unfinished upload while retaining completed audit evidence.

Upload slots expire after 24 hours. Opening one requires Discord OAuth and the
authenticated Discord ID must match the member who invoked the slash command.

The configured guild, feature switch, maximum songs, upload size and decoded
duration are enforced server-side. File extensions, signatures and audio
streams are validated independently of browser-provided MIME headers.

An Admin can replace a failed transcription with a JSON array in the stored
word-timestamp format:

```json
[{"word":"Hello","start":0.5,"end":0.9}]
```

Entries must contain exactly `word`, `start` and `end`, remain chronological and
fit the decoded song duration. A successful import replaces `word_timestamps`,
regenerates the ASS file. With LLM moderation enabled the song returns to pending
approval and moderation; otherwise it is immediately eligible for playback.

## Raven's Nest transport

The existing Raven's Nest configuration page remains the sole game-rules control
plane. DCS only controls whether commands are accepted in its configured chat and
which informational panels rotate in the player.

Native messages in the configured Discord channel and authenticated web-chat
messages dispatch through the same game handlers as Twitch. Discord-backed players
use `discord:<user-id>` keys in the existing `relic_users` and related tables, avoiding
ID collisions while sharing items, recipes, events, ritual, phrase, village and
leaderboard state. Webhook echoes and bot responses are excluded from command parsing.

## Discord application

Add this exact OAuth2 redirect URL to the Discord application used by the bot:

```text
https://bot.macfreun.de/trya-dcs/oauth/callback
```

The OAuth flow requests only:

```text
identify guilds
```

The bot then verifies the member against its configured guild. Ensure the bot
has the Server Members intent enabled and can fetch members of that guild. For
web-originated chat messages, grant it `Manage Webhooks` in the configured DCS
chat channel; a clearly marked bot-message fallback is used otherwise.

The existing environment values are reused:

```dotenv
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
GUILD_ID=...
WEB_URL=https://bot.macfreun.de
```

An optional independent data path can be supplied:

```dotenv
TRYA_DCS_DIR=/app/data/radio/trya_dcs
```

The default already points inside the persistent `/app/data` volume.

## Network boundary

MediaMTX exposes no host ports in Docker Compose. The bot exposes only the optional
OBS contribution listener on host port `1937`. Internally MediaMTX uses:

- RTMP ingest: `mediamtx:1935/trya-dcs`
- HLS: `mediamtx:8888/trya-dcs/index.m3u8`
- Control API: `mediamtx:9997`

The ARM64 DCS publisher uses x264 `ultrafast` with up to ten complex-filter threads
to keep sustained output at or above real time; TrYa Stream and Exp. Radio retain
their existing encoder preset.

The HLS playlist retains 36 segments. Browser playback initially starts 20 seconds
behind the live edge and can buffer up to 45 seconds; this trades latency for stable
playback during short network or encoding fluctuations. Playback remains fixed at
1.00x to preserve music quality and never jumps backwards after initialization. Only
a delay beyond 45 seconds is corrected forward to the 20-second target.

Caddy is the only public entry point:

```text
https://bot.macfreun.de/dcs-stream/trya-dcs/index.m3u8
```

Before Caddy proxies a manifest or segment to MediaMTX, it calls the bot's
small authorization endpoint. The bot never proxies the media body.

## Access lifecycle

1. The user opens `/trya-dcs/player`.
2. Discord OAuth identifies the user and confirms guild membership.
3. The player requests a short-lived stream token.
4. The token is stored hashed in SQLite and raw only in a Secure, HttpOnly,
   SameSite cookie scoped to `/dcs-stream/`.
5. Caddy checks each HLS request with the bot before proxying to MediaMTX.
6. The player renews the token before expiry without reloading playback. The old
   token remains valid only until its original expiry so in-flight HLS segment
   requests are not interrupted during cookie rotation.
7. Guild membership is rechecked periodically. Leaving the guild revokes all
   active DCS stream tokens for that Discord user.

## Server rollout

A normal server rebuild pulls the pinned MediaMTX image and the Caddy 2.10.2
compatibility image. Caddy 2.11.4 is not used because its `forward_auth` plus
`reverse_proxy` connection-reuse regression can route authorized HLS requests to
the authentication upstream instead of MediaMTX:

```bash
docker compose up -d --build
```

After deployment, open **TrYa DCS** in the Admin UI, verify the guild and chat
channel, then confirm that the Media Server and Discord OAuth status cards are
green before enabling the feature.

## Backup and restore

The existing **Settings** backup tools cover TrYa DCS without a separate export:

- a database backup contains DCS settings, songs, consent evidence, playlist
  snapshots and hashed stream-token records;
- a full data backup additionally contains the immutable originals, normalized
  work audio, subtitles and downloaded visual media below `radio/trya_dcs`;
- database restore is refused while TrYa DCS or another stream manager is
  running, preventing the publisher from continuing against replaced state.

The Admin UI restores SQLite database backups. A full data archive is intended
for disaster recovery and must be restored at the persistent-volume level while
the application is stopped. Raw stream tokens never need to be backed up: only
their hashes are stored and listeners can obtain fresh short-lived tokens after
signing in again.
