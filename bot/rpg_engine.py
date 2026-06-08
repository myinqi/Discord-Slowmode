"""RPG game engine — scene flow, combat resolution, hybrid LLM GM.

This module is stateless apart from what it reads/writes via the Database.
It is consumed by:
- `bot/cogs/rpg.py` — the Discord slash-command cog
- `web/app.py` — the admin UI (preview / debug only)

Combat philosophy (medium complexity):
- d20 attack roll vs. (10 + target defense). Crit on natural 20 (double damage).
- Damage = stat (attack) + ability_bonus_dice + flat
- Initiative = d20 + agility, descending
- Status effects: poison (tick damage), stun (skip turn), shield (absorb), buff/debuff
- Class abilities have mana cost + cooldown rounds; cooldowns tick per round
- Enemy AI: pick lowest-HP party member (60%) or random (40%); use abilities
  when available, otherwise basic attack
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence


# --- Dice helpers ------------------------------------------------------------

_DICE_RE = re.compile(r"^\s*(\d+)\s*d\s*(\d+)\s*(?:([+-])\s*(\d+))?\s*$", re.IGNORECASE)


def roll_dice(expr: str, *, rng: random.Random | None = None) -> int:
    """Roll a dice expression like '2d6+1' or '3d8'. Returns the sum."""
    if rng is None:
        rng = random
    m = _DICE_RE.match(expr or "")
    if not m:
        try:
            return int(expr)
        except (TypeError, ValueError):
            return 0
    count = int(m.group(1))
    sides = int(m.group(2))
    op = m.group(3)
    bonus = int(m.group(4)) if m.group(4) else 0
    if count <= 0 or sides <= 0:
        return 0
    total = sum(rng.randint(1, sides) for _ in range(count))
    if op == "+":
        total += bonus
    elif op == "-":
        total -= bonus
    return max(0, total)


def d20(rng: random.Random | None = None) -> int:
    return (rng or random).randint(1, 20)


# --- Status effect application ----------------------------------------------

def _statuses_active(combatant: dict) -> list[dict]:
    return combatant.get("statuses") or []


def _has_status(combatant: dict, name: str) -> bool:
    return any(s.get("name") == name for s in _statuses_active(combatant))


def _add_status(combatant: dict, status: dict) -> None:
    combatant.setdefault("statuses", []).append(dict(status))


def _tick_statuses(combatant: dict, narration: list[str]) -> None:
    """Apply per-round effects: poison damage, decrement durations, drop expired."""
    remaining: list[dict] = []
    for st in _statuses_active(combatant):
        name = st.get("name")
        if name == "poison":
            dmg = int(st.get("damage", 2))
            combatant["hp"] = max(0, combatant["hp"] - dmg)
            narration.append(
                f"☠️ {combatant['display_name']} suffers {dmg} poison damage."
            )
        st["duration"] = int(st.get("duration", 1)) - 1
        if st["duration"] > 0:
            remaining.append(st)
    combatant["statuses"] = remaining


def _effective_stat(combatant: dict, stat: str) -> int:
    base = int(combatant.get(stat, 0))
    for st in _statuses_active(combatant):
        if st.get("name") == "buff" and st.get("stat") == stat:
            base += int(st.get("amount", 0))
        elif st.get("name") == "debuff" and st.get("stat") == stat:
            base -= int(st.get("amount", 0))
    return base


# --- Combatant construction --------------------------------------------------

def make_player_combatant(character: dict) -> dict:
    return {
        "kind": "player",
        "id": int(character["id"]),
        "user_id": int(character["user_id"]),
        "display_name": character.get("name") or character.get("user_name") or "Hero",
        "class_key": character.get("class_key", ""),
        "hp": int(character.get("hp", 0)),
        "max_hp": int(character.get("max_hp", 1)),
        "mana": int(character.get("mana", 0)),
        "max_mana": int(character.get("max_mana", 0)),
        "attack": int(character.get("attack", 0)),
        "defense": int(character.get("defense", 0)),
        "agility": int(character.get("agility", 0)),
        "statuses": _safe_json(character.get("status_json"), []),
        "cooldowns": _safe_json(character.get("cooldowns_json"), {}),
        "shield": 0,
    }


def make_enemy_combatant(enemy_row: dict, *, instance_index: int = 0) -> dict:
    name = enemy_row["name"]
    if instance_index > 0:
        name = f"{name} {instance_index + 1}"
    return {
        "kind": "enemy",
        "enemy_key": enemy_row["enemy_key"],
        "instance_id": f"{enemy_row['enemy_key']}#{instance_index}",
        "display_name": name,
        "hp": int(enemy_row["hp"]),
        "max_hp": int(enemy_row["hp"]),
        "mana": 0,
        "max_mana": 0,
        "attack": int(enemy_row["attack"]),
        "defense": int(enemy_row["defense"]),
        "agility": int(enemy_row["agility"]),
        "abilities": _safe_json(enemy_row.get("abilities_json"), []),
        "loot": _safe_json(enemy_row.get("loot_json"), []),
        "xp_reward": int(enemy_row.get("xp_reward", 0)),
        "statuses": [],
        "cooldowns": {},
        "shield": 0,
    }


def _safe_json(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


# --- Initiative --------------------------------------------------------------

def roll_initiative(combatants: Sequence[dict],
                    rng: random.Random | None = None) -> list[dict]:
    """Return ordered list of {id, kind, init} descending by initiative."""
    rng = rng or random
    order = []
    for c in combatants:
        init = d20(rng) + int(c.get("agility", 0))
        order.append({"ref": _ref_of(c), "init": init, "kind": c["kind"]})
    order.sort(key=lambda x: x["init"], reverse=True)
    return order


def _ref_of(combatant: dict) -> str:
    if combatant["kind"] == "player":
        return f"player:{combatant['id']}"
    return f"enemy:{combatant['instance_id']}"


def find_by_ref(ref: str, players: list[dict], enemies: list[dict]) -> Optional[dict]:
    kind, _, ident = ref.partition(":")
    if kind == "player":
        try:
            pid = int(ident)
        except ValueError:
            return None
        for p in players:
            if p["id"] == pid:
                return p
    elif kind == "enemy":
        for e in enemies:
            if e["instance_id"] == ident:
                return e
    return None


# --- Combat actions ----------------------------------------------------------

@dataclass
class CombatResult:
    narration: list[str] = field(default_factory=list)
    ended: bool = False
    victory: bool = False
    xp_gained: int = 0
    loot: list[dict] = field(default_factory=list)


def basic_attack(actor: dict, target: dict, *,
                 rng: random.Random | None = None) -> list[str]:
    rng = rng or random
    lines: list[str] = []
    if target["hp"] <= 0:
        return lines
    roll = d20(rng)
    atk_bonus = _effective_stat(actor, "attack")
    def_bonus = _effective_stat(target, "defense")
    dc = 10 + def_bonus
    total = roll + atk_bonus
    if roll == 1:
        lines.append(f"⚔️ {actor['display_name']} attacks {target['display_name']} — natural 1, critical miss!")
        return lines
    if total < dc and roll != 20:
        lines.append(
            f"⚔️ {actor['display_name']} swings at {target['display_name']} "
            f"({total} vs DC {dc}) — misses."
        )
        return lines
    dmg = atk_bonus + rng.randint(1, 6)  # d6 weapon
    crit = roll == 20
    if crit:
        dmg *= 2
    dmg = max(1, dmg)
    dmg = _absorb_shield(target, dmg, lines)
    target["hp"] = max(0, target["hp"] - dmg)
    suffix = " (CRIT!)" if crit else ""
    lines.append(
        f"⚔️ {actor['display_name']} hits {target['display_name']} for **{dmg}**{suffix}. "
        f"({target['display_name']}: {target['hp']}/{target['max_hp']} HP)"
    )
    return lines


def _absorb_shield(target: dict, dmg: int, lines: list[str]) -> int:
    shield = int(target.get("shield", 0))
    if shield <= 0:
        return dmg
    absorbed = min(shield, dmg)
    target["shield"] = shield - absorbed
    if absorbed > 0:
        lines.append(
            f"🛡️ Arcane shield absorbs {absorbed} damage "
            f"(remaining: {target['shield']})."
        )
    return dmg - absorbed


def use_ability(actor: dict, ability: dict, targets: list[dict], *,
                rng: random.Random | None = None) -> list[str]:
    rng = rng or random
    lines: list[str] = []
    effect = ability.get("effect", {})
    etype = effect.get("type")
    name = ability.get("name", ability.get("key", "Ability"))
    cost = int(ability.get("mana_cost", 0))
    if actor["mana"] < cost:
        lines.append(f"💢 {actor['display_name']} lacks mana for {name}.")
        return lines
    actor["mana"] -= cost
    cd = int(ability.get("cooldown", 0))
    if cd > 0:
        actor.setdefault("cooldowns", {})[ability["key"]] = cd + 1  # +1 so next turn it ticks down

    lines.append(f"✨ {actor['display_name']} uses **{name}**.")

    if etype == "damage":
        bonus_dice = effect.get("bonus_dice", "1d4")
        stat = effect.get("stat")
        max_targets = int(effect.get("max_targets", 0)) or len(targets)
        crit_bonus = float(effect.get("crit_bonus", 0))
        applied = 0
        for tgt in targets:
            if tgt["hp"] <= 0 or applied >= max_targets:
                continue
            applied += 1
            roll = d20(rng)
            atk_bonus = _effective_stat(actor, "attack") if stat == "attack" else 0
            def_bonus = _effective_stat(tgt, "defense")
            dc = 10 + def_bonus
            total = roll + atk_bonus
            crit = roll == 20 or (crit_bonus > 0 and rng.random() < crit_bonus)
            if total < dc and roll != 20 and not crit:
                lines.append(
                    f"  ↳ {name} misses {tgt['display_name']} "
                    f"({total} vs DC {dc})."
                )
                continue
            dmg = roll_dice(bonus_dice, rng=rng) + atk_bonus
            if crit:
                dmg *= 2
            dmg = max(1, dmg)
            dmg = _absorb_shield(tgt, dmg, lines)
            tgt["hp"] = max(0, tgt["hp"] - dmg)
            suffix = " (CRIT!)" if crit else ""
            lines.append(
                f"  ↳ {tgt['display_name']} takes **{dmg}** damage{suffix}. "
                f"({tgt['hp']}/{tgt['max_hp']} HP)"
            )
            status = effect.get("status")
            if status and rng.random() <= float(status.get("chance", 1.0)):
                _add_status(tgt, {
                    "name": status["name"],
                    "duration": int(status.get("duration", 1)),
                    "damage": int(status.get("damage", 0)),
                })
                lines.append(
                    f"  ↳ {tgt['display_name']} is now **{status['name']}** "
                    f"({status.get('duration', 1)} rounds)."
                )

    elif etype == "heal":
        amt = roll_dice(effect.get("dice", "2d6"), rng=rng)
        for tgt in targets:
            healed = min(tgt["max_hp"], tgt["hp"] + amt) - tgt["hp"]
            tgt["hp"] += healed
            lines.append(
                f"  ↳ {tgt['display_name']} restores **{healed}** HP "
                f"({tgt['hp']}/{tgt['max_hp']})."
            )

    elif etype == "buff" or etype == "debuff":
        for tgt in targets:
            _add_status(tgt, {
                "name": etype,
                "stat": effect.get("stat", "attack"),
                "amount": int(effect.get("amount", 1)),
                "duration": int(effect.get("duration", 2)),
            })
            verb = "empowered" if etype == "buff" else "weakened"
            lines.append(
                f"  ↳ {tgt['display_name']} is {verb} "
                f"({effect.get('stat', '')} {('+' if etype == 'buff' else '-')}"
                f"{effect.get('amount', 0)}, {effect.get('duration', 0)} rounds)."
            )

    elif etype == "shield":
        amt = int(effect.get("amount", 0))
        duration = int(effect.get("duration", 2))
        for tgt in targets:
            tgt["shield"] = tgt.get("shield", 0) + amt
            _add_status(tgt, {"name": "shielded", "duration": duration})
            lines.append(
                f"  ↳ {tgt['display_name']} is shielded for **{amt}** "
                f"({duration} rounds)."
            )
    else:
        lines.append(f"  ↳ ({etype or 'no-op'} effect resolves.)")

    return lines


def is_stunned(c: dict) -> bool:
    return _has_status(c, "stun")


def tick_round_start(combatants: list[dict], narration: list[str]) -> None:
    """Decrement cooldowns and apply status ticks at the start of a new round."""
    for c in combatants:
        if c["hp"] <= 0:
            continue
        for k in list(c.get("cooldowns", {}).keys()):
            c["cooldowns"][k] = max(0, c["cooldowns"][k] - 1)
            if c["cooldowns"][k] == 0:
                c["cooldowns"].pop(k, None)
        _tick_statuses(c, narration)


# --- Enemy AI ---------------------------------------------------------------

def enemy_choose_action(actor: dict, alive_players: list[dict],
                       alive_enemies: list[dict], *,
                       rng: random.Random | None = None) -> tuple[str, dict | None, list[dict]]:
    """Returns (action, ability_dict | None, list-of-targets)."""
    rng = rng or random
    if not alive_players:
        return ("idle", None, [])
    # 60% target lowest HP player, 40% random
    if rng.random() < 0.6:
        target = min(alive_players, key=lambda p: p["hp"])
    else:
        target = rng.choice(alive_players)
    # Pick a ready ability with affordable cost
    for ab in actor.get("abilities", []):
        cd = actor.get("cooldowns", {}).get(ab.get("key", ""), 0)
        if cd > 0:
            continue
        if int(ab.get("mana_cost", 0)) > actor["mana"]:
            continue
        if rng.random() < 0.5:
            tgt_kind = ab.get("target", "enemy")
            if tgt_kind == "all_enemies":
                return ("ability", ab, list(alive_players))
            if tgt_kind in ("ally", "self", "party"):
                return ("ability", ab, [actor])
            return ("ability", ab, [target])
    return ("basic", None, [target])


# --- Loot / XP ---------------------------------------------------------------

def roll_loot(enemies: list[dict], *,
              rng: random.Random | None = None) -> tuple[int, list[dict]]:
    rng = rng or random
    total_xp = 0
    drops: list[dict] = []
    for e in enemies:
        total_xp += int(e.get("xp_reward", 0))
        for entry in e.get("loot", []):
            chance = float(entry.get("chance", 1.0))
            if rng.random() <= chance:
                amount = int(entry.get("amount", 1))
                drops.append({"item_key": entry["item_key"], "amount": amount})
    return total_xp, drops


def xp_to_next(level: int) -> int:
    return 50 + level * 50


def apply_xp(character: dict, xp_gain: int) -> tuple[int, int]:
    """Mutates character; returns (new_level, leftover_xp)."""
    character["xp"] = int(character.get("xp", 0)) + xp_gain
    level = int(character.get("level", 1))
    while character["xp"] >= xp_to_next(level):
        character["xp"] -= xp_to_next(level)
        level += 1
        # Level-up bonuses (modest)
        character["max_hp"] = int(character.get("max_hp", 10)) + 5
        character["hp"] = character["max_hp"]
        character["max_mana"] = int(character.get("max_mana", 5)) + 2
        character["mana"] = character["max_mana"]
        character["attack"] = int(character.get("attack", 4)) + 1
        character["defense"] = int(character.get("defense", 3)) + 1
        if level % 2 == 0:
            character["agility"] = int(character.get("agility", 3)) + 1
    character["level"] = level
    return level, character["xp"]


# --- Scene helpers -----------------------------------------------------------

def scene_data(scene: dict) -> dict:
    return _safe_json(scene.get("data_json"), {})


def scene_choices(scene: dict) -> list[dict]:
    """Returns the choices list, with normalised keys 'label' and 'next'."""
    data = scene_data(scene)
    out = []
    for c in data.get("choices") or []:
        out.append({
            "label": c.get("label") or c.get("text") or "Continue",
            "next": c.get("next") or c.get("next_scene") or "",
            "requires": c.get("requires") or {},
        })
    return out


# --- Public reporting --------------------------------------------------------

def render_combatant_status(c: dict) -> str:
    hp_bar = f"{c['hp']}/{c['max_hp']} HP"
    mana = f" • {c['mana']}/{c['max_mana']} MP" if c.get("max_mana") else ""
    statuses = ", ".join(s["name"] for s in _statuses_active(c)) or "—"
    return f"**{c['display_name']}** — {hp_bar}{mana} • status: {statuses}"
