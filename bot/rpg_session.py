"""RPG session orchestration: scene flow, combat resolution, persistence.

High-level entry points (all async, all take the Database `db`):

- `start_adventure(db, party_id, adventure_id)` — sets the start scene and
  returns the rendered scene payload.
- `advance_scene(db, party_id, choice_index)` — handle a scripted choice.
- `handle_free_text(db, party_id, character_id, text, llm_client)` —
  hybrid LLM narration; never mutates HP/scene unless the validator allows.
- `start_combat(db, party_id, enemy_keys, next_scene_key)` — kick off
  initiative + combat state.
- `player_basic_attack(db, party_id, character_id, target_ref)` —
  resolve a player's basic attack and advance turn until the next player.
- `player_use_ability(db, party_id, character_id, ability_key, target_ref)` —
  resolve an ability and advance.
- `party_status(db, party_id)` — full text summary of the party + scene.
"""

from __future__ import annotations

import json
import random
from typing import Any, Optional

from bot.rpg_engine import (
    CombatResult,
    apply_xp,
    basic_attack,
    enemy_choose_action,
    find_by_ref,
    is_stunned,
    make_enemy_combatant,
    make_player_combatant,
    render_combatant_status,
    roll_dice,
    roll_initiative,
    roll_loot,
    scene_choices,
    scene_data,
    tick_round_start,
    use_ability,
    _absorb_shield,
    _ref_of,
)


# --- Persistence helpers -----------------------------------------------------

async def _save_player(db, character: dict, combatant: dict) -> None:
    await db.rpg_update_character(
        character["id"],
        hp=combatant["hp"],
        mana=combatant["mana"],
        status_json=json.dumps(combatant.get("statuses", [])),
        cooldowns_json=json.dumps(combatant.get("cooldowns", {})),
    )


async def _save_combat(db, party_id: int, *, round_: int, turn_index: int,
                       initiative: list[dict], enemies: list[dict],
                       scene_key: Optional[str],
                       next_scene_key: Optional[str]) -> None:
    await db.rpg_set_combat(
        party_id,
        round=round_,
        turn_index=turn_index,
        initiative_json=json.dumps(initiative),
        enemies_json=json.dumps(enemies),
        scene_key=scene_key,
        next_scene_key=next_scene_key,
    )


async def _load_party_combatants(db, party_id: int) -> tuple[list[dict], list[dict]]:
    members = await db.rpg_get_party_members(party_id)
    players = [make_player_combatant(m) for m in members]
    combat = await db.rpg_get_combat(party_id)
    if combat:
        enemies = json.loads(combat.get("enemies_json") or "[]")
    else:
        enemies = []
    return players, enemies


# --- Scene flow --------------------------------------------------------------

async def start_adventure(db, party_id: int, adventure_id: int) -> dict:
    adv = await db.rpg_get_adventure(adventure_id)
    if not adv:
        return {"ok": False, "error": "Adventure not found."}
    start_key = adv.get("start_scene_key")
    if not start_key:
        return {"ok": False, "error": "Adventure has no start scene configured."}
    scene = await db.rpg_get_scene_by_key(adventure_id, start_key)
    if not scene:
        return {"ok": False, "error": f"Start scene '{start_key}' not found."}
    await db.rpg_update_party(
        party_id,
        adventure_id=adventure_id,
        current_scene_key=start_key,
        state="exploring",
        state_json="{}",
    )
    await db.rpg_log_event(party_id, "adventure_start",
                           f"Started adventure '{adv['name']}'.")
    return {"ok": True, "scene": _render_scene(scene, intro=adv.get("intro_text"))}


def _render_scene(scene: dict, intro: Optional[str] = None) -> dict:
    out = {
        "title": scene.get("title", ""),
        "narration": scene.get("narration", ""),
        "scene_type": scene.get("scene_type", "story"),
        "scene_key": scene.get("scene_key", ""),
        "choices": scene_choices(scene),
        "data": scene_data(scene),
    }
    if intro:
        out["intro"] = intro
    return out


async def current_scene(db, party_id: int) -> Optional[dict]:
    party = await db.rpg_get_party(party_id)
    if not party or not party.get("adventure_id") or not party.get("current_scene_key"):
        return None
    return await db.rpg_get_scene_by_key(party["adventure_id"], party["current_scene_key"])


async def advance_to_scene(db, party_id: int, scene_key: str) -> dict:
    party = await db.rpg_get_party(party_id)
    if not party or not party.get("adventure_id"):
        return {"ok": False, "error": "Party has no active adventure."}
    scene = await db.rpg_get_scene_by_key(party["adventure_id"], scene_key)
    if not scene:
        return {"ok": False, "error": f"Scene '{scene_key}' not found."}
    await db.rpg_update_party(party_id, current_scene_key=scene_key,
                              state="exploring")
    await db.rpg_log_event(party_id, "scene", f"-> {scene_key}")
    # Auto-trigger combat if the scene is of type 'combat'
    data = scene_data(scene)
    if scene.get("scene_type") == "combat":
        enemy_keys = data.get("enemies") or []
        next_key = data.get("next") or data.get("after_combat")
        combat_result = await start_combat(db, party_id, enemy_keys, next_key,
                                           scene_key=scene_key)
        return {"ok": True, "scene": _render_scene(scene),
                "combat": combat_result}
    return {"ok": True, "scene": _render_scene(scene)}


async def advance_scene(db, party_id: int, choice_index: int) -> dict:
    scene = await current_scene(db, party_id)
    if not scene:
        return {"ok": False, "error": "No active scene."}
    choices = scene_choices(scene)
    if choice_index < 0 or choice_index >= len(choices):
        return {"ok": False, "error": "Invalid choice."}
    choice = choices[choice_index]
    if not choice.get("next"):
        return {"ok": False, "error": "Choice has no destination scene."}
    return await advance_to_scene(db, party_id, choice["next"])


# --- Combat flow -------------------------------------------------------------

async def start_combat(db, party_id: int, enemy_keys: list[str],
                       next_scene_key: Optional[str],
                       *, scene_key: Optional[str] = None,
                       rng: random.Random | None = None) -> dict:
    rng = rng or random
    members = await db.rpg_get_party_members(party_id)
    if not members:
        return {"ok": False, "error": "Party has no members."}
    players = [make_player_combatant(m) for m in members]

    # Instantiate enemies; duplicates of same key get instance_index
    enemies: list[dict] = []
    counts: dict[str, int] = {}
    for key in enemy_keys:
        row = await db.rpg_get_enemy(key)
        if not row:
            continue
        idx = counts.get(key, 0)
        enemies.append(make_enemy_combatant(row, instance_index=idx))
        counts[key] = idx + 1
    if not enemies:
        return {"ok": False, "error": "No valid enemies."}

    initiative = roll_initiative(players + enemies, rng=rng)
    narration: list[str] = ["⚔️ **Combat begins!**"]
    narration.append("Initiative order: " + " → ".join(
        _name_from_ref(ref["ref"], players, enemies) for ref in initiative
    ))

    await db.rpg_update_party(party_id, state="combat")
    await _save_combat(db, party_id, round_=1, turn_index=0,
                       initiative=initiative, enemies=enemies,
                       scene_key=scene_key, next_scene_key=next_scene_key)
    # Auto-resolve enemy turns until first player turn (handles stuns)
    turn_info = await _advance_until_player(db, party_id, narration)
    return {"ok": True, "narration": narration, "turn": turn_info}


def _name_from_ref(ref: str, players: list[dict], enemies: list[dict]) -> str:
    c = find_by_ref(ref, players, enemies)
    return c["display_name"] if c else "?"


async def _advance_until_player(db, party_id: int, narration: list[str]) -> dict:
    """Run enemy turns (and skip dead/stunned) until the next live player's turn.

    Mutates the narration list. Persists final state. Returns turn info.
    """
    rng = random
    while True:
        combat = await db.rpg_get_combat(party_id)
        if not combat:
            return {"ended": True}
        initiative = json.loads(combat["initiative_json"])
        enemies = json.loads(combat["enemies_json"])
        # Reload players fresh from DB (HP may have changed between calls)
        members = await db.rpg_get_party_members(party_id)
        players = [make_player_combatant(m) for m in members]

        # Check win/loss
        if all(p["hp"] <= 0 for p in players):
            await _finish_combat(db, party_id, victory=False,
                                 players=players, enemies=enemies,
                                 narration=narration,
                                 next_scene_key=combat.get("next_scene_key"))
            return {"ended": True, "victory": False}
        if all(e["hp"] <= 0 for e in enemies):
            await _finish_combat(db, party_id, victory=True,
                                 players=players, enemies=enemies,
                                 narration=narration,
                                 next_scene_key=combat.get("next_scene_key"))
            return {"ended": True, "victory": True}

        round_ = int(combat["round"])
        turn_index = int(combat["turn_index"])

        if turn_index >= len(initiative):
            # New round
            round_ += 1
            turn_index = 0
            narration.append(f"— **Round {round_}** —")
            tick_round_start(players + enemies, narration)
            # Persist any status ticks back to players
            for p, m in zip(players, members):
                await _save_player(db, m, p)

        ref = initiative[turn_index]["ref"]
        actor = find_by_ref(ref, players, enemies)

        if actor is None or actor["hp"] <= 0:
            turn_index += 1
            await _save_combat(db, party_id, round_=round_, turn_index=turn_index,
                               initiative=initiative, enemies=enemies,
                               scene_key=combat.get("scene_key"),
                               next_scene_key=combat.get("next_scene_key"))
            continue

        if actor["kind"] == "player":
            # Stop here — UI/cog will prompt player
            await _save_combat(db, party_id, round_=round_, turn_index=turn_index,
                               initiative=initiative, enemies=enemies,
                               scene_key=combat.get("scene_key"),
                               next_scene_key=combat.get("next_scene_key"))
            if is_stunned(actor):
                narration.append(
                    f"💫 {actor['display_name']} is stunned and skips this turn."
                )
                turn_index += 1
                await _save_combat(db, party_id, round_=round_, turn_index=turn_index,
                                   initiative=initiative, enemies=enemies,
                                   scene_key=combat.get("scene_key"),
                                   next_scene_key=combat.get("next_scene_key"))
                continue
            return {
                "ended": False,
                "round": round_,
                "actor_kind": "player",
                "actor_id": actor["id"],
                "actor_user_id": actor["user_id"],
                "actor_name": actor["display_name"],
            }

        # Enemy turn — resolve here
        if is_stunned(actor):
            narration.append(f"💫 {actor['display_name']} is stunned and cannot act.")
        else:
            alive_players = [p for p in players if p["hp"] > 0]
            alive_enemies = [e for e in enemies if e["hp"] > 0]
            action, ability, targets = enemy_choose_action(
                actor, alive_players, alive_enemies, rng=rng
            )
            if action == "basic" and targets:
                narration.extend(basic_attack(actor, targets[0], rng=rng))
                # Persist target HP if player
                for t in targets:
                    if t["kind"] == "player":
                        for p, m in zip(players, members):
                            if p["id"] == t["id"]:
                                await _save_player(db, m, t)
            elif action == "ability" and ability:
                narration.extend(use_ability(actor, ability, targets, rng=rng))
                for t in targets:
                    if t["kind"] == "player":
                        for p, m in zip(players, members):
                            if p["id"] == t["id"]:
                                await _save_player(db, m, t)
        turn_index += 1
        await _save_combat(db, party_id, round_=round_, turn_index=turn_index,
                           initiative=initiative, enemies=enemies,
                           scene_key=combat.get("scene_key"),
                           next_scene_key=combat.get("next_scene_key"))


async def _finish_combat(db, party_id: int, *, victory: bool,
                         players: list[dict], enemies: list[dict],
                         narration: list[str],
                         next_scene_key: Optional[str]) -> None:
    if victory:
        xp, loot = roll_loot(enemies)
        narration.append(f"🏆 **Victory!** Party gains {xp} XP.")
        # Distribute XP equally
        members = await db.rpg_get_party_members(party_id)
        alive = [m for m in members if m["hp"] > 0]
        share = max(0, xp // max(1, len(alive)))
        for m in alive:
            sheet = dict(m)
            sheet["hp"] = m["hp"]
            new_level, _ = apply_xp(sheet, share)
            await db.rpg_update_character(
                m["id"],
                xp=sheet["xp"],
                level=sheet["level"],
                max_hp=sheet["max_hp"],
                hp=sheet["hp"],
                max_mana=sheet["max_mana"],
                mana=sheet["mana"],
                attack=sheet["attack"],
                defense=sheet["defense"],
                agility=sheet["agility"],
            )
            if new_level > int(m["level"]):
                narration.append(
                    f"⭐ **{m['name']}** reaches level **{new_level}**!"
                )
        if loot:
            # Add loot to leader's inventory for simplicity
            party = await db.rpg_get_party(party_id)
            if party:
                leader = await db.rpg_get_character_by_user(int(party["leader_user_id"]))
                if leader:
                    inv = json.loads(leader.get("inventory_json") or "[]")
                    for drop in loot:
                        # Stack same item_key
                        matched = next(
                            (i for i in inv if i.get("item_key") == drop["item_key"]),
                            None,
                        )
                        if matched:
                            matched["amount"] = int(matched.get("amount", 0)) + drop["amount"]
                        else:
                            inv.append(dict(drop))
                    await db.rpg_update_character(
                        leader["id"], inventory_json=json.dumps(inv)
                    )
            narration.append(
                "💰 Loot: " + ", ".join(f"{d['amount']}× {d['item_key']}" for d in loot)
            )
    else:
        narration.append("💀 **The party has fallen.**")

    await db.rpg_clear_combat(party_id)
    await db.rpg_log_event(party_id, "combat_end",
                           "Victory" if victory else "Defeat")
    if victory and next_scene_key:
        await advance_to_scene(db, party_id, next_scene_key)
    else:
        await db.rpg_update_party(party_id, state="exploring" if victory else "ended")


# --- Player combat actions ---------------------------------------------------

async def player_basic_attack(db, party_id: int, character_id: int,
                              target_ref: str) -> dict:
    return await _player_action(db, party_id, character_id, target_ref,
                                ability_key=None)


async def player_use_ability(db, party_id: int, character_id: int,
                             ability_key: str, target_ref: str | None) -> dict:
    return await _player_action(db, party_id, character_id, target_ref,
                                ability_key=ability_key)


async def _player_action(db, party_id: int, character_id: int,
                         target_ref: Optional[str],
                         ability_key: Optional[str]) -> dict:
    combat = await db.rpg_get_combat(party_id)
    if not combat:
        return {"ok": False, "error": "Not in combat."}
    initiative = json.loads(combat["initiative_json"])
    turn_index = int(combat["turn_index"])
    if turn_index >= len(initiative):
        return {"ok": False, "error": "Turn out of bounds."}

    current_ref = initiative[turn_index]["ref"]
    if current_ref != f"player:{character_id}":
        return {"ok": False, "error": "It is not your turn."}

    enemies = json.loads(combat["enemies_json"])
    members = await db.rpg_get_party_members(party_id)
    players = [make_player_combatant(m) for m in members]
    actor = next((p for p in players if p["id"] == character_id), None)
    if not actor:
        return {"ok": False, "error": "Character not in party."}

    target = None
    if target_ref:
        target = find_by_ref(target_ref, players, enemies)
        if not target or target["hp"] <= 0:
            return {"ok": False, "error": "Invalid or dead target."}

    narration: list[str] = []
    if ability_key:
        character = next(m for m in members if m["id"] == character_id)
        class_row = await db.rpg_get_class(character["class_key"])
        abilities = json.loads(class_row["abilities_json"]) if class_row else []
        ability = next((a for a in abilities if a.get("key") == ability_key), None)
        if not ability:
            return {"ok": False, "error": f"Unknown ability '{ability_key}'."}
        if actor.get("cooldowns", {}).get(ability_key, 0) > 0:
            return {"ok": False, "error": "Ability is on cooldown."}
        # Resolve targets based on ability target type
        tgt_kind = ability.get("target", "enemy")
        if tgt_kind == "self":
            targets = [actor]
        elif tgt_kind == "ally":
            if not target_ref:
                targets = [actor]
            else:
                ally = next((p for p in players if _ref_of(p) == target_ref), None)
                targets = [ally] if ally else [actor]
        elif tgt_kind == "party":
            targets = [p for p in players if p["hp"] > 0]
        elif tgt_kind == "all_enemies":
            targets = [e for e in enemies if e["hp"] > 0]
        else:  # enemy
            if not target or target["kind"] != "enemy":
                return {"ok": False, "error": "Ability requires an enemy target."}
            targets = [target]
        narration.extend(use_ability(actor, ability, targets))
    else:
        if not target or target["kind"] != "enemy":
            return {"ok": False, "error": "Choose an enemy to attack."}
        narration.extend(basic_attack(actor, target))

    # Persist actor changes
    actor_member = next(m for m in members if m["id"] == character_id)
    await _save_player(db, actor_member, actor)
    # Persist any player healing/buff to other members
    for p, m in zip(players, members):
        if p["id"] == character_id:
            continue
        await _save_player(db, m, p)

    # Save combat state with enemies (HP may have changed) and advance turn
    turn_index += 1
    await _save_combat(db, party_id, round_=int(combat["round"]),
                       turn_index=turn_index, initiative=initiative,
                       enemies=enemies, scene_key=combat.get("scene_key"),
                       next_scene_key=combat.get("next_scene_key"))

    turn_info = await _advance_until_player(db, party_id, narration)
    return {"ok": True, "narration": narration, "turn": turn_info}


# --- Hybrid LLM GM -----------------------------------------------------------

async def handle_free_text(db, party_id: int, character_id: int,
                           user_text: str, *, llm_client,
                           llm_model: Optional[str] = None) -> dict:
    """Use the LLM to narrate a free-form action in the current scene.

    The LLM is asked to return JSON with `narration` and an optional
    `effect` (one of: `damage_self`, `heal_self`, `advance:<scene_key>`).
    All mechanical effects are validated server-side before applying.
    """
    party = await db.rpg_get_party(party_id)
    if not party:
        return {"ok": False, "error": "Party not found."}
    if party.get("state") == "combat":
        return {"ok": False, "error": "Cannot use free text during combat."}
    scene = await current_scene(db, party_id)
    if not scene:
        return {"ok": False, "error": "No active scene."}
    adventure = await db.rpg_get_adventure(party["adventure_id"])
    character = await db.rpg_get_character(character_id)
    if not character:
        return {"ok": False, "error": "Character not found."}

    recent = await db.rpg_recent_log(party_id, limit=10)
    recent_str = "\n".join(f"- [{r['kind']}] {r['content']}" for r in recent)

    sys_prompt = (
        "You are the Game Master of a fantasy tabletop RPG, narrating in "
        "English. Respond strictly with a JSON object: {\"narration\": str, "
        "\"effect\": null | {\"type\": \"advance\", \"scene_key\": str} | "
        "{\"type\": \"heal_self\", \"amount\": int} | "
        "{\"type\": \"damage_self\", \"amount\": int}}. "
        "Keep narration to 2–5 short sentences. Be evocative, not verbose. "
        "Only suggest effects when the player's action clearly warrants it. "
        "Never invent stats, items, or scenes — defer mechanics back to the system."
    )
    if adventure and adventure.get("llm_system_prompt"):
        sys_prompt += "\n\nAdventure brief:\n" + adventure["llm_system_prompt"]

    user_prompt = (
        f"Current scene: {scene.get('title')} (key: {scene.get('scene_key')})\n"
        f"Scene description: {scene.get('narration')}\n"
        f"Available next scenes: "
        f"{[c.get('next') for c in scene_choices(scene) if c.get('next')]}\n\n"
        f"Player: {character.get('name')} (class: {character.get('class_key')}, "
        f"level {character.get('level')}, "
        f"HP {character.get('hp')}/{character.get('max_hp')})\n"
        f"Recent log:\n{recent_str or '(no recent events)'}\n\n"
        f"Player's free-form action: \"\"\"{user_text.strip()}\"\"\"\n\n"
        "Return ONLY the JSON object as instructed."
    )

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        resp = await llm_client.chat(messages, max_tokens=350,
                                     temperature=0.7, model=llm_model)
    except Exception as e:
        return {"ok": False, "error": f"LLM unavailable: {e}"}

    content = ""
    msg = (resp or {}).get("message") or {}
    if isinstance(msg, dict):
        content = (msg.get("content") or "").strip()

    payload = _extract_json(content)
    if not payload:
        await db.rpg_log_event(party_id, "free_text",
                               f"{character['name']}: {user_text}")
        await db.rpg_log_event(party_id, "narration",
                               content or "(no response)")
        return {"ok": True, "narration": content or "The world is silent.",
                "applied_effect": None}

    narration = payload.get("narration") or content
    effect = payload.get("effect")
    applied = None

    if isinstance(effect, dict):
        etype = effect.get("type")
        if etype == "advance":
            target_key = effect.get("scene_key", "")
            valid_keys = [c.get("next") for c in scene_choices(scene)]
            if target_key in valid_keys and target_key:
                await advance_to_scene(db, party_id, target_key)
                applied = {"type": "advance", "scene_key": target_key}
        elif etype == "heal_self":
            amount = max(0, min(10, int(effect.get("amount", 0))))
            new_hp = min(int(character["max_hp"]),
                         int(character["hp"]) + amount)
            await db.rpg_update_character(character_id, hp=new_hp)
            applied = {"type": "heal_self", "amount": amount}
        elif etype == "damage_self":
            amount = max(0, min(10, int(effect.get("amount", 0))))
            new_hp = max(0, int(character["hp"]) - amount)
            await db.rpg_update_character(character_id, hp=new_hp)
            applied = {"type": "damage_self", "amount": amount}

    await db.rpg_log_event(party_id, "free_text",
                           f"{character['name']}: {user_text}")
    await db.rpg_log_event(party_id, "narration", narration)
    return {"ok": True, "narration": narration, "applied_effect": applied}


_JSON_RE = None


def _extract_json(text: str) -> Optional[dict]:
    import re as _re
    if not text:
        return None
    # Try direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Find first {...} block
    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return None


# --- Status snapshot ---------------------------------------------------------

async def party_status(db, party_id: int) -> str:
    party = await db.rpg_get_party(party_id)
    if not party:
        return "Party not found."
    members = await db.rpg_get_party_members(party_id)
    lines = [f"**Party: {party['name']}** (state: {party['state']})"]
    for m in members:
        c = make_player_combatant(m)
        lines.append("• " + render_combatant_status(c))
    combat = await db.rpg_get_combat(party_id)
    if combat:
        enemies = json.loads(combat["enemies_json"])
        lines.append(f"\n**Combat — Round {combat['round']}**")
        for e in enemies:
            lines.append("• " + render_combatant_status(e))
    elif party.get("adventure_id") and party.get("current_scene_key"):
        scene = await db.rpg_get_scene_by_key(
            party["adventure_id"], party["current_scene_key"]
        )
        if scene:
            lines.append(f"\n**Scene:** {scene.get('title')} ({scene.get('scene_key')})")
    return "\n".join(lines)
