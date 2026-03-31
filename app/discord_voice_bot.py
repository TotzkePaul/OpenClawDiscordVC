from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from pathlib import Path
from dataclasses import dataclass, field

import discord
from discord.ext import commands, voice_recv

from .audio import TARGET_SAMPLE_RATE, WavPcmAudioSource, discord_pcm_to_mono16, pcm_duration_ms, pcm_to_wav_bytes
from .config import Settings
from .faster_whisper_client import FasterWhisperClient
from .openai_tts_tool import OpenAITTSTool
from .openclaw_client import OpenClawClient
from .voice_recv_patch import patch_voice_recv

LOGGER = logging.getLogger(__name__)
DUMPS_DIR = Path("dumps")
DISCORD_MESSAGE_LIMIT = 4000


@dataclass(slots=True)
class UserBuffer:
    pcm: bytearray = field(default_factory=bytearray)
    silence_ms: int = 0
    active_ms: int = 0


class DiscordSpeechSink(voice_recv.AudioSink):
    def __init__(self, orchestrator: "VoiceOrchestrator") -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.buffers: dict[int, UserBuffer] = {}

    def wants_opus(self) -> bool:
        return False

    def write(self, user: discord.User | discord.Member | None, data: voice_recv.VoiceData) -> None:
        if user is None or user.bot:
            return

        pcm16 = discord_pcm_to_mono16(data.pcm)
        duration_ms = pcm_duration_ms(pcm16, TARGET_SAMPLE_RATE, 1)
        state = self.buffers.setdefault(user.id, UserBuffer())
        state.pcm.extend(pcm16)
        state.active_ms += duration_ms
        state.silence_ms = 0

        if state.active_ms >= self.orchestrator.settings.voice_max_utterance_ms:
            self._flush_user(user, state)

    def cleanup(self) -> None:
        self.buffers.clear()

    def tick_silence(self, elapsed_ms: int) -> None:
        for user_id, state in list(self.buffers.items()):
            if not state.pcm:
                continue
            state.silence_ms += elapsed_ms
            if state.silence_ms >= self.orchestrator.settings.voice_silence_ms:
                guild = self.orchestrator.guild
                if guild is None:
                    continue
                user = guild.get_member(user_id) or self.orchestrator.bot.get_user(user_id)
                if user is not None:
                    self._flush_user(user, state)

    def _flush_user(self, user: discord.abc.User, state: UserBuffer) -> None:
        if state.active_ms < self.orchestrator.settings.voice_min_utterance_ms:
            LOGGER.info(
                "Discarding short utterance user=%s active_ms=%s min_ms=%s pcm_bytes=%s",
                user.id,
                state.active_ms,
                self.orchestrator.settings.voice_min_utterance_ms,
                len(state.pcm),
            )
            state.pcm.clear()
            state.silence_ms = 0
            state.active_ms = 0
            return

        pcm = bytes(state.pcm)
        state.pcm.clear()
        state.silence_ms = 0
        state.active_ms = 0
        self.orchestrator.submit_utterance(user, pcm)


class VoiceOrchestrator:
    def __init__(self, settings: Settings) -> None:
        patch_voice_recv()
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.messages = True
        intents.message_content = True
        intents.members = True

        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self.settings = settings
        self.guild: discord.Guild | None = None
        self.voice_client: voice_recv.VoiceRecvClient | None = None
        self.sink: DiscordSpeechSink | None = None
        self.processing_lock = asyncio.Lock()
        self.asr = FasterWhisperClient(settings.wyoming_asr_host, settings.wyoming_asr_port)
        self.agent = OpenClawClient(
            base_url=settings.openclaw_base_url,
            chat_path=settings.openclaw_chat_path,
            model=settings.openclaw_model,
            system_prompt=settings.openclaw_system_prompt,
            api_key=settings.openclaw_api_key,
        )
        self.tts = OpenAITTSTool(
            base_url=settings.kokoro_base_url,
            api_key=settings.kokoro_api_key,
            model=settings.kokoro_model,
            voice=settings.kokoro_voice,
        )
        self._install_events()

    def _install_events(self) -> None:
        @self.bot.event
        async def on_ready() -> None:
            LOGGER.info("Logged in as %s", self.bot.user)
            await self.ensure_connected()
            self.bot.loop.create_task(self._silence_watchdog())

    async def ensure_connected(self) -> None:
        guild = self.bot.get_guild(self.settings.discord_guild_id)
        if guild is None:
            raise RuntimeError(f"Guild not found: {self.settings.discord_guild_id}")

        channel = guild.get_channel(self.settings.discord_voice_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError(f"Voice channel not found: {self.settings.discord_voice_channel_id}")

        self.guild = guild
        if guild.voice_client and guild.voice_client.is_connected():
            self.voice_client = guild.voice_client  # type: ignore[assignment]
        else:
            self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)

        self.sink = DiscordSpeechSink(self)
        self.voice_client.listen(self.sink)
        LOGGER.info("Connected to voice channel %s", channel.name)

    async def _silence_watchdog(self) -> None:
        while not self.bot.is_closed():
            await asyncio.sleep(0.2)
            if self.sink is not None:
                self.sink.tick_silence(200)

    def submit_utterance(self, user: discord.abc.User, pcm: bytes) -> None:
        self.bot.loop.create_task(self._handle_utterance(user, pcm))

    async def _handle_utterance(self, user: discord.abc.User, pcm: bytes) -> None:
        async with self.processing_lock:
            dump_base = self._make_dump_base(user)
            transcript = ""
            response = ""
            try:
                self._dump_wav(dump_base.with_suffix(".wav"), pcm)
                transcript = await self.asr.transcribe_pcm(pcm)
                self._dump_text(dump_base.with_suffix(".txt"), user.display_name, transcript, response)
                if not transcript:
                    return

                LOGGER.info("%s said: %s", user.display_name, transcript)
                await self._safe_send_text_log(f"**{user.display_name}:** {transcript}")

                response = await self.agent.get_response(f"{user.display_name}: {transcript}")
                self._dump_text(dump_base.with_suffix(".txt"), user.display_name, transcript, response)
                LOGGER.info("OpenClaw: %s", response)
                await self._safe_send_text_log(f"**OpenClaw:** {response}")

                await self._speak_response(response)
            except Exception:
                self._dump_text(dump_base.with_suffix(".txt"), user.display_name, transcript, response, error=True)
                LOGGER.exception("Voice pipeline failed for %s", user.display_name)

    async def _send_text_log(self, message: str) -> None:
        if self.settings.discord_text_channel_id is None:
            return
        channel = self.bot.get_channel(self.settings.discord_text_channel_id)
        if isinstance(channel, discord.TextChannel):
            for chunk in _split_discord_message(message):
                await channel.send(chunk)

    async def _safe_send_text_log(self, message: str) -> None:
        try:
            await self._send_text_log(message)
        except Exception:
            LOGGER.exception("Failed to send Discord text log")

    async def _play_wav(self, wav_bytes: bytes) -> None:
        if self.voice_client is None:
            return
        while self.voice_client.is_playing():
            await asyncio.sleep(0.1)

        source = discord.PCMVolumeTransformer(
            WavPcmAudioSource(wav_bytes),
            volume=self.settings.voice_response_volume,
        )
        done = asyncio.Event()

        def _after(_: Exception | None) -> None:
            self.bot.loop.call_soon_threadsafe(done.set)

        self.voice_client.play(source, after=_after)
        await done.wait()

    async def _speak_response(self, response: str) -> None:
        try:
            wav_bytes = await self.tts.synthesize_bytes(response)
        except Exception:
            LOGGER.exception("TTS synthesis failed")
            return

        try:
            await self._play_wav(wav_bytes)
        except Exception:
            LOGGER.exception("Voice playback failed")

    def run(self) -> None:
        self.bot.run(self.settings.discord_bot_token)

    def _make_dump_base(self, user: discord.abc.User) -> Path:
        DUMPS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in user.display_name).strip("_")
        safe_name = safe_name or f"user_{user.id}"
        return DUMPS_DIR / f"{timestamp}-{safe_name}"

    def _dump_wav(self, path: Path, pcm: bytes) -> None:
        path.write_bytes(pcm_to_wav_bytes(pcm))

    def _dump_text(
        self,
        path: Path,
        user_name: str,
        transcript: str,
        response: str,
        error: bool = False,
    ) -> None:
        lines = [
            f"user: {user_name}",
            f"transcript: {transcript}",
            f"response: {response}",
        ]
        if error:
            lines.append("error: true")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _split_discord_message(message: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    remaining = message
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks
