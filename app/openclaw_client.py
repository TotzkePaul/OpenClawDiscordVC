from __future__ import annotations

from dataclasses import dataclass, field

import aiohttp


@dataclass(slots=True)
class OpenClawClient:
    base_url: str
    chat_path: str
    model: str
    system_prompt: str
    api_key: str | None = None
    _history: list[dict[str, str]] = field(default_factory=list)

    async def get_response(self, user_text: str) -> str:
        messages = [{"role": "system", "content": self.system_prompt}, *self._history]
        messages.append({"role": "user", "content": user_text})

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.6,
            "stream": False,
        }

        async with aiohttp.ClientSession(base_url=self.base_url, headers=headers) as session:
            async with session.post(self.chat_path, json=payload) as response:
                response.raise_for_status()
                data = await response.json()

        text = data["choices"][0]["message"]["content"].strip()
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": text})
        self._history = self._history[-20:]
        return text
