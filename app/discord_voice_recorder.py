from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import wave

import discord
import nacl.secret
from discord import opus as discord_opus
from discord.ext import commands, voice_recv

from .audio import TARGET_SAMPLE_RATE, discord_pcm_to_mono16, pcm_duration_ms, pcm_to_wav_bytes
from .config import Settings
from .voice_recv_patch import patch_voice_recv

LOGGER = logging.getLogger(__name__)
RECORDER_DUMPS_DIR = Path("dumps") / "recorder"
EXPECTED_PAYLOAD_TYPE = 0x78
VALID_STEREO_PCM_FRAME_BYTES = {480, 960, 1920, 3840, 5760, 7680, 9600, 11520, 15360, 19200, 23040}
VALID_MONO_PCM_FRAME_BYTES = {80, 160, 320, 640, 960, 1280, 1600, 1920, 2560, 3200, 3840, 5120, 6400, 7680}
_VOICE_RECV_DECRYPT_PATCHED = False


@dataclass(slots=True)
class StreamStats:
    ssrc: int
    user_id: int | None = None
    first_sequence: int | None = None
    last_sequence: int | None = None
    frame_count: int = 0
    decode_errors: int = 0
    probe_decode_errors: int = 0
    packet_loss_events: int = 0
    packets_lost: int = 0
    out_of_order_events: int = 0

    def register_sequence(self, sequence: int) -> None:
        if self.first_sequence is None:
            self.first_sequence = sequence
            self.last_sequence = sequence
            return

        assert self.last_sequence is not None
        expected = (self.last_sequence + 1) & 0xFFFF
        if sequence == expected:
            self.last_sequence = sequence
            return

        delta = (sequence - expected) & 0xFFFF
        if 0 < delta < 0x8000:
            self.packet_loss_events += 1
            self.packets_lost += delta
            LOGGER.warning(
                "Packet gap detected for ssrc=%s expected=%s got=%s lost=%s",
                self.ssrc,
                expected,
                sequence,
                delta,
            )
            self.last_sequence = sequence
            return

        self.out_of_order_events += 1
        LOGGER.warning(
            "Out-of-order packet for ssrc=%s last=%s got=%s",
            self.ssrc,
            self.last_sequence,
            sequence,
        )


@dataclass(slots=True)
class RecorderBuffer:
    stats: StreamStats
    raw_pcm: bytearray = field(default_factory=bytearray)
    mono_pcm: bytearray = field(default_factory=bytearray)
    opus_frames: list[bytes] = field(default_factory=list)
    frame_logs: list[dict[str, object]] = field(default_factory=list)
    silence_ms: int = 0
    active_ms: int = 0
    last_sequence: int | None = None


class BaseRecorderSink(voice_recv.AudioSink):
    def __init__(self, recorder: "VoiceRecorder") -> None:
        super().__init__()
        self.recorder = recorder
        self.buffers: dict[int, RecorderBuffer] = {}

    def tick_silence(self, elapsed_ms: int) -> None:
        for user_id, state in list(self.buffers.items()):
            if not (state.mono_pcm or state.frame_logs or state.opus_frames):
                continue
            state.silence_ms += elapsed_ms
            if state.silence_ms >= self.recorder.settings.voice_silence_ms:
                guild = self.recorder.guild
                if guild is None:
                    continue
                user = guild.get_member(user_id) or self.recorder.bot.get_user(user_id)
                if user is not None:
                    self._flush_user(user, state)

    def cleanup(self) -> None:
        if self.recorder.guild is not None:
            for user_id, state in list(self.buffers.items()):
                user = self.recorder.guild.get_member(user_id) or self.recorder.bot.get_user(user_id)
                if user is not None and (state.frame_logs or state.opus_frames or state.raw_pcm):
                    self._flush_user(user, state, reason="cleanup")
        self.buffers.clear()

    @voice_recv.AudioSink.listener()
    def on_rtcp_packet(self, packet: object, guild: discord.Guild) -> None:
        packet_type = getattr(packet, "type", "unknown")
        ssrc = getattr(packet, "ssrc", "unknown")
        LOGGER.info("RTCP packet received type=%s ssrc=%s guild=%s", packet_type, ssrc, guild.id)

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_start(self, member: discord.abc.User | None) -> None:
        LOGGER.info("Speaking start member=%s", getattr(member, "id", None))

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member: discord.abc.User | None) -> None:
        LOGGER.info("Speaking stop member=%s", getattr(member, "id", None))

    def _get_state(self, user: discord.abc.User, ssrc: int) -> RecorderBuffer:
        state = self.buffers.get(user.id)
        if state is None:
            state = RecorderBuffer(stats=StreamStats(ssrc=ssrc, user_id=user.id))
            self.buffers[user.id] = state
        elif state.stats.ssrc != ssrc:
            LOGGER.warning(
                "User %s switched SSRC from %s to %s; flushing prior buffer",
                user.id,
                state.stats.ssrc,
                ssrc,
            )
            self._flush_user(user, state)
            state = RecorderBuffer(stats=StreamStats(ssrc=ssrc, user_id=user.id))
            self.buffers[user.id] = state
        return state

    def _validate_rtp_packet(self, packet: object, stats: StreamStats) -> bool:
        version = getattr(packet, "version", None)
        payload = getattr(packet, "payload", None)
        sequence = getattr(packet, "sequence", None)
        ssrc = getattr(packet, "ssrc", None)

        LOGGER.info(
            "RTP frame ssrc=%s seq=%s version=%s payload=%s",
            ssrc,
            sequence,
            version,
            payload,
        )

        if version != 2:
            LOGGER.error("Dropping RTP packet with invalid version=%s ssrc=%s seq=%s", version, ssrc, sequence)
            return False

        if payload is None or payload >= 200:
            LOGGER.error("Dropping non-audio packet payload=%s ssrc=%s seq=%s", payload, ssrc, sequence)
            return False

        if payload != EXPECTED_PAYLOAD_TYPE:
            LOGGER.warning(
                "Unexpected RTP payload type=%s for ssrc=%s seq=%s (expected %s)",
                payload,
                ssrc,
                sequence,
                EXPECTED_PAYLOAD_TYPE,
            )

        if sequence is None:
            LOGGER.error("Dropping packet without sequence number for ssrc=%s", ssrc)
            return False

        stats.register_sequence(sequence)
        return True

    def _append_pcm(self, state: RecorderBuffer, raw_pcm: bytes) -> None:
        if len(raw_pcm) not in VALID_STEREO_PCM_FRAME_BYTES:
            LOGGER.warning(
                "Unexpected PCM frame size ssrc=%s bytes=%s valid=%s first8=%s",
                state.stats.ssrc,
                len(raw_pcm),
                sorted(VALID_STEREO_PCM_FRAME_BYTES),
                raw_pcm[:8].hex(),
            )

        mono_pcm = discord_pcm_to_mono16(raw_pcm)
        if len(mono_pcm) not in VALID_MONO_PCM_FRAME_BYTES:
            LOGGER.warning(
                "Unexpected mono PCM frame size ssrc=%s bytes=%s valid=%s first8=%s",
                state.stats.ssrc,
                len(mono_pcm),
                sorted(VALID_MONO_PCM_FRAME_BYTES),
                mono_pcm[:8].hex(),
            )

        duration_ms = pcm_duration_ms(mono_pcm, TARGET_SAMPLE_RATE, 1)
        state.raw_pcm.extend(raw_pcm)
        state.mono_pcm.extend(mono_pcm)
        state.active_ms += duration_ms
        state.silence_ms = 0
        state.stats.frame_count += 1

    def _record_frame_log(
        self,
        state: RecorderBuffer,
        packet: object,
        *,
        opus_len: int,
        opus_first8: str,
        pcm_len: int | None,
        pcm_first8: str | None,
        decode_ok: bool,
        decode_error: str | None = None,
        probe_decode_ok: bool | None = None,
        probe_decode_error: str | None = None,
        probe_pcm_len: int | None = None,
        probe_pcm_first8: str | None = None,
    ) -> None:
        state.frame_logs.append(
            {
                "ssrc": getattr(packet, "ssrc", None),
                "sequence": getattr(packet, "sequence", None),
                "timestamp": getattr(packet, "timestamp", None),
                "version": getattr(packet, "version", None),
                "payload_type": getattr(packet, "payload", None),
                "extended": getattr(packet, "extended", None),
                "opus_len": opus_len,
                "opus_first8": opus_first8,
                "pcm_len": pcm_len,
                "pcm_first8": pcm_first8,
                "decode_ok": decode_ok,
                "decode_error": decode_error,
                "probe_decode_ok": probe_decode_ok,
                "probe_decode_error": probe_decode_error,
                "probe_pcm_len": probe_pcm_len,
                "probe_pcm_first8": probe_pcm_first8,
            }
        )

    def _flush_user(self, user: discord.abc.User, state: RecorderBuffer, *, reason: str = "silence_or_limit") -> None:
        if state.active_ms < self.recorder.settings.voice_min_utterance_ms:
            if state.frame_logs or state.opus_frames or state.raw_pcm:
                LOGGER.info(
                    "Discarding short/incomplete utterance user=%s ssrc=%s reason=%s active_ms=%s frames=%s raw_bytes=%s",
                    user.id,
                    state.stats.ssrc,
                    reason,
                    state.active_ms,
                    len(state.frame_logs),
                    len(state.raw_pcm),
                )
                self.recorder.submit_utterance(
                    user=user,
                    raw_pcm=bytes(state.raw_pcm),
                    mono_pcm=bytes(state.mono_pcm),
                    opus_frames=list(state.opus_frames),
                    frame_logs=list(state.frame_logs),
                    duration_ms=state.active_ms,
                    stats=state.stats,
                    mode=f"{self.recorder.debug_mode}-discarded",
                )
            state.raw_pcm.clear()
            state.mono_pcm.clear()
            state.opus_frames.clear()
            state.frame_logs.clear()
            state.silence_ms = 0
            state.active_ms = 0
            return

        raw_pcm = bytes(state.raw_pcm)
        mono_pcm = bytes(state.mono_pcm)
        opus_frames = list(state.opus_frames)
        frame_logs = list(state.frame_logs)
        duration_ms = state.active_ms
        stats = state.stats

        state.raw_pcm.clear()
        state.mono_pcm.clear()
        state.opus_frames.clear()
        state.frame_logs.clear()
        state.silence_ms = 0
        state.active_ms = 0

        self.recorder.submit_utterance(
            user=user,
            raw_pcm=raw_pcm,
            mono_pcm=mono_pcm,
            opus_frames=opus_frames,
            frame_logs=frame_logs,
            duration_ms=duration_ms,
            stats=stats,
            mode=self.recorder.debug_mode,
        )


class ProbeDecoder:
    def __init__(self, recorder: "VoiceRecorder") -> None:
        self.recorder = recorder
        self.decoders: dict[int, discord_opus.Decoder] = {}

    def decode(self, ssrc: int, opus_bytes: bytes) -> tuple[bytes | None, str | None]:
        decoder = self.decoders.setdefault(ssrc, discord_opus.Decoder())
        try:
            return decoder.decode(opus_bytes, fec=False), None
        except discord_opus.OpusError:
            return None, "corrupted_stream"


class RecorderSink(BaseRecorderSink):
    def __init__(self, recorder: "VoiceRecorder") -> None:
        super().__init__(recorder)
        self.probe_decoder = ProbeDecoder(recorder) if recorder.debug_mode == "opus" else None

    def wants_opus(self) -> bool:
        return False

    def write(self, user: discord.User | discord.Member | None, data: voice_recv.VoiceData) -> None:
        if user is None or user.bot:
            return

        packet = data.packet
        ssrc = getattr(packet, "ssrc", None)
        sequence = getattr(packet, "sequence", None)
        opus_bytes = data.opus or b""
        raw_pcm = data.pcm or b""
        state = self._get_state(user, ssrc)

        if not self._validate_rtp_packet(packet, state.stats):
            return

        LOGGER.info(
            "PCM frame ssrc=%s seq=%s pcm_len=%s pcm_first8=%s opus_len=%s opus_first8=%s probe=%s",
            ssrc,
            sequence,
            len(raw_pcm),
            raw_pcm[:8].hex(),
            len(opus_bytes),
            opus_bytes[:8].hex(),
            self.probe_decoder is not None,
        )

        if not raw_pcm:
            state.stats.decode_errors += 1
            LOGGER.warning("Dropping empty PCM payload for ssrc=%s seq=%s", ssrc, sequence)
            self._record_frame_log(
                state,
                packet,
                opus_len=len(opus_bytes),
                opus_first8=opus_bytes[:8].hex(),
                pcm_len=0,
                pcm_first8=None,
                decode_ok=False,
                decode_error="empty_pcm",
            )
            return

        probe_pcm: bytes | None = None
        probe_error: str | None = None
        if self.probe_decoder is not None and opus_bytes:
            probe_pcm, probe_error = self.probe_decoder.decode(ssrc, opus_bytes)
            if probe_error is not None:
                state.stats.probe_decode_errors += 1
                LOGGER.warning("Probe Opus decode failed for ssrc=%s seq=%s", ssrc, sequence)

        self._record_frame_log(
            state,
            packet,
            opus_len=len(opus_bytes),
            opus_first8=opus_bytes[:8].hex(),
            pcm_len=len(raw_pcm),
            pcm_first8=raw_pcm[:8].hex(),
            decode_ok=True,
            probe_decode_ok=probe_error is None if self.probe_decoder is not None else None,
            probe_decode_error=probe_error,
            probe_pcm_len=len(probe_pcm) if probe_pcm is not None else None,
            probe_pcm_first8=probe_pcm[:8].hex() if probe_pcm is not None else None,
        )
        state.opus_frames.append(opus_bytes)
        self._append_pcm(state, raw_pcm)

        if state.active_ms >= self.recorder.settings.voice_max_utterance_ms:
            self._flush_user(user, state)


class VoiceRecorder:
    def __init__(self, settings: Settings) -> None:
        patch_voice_recv()
        _patch_voice_recv_decrypt_logging()
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.members = True

        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self.settings = settings
        self.debug_mode = os.getenv("VOICE_DEBUG_MODE", "opus").strip().lower() or "opus"
        self.guild: discord.Guild | None = None
        self.voice_client: voice_recv.VoiceRecvClient | None = None
        self.sink: BaseRecorderSink | None = None
        self._install_events()

    def _install_events(self) -> None:
        @self.bot.event
        async def on_ready() -> None:
            LOGGER.info("Recorder logged in as %s using debug mode=%s", self.bot.user, self.debug_mode)
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

        LOGGER.info(
            "Voice receive mode=%s endpoint=%s secret_key_len=%s dave_protocol=%s dave_ready=%s",
            getattr(self.voice_client, "mode", "unknown"),
            getattr(getattr(self.voice_client, "_connection", None), "endpoint", "unknown"),
            len(getattr(self.voice_client, "secret_key", []) or []),
            getattr(getattr(self.voice_client, "_connection", None), "dave_protocol_version", "unknown"),
            bool(getattr(getattr(getattr(self.voice_client, "_connection", None), "dave_session", None), "ready", False)),
        )

        self.sink = RecorderSink(self)
        LOGGER.info(
            "Recorder sink configured debug_mode=%s decode_source=library_pcm opus_probe=%s",
            self.debug_mode,
            self.debug_mode == "opus",
        )

        self.voice_client.listen(self.sink)
        LOGGER.info("Recorder connected to voice channel %s", channel.name)

    async def _silence_watchdog(self) -> None:
        while not self.bot.is_closed():
            await asyncio.sleep(0.2)
            if self.sink is not None:
                self.sink.tick_silence(200)

    def submit_utterance(
        self,
        user: discord.abc.User,
        raw_pcm: bytes,
        mono_pcm: bytes,
        opus_frames: list[bytes],
        frame_logs: list[dict[str, object]],
        duration_ms: int,
        stats: StreamStats,
        mode: str,
    ) -> None:
        self._dump_utterance(user, raw_pcm, mono_pcm, opus_frames, frame_logs, duration_ms, stats, mode)

    def _dump_utterance(
        self,
        user: discord.abc.User,
        raw_pcm: bytes,
        mono_pcm: bytes,
        opus_frames: list[bytes],
        frame_logs: list[dict[str, object]],
        duration_ms: int,
        stats: StreamStats,
        mode: str,
    ) -> None:
        RECORDER_DUMPS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in user.display_name).strip("_")
        safe_name = safe_name or f"user_{user.id}"
        base = RECORDER_DUMPS_DIR / f"{timestamp}-{safe_name}-{mode}"

        raw_wav_path = base.with_name(base.name + "-raw48k").with_suffix(".wav")
        self._write_raw_wav(raw_wav_path, raw_pcm)

        mono_wav_path = base.with_name(base.name + "-mono16k").with_suffix(".wav")
        mono_wav_path.write_bytes(pcm_to_wav_bytes(mono_pcm))

        opus_path = base.with_suffix(".opus")
        self._write_opus_dump(opus_path, opus_frames)

        frame_log_path = base.with_suffix(".frames.jsonl")
        self._write_frame_logs(frame_log_path, frame_logs)

        txt_path = base.with_suffix(".txt")
        txt_path.write_text(
            "\n".join(
                [
                    f"user: {user.display_name}",
                    f"user_id: {user.id}",
                    f"mode: {mode}",
                    f"duration_ms: {duration_ms}",
                    f"ssrc: {stats.ssrc}",
                    f"first_sequence: {stats.first_sequence}",
                    f"last_sequence: {stats.last_sequence}",
                    f"frame_count: {stats.frame_count}",
                    f"decode_errors: {stats.decode_errors}",
                    f"probe_decode_errors: {stats.probe_decode_errors}",
                    f"packet_loss_events: {stats.packet_loss_events}",
                    f"packets_lost: {stats.packets_lost}",
                    f"out_of_order_events: {stats.out_of_order_events}",
                    f"raw_wav: {raw_wav_path.name}",
                    f"mono_wav: {mono_wav_path.name}",
                    f"opus_dump: {opus_path.name}",
                    f"frame_log: {frame_log_path.name}",
                    f"raw_bytes: {len(raw_pcm)}",
                    f"mono_bytes: {len(mono_pcm)}",
                    f"opus_frames: {len(opus_frames)}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        LOGGER.info(
            "Recorder dumped %s mode=%s duration=%sms raw=%s mono=%s opus=%s",
            user.display_name,
            mode,
            duration_ms,
            raw_wav_path,
            mono_wav_path,
            opus_path,
        )

    def _write_raw_wav(self, path: Path, raw_pcm: bytes) -> None:
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(2)
            wav_file.setsampwidth(2)
            wav_file.setframerate(48000)
            wav_file.writeframes(raw_pcm)

    def _write_opus_dump(self, path: Path, opus_frames: list[bytes]) -> None:
        with path.open("wb") as opus_file:
            for frame in opus_frames:
                opus_file.write(frame)

    def _write_frame_logs(self, path: Path, frame_logs: list[dict[str, object]]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for entry in frame_logs:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def run(self) -> None:
        self.bot.run(self.settings.discord_bot_token)


def _patch_voice_recv_decrypt_logging() -> None:
    global _VOICE_RECV_DECRYPT_PATCHED
    if _VOICE_RECV_DECRYPT_PATCHED:
        return

    mode_names = [
        "_decrypt_rtp_aead_xchacha20_poly1305_rtpsize",
        "_decrypt_rtp_xsalsa20_poly1305_lite",
        "_decrypt_rtp_xsalsa20_poly1305_suffix",
        "_decrypt_rtp_xsalsa20_poly1305",
    ]

    for mode_name in mode_names:
        original = getattr(voice_recv.reader.PacketDecryptor, mode_name)

        def _make_wrapper(original_func, wrapped_mode_name):
            def _logged_decrypt_rtp(self: voice_recv.reader.PacketDecryptor, packet: object) -> bytes:
                encrypted_data = bytes(getattr(packet, "data", b""))
                header = bytes(getattr(packet, "header", b""))
                LOGGER.info(
                    "Decrypt RTP mode=%s impl=%s ssrc=%s seq=%s payload=%s extended=%s header_len=%s data_len=%s data_first8=%s",
                    getattr(self, "mode", "unknown"),
                    wrapped_mode_name,
                    getattr(packet, "ssrc", None),
                    getattr(packet, "sequence", None),
                    getattr(packet, "payload", None),
                    getattr(packet, "extended", None),
                    len(header),
                    len(encrypted_data),
                    encrypted_data[:8].hex(),
                )
                try:
                    decrypted = original_func(self, packet)
                except Exception:
                    LOGGER.exception(
                        "Decrypt failed mode=%s impl=%s ssrc=%s seq=%s",
                        getattr(self, "mode", "unknown"),
                        wrapped_mode_name,
                        getattr(packet, "ssrc", None),
                        getattr(packet, "sequence", None),
                    )
                    raise

                LOGGER.info(
                    "Decrypt OK mode=%s impl=%s ssrc=%s seq=%s decrypted_len=%s decrypted_first8=%s",
                    getattr(self, "mode", "unknown"),
                    wrapped_mode_name,
                    getattr(packet, "ssrc", None),
                    getattr(packet, "sequence", None),
                    len(decrypted),
                    decrypted[:8].hex(),
                )
                return decrypted

            return _logged_decrypt_rtp

        setattr(
            voice_recv.reader.PacketDecryptor,
            mode_name,
            _make_wrapper(original, mode_name),
        )

    _VOICE_RECV_DECRYPT_PATCHED = True
