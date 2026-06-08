"""Raven's Nest: Relic Hunt — Twitch chat mini-game.

Registers !raven / !nest / !items / !top / !rank / !daily / !ritual /
!relichelp commands on a TwitchBot instance.  All game state is stored
in the relic_* tables of the shared SQLite database.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone
from typing import Optional

from bot.live_log import log_event as _log


def _rlog(msg: str, level: str = "info") -> None:
    _log(msg, level, "[relic-hunt]")

# XP required to advance from `level` to `level + 1`
def _xp_for_next(level: int) -> int:
    return 100 + level * level * 10

RARITY_ORDER = ["common", "uncommon", "rare", "epic", "legendary", "mythic"]

DEFAULT_RANKS = [
    {"id": "nestling",            "name": "Nestling",            "required_level": 1,  "icon": "🐣"},
    {"id": "feather_finder",      "name": "Feather Finder",      "required_level": 2,  "icon": "🪶"},
    {"id": "raven_watcher",       "name": "Raven Watcher",       "required_level": 4,  "icon": "👁️"},
    {"id": "rune_collector",      "name": "Rune Collector",      "required_level": 6,  "icon": "ᚱ"},
    {"id": "blackroot_scout",     "name": "Blackroot Scout",     "required_level": 8,  "icon": "🌲"},
    {"id": "hollow_walker",       "name": "Hollow Walker",       "required_level": 10, "icon": "🌫️"},
    {"id": "storm_caller",        "name": "Storm Caller",        "required_level": 14, "icon": "⛈️"},
    {"id": "raven_seer",          "name": "Raven Seer",          "required_level": 18, "icon": "🔮"},
    {"id": "nest_guardian",       "name": "Nest Guardian",       "required_level": 24, "icon": "🛡️"},
    {"id": "raven_lord",          "name": "Raven Lord",          "required_level": 32, "icon": "👑"},
    {"id": "blackwing_ascendant", "name": "Blackwing Ascendant", "required_level": 45, "icon": "🌑"},
]

DEFAULT_ITEMS = [
    # ── Common ────────────────────────────────────────────────────────────
    {"id":"black_feather","name":"Black Feather","rarity":"common","drop_weight":160,"min_points":5,"max_points":14,"min_xp":2,"max_xp":6,"flavor_text":"A simple feather from the edge of the mist.","can_be_used_in_ritual":True,"ritual_energy":1,"icon":"🪶","category":"feather"},
    {"id":"ash_coin","name":"Ash Coin","rarity":"common","drop_weight":140,"min_points":6,"max_points":16,"min_xp":2,"max_xp":7,"flavor_text":"An old coin darkened by smoke and time.","can_be_used_in_ritual":True,"ritual_energy":1,"icon":"🪙","category":"currency"},
    {"id":"broken_twig","name":"Broken Twig","rarity":"common","drop_weight":130,"min_points":3,"max_points":10,"min_xp":1,"max_xp":5,"flavor_text":"It smells faintly of rain and pine.","can_be_used_in_ritual":True,"ritual_energy":1,"icon":"🌿","category":"wood"},
    {"id":"candle_wax","name":"Candle Wax","rarity":"common","drop_weight":120,"min_points":5,"max_points":13,"min_xp":2,"max_xp":6,"flavor_text":"Soft wax from a forgotten shrine.","can_be_used_in_ritual":True,"ritual_energy":1,"icon":"🕯️","category":"ritual"},
    {"id":"muddy_button","name":"Muddy Button","rarity":"common","drop_weight":110,"min_points":4,"max_points":12,"min_xp":1,"max_xp":5,"flavor_text":"Lost from a traveler's coat.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"🔘","category":"trinket"},
    {"id":"rain_pebble","name":"Rain Pebble","rarity":"common","drop_weight":125,"min_points":4,"max_points":11,"min_xp":1,"max_xp":5,"flavor_text":"A smooth stone washed clean by stormwater.","can_be_used_in_ritual":True,"ritual_energy":1,"icon":"🪨","category":"stone"},
    {"id":"rusty_nail","name":"Rusty Nail","rarity":"common","drop_weight":115,"min_points":4,"max_points":12,"min_xp":1,"max_xp":5,"flavor_text":"A nail from an old wooden gate.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"🧷","category":"metal"},
    {"id":"small_bone","name":"Small Bone","rarity":"common","drop_weight":95,"min_points":7,"max_points":18,"min_xp":2,"max_xp":8,"flavor_text":"Tiny, pale, and strangely clean.","can_be_used_in_ritual":True,"ritual_energy":2,"icon":"🦴","category":"bone"},
    {"id":"torn_parchment","name":"Torn Parchment","rarity":"common","drop_weight":90,"min_points":8,"max_points":20,"min_xp":3,"max_xp":9,"flavor_text":"The writing is too faded to read.","can_be_used_in_ritual":True,"ritual_energy":2,"icon":"📜","category":"lore"},
    {"id":"dull_bead","name":"Dull Bead","rarity":"common","drop_weight":100,"min_points":5,"max_points":15,"min_xp":2,"max_xp":6,"flavor_text":"A bead from an old necklace.","can_be_used_in_ritual":True,"ritual_energy":1,"icon":"⚫","category":"trinket"},
    # ── Uncommon ──────────────────────────────────────────────────────────
    {"id":"silver_feather","name":"Silver Feather","rarity":"uncommon","drop_weight":65,"min_points":18,"max_points":42,"min_xp":8,"max_xp":18,"flavor_text":"It shines softly under moonlight.","can_be_used_in_ritual":True,"ritual_energy":4,"icon":"🪶","category":"feather"},
    {"id":"rune_pebble","name":"Rune Pebble","rarity":"uncommon","drop_weight":60,"min_points":20,"max_points":45,"min_xp":8,"max_xp":20,"flavor_text":"A tiny rune has been scratched into the stone.","can_be_used_in_ritual":True,"ritual_energy":4,"icon":"ᚱ","category":"rune"},
    {"id":"old_bell_clapper","name":"Old Bell Clapper","rarity":"uncommon","drop_weight":48,"min_points":24,"max_points":55,"min_xp":10,"max_xp":24,"flavor_text":"It no longer rings, but it remembers sound.","can_be_used_in_ritual":True,"ritual_energy":5,"icon":"🔔","category":"bell"},
    {"id":"moon_thread","name":"Moon Thread","rarity":"uncommon","drop_weight":52,"min_points":22,"max_points":50,"min_xp":9,"max_xp":21,"flavor_text":"A silver thread from a forgotten cloak.","can_be_used_in_ritual":True,"ritual_energy":4,"icon":"🧵","category":"cloth"},
    {"id":"charred_harp_pin","name":"Charred Harp Pin","rarity":"uncommon","drop_weight":42,"min_points":26,"max_points":58,"min_xp":11,"max_xp":24,"flavor_text":"A tiny pin from a burned instrument.","can_be_used_in_ritual":True,"ritual_energy":5,"icon":"🎵","category":"music"},
    {"id":"blue_lantern_glass","name":"Blue Lantern Glass","rarity":"uncommon","drop_weight":44,"min_points":25,"max_points":60,"min_xp":10,"max_xp":25,"flavor_text":"A shard of glass glowing faintly blue.","can_be_used_in_ritual":True,"ritual_energy":5,"icon":"🏮","category":"light"},
    {"id":"ravenbone_charm","name":"Ravenbone Charm","rarity":"uncommon","drop_weight":38,"min_points":30,"max_points":68,"min_xp":12,"max_xp":28,"flavor_text":"A small charm tied with black thread.","can_be_used_in_ritual":True,"ritual_energy":6,"icon":"🦴","category":"bone"},
    {"id":"storm_salt","name":"Storm Salt","rarity":"uncommon","drop_weight":46,"min_points":23,"max_points":54,"min_xp":9,"max_xp":23,"flavor_text":"Salt crystals gathered after thunder rain.","can_be_used_in_ritual":True,"ritual_energy":4,"icon":"🧂","category":"ritual"},
    {"id":"wolf_tooth","name":"Wolf Tooth","rarity":"uncommon","drop_weight":35,"min_points":34,"max_points":74,"min_xp":14,"max_xp":30,"flavor_text":"Sharp, old, and colder than expected.","can_be_used_in_ritual":True,"ritual_energy":6,"icon":"🐺","category":"bone"},
    {"id":"iron_rune_ring","name":"Iron Rune Ring","rarity":"uncommon","drop_weight":32,"min_points":36,"max_points":80,"min_xp":15,"max_xp":32,"flavor_text":"A small iron ring engraved with broken symbols.","can_be_used_in_ritual":True,"ritual_energy":7,"icon":"💍","category":"rune"},
    # ── Rare ──────────────────────────────────────────────────────────────
    {"id":"moonlit_rune_shard","name":"Moonlit Rune Shard","rarity":"rare","drop_weight":20,"min_points":70,"max_points":135,"min_xp":25,"max_xp":55,"flavor_text":"A broken rune that glows when no one is watching.","can_be_used_in_ritual":True,"ritual_energy":12,"icon":"🌙","category":"rune","announce_globally":True},
    {"id":"bell_fragment","name":"Bell Fragment","rarity":"rare","drop_weight":18,"min_points":75,"max_points":150,"min_xp":28,"max_xp":60,"flavor_text":"A cracked piece of a bell from a hollow village.","can_be_used_in_ritual":True,"ritual_energy":14,"icon":"🔔","category":"bell","announce_globally":True},
    {"id":"blackroot_bark","name":"Blackroot Bark","rarity":"rare","drop_weight":22,"min_points":65,"max_points":125,"min_xp":24,"max_xp":52,"flavor_text":"Dark bark from a tree that should not still be alive.","can_be_used_in_ritual":True,"ritual_energy":12,"icon":"🌲","category":"wood","announce_globally":True},
    {"id":"mistglass_orb","name":"Mistglass Orb","rarity":"rare","drop_weight":16,"min_points":85,"max_points":170,"min_xp":30,"max_xp":66,"flavor_text":"A cloudy orb filled with moving fog.","can_be_used_in_ritual":True,"ritual_energy":15,"icon":"🔮","category":"magic","announce_globally":True},
    {"id":"grave_silver","name":"Grave Silver","rarity":"rare","drop_weight":17,"min_points":80,"max_points":160,"min_xp":30,"max_xp":62,"flavor_text":"Silver that has slept beneath old stones.","can_be_used_in_ritual":True,"ritual_energy":15,"icon":"🥈","category":"metal","announce_globally":True},
    {"id":"hollow_map","name":"Hollow Map","rarity":"rare","drop_weight":15,"min_points":90,"max_points":180,"min_xp":35,"max_xp":70,"flavor_text":"A map that changes whenever it rains.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"🗺️","category":"lore","announce_globally":True},
    {"id":"shadow_candle","name":"Shadow Candle","rarity":"rare","drop_weight":19,"min_points":72,"max_points":145,"min_xp":27,"max_xp":58,"flavor_text":"Its flame burns darker than the room around it.","can_be_used_in_ritual":True,"ritual_energy":13,"icon":"🕯️","category":"ritual","announce_globally":True},
    {"id":"raven_eye_gem","name":"Raven Eye Gem","rarity":"rare","drop_weight":13,"min_points":100,"max_points":190,"min_xp":38,"max_xp":75,"flavor_text":"A black gem with a silver point of light inside.","can_be_used_in_ritual":True,"ritual_energy":16,"icon":"💎","category":"gem","announce_globally":True},
    {"id":"forgotten_song_page","name":"Forgotten Song Page","rarity":"rare","drop_weight":14,"min_points":95,"max_points":185,"min_xp":36,"max_xp":72,"flavor_text":"A page of music from a song no one remembers.","can_be_used_in_ritual":True,"ritual_energy":14,"icon":"🎼","category":"music","announce_globally":True},
    {"id":"frosted_locket","name":"Frosted Locket","rarity":"rare","drop_weight":12,"min_points":110,"max_points":210,"min_xp":40,"max_xp":80,"flavor_text":"A locket sealed shut by unnatural frost.","can_be_used_in_ritual":True,"ritual_energy":18,"icon":"❄️","category":"trinket","announce_globally":True},
    # ── Epic ──────────────────────────────────────────────────────────────
    {"id":"bloodmoon_rune","name":"Bloodmoon Rune","rarity":"epic","drop_weight":6,"min_points":180,"max_points":340,"min_xp":70,"max_xp":130,"flavor_text":"A rune that pulses like a distant heartbeat.","can_be_used_in_ritual":True,"ritual_energy":35,"icon":"🌕","category":"rune","announce_globally":True},
    {"id":"hurdy_gurdy_crank","name":"Lost Hurdy-Gurdy Crank","rarity":"epic","drop_weight":5,"min_points":200,"max_points":380,"min_xp":80,"max_xp":145,"flavor_text":"A crank from an instrument that still hums at night.","can_be_used_in_ritual":True,"ritual_energy":40,"icon":"🎻","category":"music","announce_globally":True},
    {"id":"stormglass_heart","name":"Stormglass Heart","rarity":"epic","drop_weight":5,"min_points":210,"max_points":400,"min_xp":85,"max_xp":150,"flavor_text":"Lightning moves inside its cracked glass shell.","can_be_used_in_ritual":True,"ritual_energy":42,"icon":"⚡","category":"magic","announce_globally":True},
    {"id":"black_chapel_key","name":"Black Chapel Key","rarity":"epic","drop_weight":4,"min_points":230,"max_points":430,"min_xp":90,"max_xp":160,"flavor_text":"A heavy key to a chapel that appears only in fog.","can_be_used_in_ritual":True,"ritual_energy":45,"icon":"🗝️","category":"lore","announce_globally":True},
    {"id":"raven_king_signet","name":"Raven King Signet","rarity":"epic","drop_weight":4,"min_points":240,"max_points":450,"min_xp":95,"max_xp":170,"flavor_text":"A royal seal from a kingdom with no sun.","can_be_used_in_ritual":True,"ritual_energy":48,"icon":"💍","category":"relic","announce_globally":True},
    {"id":"echoing_bell_core","name":"Echoing Bell Core","rarity":"epic","drop_weight":3,"min_points":260,"max_points":500,"min_xp":100,"max_xp":185,"flavor_text":"It rings once whenever someone tells a lie.","can_be_used_in_ritual":True,"ritual_energy":55,"icon":"🔔","category":"bell","announce_globally":True},
    {"id":"midnight_antler","name":"Midnight Antler","rarity":"epic","drop_weight":5,"min_points":190,"max_points":360,"min_xp":75,"max_xp":140,"flavor_text":"An antler darker than the forest around it.","can_be_used_in_ritual":True,"ritual_energy":38,"icon":"🦌","category":"bone","announce_globally":True},
    {"id":"ashen_crown_fragment","name":"Ashen Crown Fragment","rarity":"epic","drop_weight":3,"min_points":280,"max_points":520,"min_xp":110,"max_xp":190,"flavor_text":"A broken piece of a crown burned in ritual fire.","can_be_used_in_ritual":True,"ritual_energy":58,"icon":"👑","category":"relic","announce_globally":True},
    # ── Legendary ─────────────────────────────────────────────────────────
    {"id":"crown_of_the_raven_lord","name":"Crown of the Raven Lord","rarity":"legendary","drop_weight":1,"min_points":700,"max_points":1000,"min_xp":220,"max_xp":350,"flavor_text":"A crown whispered about in every raven tale.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"👑","category":"legendary","announce_globally":True},
    {"id":"bell_of_blackroot_hollow","name":"Bell of Blackroot Hollow","rarity":"legendary","drop_weight":1,"min_points":666,"max_points":1111,"min_xp":250,"max_xp":400,"flavor_text":"When it rings, the hollow answers.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"🔔","category":"legendary","announce_globally":True},
    {"id":"tarjas_lost_ravenstone","name":"Tarja's Lost Ravenstone","rarity":"legendary","drop_weight":1,"min_points":800,"max_points":1200,"min_xp":260,"max_xp":420,"flavor_text":"A stone marked by song, shadow, and raven wings.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"🖤","category":"legendary","announce_globally":True},
    {"id":"black_sun_reliquary","name":"Black Sun Reliquary","rarity":"legendary","drop_weight":1,"min_points":850,"max_points":1300,"min_xp":280,"max_xp":450,"flavor_text":"A sealed relic that hums with forbidden warmth.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"🌑","category":"legendary","announce_globally":True},
    {"id":"last_song_of_the_hollow","name":"Last Song of the Hollow","rarity":"legendary","drop_weight":1,"min_points":900,"max_points":1400,"min_xp":300,"max_xp":500,"flavor_text":"A song sheet that sings itself when the room is empty.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"🎼","category":"legendary","announce_globally":True},
    # ── Mythic ────────────────────────────────────────────────────────────
    {"id":"heart_of_the_first_raven","name":"Heart of the First Raven","rarity":"mythic","drop_weight":0.2,"min_points":1500,"max_points":2500,"min_xp":500,"max_xp":900,"flavor_text":"The first wingbeat. The first shadow. The first omen.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"🔥","category":"mythic","announce_globally":True},
    {"id":"voidfeather_codex","name":"Voidfeather Codex","rarity":"mythic","drop_weight":0.2,"min_points":1600,"max_points":2800,"min_xp":550,"max_xp":950,"flavor_text":"A book written in ink that moves like living wings.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"📖","category":"mythic","announce_globally":True},
    {"id":"eclipse_bell","name":"Eclipse Bell","rarity":"mythic","drop_weight":0.15,"min_points":1800,"max_points":3000,"min_xp":600,"max_xp":1000,"flavor_text":"It rings only when the moon devours the sun.","can_be_used_in_ritual":False,"ritual_energy":0,"icon":"🌘","category":"mythic","announce_globally":True},
]

DEFAULT_EVENTS = [
    {"id":"blood_moon","name":"Blood Moon","enabled":True,"config_json":json.dumps({"durationMinutes":10,"rareDropMultiplier":2.0,"epicDropMultiplier":1.5,"pointsMultiplier":1.0,"xpMultiplier":1.0,"startMessage":"🌑 Blood Moon rises! Rare and epic relics are more likely for 10 minutes.","endMessage":"The Blood Moon fades into the mist."})},
    {"id":"raven_swarm","name":"Raven Swarm","enabled":True,"config_json":json.dumps({"durationMinutes":10,"rareDropMultiplier":1.0,"epicDropMultiplier":1.0,"pointsMultiplier":1.5,"xpMultiplier":1.25,"startMessage":"🪶 Raven Swarm! Hunts grant bonus points and XP for 10 minutes.","endMessage":"The raven swarm scatters."})},
    {"id":"blackroot_bell","name":"Blackroot Bell","enabled":True,"config_json":json.dumps({"durationMinutes":5,"rareDropMultiplier":1.25,"epicDropMultiplier":1.25,"pointsMultiplier":1.0,"xpMultiplier":1.0,"ritualEnergyMultiplier":2.0,"startMessage":"🔔 The Blackroot Bell rings! Ritual offerings count double.","endMessage":"The bell becomes silent again."})},
]


def _get_rank(level: int, ranks: Optional[list] = None) -> dict:
    r = ranks or DEFAULT_RANKS
    current = r[0]
    for rank in r:
        if level >= rank["required_level"]:
            current = rank
    return current


def _get_next_rank(level: int, ranks: Optional[list] = None) -> Optional[dict]:
    r = ranks or DEFAULT_RANKS
    for rank in r:
        if rank["required_level"] > level:
            return rank
    return None


def _fmt_cooldown(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if s else f"{m}m"


def _apply_event_multipliers(items: list, active_events: list) -> list:
    result = []
    for item in items:
        w = item["drop_weight"]
        for ev in active_events:
            cfg = ev.get("_cfg", {})
            r = item.get("rarity", "common")
            if r == "rare":
                w *= cfg.get("rareDropMultiplier", 1.0)
            elif r == "epic":
                w *= cfg.get("epicDropMultiplier", 1.0)
            elif r == "legendary":
                w *= cfg.get("legendaryDropMultiplier", 1.0)
            elif r == "mythic":
                w *= cfg.get("mythicDropMultiplier", 1.0)
        result.append({**item, "drop_weight": w})
    return result


def _weighted_choice(items: list) -> dict:
    total = sum(i["drop_weight"] for i in items)
    roll = random.random() * total
    for item in items:
        roll -= item["drop_weight"]
        if roll <= 0:
            return item
    return items[-1]


class RelicHunt:
    """Game engine.  Call `start(twitch_bot)` to register commands and begin
    listening; `stop()` to deregister and disconnect the IRC listener."""

    def __init__(self, db):
        self.db = db
        self._bot = None
        self._running = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #
    async def start(self, twitch_bot) -> None:
        self._bot = twitch_bot
        self._running = True
        await self.db.ensure_relic_tables()
        await self._seed_if_empty()

        prefix = (await self.db.relic_get_setting("command_prefix")) or "!"
        p = prefix.rstrip("!")

        for cmd, handler in [
            (f"{p}raven",      self._cmd_raven),
            (f"{p}nest",       self._cmd_nest),
            (f"{p}items",      self._cmd_items),
            (f"{p}top",        self._cmd_top),
            (f"{p}rank",       self._cmd_rank),
            (f"{p}daily",      self._cmd_daily),
            (f"{p}ritual",     self._cmd_ritual),
            (f"{p}relichelp",  self._cmd_help),
            (f"{p}relic",      self._cmd_admin),
        ]:
            twitch_bot.register_command(cmd, handler)

        asyncio.create_task(self._event_watcher_loop())
        await twitch_bot.start_listener()
        _rlog("Started — IRC listener active")

    async def stop(self) -> None:
        self._running = False
        if self._bot:
            await self._bot.stop_listener()
        _rlog("Stopped")

    # ------------------------------------------------------------------ #
    # Event watcher loop                                                   #
    # ------------------------------------------------------------------ #
    async def _event_watcher_loop(self) -> None:
        """Every 10 s: post end messages for expired events; fire auto-starts."""
        while self._running:
            try:
                await self._tick_expired_events()
                await self._tick_auto_event()
            except Exception as e:
                _rlog(f"Event watcher error: {e}", "error")
            await asyncio.sleep(10)

    async def _tick_expired_events(self) -> None:
        all_events = {e["id"]: e for e in await self.db.relic_get_all_events()}
        expired = await self.db.relic_expire_events()
        for ae in expired:
            ev = all_events.get(ae["event_id"])
            if not ev:
                continue
            try:
                cfg = json.loads(ev.get("config_json") or "{}")
            except Exception:
                cfg = {}
            end_msg = cfg.get("endMessage", f"The {ev['name']} event has ended.")
            await self._send(end_msg)
            _rlog(f"Event expired: {ev['name']}")

    async def _tick_auto_event(self) -> None:
        if (await self.db.relic_get_setting("auto_event_enabled")) != "true":
            return
        from bot.exp_stream_manager import stream_is_live
        if not stream_is_live:
            return
        if await self.db.relic_get_active_events():
            return
        now = time.time()
        next_at = float((await self.db.relic_get_setting("auto_event_next_at")) or 0)
        if next_at == 0:
            min_m = int((await self.db.relic_get_setting("auto_event_min_interval_minutes")) or 20)
            max_m = int((await self.db.relic_get_setting("auto_event_max_interval_minutes")) or 45)
            interval = random.uniform(min_m * 60, max_m * 60)
            await self.db.relic_set_setting("auto_event_next_at", str(now + interval))
            _rlog(f"Auto-event: first event scheduled in ~{int(interval // 60)}m")
            return
        if now < next_at:
            return
        all_events = [e for e in await self.db.relic_get_all_events() if e.get("enabled")]
        if not all_events:
            return
        event = random.choice(all_events)
        try:
            cfg = json.loads(event.get("config_json") or "{}")
        except Exception:
            cfg = {}
        duration_min = int(cfg.get("durationMinutes", 10))
        await self.db.relic_start_event(event["id"], duration_min * 60, "auto")
        start_msg = cfg.get("startMessage", f"{event['name']} has begun!")
        await self._send(start_msg)
        _rlog(f"Auto-started event: {event['name']} ({duration_min}min)")
        min_m = int((await self.db.relic_get_setting("auto_event_min_interval_minutes")) or 20)
        max_m = int((await self.db.relic_get_setting("auto_event_max_interval_minutes")) or 45)
        gap = random.uniform(min_m * 60, max_m * 60)
        await self.db.relic_set_setting("auto_event_next_at", str(now + duration_min * 60 + gap))
        _rlog(f"Auto-event: next scheduled in ~{int((duration_min * 60 + gap) // 60)}m")

    async def _send(self, msg: str) -> None:
        if self._bot:
            await self._bot.send(msg)

    # ------------------------------------------------------------------ #
    # Seeding                                                              #
    # ------------------------------------------------------------------ #
    async def _seed_if_empty(self) -> None:
        items = await self.db.relic_get_all_items()
        if not items:
            _rlog("Seeding default item library…")
            now = time.time()
            for item in DEFAULT_ITEMS:
                row = {
                    "id": item["id"],
                    "name": item["name"],
                    "rarity": item.get("rarity", "common"),
                    "enabled": 1,
                    "drop_weight": item.get("drop_weight", 1),
                    "min_points": item.get("min_points", 0),
                    "max_points": item.get("max_points", 0),
                    "min_xp": item.get("min_xp", 0),
                    "max_xp": item.get("max_xp", 0),
                    "flavor_text": item.get("flavor_text", ""),
                    "announce_globally": 1 if item.get("announce_globally") else 0,
                    "can_be_used_in_ritual": 1 if item.get("can_be_used_in_ritual") else 0,
                    "ritual_energy": item.get("ritual_energy", 0),
                    "icon": item.get("icon", ""),
                    "category": item.get("category", ""),
                    "seasonal_tag": None,
                    "required_event": None,
                }
                await self.db.relic_upsert_item(row)
            _rlog(f"Seeded {len(DEFAULT_ITEMS)} default items")

        events = await self.db.relic_get_all_events()
        if not events:
            for ev in DEFAULT_EVENTS:
                await self.db.relic_upsert_event(ev)
            _rlog(f"Seeded {len(DEFAULT_EVENTS)} default events")

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #
    async def _get_or_create_user(self, user_id: str, username: str) -> dict:
        user = await self.db.relic_get_user(user_id)
        if not user:
            now = time.time()
            user = {
                "twitch_user_id": user_id,
                "username": username,
                "points": 0, "xp": 0, "level": 1,
                "last_raven_at": None, "last_daily_at": None, "last_ritual_at": None,
                "commands_used": 0, "legendary_finds": 0, "mythic_finds": 0,
                "created_at": now, "updated_at": now,
            }
            await self.db.relic_upsert_user(user)
        else:
            if user.get("username") != username:
                user["username"] = username
                await self.db.relic_upsert_user(user)
        return user

    async def _apply_xp(self, user: dict, xp_gain: int) -> tuple[dict, int, Optional[dict]]:
        """Apply XP and level-ups. Returns (updated_user, level_ups, new_rank_or_None)."""
        old_rank = _get_rank(user["level"])
        user["xp"] += xp_gain
        level_ups = 0
        while user["xp"] >= _xp_for_next(user["level"]):
            user["xp"] -= _xp_for_next(user["level"])
            user["level"] += 1
            level_ups += 1
        new_rank = _get_rank(user["level"])
        rank_changed = new_rank["id"] != old_rank["id"]
        return user, level_ups, (new_rank if rank_changed else None)

    async def _is_game_enabled(self) -> bool:
        val = await self.db.relic_get_setting("enabled")
        return val != "false"  # default ON unless explicitly disabled

    async def _get_active_events_with_cfg(self) -> list:
        active = await self.db.relic_get_active_events()
        result = []
        for ae in active:
            ev = await self.db.relic_get_all_events()
            ev_map = {e["id"]: e for e in ev}
            if ae["event_id"] in ev_map:
                try:
                    cfg = json.loads(ev_map[ae["event_id"]]["config_json"])
                except Exception:
                    cfg = {}
                result.append({**ae, "_cfg": cfg, "name": ev_map[ae["event_id"]]["name"]})
        return result

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #
    async def _cmd_raven(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            await self._send("Raven's Nest: Relic Hunt is currently disabled.")
            return

        uid  = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)

        # Cooldown check
        cd_secs = int((await self.db.relic_get_setting("raven_cooldown_seconds")) or 300)
        is_mod  = ctx.get("is_mod") or ctx.get("is_broadcaster")
        bypass  = is_mod and (await self.db.relic_get_setting("mods_bypass_cooldowns")) == "true"
        if not bypass and user.get("last_raven_at"):
            elapsed = time.time() - user["last_raven_at"]
            # Apply subscriber / VIP multiplier
            mult = 1.0
            if ctx.get("is_sub"):
                mult = float((await self.db.relic_get_setting("subscriber_cooldown_multiplier")) or 1.0)
            elif ctx.get("is_vip"):
                mult = float((await self.db.relic_get_setting("vip_cooldown_multiplier")) or 1.0)
            effective_cd = cd_secs * mult
            if elapsed < effective_cd:
                remaining = effective_cd - elapsed
                await self._send(f"@{name} Your raven is still flying. Try again in {_fmt_cooldown(remaining)}.")
                return

        # Get active events
        active_events = await self._get_active_events_with_cfg()
        active_ids    = [ae["event_id"] for ae in active_events]

        # Pick item
        eligible = await self.db.relic_get_eligible_items(active_ids)
        if not eligible:
            await self._send(f"@{name} The raven found nothing in the mist…")
            return
        eligible = _apply_event_multipliers(eligible, active_events)
        item     = _weighted_choice(eligible)

        # Roll points / XP with event multipliers
        pts_mult = 1.0
        xp_mult  = 1.0
        for ae in active_events:
            pts_mult *= ae["_cfg"].get("pointsMultiplier", 1.0)
            xp_mult  *= ae["_cfg"].get("xpMultiplier", 1.0)
        pts = int(random.randint(item["min_points"], item["max_points"]) * pts_mult)
        xp  = int(random.randint(item["min_xp"],     item["max_xp"])     * xp_mult)

        # Update user
        user["points"]         += pts
        user["last_raven_at"]   = time.time()
        user["commands_used"]  += 1
        rarity = item.get("rarity", "common")
        if rarity == "legendary":
            user["legendary_finds"] += 1
        elif rarity == "mythic":
            user["mythic_finds"] += 1

        user, level_ups, new_rank = await self._apply_xp(user, xp)
        await self.db.relic_upsert_user(user)
        await self.db.relic_add_item_to_user(uid, item["id"])

        # Build message
        icon = item.get("icon", "") or ""
        iname = item["name"]
        if rarity == "mythic":
            msg = f"🔥 MYTHIC DISCOVERY! @{name} has found {icon} {iname}. The Raven's Nest will remember this. +{pts} points, +{xp} XP."
        elif rarity == "legendary":
            msg = f"🌑 LEGENDARY RELIC! @{name}'s raven returns carrying {icon} {iname}! +{pts} points, +{xp} XP."
        elif rarity == "epic":
            msg = f"🌘 EPIC RELIC! @{name}'s raven brings back {icon} {iname}. +{pts} points, +{xp} XP."
        elif rarity == "rare":
            msg = f"🪶 Rare find! @{name}'s raven returns with {icon} {iname}. +{pts} points, +{xp} XP."
        elif rarity == "uncommon":
            msg = f"🍃 @{name}'s raven finds {icon} {iname}. {item.get('flavor_text','')} +{pts} points, +{xp} XP."
        else:
            msg = f"@{name} sends a raven into the mist... It returns with {icon} {iname}. +{pts} points, +{xp} XP."

        await self._send(msg)
        _rlog(f"{name} found {iname} ({rarity}) | +{pts}pts +{xp}xp")

        # Level-up / rank announcement
        announce_lvl = (await self.db.relic_get_setting("announce_level_ups")) == "true"
        if level_ups and announce_lvl:
            rank_str = f" and became a {new_rank['icon']} {new_rank['name']}!" if new_rank else "!"
            await self._send(f"⬆️ @{name} reached level {user['level']}{rank_str}")

        # Log
        await self.db.relic_log_hunt({
            "twitch_user_id": uid, "username": name,
            "item_id": item["id"], "item_name": iname, "rarity": rarity,
            "points_awarded": pts, "xp_awarded": xp,
            "result_type": "found", "message": msg, "created_at": time.time(),
        })

    async def _cmd_nest(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        uid  = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)
        rank = _get_rank(user["level"])
        next_rank = _get_next_rank(user["level"])
        inv  = await self.db.relic_get_inventory(uid)
        total_items = sum(i["amount"] for i in inv)
        rarest = max(inv, key=lambda i: RARITY_ORDER.index(
            i.get("rarity", "common") if i.get("rarity") in RARITY_ORDER else "common"
        ), default=None)
        rarest_str = f" | Rarest: {rarest['icon'] or ''} {rarest['name']}" if rarest else ""
        next_xp = _xp_for_next(user["level"])
        next_str = f" | Next rank: {next_rank['name']} at level {next_rank['required_level']}" if next_rank else " | Max rank reached"
        await self._send(
            f"@{name}'s Nest | Rank: {rank['name']} | Level: {user['level']} | "
            f"XP: {user['xp']}/{next_xp} | Points: {user['points']} | "
            f"Items: {total_items}{rarest_str}{next_str}"
        )

    async def _cmd_items(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        uid  = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)
        inv  = await self.db.relic_get_inventory(uid)
        if not inv:
            await self._send(f"@{name} Your nest is empty. Use !raven to send your first raven.")
            return
        sorted_inv = sorted(inv, key=lambda i: (
            -RARITY_ORDER.index(i.get("rarity", "common") if i.get("rarity") in RARITY_ORDER else "common"),
            -i["amount"]
        ))[:6]
        parts = [f"{i['icon'] or ''} {i['name']} x{i['amount']}" for i in sorted_inv]
        await self._send(f"@{name}'s rarest relics: {', '.join(parts)}.")

    async def _cmd_top(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        lb_size = int((await self.db.relic_get_setting("leaderboard_size")) or 5)
        lb = await self.db.relic_get_leaderboard(lb_size)
        if not lb:
            await self._send("No Relic Hunters yet. Type !raven to begin!")
            return
        parts = [f"{i+1}. {u['username']} {u['points']} pts" for i, u in enumerate(lb)]
        await self._send(f"Top Relic Hunters: {' | '.join(parts)}")

    async def _cmd_rank(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        uid  = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)
        rank = _get_rank(user["level"])
        next_rank = _get_next_rank(user["level"])
        next_xp   = _xp_for_next(user["level"])
        next_str  = f" | Next rank: {next_rank['name']} at level {next_rank['required_level']}." if next_rank else " | Max rank reached."
        await self._send(
            f"@{name} Rank: {rank['name']} | Level: {user['level']} | "
            f"XP: {user['xp']}/{next_xp}{next_str}"
        )

    async def _cmd_daily(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        uid  = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)

        tz_name = (await self.db.relic_get_setting("timezone")) or "Europe/Berlin"
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc

        now_local = datetime.now(tz)
        today_str = now_local.strftime("%Y-%m-%d")
        if user.get("last_daily_at"):
            last_dt = datetime.fromtimestamp(user["last_daily_at"], tz=tz)
            if last_dt.strftime("%Y-%m-%d") == today_str:
                await self._send(f"@{name} You have already claimed your daily tribute. Come back tomorrow.")
                return

        # Daily reward: fixed points + XP + item drop
        pts = 75
        xp  = 40
        user["points"] += pts
        user["last_daily_at"] = time.time()
        user, level_ups, new_rank = await self._apply_xp(user, xp)

        # Give a random common/uncommon item
        all_items = await self.db.relic_get_all_items()
        gift_pool = [i for i in all_items if i.get("rarity") in ("common", "uncommon") and i.get("enabled")]
        gift_name = ""
        if gift_pool:
            gift = random.choice(gift_pool)
            await self.db.relic_add_item_to_user(uid, gift["id"])
            gift_name = f", and 1 {gift.get('icon','')} {gift['name']}"

        await self.db.relic_upsert_user(user)
        await self._send(f"@{name} claims the daily raven tribute: +{pts} points, +{xp} XP{gift_name}.")

    async def _cmd_ritual(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        uid  = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)

        # Cooldown
        cd_secs = int((await self.db.relic_get_setting("ritual_cooldown_seconds")) or 600)
        if user.get("last_ritual_at"):
            elapsed = time.time() - user["last_ritual_at"]
            if elapsed < cd_secs:
                remaining = cd_secs - elapsed
                await self._send(f"@{name} Your raven is resting from the ritual. Try again in {_fmt_cooldown(remaining)}.")
                return

        # Consume item
        consumed = await self.db.relic_consume_ritual_item(uid)
        if not consumed:
            await self._send(f"@{name} You have no ritual items. Use !raven to collect feathers, runes, wax, bones, and other offerings.")
            return

        user["last_ritual_at"] = time.time()
        await self.db.relic_upsert_user(user)

        # Check active event ritual multiplier
        active_events = await self._get_active_events_with_cfg()
        energy_mult = 1.0
        for ae in active_events:
            energy_mult *= ae["_cfg"].get("ritualEnergyMultiplier", 1.0)

        ritual = await self.db.relic_get_ritual()
        add_energy = int((consumed.get("ritual_energy") or 1) * energy_mult)
        new_energy = ritual["energy"] + add_energy
        goal       = ritual["goal"]

        item_icon = consumed.get("icon") or ""
        item_name = consumed.get("name") or consumed.get("item_id", "item")

        if new_energy >= goal:
            # RITUAL COMPLETE
            _rlog(f"Ritual COMPLETE by {name} (energy {new_energy}/{goal})!")
            await self.db.relic_update_ritual(0, goal)
            reward_pts = int((await self.db.relic_get_setting("ritual_reward_points")) or 100)
            reward_xp  = int((await self.db.relic_get_setting("ritual_reward_xp")) or 50)
            await self._send(f"🔥 The Raven Ritual is complete! All active hunters receive +{reward_pts} points and +{reward_xp} XP.")

            # Reward all active users (hunted in last 30 min)
            window = int((await self.db.relic_get_setting("ritual_active_window_minutes")) or 30) * 60
            all_users = await self.db.relic_get_all_users()
            cutoff = time.time() - window
            active_users = [u for u in all_users if (u.get("last_raven_at") or 0) >= cutoff]
            for u in active_users:
                u["points"] += reward_pts
                u, _, _ = await self._apply_xp(u, reward_xp)
                await self.db.relic_upsert_user(u)

            # Lucky legendary drop
            leg_chance = float((await self.db.relic_get_setting("ritual_legendary_chance")) or 0.05)
            if active_users and random.random() < leg_chance:
                lucky = random.choice(active_users)
                leg_items = [i for i in await self.db.relic_get_all_items()
                             if i.get("rarity") in ("epic", "legendary")]
                if leg_items:
                    prize = random.choice(leg_items)
                    await self.db.relic_add_item_to_user(lucky["twitch_user_id"], prize["id"])
                    await self._send(f"The ritual chooses @{lucky['username']} and grants them {prize.get('icon','')} {prize['name']}!")
        else:
            await self.db.relic_update_ritual(new_energy, goal)
            await self._send(f"@{name} adds {item_icon} {item_name} to the ritual circle. Ritual energy: {new_energy}/{goal}.")
            _rlog(f"Ritual +{add_energy} energy by {name} via {item_name} ({new_energy}/{goal})")

    async def _cmd_help(self, ctx: dict) -> None:
        await self._send("Raven's Nest commands: !raven, !nest, !items, !top, !rank, !daily, !ritual, !relichelp")

    async def _cmd_admin(self, ctx: dict) -> None:
        """Minimal admin commands for broadcaster/mods in chat."""
        if not (ctx.get("is_broadcaster") or ctx.get("is_mod")):
            return
        args = (ctx.get("args") or "").strip().split()
        sub  = args[0].lower() if args else ""

        if sub == "enable":
            await self.db.relic_set_setting("enabled", "true")
            await self._send("Raven's Nest: Relic Hunt enabled.")
        elif sub == "disable":
            await self.db.relic_set_setting("enabled", "false")
            await self._send("Raven's Nest: Relic Hunt disabled.")
        elif sub == "status":
            enabled = await self._is_game_enabled()
            ritual  = await self.db.relic_get_ritual()
            lb      = await self.db.relic_get_leaderboard(3)
            top_str = " | ".join(f"{u['username']} {u['points']}pts" for u in lb) or "no hunters yet"
            await self._send(
                f"Relic Hunt: {'enabled' if enabled else 'disabled'} | "
                f"Ritual: {ritual['energy']}/{ritual['goal']} | Top: {top_str}"
            )
        elif sub == "event" and len(args) >= 3 and args[1].lower() == "start":
            event_id = args[2]
            duration_min = int(args[3]) if len(args) > 3 else 10
            events = {e["id"]: e for e in await self.db.relic_get_all_events()}
            if event_id not in events:
                await self._send(f"Unknown event: {event_id}")
                return
            await self.db.relic_start_event(event_id, duration_min * 60, ctx["username"])
            try:
                cfg = json.loads(events[event_id]["config_json"])
                msg = cfg.get("startMessage", f"Event {event_id} started!")
            except Exception:
                msg = f"Event {event_id} started!"
            await self._send(msg)
        elif sub == "event" and len(args) >= 3 and args[1].lower() == "stop":
            event_id = args[2]
            await self.db.relic_stop_event(event_id)
            await self._send(f"Event {event_id} stopped.")
