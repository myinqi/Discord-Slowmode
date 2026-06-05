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
        if mode not in ("film", "music", "mixed"):
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
            mode_name = "any" if mode == "mixed" else mode
            await interaction.response.send_message(
                f"No {mode_name} quiz questions are configured yet.",
                ephemeral=True,
            )
            return

        answers = _answers_from_question(question)
        mode_label = "Film Quiz" if question["mode"] == "film" else "Music Quiz"
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

    @app_commands.command(name="quiz-highscore", description="Show the private quiz top 10 highscore")
    async def quiz_highscore(self, interaction: discord.Interaction):
        rows = await self.bot.db.get_quiz_highscore(limit=10)
        if not rows:
            await interaction.response.send_message("No quiz scores yet.", ephemeral=True)
            return

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for idx, row in enumerate(rows, start=1):
            rank = medals[idx - 1] if idx <= len(medals) else f"**{idx}.**"
            member = interaction.guild.get_member(row["user_id"]) if interaction.guild else None
            name = member.display_name if member else row["user_name"]
            point_label = "point" if row["points"] == 1 else "points"
            lines.append(f"{rank} **{name}** — {row['points']} {point_label}")

        embed = discord.Embed(
            title="Quiz Highscore",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Only you can see this highscore.")
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
        total_points = await self.bot.db.increment_quiz_score(
            message.author.id,
            message.author.display_name,
        )

        embed = discord.Embed(
            title="Quiz Solved",
            description=(
                f"{message.author.mention} solved the quiz.\n"
                f"Correct answer: **{correct_answer}**\n"
                f"Score: **{total_points}**"
            ),
            color=discord.Color.green(),
        )
        embed.timestamp = discord.utils.utcnow()
        await message.channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuizCog(bot))
