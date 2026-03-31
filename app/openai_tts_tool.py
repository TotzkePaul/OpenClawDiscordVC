from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiohttp


@dataclass(slots=True)
class OpenAITTSTool:
    base_url: str
    api_key: str
    model: str
    voice: str
    speech_path: str = "/v1/audio/speech"
    response_format: str = "wav"

    async def synthesize_bytes(self, text: str, voice: str | None = None) -> bytes:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": text,
            "voice": voice or self.voice,
            "response_format": self.response_format,
        }

        async with aiohttp.ClientSession(base_url=self.base_url, headers=headers) as session:
            async with session.post(self.speech_path, json=payload) as response:
                response.raise_for_status()
                audio_bytes = await response.read()

        if not audio_bytes:
            raise RuntimeError("TTS server returned an empty audio response")

        return audio_bytes

    async def synthesize_to_wav(self, text: str, output_path: str | Path, voice: str | None = None) -> Path:
        audio_bytes = await self.synthesize_bytes(text, voice=voice)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio_bytes)
        return path
