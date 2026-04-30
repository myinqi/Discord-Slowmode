import discord
from discord.ext import commands


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
        cfg = await self.bot.db.get_reaction_role(payload.message_id, emoji_str)
        if not cfg:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        role = guild.get_role(cfg["role_id"])
        if not role:
            return

        try:
            await payload.member.add_roles(role, reason="Reaction-Role (add)")
        except discord.Forbidden:
            pass
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return

        emoji_str = str(payload.emoji)
        cfg = await self.bot.db.get_reaction_role(payload.message_id, emoji_str)
        if not cfg:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        role = guild.get_role(cfg["role_id"])
        if not role:
            return

        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return

        try:
            await member.remove_roles(role, reason="Reaction-Role (remove)")
        except discord.Forbidden:
            pass
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRolesCog(bot))
