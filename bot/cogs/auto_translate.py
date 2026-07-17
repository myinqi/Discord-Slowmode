import asyncio
import aiohttp
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
_TRANSLATE_TIMEOUT_OPENAI = 25.0
_TRANSLATE_TIMEOUT_DEEPL = 15.0

_LLM_SYSTEM = (
    "You are a professional translator. "
    "Translate the user's message to the requested language. "
    "Output ONLY the translated text — no explanations, no quotes, no preamble."
)

_DEEPL_LANG_MAP: dict[str, str] = {
    "en": "EN-US",
    "de": "DE",
    "fr": "FR",
    "es": "ES",
    "it": "IT",
    "pt": "PT-PT",
    "nl": "NL",
    "ru": "RU",
    "no": "NB",
    "ja": "JA",
    "sv": "SV",
    "pl": "PL",
    "tr": "TR",
    "ko": "KO",
    "zh": "ZH",
}


def _clean_translation(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def _estimate_openai_tokens(text: str) -> int:
    # Conservative enough for short chat messages: prompt + completion budget.
    return max(1, len(text) // 3) + 350


def _parse_positive_int(value: str, default: int = 0) -> int:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return default
    return max(0, int(digits))


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
    return _clean_translation((resp.get("message") or {}).get("content") or "") or None


async def _translate_openai(text: str, lang_name: str, api_key: str, model: str) -> tuple[str | None, int]:
    messages = [
        {"role": "system", "content": _LLM_SYSTEM},
        {"role": "user", "content": f"Translate to {lang_name}:\n\n{text}"},
    ]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.1,
        "max_completion_tokens": 1024,
    }
    timeout = aiohttp.ClientTimeout(total=_TRANSLATE_TIMEOUT_OPENAI)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post("https://api.openai.com/v1/chat/completions", json=payload) as response:
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(f"OpenAI HTTP {response.status}: {body[:300]}")
            data = await response.json()
    content = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        or ""
    )
    tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
    return _clean_translation(content) or None, tokens


async def _translate_deepl(text: str, lang: str, api_key: str, api_url: str) -> str | None:
    target_lang = _DEEPL_LANG_MAP.get(lang, lang.upper())
    url = (api_url or "https://api-free.deepl.com/v2/translate").rstrip("/")
    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}
    payload = {"text": text, "target_lang": target_lang}
    timeout = aiohttp.ClientTimeout(total=_TRANSLATE_TIMEOUT_DEEPL)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(url, data=payload) as response:
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(f"DeepL HTTP {response.status}: {body[:300]}")
            data = await response.json()
    translations = data.get("translations") or []
    if not translations:
        return None
    return _clean_translation(translations[0].get("text") or "") or None


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

        skip_open  = (await db.get_setting("auto_translate_skip_open")  or "").strip()
        skip_close = (await db.get_setting("auto_translate_skip_close") or "").strip()
        if skip_open and skip_close:
            if text.startswith(skip_open) and text.endswith(skip_close):
                return
        elif skip_open:
            if text.startswith(skip_open):
                return

        engine = (await db.get_setting("auto_translate_engine") or "google").strip()
        output_mode = (await db.get_setting("auto_translate_output_mode") or "separate").strip()
        if output_mode not in ("separate", "combined"):
            output_mode = "separate"

        if engine == "llm":
            import bot.exp_stream_manager as _esm
            if _esm.stream_is_live:
                print("[auto_translate] skipped (stream is live, LLM disabled during stream)", flush=True)
                return

        openai_api_key = (await db.get_setting("auto_translate_openai_api_key") or "").strip()
        openai_model = (await db.get_setting("auto_translate_openai_model") or "gpt-4o-mini").strip()
        openai_daily_token_limit = _parse_positive_int(
            await db.get_setting("auto_translate_openai_daily_token_limit") or "0"
        )
        deepl_api_key = (await db.get_setting("auto_translate_deepl_api_key") or "").strip()
        deepl_api_url = (
            await db.get_setting("auto_translate_deepl_api_url")
            or "https://api-free.deepl.com/v2/translate"
        ).strip()
        if engine == "openai" and not openai_api_key:
            print("[auto_translate] skipped (OpenAI API key missing)", flush=True)
            return
        if engine == "deepl" and not deepl_api_key:
            print("[auto_translate] skipped (DeepL API key missing)", flush=True)
            return

        loop = asyncio.get_event_loop()
        src_lang: str | None = None
        try:
            from langdetect import detect as _detect
            src_lang = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _detect(text)),
                timeout=3.0,
            )
        except Exception:
            pass

        author_name = message.author.display_name
        translated_parts: list[tuple[str, str, str]] = []
        openai_tokens_today: int | None = None

        for lang in langs:
            try:
                if src_lang and src_lang.lower() == lang.lower():
                    continue
                meta = _LANG_META.get(lang, (lang.capitalize(), f"`{lang}`"))
                lang_name, flag = meta
                if engine == "llm":
                    translated = await _translate_llm(self._llm_client, text, lang_name)
                elif engine == "openai":
                    if openai_daily_token_limit > 0:
                        if openai_tokens_today is None:
                            openai_tokens_today = await db.get_auto_translate_daily_tokens("openai")
                        estimated = _estimate_openai_tokens(text)
                        if openai_tokens_today + estimated > openai_daily_token_limit:
                            print(
                                "[auto_translate] skipped OpenAI translation "
                                f"(daily token limit {openai_daily_token_limit} reached; "
                                f"used={openai_tokens_today}, estimated_next={estimated})",
                                flush=True,
                            )
                            continue
                    translated, token_count = await _translate_openai(
                        text, lang_name, openai_api_key, openai_model
                    )
                    if openai_tokens_today is not None:
                        openai_tokens_today += token_count
                elif engine == "deepl":
                    translated = await _translate_deepl(text, lang, deepl_api_key, deepl_api_url)
                else:
                    translated = await _translate_google(text, lang)
                if not translated or translated.strip() == text.strip():
                    continue
                translated_parts.append((lang, flag, translated))
                try:
                    await db.add_auto_translate_usage(
                        engine=engine,
                        target_lang=lang,
                        source_chars=len(text),
                        translated_chars=len(translated),
                        token_count=token_count if engine == "openai" else 0,
                    )
                except Exception as e:
                    print(f"[auto_translate] Usage logging failed: {e}", flush=True)
            except asyncio.TimeoutError:
                print(f"[auto_translate] Timeout for lang={lang} engine={engine}", flush=True)
            except Exception as e:
                print(f"[auto_translate] Error for lang={lang} engine={engine}: {e}", flush=True)

        if not translated_parts:
            return

        if output_mode == "combined":
            joined = "\n".join(f"{flag} {translated}" for _lang, flag, translated in translated_parts)
            combined = f"**{author_name}** {joined}"
            if len(combined) <= 1900:
                await message.channel.send(combined)
            else:
                for _lang, flag, translated in translated_parts:
                    await message.channel.send(f"**{author_name}** {flag} {translated}")
        else:
            for _lang, flag, translated in translated_parts:
                await message.channel.send(f"**{author_name}** {flag} {translated}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoTranslateCog(bot))
