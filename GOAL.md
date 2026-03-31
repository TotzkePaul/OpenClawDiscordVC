# Project Goal

This project turns OpenClaw into a live Discord voice participant.

The intended end-to-end loop is:

`Discord microphone / voice chat -> speech-to-text -> OpenClaw reasoning -> text response -> text-to-speech -> Discord voice playback`

The practical goal is to let people in a Discord voice channel talk naturally with a local OpenClaw-based agent, with the bot joining the channel, listening for speech, generating a response, and speaking that response back into the same channel.

# High-Level Design

The system is designed as a small bridge service with clear stages:

1. Discord voice receive
2. Speech transcription
3. OpenClaw chat/reasoning
4. Speech synthesis
5. Discord voice playback

Each stage is intentionally separated so that we can swap implementations without rewriting the whole pipeline.

# Current Architecture

## 1. Discord Receive

The bot connects to a configured Discord voice channel and listens for incoming audio.

Current implementation:
- `discord.py`
- `discord-ext-voice-recv`
- custom sink/orchestrator in [app/discord_voice_bot.py](C:/Users/DeepThought/source/repos/OpenClawDiscordVC/app/discord_voice_bot.py)

Responsibilities:
- join the configured guild/channel
- receive voice packets
- convert Discord PCM audio into 16 kHz mono PCM
- buffer per-user utterances
- detect utterance boundaries using silence and max-duration thresholds

## 2. Speech-to-Text

Incoming audio is sent to a Wyoming-compatible ASR service backed by faster-whisper.

Current implementation:
- [app/faster_whisper_client.py](C:/Users/DeepThought/source/repos/OpenClawDiscordVC/app/faster_whisper_client.py)

Responsibilities:
- open a TCP connection to the Wyoming ASR server
- stream PCM audio
- request transcription
- return transcript text back to the orchestrator

## 3. OpenClaw Reasoning

Transcribed user speech is sent to OpenClaw through an OpenAI-compatible chat completions API.

Current implementation:
- [app/openclaw_client.py](C:/Users/DeepThought/source/repos/OpenClawDiscordVC/app/openclaw_client.py)

Responsibilities:
- maintain short rolling conversation context
- send user text plus system prompt to OpenClaw
- receive the assistant response text

## 4. Text-to-Speech

OpenClaw’s response text is converted to speech using a self-hosted Kokoro FastAPI service that implements the OpenAI-compatible speech endpoint.

Current implementation:
- [app/openai_tts_tool.py](C:/Users/DeepThought/source/repos/OpenClawDiscordVC/app/openai_tts_tool.py)

Responsibilities:
- call `POST /v1/audio/speech`
- send `model`, `input`, `voice`
- request `response_format="wav"`
- return audio bytes or save a WAV file

## 5. Discord Playback

Synthesized WAV audio is played back into the Discord voice channel.

Current implementation:
- playback orchestration in [app/discord_voice_bot.py](C:/Users/DeepThought/source/repos/OpenClawDiscordVC/app/discord_voice_bot.py)
- `ffmpeg` is required locally for playback

Responsibilities:
- write synthesized audio to a temp WAV file
- play it with `discord.FFmpegPCMAudio`
- avoid overlapping responses

# Design Principles

## Modular components

Each networked service is wrapped in a small client class so components can be replaced independently.

Examples:
- faster-whisper/Wyoming could be replaced by another STT service
- Kokoro could be replaced by another OpenAI-compatible TTS backend
- OpenClaw could be replaced by another local chat endpoint with the same API shape

## Local-first operation

The project is intended to run primarily on a local network with self-hosted services:
- local OpenClaw
- local faster-whisper ASR
- local Kokoro TTS

This keeps latency low and avoids depending on public cloud APIs for the main loop.

## Reusable tool boundaries

The TTS layer is implemented as a reusable tool, not embedded directly into Discord logic.

That matters because the same TTS tool can later be reused for:
- CLI experiments
- agent tools
- web UI playback
- offline file generation

## Fail-soft behavior

The bridge should avoid crashing the full session when a single dependency misbehaves.

Examples:
- if TTS fails, the bot should still be able to log text replies
- if playback fails, transcription and reasoning should still be debuggable
- if Discord voice packets are malformed, the bot should degrade as safely as possible

# Current Known Constraints

## FFmpeg is required for playback

The bot can connect and reason without `ffmpeg`, but it cannot speak back into Discord without it.

## Discord receive is still the riskiest part

The hardest part of this project is reliable Discord voice receive.

Current logs show recurring `discord.opus.OpusError: corrupted stream` issues when decoding incoming audio. The project already contains defensive patches to prevent the whole receive loop from crashing, but receive reliability is still an active integration risk.

## OpenClaw endpoint availability matters

The chat stage assumes OpenClaw is reachable and exposes an OpenAI-compatible chat completions endpoint.

## TTS output format must be requested explicitly

Kokoro may return compressed audio by default. The tool requests `response_format="wav"` so saved output is real WAV data.

# What Success Looks Like

The project is successful when:

- the bot joins the target Discord voice channel automatically
- a user speaks naturally in the channel
- the utterance is transcribed correctly
- OpenClaw produces a relevant response
- Kokoro synthesizes that response into WAV audio
- the bot plays the response back into Discord
- the cycle repeats reliably for multiple turns

# Near-Term Priorities

1. Stabilize Discord voice receive and Opus decoding.
2. Confirm end-to-end transcription on real speech, not just synthetic test data.
3. Verify OpenClaw response generation from live transcripts.
4. Verify Kokoro synthesis plus Discord playback with `ffmpeg` installed.
5. Improve turn-taking, filtering, and user experience.

# Future Improvements

- Wake word or push-to-talk support
- Per-user voice settings
- Better silence / VAD handling
- Persistent conversation memory
- Safer queueing for multi-user voice channels
- Better observability and structured logs
- Explicit tool layer for agent-triggered TTS outside Discord
