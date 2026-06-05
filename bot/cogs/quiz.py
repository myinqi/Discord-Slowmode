import re
import time

import discord
from discord import app_commands
from discord.ext import commands


def _normalize_answer(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^\w\s]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
    return value


def _answers_from_question(question: dict) -> list[str]:
    return [
        question["answer_1"],
        question["answer_2"],
        question["answer_3"],
        question["answer_4"],
        question["answer_5"],
    ]


class QuizCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_quizzes: dict[int, dict] = {}

    @app_commands.command(name="quiz", description="Post a random quiz question in the configured quiz channel")
    async def quiz(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        mode = (await self.bot.db.get_setting("quiz_mode") or "film").strip().lower()
        if mode not in ("film", "music"):
            mode = "film"

        channel_id_str = (await self.bot.db.get_setting("quiz_channel_id") or "").strip()
        if not channel_id_str.isdigit():
            await interaction.response.send_message(
                "No quiz channel is configured. Please set one in the admin UI.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(int(channel_id_str))
        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The configured quiz channel could not be found.",
                ephemeral=True,
            )
            return

        question = await self.bot.db.get_random_quiz_question(mode)
        if not question:
            await interaction.response.send_message(
                f"No {mode} quiz questions are configured yet.",
                ephemeral=True,
            )
            return

        answers = _answers_from_question(question)
        mode_label = "Film Quiz" if mode == "film" else "Music Quiz"
        options_text = "\n".join(f"**{idx}.** {answer}" for idx, answer in enumerate(answers, start=1))

        embed = discord.Embed(
            title=f"{mode_label} Question",
            description=f"**{question['question']}**\n\n{options_text}",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Type the correct answer in this channel.")
        embed.timestamp = discord.utils.utcnow()

        message = await channel.send(embed=embed)
        self.active_quizzes[channel.id] = {
            "question_id": question["id"],
            "message_id": message.id,
            "correct_answer": question["correct_answer"],
            "correct_answer_number": str(answers.index(question["correct_answer"]) + 1),
            "normalized_answer": _normalize_answer(question["correct_answer"]),
            "started_at": time.time(),
            "started_by": interaction.user.id,
        }

        if channel.id == interaction.channel_id:
            await interaction.response.send_message("Quiz question posted.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Quiz question posted in {channel.mention}.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        active = self.active_quizzes.get(message.channel.id)
        if not active:
            return

        submitted = _normalize_answer(message.content)
        if not submitted:
            return

        if submitted != active["normalized_answer"] and submitted != active["correct_answer_number"]:
            return

        correct_answer = active["correct_answer"]
        self.active_quizzes.pop(message.channel.id, None)

        embed = discord.Embed(
            title="Quiz Solved",
            description=f"{message.author.mention} solved the quiz.\nCorrect answer: **{correct_answer}**",
            color=discord.Color.green(),
        )
        embed.timestamp = discord.utils.utcnow()
        await message.channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuizCog(bot))
