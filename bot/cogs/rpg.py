"""Discord slash commands for the RPG system.

All commands live under the `/rpg` group. The cog wires the database +
session orchestrator + LLM client together and renders results as Discord
embeds.

Channel restriction: if `rpg_channel_id` setting is configured, commands
will refuse to run elsewhere (with the exception of ephemeral lookups like
`sheet`).
"""

from __future__ import annotations

import json
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import rpg_session
from bot.llm import OllamaClient
from config import Config


# --- Helpers -----------------------------------------------------------------

DICE_COLOR = discord.Color.from_rgb(140, 90, 200)


async def _ensure_tables(db) -> None:
    await db.ensure_rpg_tables()


async def _channel_ok(db, interaction: discord.Interaction) -> bool:
    """Returns True if the command is allowed in this channel."""
    setting = (await db.get_setting("rpg_channel_id") or "").strip()
    if not setting or not setting.isdigit():
        return True
    return interaction.channel_id == int(setting)


async def _system_blocked_reason(db) -> Optional[str]:
    """Return a user-facing reason string if the RPG system is disabled
    (master toggle off, or auto-pause active while the stream is live).
    Returns None if play is allowed.
    """
    if (await db.get_setting("rpg_enabled")) == "false":
        return ("🛑 The RPG system is currently disabled by an administrator. "
                "Please try again later.")
    if (await db.get_setting("rpg_block_during_stream")) != "false":
        try:
            from bot.exp_stream_manager import stream_is_live
            if stream_is_live:
                return ("📻 The RPG is paused while the Exp. Radio Twitch "
                        "stream is live, to keep the stream stable. Try again "
                        "after the stream ends.")
        except Exception:
            pass
    return None


async def _guard(interaction: discord.Interaction, db,
                 *, check_channel: bool = True) -> bool:
    """Run the standard pre-command checks. Returns True if the command may
    proceed, otherwise replies to the user and returns False.
    """
    reason = await _system_blocked_reason(db)
    if reason:
        await interaction.response.send_message(reason, ephemeral=True)
        return False
    if check_channel and not await _channel_ok(db, interaction):
        await interaction.response.send_message(
            "This command is restricted to the configured RPG channel.",
            ephemeral=True,
        )
        return False
    return True


def _hp_bar(cur: int, mx: int, width: int = 12) -> str:
    if mx <= 0:
        return "—"
    filled = max(0, min(width, round(width * cur / mx)))
    return "█" * filled + "░" * (width - filled)


def _char_embed(character: dict, class_row: Optional[dict]) -> discord.Embed:
    title = f"{character['name']} — {(class_row or {}).get('name', character['class_key'].title())}"
    e = discord.Embed(
        title=title,
        description=(class_row or {}).get("description") or "",
        color=DICE_COLOR,
    )
    e.add_field(name="Level", value=str(character["level"]), inline=True)
    e.add_field(name="XP", value=str(character["xp"]), inline=True)
    e.add_field(name="Party", value=str(character.get("party_id") or "—"), inline=True)
    e.add_field(
        name="HP",
        value=f"`{_hp_bar(character['hp'], character['max_hp'])}` "
              f"{character['hp']}/{character['max_hp']}",
        inline=False,
    )
    if character["max_mana"] > 0:
        e.add_field(
            name="Mana",
            value=f"`{_hp_bar(character['mana'], character['max_mana'])}` "
                  f"{character['mana']}/{character['max_mana']}",
            inline=False,
        )
    e.add_field(name="ATK", value=str(character["attack"]), inline=True)
    e.add_field(name="DEF", value=str(character["defense"]), inline=True)
    e.add_field(name="AGI", value=str(character["agility"]), inline=True)

    inv = []
    try:
        inv = json.loads(character.get("inventory_json") or "[]")
    except (ValueError, TypeError):
        pass
    if inv:
        inv_lines = [f"• {it.get('amount', 1)}× `{it.get('item_key')}`" for it in inv]
        e.add_field(name="Inventory", value="\n".join(inv_lines), inline=False)

    if class_row:
        abilities = json.loads(class_row.get("abilities_json") or "[]")
        if abilities:
            ab_lines = []
            for ab in abilities:
                ab_lines.append(
                    f"• **{ab['name']}** (`{ab['key']}`) — "
                    f"{ab['description']} _[cost {ab.get('mana_cost', 0)} MP"
                    f"{', cd ' + str(ab['cooldown']) if ab.get('cooldown') else ''}]_"
                )
            e.add_field(name="Abilities", value="\n".join(ab_lines), inline=False)

    return e


def _scene_embed(scene_payload: dict) -> discord.Embed:
    e = discord.Embed(
        title=scene_payload.get("title") or "Scene",
        description=scene_payload.get("narration") or "",
        color=discord.Color.blurple(),
    )
    if scene_payload.get("intro"):
        e.add_field(name="Intro", value=scene_payload["intro"][:1000], inline=False)
    if scene_payload.get("choices"):
        lines = []
        for idx, c in enumerate(scene_payload["choices"], start=1):
            lines.append(f"**{idx}.** {c['label']} → `{c['next']}`")
        e.add_field(name="Choices", value="\n".join(lines), inline=False)
    e.set_footer(text=f"scene: {scene_payload.get('scene_key', '')} "
                      f"• type: {scene_payload.get('scene_type', 'story')}")
    return e


def _split_narration(lines: list[str], limit: int = 1900) -> list[str]:
    """Pack narration lines into chunks below the Discord per-message limit."""
    chunks: list[str] = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            if buf:
                chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


# --- Cog ---------------------------------------------------------------------

class RPGCog(commands.Cog):
    rpg_group = app_commands.Group(name="rpg",
                                   description="Multiuser RPG commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.llm_client = OllamaClient(
            base_url=Config.OLLAMA_URL,
            model=Config.LLM_MODEL,
            timeout=Config.LLM_REQUEST_TIMEOUT,
        )

    async def cog_load(self) -> None:
        await _ensure_tables(self.bot.db)

    # ---- Character ----

    @rpg_group.command(name="classes",
                       description="List the available character classes.")
    async def cmd_classes(self, interaction: discord.Interaction):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        classes = await self.bot.db.rpg_list_classes()
        if not classes:
            await interaction.response.send_message("No classes configured.",
                                                    ephemeral=True)
            return
        embed = discord.Embed(
            title="Character Classes",
            color=DICE_COLOR,
        )
        for c in classes:
            embed.add_field(
                name=f"{c['name']} (`{c['class_key']}`)",
                value=(f"{c.get('description') or '_no description_'}\n"
                       f"HP {c['base_hp']} · ATK {c['base_attack']} · "
                       f"DEF {c['base_defense']} · AGI {c['base_agility']} · "
                       f"MP {c['base_mana']}"),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rpg_group.command(name="create",
                       description="Create your character.")
    @app_commands.describe(name="Display name",
                           class_key="Class key (see /rpg classes)")
    async def cmd_create(self, interaction: discord.Interaction,
                         name: str, class_key: str):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        existing = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"You already have a character: **{existing['name']}**. "
                "Use `/rpg delete-character` first to start over.",
                ephemeral=True,
            )
            return
        class_row = await self.bot.db.rpg_get_class(class_key.strip().lower())
        if not class_row:
            await interaction.response.send_message(
                f"Unknown class `{class_key}`. Use `/rpg classes` to see options.",
                ephemeral=True,
            )
            return
        name_clean = name.strip()[:32]
        if not name_clean:
            await interaction.response.send_message("Name cannot be empty.",
                                                    ephemeral=True)
            return
        cid = await self.bot.db.rpg_create_character(
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
            name=name_clean,
            class_key=class_row["class_key"],
            max_hp=class_row["base_hp"],
            max_mana=class_row["base_mana"],
            attack=class_row["base_attack"],
            defense=class_row["base_defense"],
            agility=class_row["base_agility"],
        )
        character = await self.bot.db.rpg_get_character(cid)
        await interaction.response.send_message(
            f"🎉 **{name_clean}** the {class_row['name']} has joined the world!",
            embed=_char_embed(character, class_row),
        )

    @rpg_group.command(name="sheet", description="Show your character sheet.")
    @app_commands.describe(user="Show another player's sheet (optional).")
    async def cmd_sheet(self, interaction: discord.Interaction,
                        user: Optional[discord.User] = None):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        target_id = (user or interaction.user).id
        char = await self.bot.db.rpg_get_character_by_user(target_id)
        if not char:
            await interaction.response.send_message(
                "No character found. Use `/rpg create` first.",
                ephemeral=True,
            )
            return
        class_row = await self.bot.db.rpg_get_class(char["class_key"])
        await interaction.response.send_message(
            embed=_char_embed(char, class_row), ephemeral=True
        )

    @rpg_group.command(name="delete-character",
                       description="Permanently delete your character.")
    async def cmd_delete_character(self, interaction: discord.Interaction):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char:
            await interaction.response.send_message("You have no character.",
                                                    ephemeral=True)
            return
        await self.bot.db.rpg_delete_character(char["id"])
        await interaction.response.send_message(
            f"🪦 **{char['name']}** has been laid to rest.", ephemeral=True
        )

    # ---- Parties ----

    @rpg_group.command(name="party-create",
                       description="Create a new party (you become leader).")
    @app_commands.describe(name="Party name")
    async def cmd_party_create(self, interaction: discord.Interaction, name: str):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char:
            await interaction.response.send_message(
                "Create a character first with `/rpg create`.", ephemeral=True
            )
            return
        if char.get("party_id"):
            await interaction.response.send_message(
                f"You are already in party #{char['party_id']}. "
                "Use `/rpg party-leave` first.",
                ephemeral=True,
            )
            return
        pid = await self.bot.db.rpg_create_party(
            name=name.strip()[:48],
            leader_user_id=interaction.user.id,
            channel_id=interaction.channel_id,
        )
        await self.bot.db.rpg_update_character(char["id"], party_id=pid)
        await interaction.response.send_message(
            f"🤝 Party **{name}** (#{pid}) created. "
            f"Other adventurers can join with `/rpg party-join party_id:{pid}`."
        )

    @rpg_group.command(name="party-join",
                       description="Join an existing party by id.")
    @app_commands.describe(party_id="The party id to join")
    async def cmd_party_join(self, interaction: discord.Interaction, party_id: int):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char:
            await interaction.response.send_message(
                "Create a character first with `/rpg create`.", ephemeral=True
            )
            return
        if char.get("party_id"):
            await interaction.response.send_message(
                f"You are already in party #{char['party_id']}.", ephemeral=True
            )
            return
        party = await self.bot.db.rpg_get_party(party_id)
        if not party:
            await interaction.response.send_message("Party not found.",
                                                    ephemeral=True)
            return
        if party["state"] not in ("idle", "exploring"):
            await interaction.response.send_message(
                f"Party is currently `{party['state']}` and cannot accept new members.",
                ephemeral=True,
            )
            return
        members = await self.bot.db.rpg_get_party_members(party_id)
        if len(members) >= 6:
            await interaction.response.send_message(
                "Party is full (max 6 members).", ephemeral=True
            )
            return
        await self.bot.db.rpg_update_character(char["id"], party_id=party_id)
        await interaction.response.send_message(
            f"🤝 **{char['name']}** joined party **{party['name']}** (#{party_id})."
        )

    @rpg_group.command(name="party-leave",
                       description="Leave your current party.")
    async def cmd_party_leave(self, interaction: discord.Interaction):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char or not char.get("party_id"):
            await interaction.response.send_message("You are not in a party.",
                                                    ephemeral=True)
            return
        party_id = char["party_id"]
        await self.bot.db.rpg_update_character(char["id"], party_id=None)
        party = await self.bot.db.rpg_get_party(party_id)
        # If leader left, disband or hand off to first remaining member
        if party and party["leader_user_id"] == interaction.user.id:
            remaining = await self.bot.db.rpg_get_party_members(party_id)
            if not remaining:
                await self.bot.db.rpg_delete_party(party_id)
                await interaction.response.send_message(
                    f"🗑️ Party **{party['name']}** disbanded (last member left)."
                )
                return
            new_leader = remaining[0]
            await self.bot.db.rpg_update_party(
                party_id, leader_user_id=new_leader["user_id"]
            )
            await interaction.response.send_message(
                f"👋 {char['name']} left. **{new_leader['name']}** is the new leader."
            )
            return
        await interaction.response.send_message(
            f"👋 **{char['name']}** left party #{party_id}."
        )

    @rpg_group.command(name="party-list",
                       description="List active parties.")
    async def cmd_party_list(self, interaction: discord.Interaction):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        parties = await self.bot.db.rpg_list_parties()
        if not parties:
            await interaction.response.send_message("No active parties.",
                                                    ephemeral=True)
            return
        lines = []
        for p in parties[:20]:
            members = await self.bot.db.rpg_get_party_members(p["id"])
            lines.append(
                f"**#{p['id']}** {p['name']} — {len(members)} member(s) · "
                f"state: `{p['state']}`"
            )
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    @rpg_group.command(name="party-status",
                       description="Show your party's current status.")
    async def cmd_party_status(self, interaction: discord.Interaction):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char or not char.get("party_id"):
            await interaction.response.send_message("You are not in a party.",
                                                    ephemeral=True)
            return
        text = await rpg_session.party_status(self.bot.db, char["party_id"])
        await interaction.response.send_message(text, ephemeral=False)

    # ---- Adventure flow ----

    @rpg_group.command(name="adventures",
                       description="List available adventures.")
    async def cmd_adventures(self, interaction: discord.Interaction):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        advs = [a for a in await self.bot.db.rpg_list_adventures() if a["is_active"]]
        if not advs:
            await interaction.response.send_message(
                "No active adventures. Ask an admin to create one.",
                ephemeral=True,
            )
            return
        lines = [
            f"**#{a['id']}** {a['name']} — {a['description'] or '_no description_'}"
            for a in advs
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @rpg_group.command(name="start",
                       description="Leader: start an adventure for the party.")
    @app_commands.describe(adventure_id="The adventure id to start")
    async def cmd_start(self, interaction: discord.Interaction, adventure_id: int):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char or not char.get("party_id"):
            await interaction.response.send_message(
                "Join or create a party first.", ephemeral=True
            )
            return
        party = await self.bot.db.rpg_get_party(char["party_id"])
        if party["leader_user_id"] != interaction.user.id:
            await interaction.response.send_message(
                "Only the party leader can start an adventure.", ephemeral=True
            )
            return
        await interaction.response.defer()
        result = await rpg_session.start_adventure(
            self.bot.db, party["id"], adventure_id
        )
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error')}")
            return
        await interaction.followup.send(
            f"📜 The party **{party['name']}** begins their adventure!",
            embed=_scene_embed(result["scene"]),
        )

    @rpg_group.command(name="scene",
                       description="Show the current scene.")
    async def cmd_scene(self, interaction: discord.Interaction):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char or not char.get("party_id"):
            await interaction.response.send_message("You are not in a party.",
                                                    ephemeral=True)
            return
        scene = await rpg_session.current_scene(self.bot.db, char["party_id"])
        if not scene:
            await interaction.response.send_message(
                "No active scene. Has the leader started an adventure?",
                ephemeral=True,
            )
            return
        payload = rpg_session._render_scene(scene)
        await interaction.response.send_message(embed=_scene_embed(payload))

    @rpg_group.command(name="choose",
                       description="Pick a scripted choice (1-based number).")
    @app_commands.describe(choice="Choice number from the scene")
    async def cmd_choose(self, interaction: discord.Interaction, choice: int):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char or not char.get("party_id"):
            await interaction.response.send_message("You are not in a party.",
                                                    ephemeral=True)
            return
        party = await self.bot.db.rpg_get_party(char["party_id"])
        if party["leader_user_id"] != interaction.user.id:
            await interaction.response.send_message(
                "Only the party leader can make scripted choices.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        result = await rpg_session.advance_scene(
            self.bot.db, char["party_id"], choice - 1
        )
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error')}")
            return
        await interaction.followup.send(embed=_scene_embed(result["scene"]))
        if result.get("combat"):
            combat = result["combat"]
            for chunk in _split_narration(combat.get("narration", [])):
                await interaction.followup.send(chunk)
            await self._announce_turn(interaction, char["party_id"], combat.get("turn"))

    @rpg_group.command(name="say",
                       description="Take a free-text action; the GM will narrate.")
    @app_commands.describe(text="What does your character do/say?")
    async def cmd_say(self, interaction: discord.Interaction, text: str):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char or not char.get("party_id"):
            await interaction.response.send_message("You are not in a party.",
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        result = await rpg_session.handle_free_text(
            self.bot.db, char["party_id"], char["id"], text,
            llm_client=self.llm_client,
        )
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error')}")
            return
        embed = discord.Embed(
            title=f"{char['name']}'s action",
            description=f"_{text[:200]}_",
            color=discord.Color.green(),
        )
        embed.add_field(name="GM", value=result["narration"][:1024], inline=False)
        if result.get("applied_effect"):
            embed.set_footer(text=f"Effect applied: {result['applied_effect']}")
        await interaction.followup.send(embed=embed)
        if result.get("applied_effect", {}).get("type") == "advance":
            scene = await rpg_session.current_scene(self.bot.db, char["party_id"])
            if scene:
                await interaction.followup.send(
                    embed=_scene_embed(rpg_session._render_scene(scene))
                )

    # ---- Combat ----

    @rpg_group.command(name="attack",
                       description="Combat: basic melee/ranged attack on an enemy.")
    @app_commands.describe(target="Enemy instance id (see /rpg status)")
    async def cmd_attack(self, interaction: discord.Interaction, target: str):
        await self._do_combat_action(interaction, ability_key=None,
                                     target=target)

    @rpg_group.command(name="ability",
                       description="Combat: use one of your class abilities.")
    @app_commands.describe(ability="Ability key (see /rpg sheet)",
                           target="Optional target id")
    async def cmd_ability(self, interaction: discord.Interaction,
                          ability: str, target: Optional[str] = None):
        await self._do_combat_action(interaction, ability_key=ability,
                                     target=target)

    async def _do_combat_action(self, interaction: discord.Interaction, *,
                                ability_key: Optional[str],
                                target: Optional[str]):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char or not char.get("party_id"):
            await interaction.response.send_message("You are not in a party.",
                                                    ephemeral=True)
            return
        target_ref = self._normalise_target(target) if target else None
        await interaction.response.defer()
        if ability_key:
            result = await rpg_session.player_use_ability(
                self.bot.db, char["party_id"], char["id"],
                ability_key.strip().lower(), target_ref,
            )
        else:
            result = await rpg_session.player_basic_attack(
                self.bot.db, char["party_id"], char["id"], target_ref or "",
            )
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error')}")
            return
        for chunk in _split_narration(result.get("narration", [])):
            await interaction.followup.send(chunk)
        await self._announce_turn(interaction, char["party_id"],
                                  result.get("turn"))

    @staticmethod
    def _normalise_target(target: str) -> str:
        target = (target or "").strip()
        if not target:
            return ""
        if ":" in target:
            return target
        # Convenience: "goblin#0" -> "enemy:goblin#0"; "1" -> "enemy:#1" (won't work)
        return f"enemy:{target}"

    @rpg_group.command(name="status",
                       description="Show party + combat status (HP, enemies, turn).")
    async def cmd_status(self, interaction: discord.Interaction):
        await _ensure_tables(self.bot.db)
        if not await _guard(interaction, self.bot.db, check_channel=False):
            return
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char or not char.get("party_id"):
            await interaction.response.send_message("You are not in a party.",
                                                    ephemeral=True)
            return
        text = await rpg_session.party_status(self.bot.db, char["party_id"])
        combat = await self.bot.db.rpg_get_combat(char["party_id"])
        if combat:
            initiative = json.loads(combat["initiative_json"])
            turn_index = int(combat["turn_index"]) % max(1, len(initiative))
            current_ref = initiative[turn_index]["ref"] if initiative else None
            enemies = json.loads(combat["enemies_json"])
            target_lines = ["", "**Enemy refs (for targeting):**"]
            for e in enemies:
                if e["hp"] <= 0:
                    target_lines.append(f"• ~~{e['instance_id']}~~ (defeated)")
                else:
                    target_lines.append(
                        f"• `{e['instance_id']}` — {e['display_name']} "
                        f"({e['hp']}/{e['max_hp']} HP)"
                    )
            target_lines.append(f"\nCurrent turn: `{current_ref}`")
            text += "\n" + "\n".join(target_lines)
        await interaction.response.send_message(text, ephemeral=True)

    async def _announce_turn(self, interaction: discord.Interaction,
                             party_id: int, turn_info: Optional[dict]) -> None:
        if not turn_info:
            return
        if turn_info.get("ended"):
            return
        if turn_info.get("actor_kind") == "player":
            user_id = turn_info.get("actor_user_id")
            await interaction.followup.send(
                f"🎯 <@{user_id}> — it's your turn "
                f"(round {turn_info.get('round', '?')}). "
                f"Use `/rpg attack` or `/rpg ability`."
            )

    # ---- Autocomplete ----

    @cmd_create.autocomplete("class_key")
    async def class_autocomplete(self, interaction: discord.Interaction,
                                 current: str):
        await _ensure_tables(self.bot.db)
        rows = await self.bot.db.rpg_list_classes()
        cur = (current or "").lower()
        out = []
        for r in rows:
            if cur in r["class_key"].lower() or cur in r["name"].lower():
                out.append(app_commands.Choice(
                    name=f"{r['name']} ({r['class_key']})",
                    value=r["class_key"],
                ))
            if len(out) >= 25:
                break
        return out

    @cmd_ability.autocomplete("ability")
    async def ability_autocomplete(self, interaction: discord.Interaction,
                                   current: str):
        await _ensure_tables(self.bot.db)
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char:
            return []
        class_row = await self.bot.db.rpg_get_class(char["class_key"])
        if not class_row:
            return []
        abilities = json.loads(class_row.get("abilities_json") or "[]")
        cur = (current or "").lower()
        out = []
        for a in abilities:
            if cur in a.get("key", "").lower() or cur in a.get("name", "").lower():
                out.append(app_commands.Choice(
                    name=a.get("name") or a.get("key"),
                    value=a["key"],
                ))
        return out[:25]

    @cmd_attack.autocomplete("target")
    @cmd_ability.autocomplete("target")
    async def target_autocomplete(self, interaction: discord.Interaction,
                                  current: str):
        await _ensure_tables(self.bot.db)
        char = await self.bot.db.rpg_get_character_by_user(interaction.user.id)
        if not char or not char.get("party_id"):
            return []
        combat = await self.bot.db.rpg_get_combat(char["party_id"])
        if not combat:
            return []
        enemies = json.loads(combat["enemies_json"])
        out = []
        cur = (current or "").lower()
        for e in enemies:
            if e["hp"] <= 0:
                continue
            label = f"{e['display_name']} ({e['hp']}/{e['max_hp']})"
            if cur in e["instance_id"].lower() or cur in e["display_name"].lower():
                out.append(app_commands.Choice(name=label, value=e["instance_id"]))
        return out[:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(RPGCog(bot))
