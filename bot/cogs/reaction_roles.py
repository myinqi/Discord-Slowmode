import logging
import discord
from discord.ext import commands

log = logging.getLogger(__name__)


class ReactionRolesCog(commands.Cog):
    """Cog that handles reaction-based role assignment (toggle behavior)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id or payload.member is None:
            return
        if payload.member.bot:
            return

        emoji_str = str(payload.emoji)
        log.info("[RR-add] msg=%s emoji=%r user=%s", payload.message_id, emoji_str, payload.member)
        cfg = await self.bot.db.get_reaction_role(payload.message_id, emoji_str)
        if not cfg:
            log.info("[RR-add] no config found for this message+emoji")
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            log.warning("[RR-add] guild %s not found in cache", payload.guild_id)
            return
        role = guild.get_role(cfg["role_id"])
        if not role:
            log.warning("[RR-add] role %s not found in guild", cfg["role_id"])
            return

        log.info("[RR-add] assigning role %s to %s", role.name, payload.member)
        try:
            await payload.member.add_roles(role, reason="Reaction-Role (add)")
            log.info("[RR-add] success")
        except discord.Forbidden as ex:
            log.error("[RR-add] Forbidden – missing Manage Roles or role hierarchy issue: %s", ex)
        except Exception as ex:
            log.error("[RR-add] unexpected error: %s", ex)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return

        emoji_str = str(payload.emoji)
        log.info("[RR-remove] msg=%s emoji=%r user_id=%s", payload.message_id, emoji_str, payload.user_id)
        cfg = await self.bot.db.get_reaction_role(payload.message_id, emoji_str)
        if not cfg:
            log.info("[RR-remove] no config found for this message+emoji")
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            log.warning("[RR-remove] guild %s not found in cache", payload.guild_id)
            return
        role = guild.get_role(cfg["role_id"])
        if not role:
            log.warning("[RR-remove] role %s not found in guild", cfg["role_id"])
            return

        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            log.info("[RR-remove] member not in cache or is bot (user_id=%s)", payload.user_id)
            return

        log.info("[RR-remove] removing role %s from %s", role.name, member)
        try:
            await member.remove_roles(role, reason="Reaction-Role (remove)")
            log.info("[RR-remove] success")
        except discord.Forbidden as ex:
            log.error("[RR-remove] Forbidden – missing Manage Roles or role hierarchy issue: %s", ex)
        except Exception as ex:
            log.error("[RR-remove] unexpected error: %s", ex)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRolesCog(bot))
