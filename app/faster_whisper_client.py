from __future__ import annotations

from dataclasses import dataclass

from wyoming.asr import Transcript, Transcribe
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient


@dataclass(slots=True)
class FasterWhisperClient:
    host: str
    port: int

    async def transcribe_pcm(self, pcm: bytes, rate: int = 16000, channels: int = 1, width: int = 2) -> str:
        async with AsyncTcpClient(self.host, self.port) as client:
            await client.write_event(AudioStart(rate=rate, width=width, channels=channels).event())
            await client.write_event(AudioChunk(rate=rate, width=width, channels=channels, audio=pcm).event())
            await client.write_event(AudioStop().event())
            await client.write_event(Transcribe().event())

            while True:
                event = await client.read_event()
                transcript = Transcript.from_event(event)
                if transcript is not None:
                    return transcript.text.strip()
