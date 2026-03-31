from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    discord_bot_token: str
    discord_guild_id: int
    discord_voice_channel_id: int
    discord_text_channel_id: int | None
    openclaw_base_url: str
    openclaw_chat_path: str
    openclaw_api_key: str | None
    openclaw_model: str
    openclaw_system_prompt: str
    wyoming_asr_host: str
    wyoming_asr_port: int
    kokoro_base_url: str
    kokoro_api_key: str
    kokoro_model: str
    kokoro_voice: str
    voice_silence_ms: int
    voice_min_utterance_ms: int
    voice_max_utterance_ms: int
    voice_response_volume: float


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        discord_guild_id=int(_require("DISCORD_GUILD_ID")),
        discord_voice_channel_id=int(_require("DISCORD_VOICE_CHANNEL_ID")),
        discord_text_channel_id=_optional_int("DISCORD_TEXT_CHANNEL_ID"),
        openclaw_base_url=os.getenv("OPENCLAW_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        openclaw_chat_path=os.getenv("OPENCLAW_CHAT_PATH", "/v1/chat/completions"),
        openclaw_api_key=_optional_str("OPENCLAW_API_KEY"),
        openclaw_model=os.getenv("OPENCLAW_MODEL", "openclaw"),
        openclaw_system_prompt=os.getenv(
            "OPENCLAW_SYSTEM_PROMPT",
            "You are OpenClaw. Keep spoken replies concise and natural for a Discord voice conversation.",
        ),
        wyoming_asr_host=os.getenv("WYOMING_ASR_HOST", "127.0.0.1"),
        wyoming_asr_port=int(os.getenv("WYOMING_ASR_PORT", "10300")),
        kokoro_base_url=os.getenv("KOKORO_BASE_URL", "http://192.168.1.34:8880").rstrip("/"),
        kokoro_api_key=os.getenv("KOKORO_API_KEY", "123"),
        kokoro_model=os.getenv("KOKORO_MODEL", "kokoro"),
        kokoro_voice=os.getenv("KOKORO_VOICE", "af_heart"),
        voice_silence_ms=int(os.getenv("VOICE_SILENCE_MS", "1200")),
        voice_min_utterance_ms=int(os.getenv("VOICE_MIN_UTTERANCE_MS", "800")),
        voice_max_utterance_ms=int(os.getenv("VOICE_MAX_UTTERANCE_MS", "12000")),
        voice_response_volume=float(os.getenv("VOICE_RESPONSE_VOLUME", "0.9")),
    )


def _optional_str(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _optional_int(name: str) -> int | None:
    value = _optional_str(name)
    if value is None:
        return None
    return int(value)
