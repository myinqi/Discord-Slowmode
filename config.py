import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Discord
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    GUILD_ID: int = int(os.getenv("GUILD_ID", "0"))

    # Web
    WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
    WEB_PORT: int = int(os.getenv("WEB_PORT", "5000"))
    WEB_URL: str = os.getenv("WEB_URL", "")  # e.g. http://yourserver.de:5000
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me")

    # Admin
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "changeme")

    # Database
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/bot.db")

    # Bot
    BOT_NAME: str = os.getenv("BOT_NAME", "Slowmode Bot")

    # Experimental Radio
    EXP_RADIO_DIR: str = os.getenv("EXP_RADIO_DIR", "data/radio/exp_radio")

    # LLM (Corax chat)
    OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://ollama:11434")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "qwen2.5:7b-instruct")
    LLM_REQUEST_TIMEOUT: int = int(os.getenv("LLM_REQUEST_TIMEOUT", "180"))
