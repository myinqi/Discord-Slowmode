"""Raven's Nest: Relic Hunt — Twitch chat mini-game.

Registers !raven / !nest / !items / !top / !rank / !daily / !ritual /
!relichelp commands on a TwitchBot instance.  All game state is stored
in the relic_* tables of the shared SQLite database.
"""

from __future__ import annotations

import asyncio
import contextvars
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

VILLAGE_AREAS = {
    "culture": {
        "name": "Culture",
        "resource": "points",
        "command": "entertain",
        "verb": "entertains the village square",
    },
    "education": {
        "name": "Education",
        "resource": "xp",
        "command": "teach",
        "verb": "teaches under the old raven tree",
    },
    "trade": {
        "name": "Trade",
        "resource": "items",
        "command": "trade",
        "verb": "opens a trade route through the mist",
    },
    "treasury": {
        "name": "Treasury",
        "resource": "shinies",
        "command": "invest",
        "verb": "invests in the glittering treasury",
    },
}

DEFAULT_RANKS = [
    {"id": "nestling",            "name": "Nestling",            "min_points": 0,     "icon": "🐣"},
    {"id": "feather_finder",      "name": "Feather Finder",      "min_points": 250,   "icon": "🪶"},
    {"id": "raven_watcher",       "name": "Raven Watcher",       "min_points": 1200,  "icon": "👁️"},
    {"id": "rune_collector",      "name": "Rune Collector",      "min_points": 2500,  "icon": "ᚱ"},
    {"id": "blackroot_scout",     "name": "Blackroot Scout",     "min_points": 6000,  "icon": "🌲"},
    {"id": "hollow_walker",       "name": "Hollow Walker",       "min_points": 10000, "icon": "🌫️"},
    {"id": "storm_caller",        "name": "Storm Caller",        "min_points": 15000, "icon": "⛈️"},
    {"id": "raven_seer",          "name": "Raven Seer",          "min_points": 22000, "icon": "🔮"},
    {"id": "nest_guardian",       "name": "Nest Guardian",       "min_points": 32000, "icon": "🛡️"},
    {"id": "raven_lord",          "name": "Raven Lord",          "min_points": 45000, "icon": "👑"},
    {"id": "blackwing_ascendant", "name": "Blackwing Ascendant", "min_points": 65000, "icon": "🌑"},
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

DEFAULT_COMBINE_RECIPES = [
    {"id":"raven_nest_twig","ingredient_a_id":"broken_twig","ingredient_b_id":"black_thread_knot","result_item_id":"raven_nest_twig","bonus_points":25,"priority":10},
    {"id":"black_feather_quill","ingredient_a_id":"black_feather","ingredient_b_id":"wax_sealed_note","result_item_id":"black_feather_quill","bonus_points":75,"priority":20},
    {"id":"blue_candle_tear","ingredient_a_id":"candle_wax","ingredient_b_id":"blue_lantern_glass","result_item_id":"blue_candle_tear","bonus_points":25,"priority":30},
    {"id":"ravenbone_charm","ingredient_a_id":"small_bone","ingredient_b_id":"little_bone_whistle","result_item_id":"ravenbone_charm","bonus_points":25,"priority":40},
    {"id":"iron_rune_ring","ingredient_a_id":"rune_pebble","ingredient_b_id":"cold_iron_button","result_item_id":"iron_rune_ring","bonus_points":25,"priority":50},
    {"id":"blackroot_tea_leaf","ingredient_a_id":"pine_needle_bundle","ingredient_b_id":"raven_nest_twig","result_item_id":"blackroot_tea_leaf","bonus_points":25,"priority":60},
    {"id":"forgotten_song_page","ingredient_a_id":"soggy_hymn_scrap","ingredient_b_id":"charred_harp_pin","result_item_id":"forgotten_song_page","bonus_points":75,"priority":70},
    {"id":"bell_fragment","ingredient_a_id":"old_bell_clapper","ingredient_b_id":"tiny_grave_bell","result_item_id":"bell_fragment","bonus_points":75,"priority":80},
    {"id":"candlelit_jaw_harp_reed","ingredient_a_id":"charred_harp_pin","ingredient_b_id":"fiddle_string_loop","result_item_id":"candlelit_jaw_harp_reed","bonus_points":75,"priority":90},
    {"id":"moth_eaten_prayer_shawl","ingredient_a_id":"moon_thread","ingredient_b_id":"silvered_bone_needle","result_item_id":"moth_eaten_prayer_shawl","bonus_points":75,"priority":100},
    {"id":"shadow_candle","ingredient_a_id":"blue_candle_tear","ingredient_b_id":"moonwater_flask","result_item_id":"shadow_candle","bonus_points":75,"priority":110},
    {"id":"gilded_cheese_grater","ingredient_a_id":"cheese_grater","ingredient_b_id":"purple_wax_seal","result_item_id":"gilded_cheese_grater","bonus_points":75,"priority":120},
    {"id":"witchlight_pickle_jar","ingredient_a_id":"pickled_moon_onion","ingredient_b_id":"lube_slick_pickle","result_item_id":"witchlight_pickle_jar","bonus_points":75,"priority":130},
    {"id":"gold_trimmed_bra","ingredient_a_id":"meredins_worn_underwear","ingredient_b_id":"moon_thread","result_item_id":"gold_trimmed_bra","bonus_points":75,"priority":140},
    {"id":"echoing_bell_core","ingredient_a_id":"old_bell_clapper","ingredient_b_id":"bell_fragment","result_item_id":"echoing_bell_core","bonus_points":200,"priority":150},
    {"id":"rune_carved_cheese_grater","ingredient_a_id":"gilded_cheese_grater","ingredient_b_id":"half_melted_rune_wax","result_item_id":"rune_carved_cheese_grater","bonus_points":200,"priority":160},
    {"id":"bloodmoon_rune","ingredient_a_id":"moonlit_rune_shard","ingredient_b_id":"blood_sealed_envelope","result_item_id":"bloodmoon_rune","bonus_points":200,"priority":170},
    {"id":"stormglass_heart","ingredient_a_id":"mistglass_orb","ingredient_b_id":"storm_salt","result_item_id":"stormglass_heart","bonus_points":200,"priority":180},
    {"id":"bloodroot_incense_burner","ingredient_a_id":"blackroot_bark","ingredient_b_id":"midnight_antler","result_item_id":"bloodroot_incense_burner","bonus_points":200,"priority":190},
    {"id":"choir_bell_of_the_deep_nest","ingredient_a_id":"bell_fragment","ingredient_b_id":"raven_choir_tuning_fork","result_item_id":"choir_bell_of_the_deep_nest","bonus_points":200,"priority":200},
    {"id":"golden_raven_chalice","ingredient_a_id":"obsidian_offering_bowl","ingredient_b_id":"black_salt_rosary","result_item_id":"golden_raven_chalice","bonus_points":200,"priority":210},
    {"id":"bra_of_gilded_protection","ingredient_a_id":"gold_trimmed_bra","ingredient_b_id":"raven_king_signet","result_item_id":"bra_of_gilded_protection","bonus_points":200,"priority":220},
    {"id":"pickle_of_the_velvet_eclipse","ingredient_a_id":"witchlight_pickle_jar","ingredient_b_id":"stormglass_pickle_fork","result_item_id":"pickle_of_the_velvet_eclipse","bonus_points":200,"priority":230},
    {"id":"black_sun_reliquary","ingredient_a_id":"hollow_map","ingredient_b_id":"black_chapel_key","result_item_id":"black_sun_reliquary","bonus_points":500,"priority":240},
    {"id":"last_song_of_the_hollow","ingredient_a_id":"forgotten_song_page","ingredient_b_id":"echoing_bell_core","result_item_id":"last_song_of_the_hollow","bonus_points":500,"priority":250},
    {"id":"crown_of_the_raven_lord","ingredient_a_id":"raven_king_signet","ingredient_b_id":"ashen_crown_fragment","result_item_id":"crown_of_the_raven_lord","bonus_points":500,"priority":260},
    {"id":"the_golden_bra_of_blackroot","ingredient_a_id":"bra_of_gilded_protection","ingredient_b_id":"bloodroot_incense_burner","result_item_id":"the_golden_bra_of_blackroot","bonus_points":500,"priority":270},
    {"id":"pickle_of_infinite_slipperiness","ingredient_a_id":"pickle_of_the_velvet_eclipse","ingredient_b_id":"mistglass_orb","result_item_id":"pickle_of_infinite_slipperiness","bonus_points":500,"priority":280},
    {"id":"voidfeather_codex","ingredient_a_id":"black_feather_quill","ingredient_b_id":"bloodmoon_rune","result_item_id":"voidfeather_codex","bonus_points":1000,"priority":290},
    {"id":"the_unblinking_raven_idol","ingredient_a_id":"silver_raven_mask","ingredient_b_id":"raven_eye_gem","result_item_id":"the_unblinking_raven_idol","bonus_points":1000,"priority":300},
]
DEFAULT_COMBINE_RECIPES_VERSION = 2


def _rank_min_points(rank: dict) -> int:
    return int(rank.get("min_points", rank.get("required_level", 0)) or 0)


def _get_rank(points: int, ranks: Optional[list] = None) -> dict:
    r = sorted(ranks or DEFAULT_RANKS, key=_rank_min_points)
    current = r[0]
    for rank in r:
        if points >= _rank_min_points(rank):
            current = rank
    return current


def _get_next_rank(points: int, ranks: Optional[list] = None) -> Optional[dict]:
    r = sorted(ranks or DEFAULT_RANKS, key=_rank_min_points)
    for rank in r:
        if _rank_min_points(rank) > points:
            return rank
    return None


def _fmt_cooldown(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if s else f"{m}m"


def _phrase_progress(phrase: str, revealed_mask: str) -> str:
    if not phrase:
        return ""
    if len(revealed_mask) != len(phrase):
        revealed_mask = "".join("0" if char.isalpha() else "1" for char in phrase)
    parts = []
    for index, char in enumerate(phrase):
        if char.isspace():
            parts.append("/")
        elif char.isalpha():
            parts.append(char.upper() if revealed_mask[index] == "1" else "_")
        else:
            parts.append(char)
    return " ".join(parts)


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

    def __init__(self, db, stream_kind: str = "exp"):
        self.db = db
        self.stream_kind = stream_kind if stream_kind in {"exp", "trya", "dcs"} else "exp"
        self._bot = None
        self._running = False
        self._watcher_task = None
        self._prepare_lock = asyncio.Lock()
        self._prepared = False
        self._response_sender = contextvars.ContextVar(
            f"relic_response_sender_{id(self)}", default=None
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #
    def _command_handlers(self) -> dict:
        return {
            "raven": self._cmd_raven,
            "nest": self._cmd_nest,
            "items": self._cmd_items,
            "top": self._cmd_top,
            "rank": self._cmd_rank,
            "daily": self._cmd_daily,
            "ritual": self._cmd_ritual,
            "combine": self._cmd_combine,
            "village": self._cmd_village,
            "entertain": self._cmd_village_donate,
            "teach": self._cmd_village_donate,
            "trade": self._cmd_village_donate,
            "invest": self._cmd_village_donate,
            "nextvillage": self._cmd_next_village,
            "phrase": self._cmd_phrase,
            "solve": self._cmd_solve,
            "relichelp": self._cmd_help,
            "relic": self._cmd_admin,
        }

    async def prepare(self) -> None:
        async with self._prepare_lock:
            if self._prepared:
                return
            await self.db.ensure_relic_tables()
            await self._seed_if_empty()
            self._prepared = True

    async def dispatch_message(
        self,
        text: str,
        context: dict,
        sender,
        custom_sender=None,
    ) -> bool:
        await self.prepare()
        clean = str(text or "").strip()
        prefix = (await self.db.relic_get_setting("command_prefix")) or "!"
        if not clean.startswith(prefix):
            return False
        parts = clean[len(prefix):].split(None, 1)
        command = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        handler = self._command_handlers().get(command)
        context = {
            **context,
            "text": clean,
            "args": args,
        }
        token = self._response_sender.set(sender)
        try:
            if handler:
                if command == "relic":
                    await handler(context)
                else:
                    await self._subscriber_guard(handler)(context)
                return True
            custom = await self.db.relic_get_custom_command(command)
            if custom and custom.get("enabled") and custom.get("response"):
                if custom_sender:
                    await custom_sender(custom["response"])
                else:
                    await self._send(custom["response"])
                return True
            return False
        finally:
            self._response_sender.reset(token)

    async def start(self, twitch_bot) -> None:
        self._bot = twitch_bot
        self._running = True
        await self.prepare()

        prefix = (await self.db.relic_get_setting("command_prefix")) or "!"
        p = prefix.rstrip("!")

        for command, handler in self._command_handlers().items():
            cmd = f"{p}{command}"
            if command == "relic":
                twitch_bot.register_command(cmd, handler)
            else:
                twitch_bot.register_command(cmd, self._subscriber_guard(handler))

        if not self._watcher_task or self._watcher_task.done():
            self._watcher_task = asyncio.create_task(self._event_watcher_loop())
        await twitch_bot.start_listener()
        _rlog(f"Started — {self.stream_kind} IRC listener active")

    async def stop(self) -> None:
        self._running = False
        if self._watcher_task and not self._watcher_task.done():
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except asyncio.CancelledError:
                pass
        self._watcher_task = None
        if self._bot:
            await self._bot.stop_listener()
        self._bot = None
        _rlog(f"Stopped — {self.stream_kind} IRC listener")

    # ------------------------------------------------------------------ #
    # Event watcher loop                                                   #
    # ------------------------------------------------------------------ #
    def _stream_is_live(self) -> bool:
        if self.stream_kind == "trya":
            from bot.trya_stream_manager import stream_is_live
        else:
            from bot.exp_stream_manager import stream_is_live
        return bool(stream_is_live)

    def _other_stream_is_live(self) -> bool:
        if self.stream_kind == "trya":
            from bot.exp_stream_manager import stream_is_live
        else:
            from bot.trya_stream_manager import stream_is_live
        return bool(stream_is_live)

    async def _event_watcher_loop(self) -> None:
        """Every 10 s: post end messages for expired events; fire auto-starts."""
        while self._running:
            try:
                owns_events = (
                    self._stream_is_live()
                    or (self.stream_kind == "exp" and not self._other_stream_is_live())
                )
                if owns_events and not self._other_stream_is_live():
                    await self._tick_expired_events()
                    await self._tick_auto_event()
                    await self._tick_village_payout()
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
        if not self._stream_is_live():
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

    async def _tick_village_payout(self) -> None:
        if (await self.db.relic_get_setting("village_payout_enabled")) == "false":
            return
        if not self._stream_is_live():
            return
        now = time.time()
        interval_min = max(1, int((await self.db.relic_get_setting("village_payout_interval_minutes")) or 15))
        next_at = float((await self.db.relic_get_setting("village_next_payout_at")) or 0)
        if next_at == 0:
            await self.db.relic_set_setting("village_next_payout_at", str(now + interval_min * 60))
            _rlog(f"Hrafnathorp: first village payout scheduled in ~{interval_min}m")
            return
        if now < next_at:
            return

        await self.db.relic_set_setting("village_next_payout_at", str(now + interval_min * 60))
        areas = await self.db.relic_get_village_areas()
        window_min = max(1, int((await self.db.relic_get_setting("village_active_window_minutes")) or 30))
        active_users = await self.db.relic_get_active_users_since(now - window_min * 60)
        if not active_users:
            return
        village_count = max(1, int((await self.db.relic_get_setting("village_count")) or 1))
        completed_count = max(0, village_count - 1)

        payout_sources = []
        for index in range(completed_count):
            area = dict(random.choice(areas))
            area["level"] = 5
            payout_sources.append((f"Village {index + 1} {area['name']} L5", area))

        current_areas = [a for a in areas if int(a.get("level") or 0) > 0]
        if current_areas:
            area = dict(random.choice(current_areas))
            payout_sources.append((f"Current {area['name']} L{area['level']}", area))
        if not payout_sources:
            return

        recipients = list(active_users)
        random.shuffle(recipients)
        rewards = []
        for index, (source, area) in enumerate(payout_sources):
            reward = await self._grant_village_reward(
                recipients[index % len(recipients)],
                area,
            )
            reward["source"] = source
            rewards.append(reward)

        message = self._format_village_payout_message(
            rewards,
            completed_count=completed_count,
            includes_current=bool(current_areas),
        )
        await self._send(message)
        detail = " | ".join(f"{r['source']}: {r['text']}" for r in rewards)
        _rlog(f"Hrafnathorp payout: {detail}")

    async def _send(self, msg: str) -> None:
        sender = self._response_sender.get()
        if sender:
            await sender(msg)
        elif self._bot:
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

        ranks = await self.db.relic_get_all_ranks()
        if not ranks:
            for rank in DEFAULT_RANKS:
                await self.db.relic_upsert_rank(rank)
            _rlog(f"Seeded {len(DEFAULT_RANKS)} default ranks")

        recipe_version = int(
            (await self.db.relic_get_setting("combine_recipes_version")) or 0
        )
        if recipe_version < DEFAULT_COMBINE_RECIPES_VERSION:
            item_ids = {item["id"] for item in await self.db.relic_get_all_items()}
            seeded = 0
            for recipe in DEFAULT_COMBINE_RECIPES:
                recipe_items = {
                    recipe["ingredient_a_id"],
                    recipe["ingredient_b_id"],
                    recipe["result_item_id"],
                }
                if recipe_items.issubset(item_ids):
                    inserted = await self.db.relic_insert_combine_recipe_if_missing(
                        recipe
                    )
                    seeded += int(inserted)
            await self.db.relic_set_setting(
                "combine_recipes_version",
                str(DEFAULT_COMBINE_RECIPES_VERSION),
            )
            _rlog(f"Added {seeded} missing default combine recipes")

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
                "points": 0, "xp": 0, "shinies": 0, "level": 1,
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

    async def _grant_village_reward(self, user: dict, area: dict) -> dict:
        level = max(0, int(area.get("level") or 0))
        resource = area.get("resource_type")
        uid = user["twitch_user_id"]
        name = user["username"]

        if resource == "points":
            amount = level * int((await self.db.relic_get_setting("village_points_per_level")) or 20)
            user["points"] = int(user.get("points") or 0) + amount
            await self.db.relic_upsert_user(user)
            return {"username": name, "resource": resource, "amount": amount,
                    "text": f"@{name} +{amount} points"}

        if resource == "xp":
            amount = level * int((await self.db.relic_get_setting("village_xp_per_level")) or 12)
            user, _, _ = await self._apply_xp(user, amount)
            await self.db.relic_upsert_user(user)
            return {"username": name, "resource": resource, "amount": amount,
                    "text": f"@{name} +{amount} XP"}

        if resource == "shinies":
            amount = level * int((await self.db.relic_get_setting("village_shinies_per_level")) or 1)
            user["shinies"] = int(user.get("shinies") or 0) + amount
            await self.db.relic_upsert_user(user)
            return {"username": name, "resource": resource, "amount": amount,
                    "text": f"@{name} +{amount} Shiny"}

        if resource == "items":
            amount = level * int((await self.db.relic_get_setting("village_items_per_level")) or 1)
            all_items = await self.db.relic_get_all_items()
            pool = [
                item for item in all_items
                if item.get("enabled") and item.get("rarity") in ("common", "uncommon")
            ]
            if pool and amount > 0:
                prizes = [random.choice(pool) for _ in range(amount)]
                for prize in prizes:
                    await self.db.relic_add_item_to_user(uid, prize["id"])
                counts: dict[str, dict] = {}
                for prize in prizes:
                    entry = counts.setdefault(
                        prize["id"],
                        {"item": prize, "count": 0},
                    )
                    entry["count"] += 1
                parts = []
                for entry in counts.values():
                    prize = entry["item"]
                    count = entry["count"]
                    label = f"{prize.get('icon','')} {prize['name']}".strip()
                    parts.append(f"{count}x {label}" if count > 1 else label)
                return {"username": name, "resource": resource, "amount": amount,
                        "text": f"@{name} receives {', '.join(parts)}"}
            return {"username": name, "resource": resource, "amount": 0,
                    "text": f"@{name} receives a trade blessing"}

        return {"username": name, "resource": "blessing", "amount": 0,
                "text": f"@{name} receives a village blessing"}

    @staticmethod
    def _format_village_payout_message(
        rewards: list[dict],
        *,
        completed_count: int,
        includes_current: bool,
    ) -> str:
        detailed = "🏘️ Hrafnathorp payout: " + " | ".join(
            f"{reward['source']} → {reward['text']}" for reward in rewards
        )
        if len(detailed) <= 490:
            return detailed

        totals: dict[str, dict[str, int]] = {}
        for reward in rewards:
            user_totals = totals.setdefault(reward["username"], {})
            resource = reward["resource"]
            user_totals[resource] = user_totals.get(resource, 0) + int(reward["amount"] or 0)

        parts = []
        labels = {
            "points": "points",
            "xp": "XP",
            "items": "items",
            "shinies": "Shinies",
        }
        for username, resources in totals.items():
            amounts = [
                f"+{amount} {labels[resource]}"
                for resource, amount in resources.items()
                if amount > 0 and resource in labels
            ]
            parts.append(f"@{username} {', '.join(amounts) or 'village blessing'}")

        source_summary = f"{completed_count} completed village{'s' if completed_count != 1 else ''}"
        if includes_current:
            source_summary += " + current village"
        prefix = f"🏘️ Hrafnathorp payout ({source_summary}): "
        included = []
        for index, part in enumerate(parts):
            remaining = len(parts) - index - 1
            suffix = f" | +{remaining} more rewarded" if remaining else ""
            candidate = prefix + " | ".join([*included, part]) + suffix
            if len(candidate) > 490:
                break
            included.append(part)
        remaining = len(parts) - len(included)
        message = prefix + " | ".join(included)
        if remaining:
            message += f" | +{remaining} more rewarded"
        return message[:490]

    async def _get_ranks(self) -> list[dict]:
        ranks = await self.db.relic_get_all_ranks(active_only=True)
        return ranks or DEFAULT_RANKS

    async def _apply_xp(self, user: dict, xp_gain: int, old_points: Optional[int] = None) -> tuple[dict, int, Optional[dict]]:
        """Apply XP and level-ups. Returns (updated_user, level_ups, new_rank_or_None)."""
        ranks = await self._get_ranks()
        old_rank = _get_rank(user["points"] if old_points is None else old_points, ranks)
        user["xp"] += xp_gain
        level_ups = 0
        while user["xp"] >= _xp_for_next(user["level"]):
            user["xp"] -= _xp_for_next(user["level"])
            user["level"] += 1
            level_ups += 1
        new_rank = _get_rank(user["points"], ranks)
        rank_changed = new_rank["id"] != old_rank["id"]
        return user, level_ups, (new_rank if rank_changed else None)

    async def _send_progress_announcement(
        self,
        name: str,
        user: dict,
        level_ups: int,
        new_rank: Optional[dict],
    ) -> None:
        announce_lvl = (await self.db.relic_get_setting("announce_level_ups")) == "true"
        announce_rank = (await self.db.relic_get_setting("announce_rank_ups")) == "true"
        if level_ups and announce_lvl:
            rank_str = (
                f" and became a {new_rank['icon']} {new_rank['name']}!"
                if new_rank and announce_rank
                else "!"
            )
            await self._send(f"⬆️ @{name} reached level {user['level']}{rank_str}")
        elif new_rank and announce_rank:
            await self._send(f"⬆️ @{name} became a {new_rank['icon']} {new_rank['name']}!")

    async def _is_game_enabled(self) -> bool:
        val = await self.db.relic_get_setting("enabled")
        return val != "false"  # default ON unless explicitly disabled

    def _subscriber_guard(self, handler):
        async def wrapped(ctx: dict) -> None:
            access_mode = (await self.db.relic_get_setting("access_mode")) or "everyone"
            is_staff = ctx.get("is_broadcaster") or ctx.get("is_mod")
            if access_mode == "subscribers" and not (ctx.get("is_sub") or is_staff):
                name = ctx.get("username") or "there"
                await self._send(f"@{name} Raven's Nest: Relic Hunt is currently available to subscribers only.")
                return
            await handler(ctx)
        return wrapped

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
        old_points = user["points"]
        user["points"]         += pts
        shiny_gain = max(0, int((await self.db.relic_get_setting("shiny_per_find")) or 1))
        user["shinies"] = int(user.get("shinies") or 0) + shiny_gain
        user["last_raven_at"]   = time.time()
        user["commands_used"]  += 1
        rarity = item.get("rarity", "common")
        if rarity == "legendary":
            user["legendary_finds"] += 1
        elif rarity == "mythic":
            user["mythic_finds"] += 1

        user, level_ups, new_rank = await self._apply_xp(user, xp, old_points)
        await self.db.relic_upsert_user(user)
        await self.db.relic_add_item_to_user(uid, item["id"])

        # Build message
        icon = item.get("icon", "") or ""
        iname = item["name"]
        if rarity == "mythic":
            msg = f"🔥 MYTHIC DISCOVERY! @{name} has found {icon} {iname}. The Raven's Nest will remember this. +{pts} points, +{xp} XP, +{shiny_gain} Shiny."
        elif rarity == "legendary":
            msg = f"🌑 LEGENDARY RELIC! @{name}'s raven returns carrying {icon} {iname}! +{pts} points, +{xp} XP, +{shiny_gain} Shiny."
        elif rarity == "epic":
            msg = f"🌘 EPIC RELIC! @{name}'s raven brings back {icon} {iname}. +{pts} points, +{xp} XP, +{shiny_gain} Shiny."
        elif rarity == "rare":
            msg = f"🪶 Rare find! @{name}'s raven returns with {icon} {iname}. +{pts} points, +{xp} XP, +{shiny_gain} Shiny."
        elif rarity == "uncommon":
            msg = f"🍃 @{name}'s raven finds {icon} {iname}. {item.get('flavor_text','')} +{pts} points, +{xp} XP, +{shiny_gain} Shiny."
        else:
            msg = f"@{name} sends a raven into the mist... It returns with {icon} {iname}. +{pts} points, +{xp} XP, +{shiny_gain} Shiny."

        await self._send(msg)
        _rlog(f"{name} found {iname} ({rarity}) | +{pts}pts +{xp}xp")

        await self._send_progress_announcement(name, user, level_ups, new_rank)

        # Log
        await self.db.relic_log_hunt({
            "twitch_user_id": uid, "username": name,
            "item_id": item["id"], "item_name": iname, "rarity": rarity,
            "points_awarded": pts, "xp_awarded": xp,
            "result_type": "found", "message": msg, "created_at": time.time(),
        })

        puzzle = await self.db.relic_get_phrase_puzzle()
        if (
            puzzle.get("enabled")
            and not puzzle.get("solved_at")
            and random.random() < float(puzzle.get("letter_find_chance") or 0)
        ):
            revealed = await self.db.relic_reveal_random_phrase_letter()
            if revealed:
                progress = _phrase_progress(
                    revealed["phrase"], revealed["revealed_mask"]
                )
                await self._send(
                    f"🔤 @{name}'s raven found the letter "
                    f"{revealed['revealed_letter'].upper()} for the hidden phrase! "
                    f"{progress}"
                )

    async def _cmd_nest(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        uid  = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)
        ranks = await self._get_ranks()
        rank = _get_rank(user["points"], ranks)
        next_rank = _get_next_rank(user["points"], ranks)
        inv  = await self.db.relic_get_inventory(uid)
        total_items = sum(i["amount"] for i in inv)
        rarest = max(inv, key=lambda i: RARITY_ORDER.index(
            i.get("rarity", "common") if i.get("rarity") in RARITY_ORDER else "common"
        ), default=None)
        rarest_str = f" | Rarest: {rarest['icon'] or ''} {rarest['name']}" if rarest else ""
        next_xp = _xp_for_next(user["level"])
        next_str = f" | Next rank: {next_rank['name']} at {next_rank['min_points']} pts" if next_rank else " | Max rank reached"
        await self._send(
            f"@{name}'s Nest | Rank: {rank['name']} | Level: {user['level']} | "
            f"XP: {user['xp']}/{next_xp} | Points: {user['points']} | "
            f"Shinies: {int(user.get('shinies') or 0)} | Items: {total_items}{rarest_str}{next_str}"
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
        ranks = await self._get_ranks()
        rank = _get_rank(user["points"], ranks)
        next_rank = _get_next_rank(user["points"], ranks)
        next_xp   = _xp_for_next(user["level"])
        next_str  = f" | Next rank: {next_rank['name']} at {next_rank['min_points']} pts." if next_rank else " | Max rank reached."
        await self._send(
            f"@{name} Rank: {rank['name']} | Level: {user['level']} | "
            f"XP: {user['xp']}/{next_xp} | Points: {user['points']}{next_str}"
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
        old_points = user["points"]
        user["points"] += pts
        user["last_daily_at"] = time.time()
        user, level_ups, new_rank = await self._apply_xp(user, xp, old_points)

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
        await self._send_progress_announcement(name, user, level_ups, new_rank)

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
        ritual_shiny_gain = max(0, int((await self.db.relic_get_setting("shiny_per_ritual")) or 1))
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
            await self._send(
                f"🔥 The Raven Ritual is complete! All active hunters receive "
                f"+{reward_pts} points, +{reward_xp} XP and +{ritual_shiny_gain} Shiny."
            )

            # Reward all active users (hunted in last 30 min)
            window = int((await self.db.relic_get_setting("ritual_active_window_minutes")) or 30) * 60
            all_users = await self.db.relic_get_all_users()
            cutoff = time.time() - window
            active_users = [
                u for u in all_users
                if max(u.get("last_raven_at") or 0, u.get("last_ritual_at") or 0) >= cutoff
            ]
            for u in active_users:
                old_points = u["points"]
                u["points"] += reward_pts
                u["shinies"] = int(u.get("shinies") or 0) + ritual_shiny_gain
                u, _, _ = await self._apply_xp(u, reward_xp, old_points)
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

    async def _cmd_combine(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        uid = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)
        inventory = {
            item["item_id"]: item
            for item in await self.db.relic_get_inventory(uid)
        }
        recipes = await self.db.relic_get_all_combine_recipes(active_only=True)

        selected = None
        for recipe in recipes:
            if not all((
                recipe.get("ingredient_a_name"),
                recipe.get("ingredient_b_name"),
                recipe.get("result_item_name"),
            )):
                continue
            if recipe.get("ingredient_a_rarity") in ("legendary", "mythic"):
                continue
            if recipe.get("ingredient_b_rarity") in ("legendary", "mythic"):
                continue
            amount_a = inventory.get(recipe["ingredient_a_id"], {}).get("amount", 0)
            amount_b = inventory.get(recipe["ingredient_b_id"], {}).get("amount", 0)
            if recipe["ingredient_a_id"] == recipe["ingredient_b_id"]:
                if amount_a >= 2:
                    selected = recipe
                    break
            elif amount_a >= 1 and amount_b >= 1:
                selected = recipe
                break

        if not selected:
            await self._send(f"@{name}'s raven cannot combine anything in the nest yet.")
            return

        icon = selected.get("result_item_icon") or ""
        result_name = selected["result_item_name"]
        ingredient_a = selected["ingredient_a_name"]
        ingredient_b = selected["ingredient_b_name"]
        bonus = int(selected.get("bonus_points") or 0)
        bonus_text = f" +{bonus} bonus points." if bonus else ""
        msg = (
            f"🔮 @{name} combines {ingredient_a} and {ingredient_b}... "
            f"{icon} {result_name} created!{bonus_text}"
        )
        selected["activity_text"] = (
            f"{ingredient_a} + {ingredient_b} → {result_name}"
        )
        old_points = user["points"]
        success = await self.db.relic_apply_combine_recipe(
            uid, name, selected, msg
        )
        if not success:
            await self._send(f"@{name}'s raven lost track of the ingredients. Try again.")
            return
        shiny_gain = max(0, int((await self.db.relic_get_setting("shiny_per_combine")) or 1))
        if shiny_gain:
            await self.db.relic_add_shinies(uid, shiny_gain)
            msg = f"{msg} +{shiny_gain} Shiny."

        await self._send(msg)
        _rlog(
            f"{name} combined {ingredient_a} + {ingredient_b} into "
            f"{result_name} | +{bonus}pts"
        )

        if bonus and (await self.db.relic_get_setting("announce_rank_ups")) == "true":
            ranks = await self._get_ranks()
            old_rank = _get_rank(old_points, ranks)
            new_rank = _get_rank(old_points + bonus, ranks)
            if new_rank["id"] != old_rank["id"]:
                await self._send(
                    f"⬆️ @{name} became a {new_rank['icon']} {new_rank['name']}!"
                )

    async def _cmd_village(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        areas = await self.db.relic_get_village_areas()
        village_count = max(1, int((await self.db.relic_get_setting("village_count")) or 1))
        parts = []
        for area in areas:
            level = int(area.get("level") or 0)
            progress = int(area.get("progress") or 0)
            max_level = int(area.get("max_level") or 5)
            if area.get("resource_type") == "points":
                payout = f"{level * int((await self.db.relic_get_setting('village_points_per_level')) or 20)} points"
            elif area.get("resource_type") == "xp":
                payout = f"{level * int((await self.db.relic_get_setting('village_xp_per_level')) or 12)} XP"
            elif area.get("resource_type") == "shinies":
                payout = f"{level * int((await self.db.relic_get_setting('village_shinies_per_level')) or 1)} Shiny"
            else:
                amount = level * int((await self.db.relic_get_setting("village_items_per_level")) or 1)
                payout = f"{amount} item{'s' if amount != 1 else ''}"
            parts.append(f"{area['name']} L{level}/{max_level} {progress}/100 ({payout})")
        await self._send(
            f"🏘️ Hrafnathorp | Villages: {village_count} | " + " | ".join(parts)
        )

    async def _cmd_village_donate(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        raw_command = ((ctx.get("text") or "").split(None, 1)[0]).lstrip("!").lower()
        area_id = None
        for candidate, cfg in VILLAGE_AREAS.items():
            if raw_command == cfg["command"]:
                area_id = candidate
                break
        if not area_id:
            return
        uid = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)
        cost = max(1, int((await self.db.relic_get_setting("village_progress_cost_shinies")) or 5))
        area = await self.db.relic_get_village_area(area_id)
        if not area:
            await self._send("Hrafnathorp is not ready yet.")
            return
        if int(area.get("level") or 0) >= int(area.get("max_level") or 5):
            await self._send(f"@{name} {area['name']} is already at max level.")
            return
        if int(user.get("shinies") or 0) < cost:
            await self._send(f"@{name} You need {cost} Shinies to help Hrafnathorp.")
            return
        if not await self.db.relic_try_spend_shinies(uid, cost):
            await self._send(f"@{name} You need {cost} Shinies to help Hrafnathorp.")
            return
        updated = await self.db.relic_add_village_progress(area_id, 1)
        if not updated:
            await self._send("Hrafnathorp could not receive the donation.")
            return
        cfg = VILLAGE_AREAS[area_id]
        if updated.get("leveled_up"):
            await self._send(
                f"🏘️ @{name} {cfg['verb']}. {updated['name']} rises to "
                f"level {updated['level']}!"
            )
        else:
            await self._send(
                f"🏘️ @{name} {cfg['verb']}. {updated['name']} progress: "
                f"{updated['progress']}/100."
            )
        _rlog(f"{name} donated {cost} Shinies to {area_id} ({updated['progress']}/100 L{updated['level']})")

    async def _cmd_next_village(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        uid = ctx["user_id"]
        name = ctx["username"]
        user = await self._get_or_create_user(uid, name)
        cost = max(1, int((await self.db.relic_get_setting("village_next_cost_shinies")) or 50))
        areas = await self.db.relic_get_village_areas()
        unfinished = [
            area for area in areas
            if int(area.get("level") or 0) < int(area.get("max_level") or 5)
        ]
        if unfinished:
            names = ", ".join(area["name"] for area in unfinished)
            await self._send(
                f"@{name} Hrafnathorp is not fully built yet. "
                f"Finish these areas first: {names}."
            )
            return
        if int(user.get("shinies") or 0) < cost:
            await self._send(f"@{name} You need {cost} Shinies to found another village.")
            return
        if not await self.db.relic_try_spend_shinies(uid, cost):
            await self._send(f"@{name} You need {cost} Shinies to found another village.")
            return
        village_count = max(1, int((await self.db.relic_get_setting("village_count")) or 1)) + 1
        await self.db.relic_set_setting("village_count", str(village_count))
        await self.db.relic_reset_village()
        await self.db.relic_set_setting("village_next_payout_at", "0")
        await self._send(
            f"🏘️ @{name} founds another Hrafnathorp outpost! "
            f"Village count: {village_count}. A new village can now be built."
        )
        _rlog(f"{name} founded village #{village_count} for {cost} Shinies")

    async def _cmd_phrase(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        puzzle = await self.db.relic_get_phrase_puzzle()
        if not puzzle.get("phrase"):
            await self._send("There is no hidden phrase yet.")
            return
        if puzzle.get("solved_at"):
            await self._send(
                f"🧩 The phrase was solved by "
                f"@{puzzle.get('solved_by_username') or 'an unknown hunter'}: "
                f"{puzzle['phrase']}"
            )
            return
        if not puzzle.get("enabled"):
            await self._send("The hidden phrase puzzle is currently disabled.")
            return
        progress = _phrase_progress(
            puzzle["phrase"], puzzle.get("revealed_mask", "")
        )
        found = sum(
            1 for index, char in enumerate(puzzle["phrase"])
            if char.isalpha()
            and index < len(puzzle.get("revealed_mask", ""))
            and puzzle["revealed_mask"][index] == "1"
        )
        total = sum(char.isalpha() for char in puzzle["phrase"])
        await self._send(
            f"🧩 Hidden phrase: {progress} | Letters found: {found}/{total}"
        )

    async def _cmd_solve(self, ctx: dict) -> None:
        if not await self._is_game_enabled():
            return
        uid = ctx["user_id"]
        name = ctx["username"]
        guess = (ctx.get("args") or "").strip()
        if not guess:
            await self._send(f"@{name} Use !solve followed by your answer.")
            return

        result = await self.db.relic_try_solve_phrase(
            uid,
            name,
            " ".join(guess.casefold().split()),
            cooldown_seconds=3600,
        )
        status = result["status"]
        if status == "inactive":
            await self._send("The hidden phrase puzzle is currently inactive.")
            return
        if status == "solved":
            puzzle = result.get("puzzle") or {}
            await self._send(
                f"The phrase has already been solved by "
                f"@{puzzle.get('solved_by_username') or 'another hunter'}."
            )
            return
        if status == "cooldown":
            await self._send(
                f"@{name} You can try another solution in "
                f"{_fmt_cooldown(result['remaining'])}."
            )
            return
        if status == "wrong":
            await self._send(
                f"@{name} That is not the hidden phrase. "
                f"You can try again in 60 minutes."
            )
            return

        puzzle = result["puzzle"]
        reward_xp = int(puzzle.get("winner_xp_reward") or 0)
        user = await self._get_or_create_user(uid, name)
        user, level_ups, _ = await self._apply_xp(user, reward_xp)
        await self.db.relic_upsert_user(user)
        await self.db.relic_mark_current_phrase_solved(
            uid, name, time.time()
        )
        next_phrase = await self.db.relic_activate_next_phrase()
        await self._send(
            f"🎉 @{name} solved the hidden phrase: {puzzle['phrase']} "
            f"and wins +{reward_xp} XP!"
        )
        if next_phrase:
            await self._send("🧩 A new hidden phrase has begun.")
        else:
            await self._send("🧩 No queued phrases remain. Phrase Puzzle is now disabled.")
        if level_ups and (
            await self.db.relic_get_setting("announce_level_ups")
        ) == "true":
            await self._send(f"⬆️ @{name} reached level {user['level']}!")

    async def _cmd_help(self, ctx: dict) -> None:
        await self._send(
            "Raven's Nest commands: !raven, !nest, !items, !top, !rank, !daily, "
            "!ritual, !combine, !village, !entertain, !teach, !trade, !invest, "
            "!nextVillage, !phrase, !solve, !relichelp"
        )

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
