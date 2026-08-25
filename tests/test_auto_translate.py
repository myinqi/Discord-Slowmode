import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


def _install_import_stubs() -> None:
    """Allow importing the cog on the host Python without Discord/web deps."""
    if "dotenv" not in sys.modules:
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *a, **k: True
        sys.modules["dotenv"] = dotenv
    if "aiohttp" not in sys.modules:
        aiohttp = types.ModuleType("aiohttp")

        class ClientTimeout:
            def __init__(self, total=None, **kwargs):
                self.total = total

        class ClientSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, *args, **kwargs):
                raise RuntimeError("aiohttp ClientSession stub")

        aiohttp.ClientTimeout = ClientTimeout
        aiohttp.ClientSession = ClientSession
        sys.modules["aiohttp"] = aiohttp
    if "discord" not in sys.modules:
        discord = types.ModuleType("discord")
        discord.Message = type("Message", (), {})
        ext = types.ModuleType("discord.ext")
        commands_mod = types.ModuleType("discord.ext.commands")

        class Cog:
            @staticmethod
            def listener(*args, **kwargs):
                def decorator(fn):
                    return fn
                return decorator

        commands_mod.Cog = Cog
        commands_mod.Bot = object
        ext.commands = commands_mod
        discord.ext = ext
        sys.modules["discord"] = discord
        sys.modules["discord.ext"] = ext
        sys.modules["discord.ext.commands"] = commands_mod


_install_import_stubs()

from bot.cogs.auto_translate import (
    TranslationError,
    TranslateEngineSettings,
    _is_google_error_page,
    load_translate_settings,
    run_configured_translator,
    translate_manual,
)


class FakeDB:
    def __init__(self, settings=None, daily_tokens=0):
        self.settings = settings or {}
        self.daily_tokens = daily_tokens
        self.usage = []

    async def get_setting(self, key):
        return self.settings.get(key)

    async def get_auto_translate_daily_tokens(self, engine):
        return self.daily_tokens

    async def add_auto_translate_usage(self, **kwargs):
        self.usage.append(kwargs)


class FakeBot:
    def __init__(self, db):
        self.db = db

    def get_cog(self, name):
        return None


def _settings(**overrides):
    data = dict(
        engine="google",
        openai_api_key="",
        openai_model="gpt-4o-mini",
        openai_daily_token_limit=0,
        deepl_api_key="",
        deepl_api_url="https://api-free.deepl.com/v2/translate",
    )
    data.update(overrides)
    return TranslateEngineSettings(**data)


class GoogleErrorPageTests(unittest.TestCase):
    def test_detects_scraped_500_page(self):
        text = (
            "Error 500 (Server Error)!!!500.That's an error."
            "There was an error. Please try again later.That's all we know."
        )
        self.assertTrue(_is_google_error_page(text))

    def test_ignores_normal_translations(self):
        self.assertFalse(_is_google_error_page("I once printed Picard's Enterprise"))
        self.assertFalse(_is_google_error_page(""))
        self.assertFalse(_is_google_error_page("There was an error in the print job"))


class LoadSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_to_google(self):
        settings = await load_translate_settings(FakeDB())
        self.assertEqual(settings.engine, "google")
        self.assertEqual(settings.openai_model, "gpt-4o-mini")

    async def test_reads_openai_engine(self):
        settings = await load_translate_settings(FakeDB({
            "auto_translate_engine": "openai",
            "auto_translate_openai_api_key": " sk-test ",
            "auto_translate_openai_model": "gpt-4.1-mini",
            "auto_translate_openai_daily_token_limit": "1000",
        }))
        self.assertEqual(settings.engine, "openai")
        self.assertEqual(settings.openai_api_key, "sk-test")
        self.assertEqual(settings.openai_model, "gpt-4.1-mini")
        self.assertEqual(settings.openai_daily_token_limit, 1000)


class RunConfiguredTranslatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_to_openai(self):
        settings = _settings(engine="openai", openai_api_key="sk-test")
        with patch(
            "bot.cogs.auto_translate._translate_openai",
            new=AsyncMock(return_value=("Hello", 12)),
        ) as mocked:
            translated, tokens = await run_configured_translator(
                "Hallo", "en", settings
            )
        self.assertEqual(translated, "Hello")
        self.assertEqual(tokens, 12)
        mocked.assert_awaited_once()
        args, _kwargs = mocked.call_args
        self.assertEqual(args[0], "Hallo")
        self.assertEqual(args[1], "English")
        self.assertEqual(args[2], "sk-test")

    async def test_dispatches_to_google(self):
        settings = _settings(engine="google")
        with patch(
            "bot.cogs.auto_translate._translate_google",
            new=AsyncMock(return_value="Hello"),
        ) as mocked:
            translated, tokens = await run_configured_translator(
                "Hallo", "en", settings
            )
        self.assertEqual(translated, "Hello")
        self.assertEqual(tokens, 0)
        mocked.assert_awaited_once_with("Hallo", "en")


class TranslateManualTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_openai_when_configured(self):
        db = FakeDB({
            "auto_translate_engine": "openai",
            "auto_translate_openai_api_key": "sk-test",
        })
        bot = FakeBot(db)
        with patch(
            "bot.cogs.auto_translate._translate_openai",
            new=AsyncMock(return_value=("I printed the Enterprise", 40)),
        ):
            result = await translate_manual(bot, "Ich habe die Enterprise gedruckt", "en")
        self.assertEqual(result, "I printed the Enterprise")
        self.assertEqual(len(db.usage), 1)
        self.assertEqual(db.usage[0]["engine"], "openai")
        self.assertEqual(db.usage[0]["token_count"], 40)

    async def test_openai_missing_key_raises(self):
        bot = FakeBot(FakeDB({"auto_translate_engine": "openai"}))
        with self.assertRaises(TranslationError) as ctx:
            await translate_manual(bot, "Hallo", "en")
        self.assertIn("API key", str(ctx.exception))

    async def test_openai_token_limit_raises(self):
        db = FakeDB(
            {
                "auto_translate_engine": "openai",
                "auto_translate_openai_api_key": "sk-test",
                "auto_translate_openai_daily_token_limit": "10",
            },
            daily_tokens=10,
        )
        bot = FakeBot(db)
        with self.assertRaises(TranslationError) as ctx:
            await translate_manual(bot, "Hallo Welt", "en")
        self.assertIn("token limit", str(ctx.exception).lower())

    async def test_same_language_returns_none(self):
        db = FakeDB({
            "auto_translate_engine": "openai",
            "auto_translate_openai_api_key": "sk-test",
        })
        bot = FakeBot(db)
        with patch(
            "bot.cogs.auto_translate._translate_openai",
            new=AsyncMock(return_value=(None, 8)),
        ):
            result = await translate_manual(bot, "Hello there", "en")
        self.assertIsNone(result)
        self.assertEqual(db.usage[0]["translated_chars"], 0)


if __name__ == "__main__":
    unittest.main()
