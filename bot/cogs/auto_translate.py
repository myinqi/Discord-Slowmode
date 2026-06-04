import asyncio
import discord
from discord.ext import commands
from bot.llm import OllamaClient
from config import Config

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

_TRANSLATE_TIMEOUT_GOOGLE = 10.0
_TRANSLATE_TIMEOUT_LLM = 45.0

_LLM_SYSTEM = (
    "You are a professional translator. "
    "Translate the user's message to the requested language. "
    "Output ONLY the translated text — no explanations, no quotes, no preamble."
)


async def _translate_google(text: str, lang: str) -> str | None:
    from deep_translator import GoogleTranslator
    loop = asyncio.get_event_loop()
    result = await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: GoogleTranslator(source="auto", target=lang).translate(text),
        ),
        timeout=_TRANSLATE_TIMEOUT_GOOGLE,
    )
    return result or None


async def _translate_llm(client: OllamaClient, text: str, lang_name: str) -> str | None:
    messages = [
        {"role": "system", "content": _LLM_SYSTEM},
        {"role": "user", "content": f"Translate to {lang_name}:\n\n{text}"},
    ]
    resp = await asyncio.wait_for(
        client.chat(messages, max_tokens=1024, temperature=0.1, top_p=0.9),
        timeout=_TRANSLATE_TIMEOUT_LLM,
    )
    return ((resp.get("message") or {}).get("content") or "").strip() or None


class AutoTranslateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._llm_client = OllamaClient(
            base_url=Config.OLLAMA_URL,
            model=Config.LLM_MODEL,
            timeout=int(_TRANSLATE_TIMEOUT_LLM),
        )

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

        engine = (await db.get_setting("auto_translate_engine") or "google").strip()

        if engine == "llm":
            import bot.exp_stream_manager as _esm
            if _esm.stream_is_live:
                print("[auto_translate] skipped (stream is live, LLM disabled during stream)", flush=True)
                return

        author_name = message.author.display_name

        for lang in langs:
            try:
                meta = _LANG_META.get(lang, (lang.capitalize(), f"`{lang}`"))
                lang_name, flag = meta
                if engine == "llm":
                    translated = await _translate_llm(self._llm_client, text, lang_name)
                else:
                    translated = await _translate_google(text, lang)
                if not translated or translated.strip() == text.strip():
                    continue
                await message.channel.send(f"**{author_name}** {flag} {translated}")
            except asyncio.TimeoutError:
                print(f"[auto_translate] Timeout for lang={lang} engine={engine}", flush=True)
            except Exception as e:
                print(f"[auto_translate] Error for lang={lang} engine={engine}: {e}", flush=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoTranslateCog(bot))
