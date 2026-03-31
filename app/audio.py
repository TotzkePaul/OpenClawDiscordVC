from __future__ import annotations

import audioop
import io
import wave

import discord


DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
SAMPLE_WIDTH = 2
DISCORD_FRAME_SAMPLES = 960
DISCORD_FRAME_BYTES = DISCORD_FRAME_SAMPLES * DISCORD_CHANNELS * SAMPLE_WIDTH


def discord_pcm_to_mono16(pcm: bytes) -> bytes:
    mono = audioop.tomono(pcm, SAMPLE_WIDTH, 0.5, 0.5)
    converted, _ = audioop.ratecv(mono, SAMPLE_WIDTH, 1, DISCORD_SAMPLE_RATE, TARGET_SAMPLE_RATE, None)
    return converted


def pcm_duration_ms(pcm: bytes, rate: int = TARGET_SAMPLE_RATE, channels: int = TARGET_CHANNELS) -> int:
    frame_count = len(pcm) / (SAMPLE_WIDTH * channels)
    return int((frame_count / rate) * 1000)


def pcm_to_wav_bytes(
    pcm: bytes,
    rate: int = TARGET_SAMPLE_RATE,
    channels: int = TARGET_CHANNELS,
    width: int = SAMPLE_WIDTH,
) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


class WavPcmAudioSource(discord.AudioSource):
    def __init__(self, wav_bytes: bytes) -> None:
        self._reader = wave.open(io.BytesIO(wav_bytes), "rb")
        self._width = self._reader.getsampwidth()
        self._channels = self._reader.getnchannels()
        self._rate = self._reader.getframerate()
        self._rate_state = None
        self._buffer = bytearray()
        self._done = False

        if self._width != SAMPLE_WIDTH:
            raise RuntimeError(f"Unsupported WAV sample width: {self._width}")

    def read(self) -> bytes:
        while len(self._buffer) < DISCORD_FRAME_BYTES and not self._done:
            pcm = self._reader.readframes(DISCORD_FRAME_SAMPLES)
            if not pcm:
                self._done = True
                break

            if self._channels == 1:
                pcm = audioop.tostereo(pcm, SAMPLE_WIDTH, 1.0, 1.0)
            elif self._channels != DISCORD_CHANNELS:
                raise RuntimeError(f"Unsupported WAV channel count: {self._channels}")

            if self._rate != DISCORD_SAMPLE_RATE:
                pcm, self._rate_state = audioop.ratecv(
                    pcm,
                    SAMPLE_WIDTH,
                    DISCORD_CHANNELS,
                    self._rate,
                    DISCORD_SAMPLE_RATE,
                    self._rate_state,
                )

            self._buffer.extend(pcm)

        if not self._buffer:
            return b""

        frame = bytes(self._buffer[:DISCORD_FRAME_BYTES])
        del self._buffer[:DISCORD_FRAME_BYTES]

        if len(frame) < DISCORD_FRAME_BYTES:
            frame = frame + (b"\x00" * (DISCORD_FRAME_BYTES - len(frame)))

        return frame

    def cleanup(self) -> None:
        try:
            self._reader.close()
        except Exception:
            pass
