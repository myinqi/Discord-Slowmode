# TrYa DCS - Discord Community Stream

TrYa DCS is a separate, self-hosted, non-commercial community stream. Discord
remains the identity, membership and chat platform. The bot controls radio
logic and FFmpeg, while Caddy and MediaMTX distribute media to listeners.

## Architectural rules

- TrYa DCS has separate settings, database records, media directories,
  permissions, commands and runtime state from TrYa Stream and Exp. Radio.
- The existing TrYa visual composition and proven upload pipeline may be
  reused as implementation building blocks.
- The bot never acts as the media CDN. It only authorizes short requests and
  controls FFmpeg/MediaMTX.
- The public MediaMTX ports stay inside the Docker network. Caddy is the only
  public media entry point.
- Discord OAuth2 and current membership in one configured guild are mandatory.
- Stream access uses short-lived, renewable tokens. Protecting the HTML page
  alone is insufficient.
- User-provided text is transported as data and rendered safely. It is never
  inserted as untrusted HTML.
- Uploaded originals remain outside the public web root. FFmpeg arguments are
  assembled from validated server-side values only.

## Milestone 1 - Project foundation

- [x] Add a dedicated `trya_dcs` Admin UI permission and sidebar entry.
- [x] Add independent TrYa DCS settings and persistent runtime defaults.
- [x] Add dedicated database tables for songs, upload evidence and playlist
      snapshots.
- [x] Add a dedicated persistent media directory.
- [x] Add Admin UI status cards for stream, MediaMTX, OAuth and chat.
- [x] Keep all visible UI and Discord output in English.

Acceptance:

- Admin permissions can grant TrYa DCS access without granting TrYa Stream.
- Existing TrYa Stream and Exp. Radio data and controls are unaffected.

## Milestone 2 - Submission and moderation

- [x] Add `/trya-dcs-submit`, `/trya-dcs-replace` and `/trya-dcs-delete`.
- [x] Reuse the proven two-step Discord-to-web upload flow.
- [x] Require the following confirmations before final submission:
  - sharing in the private, non-commercial community stream is permitted;
  - audio came from an official Suno download channel;
  - necessary rights for lyrics, samples, voices and supplied material exist;
  - technical copies and transcoding are permitted;
  - playback inside the closed TrYa DCS service is permitted.
- [x] Store Free/Paid and original/cover/remix status as evidence.
- [x] Do not reject solely because a song is Free, a cover or a remix.
- [x] Validate MIME type, file signature, extension and configurable size.
- [x] Decode the real audio duration instead of trusting container headers.
- [x] Preserve consent version, timestamp and hash.
- [x] Reuse Whisper, subtitle generation, metadata and optional LLM moderation.
      Uncertain, flagged and failed LLM reviews require manual approval.
- [x] Add all new commands to `/help`.

Acceptance:

- A guild member can submit, replace and delete only their own DCS songs.
- Rights/status evidence is exportable and remains auditable.
- Free/remix status is visible but does not automatically exclude the song.

## Milestone 3 - Media stack

- [x] Add MediaMTX as an internal Docker Compose service.
- [x] Add a persistent MediaMTX configuration.
- [x] Keep the existing RTMP ingest endpoint available.
- [x] Publish the bot-controlled FFmpeg output to a dedicated MediaMTX path.
- [x] Provide HLS playback through Caddy without routing video bytes through
      Quart/Python.
- [x] Add configurable output settings:
  - 1920x1080 default;
  - 30 FPS default;
  - H.264;
  - AAC;
  - 2-3 Mbit/s configurable video bitrate;
  - 160-192 kbit/s configurable audio bitrate.
- [x] Reuse playlist, intro/outro, overlay, title, disclaimer and media-frame
      logic where appropriate. Intro/outro pools, one-time startup intro,
      stop-mode/safe-stop outro and dedicated OBS overlay contribution are implemented.
- [x] Add health reporting for FFmpeg and MediaMTX.
- [x] Add safe stop and playlist-end behaviour.

Acceptance:

- The stream runs without Twitch and can be viewed through MediaMTX HLS.
- 100 listeners do not create 100 media responses from the bot process.
- Existing RTMP/OBS contribution remains possible.

## Milestone 4 - Discord OAuth and protected media

- [x] Add a dedicated Discord OAuth callback for TrYa DCS.
- [x] Request `identify` and `guilds` only where possible.
- [x] Verify membership server-side against the configured guild and bot cache/API.
- [x] Return HTTP 403 to authenticated non-members.
- [x] Add `POST /trya-dcs/api/stream-token`.
- [x] Issue cryptographically random, single-user tokens with a 10-minute default
      lifetime.
- [x] Store token hashes rather than raw tokens.
- [x] Protect every HLS manifest and segment request through Caddy authorization.
- [x] Renew tokens in the player before expiry.
- [x] Re-check guild membership periodically and revoke access after leaving.
- [x] Apply secure cookie, CSRF, origin and rate-limit protections.

Acceptance:

- Copying an HLS URL into an unauthenticated browser fails.
- A token expires and cannot be reused indefinitely.
- Removing a user from the guild ends access after the configured re-check window.

## Milestone 5 - Webplayer

- [x] Build a responsive desktop/mobile player matching the TrYa stream design.
- [x] Show live video, current song, artist, submitter, progress and queue status.
- [x] Add reconnect states and clear offline/error feedback.
- [x] Use native HLS where available and hls.js as the browser fallback.
- [x] Keep controls stable across mobile and desktop widths.
- [x] Add an explicit AI-generated audio/video disclosure.

Acceptance:

- The player works on current Firefox/Zen, Chromium and mobile Safari/Chrome.
- No stream URL remains usable without an authenticated, current guild member.

## Milestone 6 - Discord-backed live chat

- [x] Configure one Discord text channel as the DCS chat source.
- [x] Add an authenticated WebSocket endpoint.
- [x] Relay Discord messages to connected browsers immediately.
- [x] Render display name, avatar, role colour, text, attachments, images, GIFs,
      replies and Discord embeds safely.
- [x] Convert static and animated guild custom emojis to Discord CDN assets.
- [x] Add a searchable picker with Unicode and live guild emojis.
      Recently used emojis are kept locally in each browser.
- [x] Relay browser messages through a managed Discord webhook using the
      authenticated member's current display name and avatar.
- [x] Mark web-originated messages clearly and prevent arbitrary identity data
      from the client.
- [x] Add per-user and per-session chat rate limits.
- [x] Handle message edits and deletions.

Acceptance:

- Discord and web participants share one conversation, not two parallel chats.
- A browser user cannot impersonate another Discord member.
- Animated emojis and Discord-posted GIFs remain animated.

## Milestone 7 - Event system

- [x] Add a central server-side event broker.
- [x] Define versioned payloads for:
  - `chat.message`, `chat.edit`, `chat.delete`;
  - `radio.now_playing`, `radio.next_song`, `radio.progress`;
  - `radio.listener_count`, `radio.mode`;
  - `radio.obs_online`, `radio.obs_offline`;
  - `radio.queue_update`.
- [x] Add heartbeat, reconnect, missed-state refresh and passive membership
      revalidation behaviour.
- [x] Never trust event/user IDs supplied by browser clients.

## Milestone 8 - Optional second stage

- [ ] Synchronize Discord reactions in both directions.
- [ ] Add reaction events and guild custom reaction rendering.
- [ ] Add WebRTC/WHEP playback after HLS is stable.
- [ ] Add emoji favourites and recently-used history.
- [ ] Add listener-presence statistics with privacy-preserving retention.

These items are intentionally outside the first production-ready release.

## Verification and deployment

- [x] Add database lifecycle tests for DCS tables and replacement transactions.
- [ ] Add route-level membership and authorization tests. Token expiry and
      revocation are covered at the persistence boundary.
- [ ] Add route-level upload validation tests. Ownership deletion and atomic
      replacement are covered at the persistence boundary.
- [ ] Add WebSocket authentication and XSS regression tests.
- [x] Add event-broker lifecycle and slow-listener regression tests.
- [ ] Validate Caddy and MediaMTX configurations in CI/startup checks.
- [ ] Verify HLS playback and token renewal on desktop and mobile.
- [ ] Verify that TrYa Stream and Exp. Radio still start independently.
- [x] Document required Discord redirect URLs, environment variables, ports and
      the VServer deployment procedure.
- [x] Document backup/restore coverage for DCS settings, evidence and media,
      and block database restore while the DCS publisher is running.

## Progress log

- [x] 2026-08-20: Requirements captured and architecture split into milestones.
- [x] 2026-08-20: Selected MediaMTX for distribution and HLS as the first
      production playback protocol.
- [x] 2026-08-20: Foundation Admin UI, settings, permissions, storage boundary
      and database schema implemented.
- [x] 2026-08-20: Internal MediaMTX service, Caddy HLS authorization, Discord
      OAuth membership gate and renewable hashed stream tokens implemented.
- [x] 2026-08-20: Private two-step submission, replacement and owner deletion,
      immutable upload evidence, decoded duration validation, metadata, Whisper
      and subtitle processing implemented.
- [x] 2026-08-20: Dedicated DCS FFmpeg manager, MediaMTX publishing, stream
      controls, FFmpeg health, playlist snapshots and end behaviour implemented.
- [x] 2026-08-20: Manual song approval, authenticated Discord-backed chat,
      managed webhooks, guild emojis and the central WebSocket event broker added.
- [x] 2026-08-20: Added DCS intro/outro pools and dedicated OBS RTMP overlay contribution.
- [ ] Broaden authorization/database route tests.
