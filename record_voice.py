from __future__ import annotations

import logging

from app.config import load_settings
from app.discord_voice_recorder import VoiceRecorder


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)
    settings = load_settings()
    recorder = VoiceRecorder(settings)
    recorder.run()


if __name__ == "__main__":
    main()
