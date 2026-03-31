from __future__ import annotations

import logging
from typing import Any

from discord import opus as discord_opus
from discord.ext import voice_recv

try:
    import davey
except ImportError:  # pragma: no cover - discord.py 2.7 voice requires davey, but keep fallback safe.
    davey = None


LOGGER = logging.getLogger(__name__)
_VOICE_RECV_PATCHED = False
_SEEN_DAVE_AUDIO_STREAMS: set[tuple[int, int]] = set()
_DECODE_ERROR_LOG_INTERVAL = 25


def patch_voice_recv() -> None:
    global _VOICE_RECV_PATCHED
    if _VOICE_RECV_PATCHED:
        return

    original_process_packet = voice_recv.opus.PacketDecoder._process_packet
    original_decode_packet = voice_recv.opus.PacketDecoder._decode_packet

    def _patched_process_packet(self: voice_recv.opus.PacketDecoder, packet: Any) -> voice_recv.VoiceData:
        _maybe_decrypt_dave(self, packet)
        return original_process_packet(self, packet)

    def _patched_decode_packet(self: voice_recv.opus.PacketDecoder, packet: Any) -> tuple[Any, bytes]:
        try:
            result = original_decode_packet(self, packet)
            _clear_decode_error_state(self)
            return result
        except discord_opus.OpusError as exc:
            _record_decode_error(self, packet, exc)
            plc_pcm = _decode_packet_loss_concealment(self)
            self._decoder = None if self.sink.wants_opus() else discord_opus.Decoder()
            return packet, plc_pcm

    voice_recv.opus.PacketDecoder._process_packet = _patched_process_packet
    voice_recv.opus.PacketDecoder._decode_packet = _patched_decode_packet
    _VOICE_RECV_PATCHED = True


def _maybe_decrypt_dave(decoder: voice_recv.opus.PacketDecoder, packet: Any) -> None:
    if davey is None or not packet:
        return

    payload = getattr(packet, "decrypted_data", None)
    if not payload:
        return

    voice_client = decoder.sink.voice_client
    if voice_client is None:
        return

    connection = getattr(voice_client, "_connection", None)
    if connection is None:
        return

    session = getattr(connection, "dave_session", None)
    protocol_version = getattr(connection, "dave_protocol_version", 0)
    if protocol_version == 0 or session is None or not getattr(session, "ready", False):
        return

    user_id = decoder._cached_id or voice_client._get_id_from_ssrc(decoder.ssrc)  # type: ignore[attr-defined]
    if not user_id:
        return

    try:
        packet.decrypted_data = session.decrypt(user_id, davey.MediaType.audio, payload)
        stream_key = (decoder.ssrc, user_id)
        if stream_key not in _SEEN_DAVE_AUDIO_STREAMS:
            _SEEN_DAVE_AUDIO_STREAMS.add(stream_key)
            LOGGER.info(
                "DAVE audio decrypt active for ssrc=%s user_id=%s protocol=%s ready=%s",
                decoder.ssrc,
                user_id,
                protocol_version,
                getattr(session, "ready", False),
            )
    except Exception as exc:
        if _is_unencrypted_passthrough_error(exc):
            LOGGER.warning(
                "DAVE passthrough fallback for ssrc=%s user_id=%s seq=%s ts=%s len=%s",
                decoder.ssrc,
                user_id,
                getattr(packet, "sequence", None),
                getattr(packet, "timestamp", None),
                len(payload),
            )
            try:
                session.set_passthrough_mode(True, 10)
            except Exception:
                LOGGER.exception("Failed to enable DAVE passthrough mode")
            packet.decrypted_data = payload
            return

        LOGGER.exception(
            "DAVE audio decrypt failed for ssrc=%s user_id=%s seq=%s ts=%s len=%s",
            decoder.ssrc,
            user_id,
            getattr(packet, "sequence", None),
            getattr(packet, "timestamp", None),
            len(payload),
        )
        packet.decrypted_data = b""


def _is_unencrypted_passthrough_error(exc: Exception) -> bool:
    message = str(exc)
    return "UnencryptedWhenPassthroughDisabled" in message


def _record_decode_error(decoder: voice_recv.opus.PacketDecoder, packet: Any, exc: Exception) -> None:
    signature = (
        getattr(packet, "timestamp", None),
        len(getattr(packet, "decrypted_data", b"") or b""),
        type(exc).__name__,
        str(exc),
    )
    previous_signature = getattr(decoder, "_codex_last_decode_error_signature", None)
    previous_count = getattr(decoder, "_codex_last_decode_error_count", 0)

    if signature == previous_signature:
        count = previous_count + 1
    else:
        if previous_count > 1:
            LOGGER.warning(
                "Suppressed %s repeated Opus decode failures for ssrc=%s last_ts=%s",
                previous_count - 1,
                decoder.ssrc,
                previous_signature[0] if previous_signature else None,
            )
        count = 1

    decoder._codex_last_decode_error_signature = signature
    decoder._codex_last_decode_error_count = count

    if count == 1 or count % _DECODE_ERROR_LOG_INTERVAL == 0:
        LOGGER.warning(
            "Opus decode failed for ssrc=%s seq=%s ts=%s len=%s dave=%s err=%s repeat=%s",
            decoder.ssrc,
            getattr(packet, "sequence", None),
            getattr(packet, "timestamp", None),
            len(getattr(packet, "decrypted_data", b"") or b""),
            _dave_state(decoder),
            exc,
            count,
        )


def _clear_decode_error_state(decoder: voice_recv.opus.PacketDecoder) -> None:
    previous_signature = getattr(decoder, "_codex_last_decode_error_signature", None)
    previous_count = getattr(decoder, "_codex_last_decode_error_count", 0)
    if previous_signature is not None and previous_count > 1:
        LOGGER.warning(
            "Suppressed %s repeated Opus decode failures for ssrc=%s last_ts=%s",
            previous_count - 1,
            decoder.ssrc,
            previous_signature[0],
        )
    decoder._codex_last_decode_error_signature = None
    decoder._codex_last_decode_error_count = 0


def _decode_packet_loss_concealment(decoder: voice_recv.opus.PacketDecoder) -> bytes:
    inner = getattr(decoder, "_decoder", None)
    if inner is None:
        return b""

    try:
        pcm = inner.decode(None, fec=False)
    except Exception:
        return b""

    return pcm or b""


def _dave_state(decoder: voice_recv.opus.PacketDecoder) -> str:
    voice_client = decoder.sink.voice_client
    if voice_client is None:
        return "no_voice_client"

    connection = getattr(voice_client, "_connection", None)
    if connection is None:
        return "no_connection"

    session = getattr(connection, "dave_session", None)
    protocol_version = getattr(connection, "dave_protocol_version", 0)
    ready = bool(session and getattr(session, "ready", False))
    return f"protocol={protocol_version} ready={ready}"
