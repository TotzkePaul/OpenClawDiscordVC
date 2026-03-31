# OpenClaw Discord Voice Bridge

This project wires a Discord voice channel into an OpenClaw-style agent loop:

`Discord voice -> Wyoming faster-whisper -> text -> OpenClaw chat API -> text -> Kokoro OpenAI-compatible TTS -> Discord voice`

## What is included

- Discord voice receive and playback
- Audio conversion from Discord PCM to 16 kHz mono PCM for faster-whisper
- Wyoming ASR client shaped from your validated sample
- OpenAI-compatible chat client for OpenClaw
- OpenAI-compatible TTS tool for speaking the response back into Discord
- Optional transcript logging to a Discord text channel

## Assumptions

- OpenClaw exposes an OpenAI-compatible chat completions endpoint
- faster-whisper is reachable through a Wyoming ASR server
- TTS is reachable through a Kokoro FastAPI server that implements `POST /v1/audio/speech`
- `ffmpeg` is installed and available on `PATH`

If your OpenClaw API shape differs, update `app/openclaw_client.py`.
If your Kokoro defaults differ, update `app/openai_tts_tool.py` or the related environment variables.

## Setup

1. Create a virtual environment and install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Copy the example environment file and fill in your values:

```powershell
Copy-Item .env.example .env
```

3. Start your Wyoming ASR service and your Kokoro TTS service.

4. Run the bot:

```powershell
python main.py
```

## Recorder mode

To debug Discord receive without STT, OpenClaw, or TTS, run the standalone recorder:

```powershell
python record_voice.py
```

It will write captured utterances into `dumps/recorder/` as WAV, Opus, and TXT artifacts.

Recorder debug modes:

```powershell
$env:VOICE_DEBUG_MODE="opus"
python record_voice.py
```

`VOICE_DEBUG_MODE=opus`:
- uses `discord-ext-voice-recv`'s PCM decode path for the actual WAV output
- keeps streams isolated per SSRC
- also runs a probe-only per-SSRC Opus decode for diagnostics without affecting output audio
- logs RTP structure, packet ordering, decrypt state, and probe failures

```powershell
$env:VOICE_DEBUG_MODE="pcm"
python record_voice.py
```

`VOICE_DEBUG_MODE=pcm`:
- uses the library PCM decode path without the extra probe decoder

Mode comparison:
- If both modes sound clean, the recorder is no longer corrupting packets itself.
- If the WAV is clean but `probe_decode_errors` is high, the manual per-packet Opus probe is unreliable and should not be used for production decode decisions.
- If both modes still sound corrupted, the problem is earlier in RTP/decrypt/stream handling.

Voice privacy note:
- Modern `discord.py` voice connections can negotiate DAVE end-to-end media encryption on top of RTP/SRTP.
- The project now patches the receive path to DAVE-decrypt audio before Opus decode when a DAVE session is active.

## Environment variables

- `DISCORD_BOT_TOKEN`: Discord bot token
- `DISCORD_GUILD_ID`: Guild the bot should join
- `DISCORD_VOICE_CHANNEL_ID`: Voice channel to connect to
- `DISCORD_TEXT_CHANNEL_ID`: Optional text channel for transcripts and replies
- `OPENCLAW_BASE_URL`: Base URL for OpenClaw
- `OPENCLAW_CHAT_PATH`: Chat completions path
- `OPENCLAW_API_KEY`: Optional bearer token
- `OPENCLAW_MODEL`: Model identifier sent to OpenClaw
- `OPENCLAW_SYSTEM_PROMPT`: Spoken conversation behavior
- `WYOMING_ASR_HOST` / `WYOMING_ASR_PORT`: faster-whisper Wyoming server
- `KOKORO_BASE_URL`: Base URL for the Kokoro FastAPI server
- `KOKORO_API_KEY`: Placeholder bearer token for the OpenAI-compatible speech API
- `KOKORO_MODEL`: Model name sent to `/v1/audio/speech`
- `KOKORO_VOICE`: Default Kokoro voice
- `VOICE_SILENCE_MS`: Silence window before an utterance is flushed
- `VOICE_MIN_UTTERANCE_MS`: Ignore very short clips
- `VOICE_MAX_UTTERANCE_MS`: Force flush long utterances
- `VOICE_RESPONSE_VOLUME`: Playback volume for replies

## Notes

- The bot serializes voice turns with a lock so responses do not overlap.
- Incoming Discord audio is downmixed from 48 kHz stereo to 16 kHz mono before ASR.
- The current implementation keeps a short rolling conversation history in memory.
- The voice receive layer uses `discord-ext-voice-recv`, which is designed for Discord receive streams.
- The TTS tool uses the standard OpenAI-compatible speech contract and requests `response_format="wav"` so saved files are actual WAV output.

## Next likely tweaks

- Add wake-word handling or push-to-talk gating
- Filter by a specific user or role
- Add persistent conversation memory
- Replace the generic OpenAI-compatible client with your exact OpenClaw API contract
- Add per-user or per-command voice selection for Kokoro
