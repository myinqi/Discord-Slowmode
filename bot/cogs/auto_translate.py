import asyncio
import discord
from discord.ext import commands

_LANG_META: dict[str, tuple[str, str]] = {
    "en": ("English",    "🇬🇧"),
    "de": ("German",     "🇩🇪"),
    "fr": ("French",     "🇫🇷"),
    "es": ("Spanish",    "🇪🇸"),
    "it": ("Italian",    "🇮🇹"),
    "pt": ("Portuguese", "🇵🇹"),
    "nl": ("Dutch",      "🇳🇱"),
    "ru": ("Russian",    "🇷🇺"),
    "no": ("Norwegian",  "🇳🇴"),
    "ja": ("Japanese",   "🇯🇵"),
    "sv": ("Swedish",    "🇸🇪"),
    "pl": ("Polish",     "🇵🇱"),
    "tr": ("Turkish",    "🇹🇷"),
    "ko": ("Korean",     "🇰🇷"),
    "zh": ("Chinese",    "🇨🇳"),
}

_TRANSLATE_TIMEOUT = 10.0


class AutoTranslateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        text = (message.content or "").strip()
        if not text:
            return

        db = self.bot.db

        enabled = await db.get_setting("auto_translate_enabled")
        if enabled != "on":
            return

        channel_id_str = await db.get_setting("auto_translate_channel_id")
        if not channel_id_str:
            return
        try:
            channel_id = int(channel_id_str)
        except (ValueError, TypeError):
            return

        if message.channel.id != channel_id:
            return

        langs_str = await db.get_setting("auto_translate_languages") or ""
        langs = [l.strip().lower() for l in langs_str.split(",") if l.strip()]
        if not langs:
            return

        try:
            from deep_translator import GoogleTranslator
        except ImportError:
            print("[auto_translate] deep_translator not installed", flush=True)
            return

        loop = asyncio.get_event_loop()
        author_name = message.author.display_name

        for lang in langs:
            try:
                translated = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda l=lang: GoogleTranslator(source="auto", target=l).translate(text),
                    ),
                    timeout=_TRANSLATE_TIMEOUT,
                )
                if not translated or translated.strip() == text.strip():
                    continue
                flag = _LANG_META.get(lang, ("", ""))[1] or f"`{lang}`"
                await message.channel.send(f"**{author_name}** {flag} {translated}")
            except asyncio.TimeoutError:
                print(f"[auto_translate] Timeout for lang={lang}", flush=True)
            except Exception as e:
                print(f"[auto_translate] Error for lang={lang}: {e}", flush=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoTranslateCog(bot))
