# Galaxy Expeditions for Corax

## Purpose

Galaxy Expeditions is a modern, browser-rendered music exploration game inspired by
the basic travel loop of GalaxyTraders. It is not a port and does not reuse an
existing GalaxyTraders codebase. Discord showcase songs become planets in a generated
star system. A Discord member listens to songs while a ship travels between those
planets, earns server-validated credits and buys mostly cosmetic ship upgrades.

The feature must remain operationally independent from TrYa DCS. No game graphics,
audio encoding or orbital simulation runs on the ARM server. The browser renders the
map and plays audio; the server only provides authenticated JSON APIs, persistence and
rate-limited Discord actions.

## Product principles

- Discord OAuth2 identifies every player.
- Current membership in the configured guild is required.
- Only explicitly enabled showcase channels are available.
- A player selects a channel, time range and expedition size.
- Songs are ordered newest first by default.
- Every song is represented by a deterministic planet.
- Ship travel is synchronized to the selected song.
- Credits are based on validated listening time, not merely a client-side `ended`
  event.
- Seeking changes the remaining flight velocity without granting credit for skipped
  audio.
- The maximum song reward is reserved for a complete listen without seeking. Every
  forward seek loses the full-listen bonus and applies a server-calculated credit
  deduction proportional to the skipped duration.
- Every song exposes its canonical Suno link in the player so the listener can open
  the song page and leave a comment without searching for it.
- A configured guild custom emoji marks qualifying showcase posts through the bot.
- The Discord-visible reaction is a bot reaction. Per-player listens remain visible in
  the game database because Discord does not allow OAuth `identify` sessions to react
  as users.
- All browser-provided progress and economy values are untrusted.
- DCS FFmpeg and MediaMTX receive no additional work.

## User journey

1. The user opens `/galaxy`.
2. Discord OAuth requests `identify` and verifies current guild membership through the
   bot cache/API.
3. The user selects an enabled showcase channel.
4. The user selects a time range and expedition size.
5. The server creates an expedition from matching `song_posts`, newest first.
6. The browser generates a deterministic star map from channel ID and message IDs.
7. The ship starts at a neutral station and the newest song planet is selected.
8. Pressing Play starts the audio and the flight.
9. The ship reaches the target exactly when the audio ends and then continuously
   orbits the moving planet. Reached planets, their rings and labels are desaturated
   for the remainder of the expedition so unexplored destinations remain obvious.
10. Per-user auto-navigation is enabled by default and leaves the ship in a short,
    visible arrival orbit before selecting the next older song. If disabled, the ship
    remains in orbit until the user selects another planet.
11. Songs verified as fully heard remain desaturated on later expeditions. The
    per-user skip option is enabled by default, so automatic navigation bypasses them.
12. Valid listening heartbeats accumulate eligible seconds and credits.
13. A qualifying completion queues the configured bot reaction for the original
    showcase message and records the listener's Discord display name in the shared
    song reaction thread under a separate **Galaxy Player** heading.
14. Credits can be spent on ship hulls, engine trails and scanner effects.

## Travel synchronization

Each journey stores client-side visual state:

```json
{
  "from": {"x": 0, "y": 0},
  "toPlanetId": "message:123",
  "pathProgress": 0.0,
  "audioPosition": 0.0,
  "audioDuration": 240.0
}
```

The route is a quadratic or cubic Bezier transfer curve. Normal playback derives
progress from the audio clock. On a seek, the ship remains at its current visual
position, a new curve is created from that position to the same target, and its new
duration equals the remaining audio duration. Seeking forward therefore increases
velocity; seeking backward decreases it. Planets use visually compressed Kepler-like
motion: inner planets advance faster while distant planets retain a practical minimum
speed. The route targets the planet's predicted position at the end of the song rather
than its current position and ends tangentially at an orbital entry point. Pausing also
pauses the active transfer clock, while seeking recalculates the intercept. The `ended`
event transitions the ship into a 20-second elliptical orbit which follows the planet
as it continues around the star.

The purchasable Raven hull uses the Klangtresor raven vector model: powered transit
has animated wingbeats, while the arrival orbit switches to a slower gliding motion
with a violet aura and feather particles. Reduced-motion mode parks the Raven on its
orbit and suppresses particles. Other hulls retain the standard spacecraft rendering.

The top-right map overlay shows the selected song's CDN artwork immediately. A cached Galaxy
metadata request uses the same Suno embed fields as the existing Suno Player and
replaces the artwork with the muted looping `video_cover_url` when one is available.

When a tab resumes after browser throttling, the route is recalculated from
`audio.currentTime`; the server is never asked to update animation frames.

## Star-system generation

The first implementation uses one responsive Canvas 2D surface. WebGL can be added
later, but is not required for attractive orbital maps and would increase complexity.

Planet visuals are deterministic. A stable hash of channel ID and message ID controls:

- orbital radius and eccentricity;
- orbit inclination;
- planet radius within bounded visual limits;
- surface palette and gradient;
- ring presence;
- moon count;
- atmospheric glow;
- initial orbital angle.

Song duration may influence planet size slightly. Reaction counts can influence rings
or moons. These mappings are cosmetic and must not imply factual song properties.

The map uses level of detail:

- no more than the configured expedition limit is loaded;
- labels are hidden while zoomed out;
- distant planets use simpler shading;
- particle density and FPS are reduced on mobile or when `prefers-reduced-motion` is
  active.

## Existing systems to reuse

The existing Suno Player already provides useful building blocks:

- monitored/showcase channel discovery;
- `song_posts` with channel ID, message ID, author and URL;
- channel and time filtering;
- Discord OAuth credentials;
- Guild membership lookup;
- public-player Discord identity;
- Suno UUID resolution and metadata;
- player reaction persistence;
- Media Session patterns.

Shared logic should be moved into small service helpers instead of duplicating the
large existing templates.

## Audio-source boundary

The current Suno Player loads UUID-based audio directly from Suno CDN with MP3 and M4A
fallback. This keeps bandwidth away from Corax but depends on an external,
undocumented path, CORS behavior and applicable terms. Galaxy audio access must be
wrapped behind a source descriptor so an allowed alternative can replace it later:

```json
{
  "kind": "suno",
  "primary": "https://cdn1.suno.ai/<uuid>.mp3",
  "fallback": "https://cdn1.suno.ai/<uuid>.m4a"
}
```

Corax must not become an unbounded public audio proxy.

## Authentication and sessions

Galaxy uses a dedicated session namespace and callback:

```text
/galaxy/oauth/start
/galaxy/oauth/callback
/galaxy/logout
```

OAuth scope:

```text
identify
```

The bot verifies membership in `GUILD_ID`. The server stores only the Discord user ID,
display name and avatar needed for the game. Access-changing requests require CSRF,
Origin validation and per-user rate limits.

## Server-authoritative listening

A browser cannot award itself credits. The flow is:

1. `POST /galaxy/api/expeditions` creates a server session with an opaque random ID.
2. `POST /galaxy/api/listens/start` selects one expedition song.
3. The client sends a heartbeat every configured 10-15 seconds.
4. The server compares wall-clock delta, reported audio delta, pause state and seek
   markers.
5. Eligible seconds increase by no more than plausible elapsed wall time.
6. A completion transaction grants credits once and queues a reaction once.

Seeking does not grant eligible seconds for skipped ranges. A configurable threshold
combines minimum seconds and minimum percentage. Suggested defaults:

```text
heartbeat interval: 12 seconds
minimum seconds: 30
minimum percentage: 70%
credits per eligible minute: 2
complete listen bonus: 25%
forward-seek penalty: 100% of the skipped share, at least 1 credit
daily credit cap: 200
repeat reward cooldown: 7 days
```

A determined user can automate a browser, so this is abuse resistance rather than DRM.
Daily caps, idempotent transactions and audit ledgers bound the impact.

## Discord reactions

Admin configuration selects an actual custom emoji from the current guild and stores:

```json
{
  "id": "123456789012345678",
  "name": "blue_panther",
  "animated": false
}
```

After a qualifying listen, a worker resolves the source channel and message and calls:

```python
await message.add_reaction(custom_emoji)
```

Jobs are idempotent and retry with bounded backoff. Missing messages, permissions or
emojis become visible Admin errors. The bot reaction can exist only once per message;
individual player visits are counted separately in `galaxy_listens`.

## Economy and shop

The first shop is predominantly cosmetic:

- starter scout hull;
- alternative hull silhouettes;
- hull palettes;
- engine trail colors;
- scanner pulse effects;
- orbit-guide themes;
- profile badges.

Every credit mutation is represented in an immutable ledger. Purchases run in one
SQLite transaction and cannot produce negative balances. Initial versions should avoid
compounding credit multipliers.

## Database model

### `galaxy_users`

```text
discord_user_id PK
discord_name
avatar_url
credits
lifetime_credits
selected_hull
selected_trail
selected_scanner
auto_navigation
expedition_days
skip_completed
shop_collapsed
volume_percent
created_at
updated_at
last_seen_at
```

### `galaxy_upgrades`

```text
id PK
category
name
description
price
config_json
enabled
sort_order
created_at
updated_at
```

### `galaxy_user_upgrades`

```text
discord_user_id
upgrade_id
purchased_at
PRIMARY KEY(discord_user_id, upgrade_id)
```

### `galaxy_expeditions`

```text
id PK
token_hash UNIQUE
discord_user_id
channel_id
time_range_days
song_limit
songs_json
created_at
expires_at
completed_at
```

### `galaxy_listens`

```text
id PK
expedition_id
discord_user_id
message_id
channel_id
suno_uuid
duration_seconds
eligible_seconds
max_audio_position
seeked_seconds
started_at
last_heartbeat_at
completed_at
fully_listened
credits_awarded
reaction_status
UNIQUE(expedition_id, message_id)
```

### `galaxy_credit_ledger`

```text
id PK
discord_user_id
amount
reason
reference_type
reference_id
created_at
```

### `galaxy_reaction_jobs`

```text
id PK
message_id
channel_id
emoji_id
status
attempts
last_error
created_at
updated_at
UNIQUE(message_id, emoji_id)
```

### `galaxy_player_song_reactions`

```text
message_id
channel_id
discord_user_id
discord_display_name
emoji_id
reacted_at
PRIMARY KEY(message_id, discord_user_id, emoji_id)
```

Settings continue to use the existing generic `settings` table with a `galaxy_` prefix.

## API surface

```text
GET  /galaxy
GET  /galaxy/oauth/start
GET  /galaxy/oauth/callback
POST /galaxy/logout
GET  /galaxy/api/config
GET  /galaxy/api/channels
POST /galaxy/api/expeditions
GET  /galaxy/api/expeditions/<token>
POST /galaxy/api/listens/start
POST /galaxy/api/listens/heartbeat
POST /galaxy/api/listens/complete
GET  /galaxy/api/profile
GET  /galaxy/api/media/<uuid>
GET  /galaxy/api/shop
POST /galaxy/api/shop/buy
POST /galaxy/api/loadout
POST /galaxy/api/preferences
```

JSON responses expose only validated, bounded fields. Expedition tokens are random and
stored hashed. Song queries enforce configured channels and maximum ranges server-side.

## Admin UI

A dedicated `galaxy_game` permission and sidebar entry control access.

Configuration groups:

### General

- enabled;
- allowed showcase channels;
- default channel;
- maximum date range;
- default/max expedition size;
- auto-navigation default;
- generation seed version.

### Listening and credits

- heartbeat interval;
- minimum listened seconds;
- minimum listened percentage;
- credits per minute;
- full-listen bonus percentage;
- forward-seek penalty percentage;
- daily cap;
- repeat cooldown;
- reward repeats on/off.

### Discord reaction

- reaction enabled;
- selected custom guild emoji;
- reaction threshold;
- retry limit;
- current queue/error summary.

### Visual performance

- desktop planet limit;
- mobile planet limit;
- target FPS;
- default particle density;
- reduced-motion behavior.

### Shop

- upgrade enable/disable;
- editable names, descriptions and prices;
- category-safe effect presets and colors (`ship`/`raven`/Klangtresor `cube`,
  `engine`/`sparkle`/Klangtresor `nebula`/`warp`, `pulse`/`rune`);
- creation of additional hull, trail and scanner shop items from those safe presets;
  creation never equips or grants an item automatically;
- sort order;
- user balance adjustment with audit entry;
- economy reset only through explicit destructive confirmation.

## ARM and DCS isolation

The server has ten CPU cores, 15.5 GiB RAM and substantial free disk space. Galaxy
should remain lightweight because rendering is client-side. To protect DCS:

- never run a server-side animation loop;
- never invoke FFmpeg for Galaxy;
- never proxy normal song audio through Python;
- cache metadata and avoid per-user scraping;
- cap expedition size;
- heartbeat no faster than configured minimum;
- aggregate/checkpoint writes instead of writing animation state;
- queue Discord API work with low concurrency;
- use short SQLite transactions and indexes;
- expose health counters for active sessions, heartbeat rate and reaction failures.

The game should remain responsive if DCS is live, but DCS always has priority. Optional
API load shedding can reject new expeditions when system load or DCS FFmpeg health
crosses configured thresholds.

## Security checklist

- OAuth state validation.
- Guild membership validation on login and periodically.
- Secure, HttpOnly, SameSite cookies.
- CSRF for state changes.
- Origin checking for JSON POSTs.
- Random hashed expedition tokens.
- Server-side channel allow-list.
- Numeric bounds for ranges and limits.
- No client-authoritative credits, prices or ownership.
- Idempotent completion, purchase and reaction transactions.
- Per-user and per-IP rate limits.
- Text rendered through `textContent`, never untrusted HTML.
- Custom emoji selected from bot-known guild emoji objects.
- Audit logs for settings, balance adjustments and reaction failures.

## Delivery phases

### Phase 1: Foundation

- database schema and default upgrades;
- dedicated permission and Admin page;
- OAuth and membership gate;
- shared showcase query service;
- profile/config APIs.

### Phase 2: Playable expedition

- Canvas star map;
- deterministic planets and orbital animation;
- channel/range selection;
- audio player;
- canonical Suno link on every selected song for quick commenting;
- clickable navigation;
- duration- and seek-synchronized ship path;
- responsive/mobile rendering.

### Phase 3: Economy

- server listen sessions and heartbeat validation;
- maximum reward plus bonus for verified full, seek-free listens;
- proportional mandatory credit deductions for every forward seek;
- credit ledger and daily cap;
- shop and loadout;
- reaction queue and custom emoji selection.

### Phase 4: Polish

- achievements and collections;
- richer planet shaders;
- accessibility and reduced motion;
- observability and load shedding;
- optional missions without real-time multiplayer.

## Acceptance criteria for the first production release

- A current guild member can sign in and create an expedition from an allowed channel.
- The newest matching song is selected first.
- A selected planet starts its song and the ship reaches it at song end.
- Every selected song provides a safe direct link to its canonical Suno page.
- Seeking recalculates velocity and never causes arrival after the audio ends.
- Clicking another planet safely reroutes and selects its song.
- Credits cannot be granted by a single forged completion request.
- A verified complete listen without seeking earns the maximum reward; every forward
  seek produces a lower reward and never earns credit for the skipped range.
- Purchases are transactional and balances cannot become negative.
- A completed qualifying listen creates at most one reaction job per song/emoji.
- DCS FFmpeg speed and MediaMTX behavior are unaffected by active players.
- Mobile and reduced-motion modes remain usable.
- No user-provided value is rendered as HTML or used as an unvalidated Discord target.
