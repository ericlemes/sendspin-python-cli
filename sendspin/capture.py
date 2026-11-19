"""Diagnostic PCM capture with frame-exact files and a timestamped JSONL journal."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from aiosendspin.models.types import AudioCodec

from sendspin.decoder import FlacDecoder

if TYPE_CHECKING:
    from aiosendspin.client import AudioFormat, SendspinClient
    from aiosendspin.models.base import SendspinModel
    from aiosendspin.models.core import StreamStartMessage

logger = logging.getLogger(__name__)

CHUNK_SECONDS = 600


@dataclass
class _Record:
    event: dict[str, object]
    audio: bytes | None = None
    fmt: AudioFormat | None = None


class _CaptureWriter:
    """Write queued records on the capture thread only."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._events = (directory / "events.jsonl").open("x", encoding="utf-8")
        self._wave: wave.Wave_write | None = None
        self._format: AudioFormat | None = None
        self._decoder: FlacDecoder | None = None
        self._file_number = 0
        self._file_frames = 0
        self._packet_number = 0

    def event(self, event: dict[str, object]) -> None:
        """Append a complete JSON object, flushing so live captures are readable."""
        self._events.write(json.dumps(event, ensure_ascii=True) + "\n")
        self._events.flush()

    def boundary(self) -> None:
        """Finish a partial file and reset decoding across stream discontinuities."""
        self._close_wave()
        self._format = None
        self._decoder = None

    def _close_wave(self) -> None:
        if self._wave is not None:
            self._wave.close()
            self._wave = None

    def write(self, record: _Record) -> None:
        """Persist a protocol event or decoded audio packet."""
        if record.fmt is None:
            self.event(record.event)
            if record.event["type"] in {
                "stream_start",
                "stream_clear",
                "stream_end",
                "disconnect",
                "client_attached",
            } and record.event.get("player_boundary", True):
                self.boundary()
            return

        fmt = record.fmt
        assert record.audio is not None
        if fmt != self._format:
            self.boundary()
            self._format = fmt
            if fmt.codec == AudioCodec.FLAC:
                self._decoder = FlacDecoder(fmt)
            elif fmt.codec != AudioCodec.PCM:
                raise ValueError(f"Unsupported capture codec: {fmt.codec}")
            self.event(
                {
                    **record.event,
                    "type": "format",
                    "codec": fmt.codec.value,
                    "sample_rate": fmt.pcm_format.sample_rate,
                    "bit_depth": fmt.pcm_format.bit_depth,
                    "channels": fmt.pcm_format.channels,
                    "codec_header_hex": (
                        fmt.codec_header.hex() if fmt.codec_header is not None else None
                    ),
                }
            )

        pcm = self._decoder.decode(record.audio) if self._decoder is not None else record.audio
        self._packet_number += 1
        if not pcm:
            self.event({**record.event, "type": "decode_no_output", "packet": self._packet_number})
            raise ValueError(f"Capture decoder produced no PCM for packet {self._packet_number}")
        frame_size = fmt.pcm_format.frame_size
        if len(pcm) % frame_size:
            raise ValueError("Capture PCM packet does not contain complete sample frames")

        total_frames = len(pcm) // frame_size
        offset = 0
        while offset < total_frames:
            if self._wave is None:
                self._file_number += 1
                self._file_frames = 0
                self._wave = wave.open(
                    str(self._directory / f"audio-{self._file_number:06d}.wav"), "wb"
                )
                self._wave.setparams(
                    (
                        fmt.pcm_format.channels,
                        fmt.pcm_format.bit_depth // 8,
                        fmt.pcm_format.sample_rate,
                        0,
                        "NONE",
                        "not compressed",
                    )
                )
            count = min(
                total_frames - offset,
                fmt.pcm_format.sample_rate * CHUNK_SECONDS - self._file_frames,
            )
            self._wave.writeframes(pcm[offset * frame_size : (offset + count) * frame_size])
            self.event(
                {
                    **record.event,
                    "packet": self._packet_number,
                    "file": f"audio-{self._file_number:06d}.wav",
                    "file_frame_offset": self._file_frames,
                    "frame_count": count,
                    "packet_frame_offset": offset,
                    "packet_frame_count": total_frames,
                    "encoded_bytes": len(record.audio),
                }
            )
            offset += count
            self._file_frames += count
            if self._file_frames == fmt.pcm_format.sample_rate * CHUNK_SECONDS:
                self._close_wave()

    def close(self, error: str | None) -> None:
        """Finalize WAV headers and record whether the capture completed cleanly."""
        try:
            self._close_wave()
            self.event({"type": "capture_end", "complete": error is None, "error": error})
        finally:
            self._events.close()


class AudioCapture:
    """Capture incoming audio independently of playback, with bounded buffering."""

    def __init__(self, directory: str) -> None:
        """Create a unique capture directory and start its disk/decoder worker."""
        root = Path(directory).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.directory = Path(
            tempfile.mkdtemp(prefix=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-"), dir=root)
        )
        (self.directory / "session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "started_at": datetime.now(UTC).isoformat(),
                    "chunk_seconds": CHUNK_SECONDS,
                    "audio": "signed little-endian interleaved PCM in WAV containers",
                    "capture_point": "received packets, independently decoded before playback",
                    "timestamp_unit": "microseconds",
                    "events": "events.jsonl",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._writer = _CaptureWriter(self.directory)
        self._queue: queue.Queue[_Record] = queue.Queue(maxsize=512)
        self._closing = threading.Event()
        self._error: str | None = None
        self._client: SendspinClient | None = None
        self._unsubscribers: list[Callable[[], None]] = []
        self._sequence = 0
        self._thread = threading.Thread(target=self._run, name="sendspin-capture", daemon=True)
        self._thread.start()
        logger.info("Saving diagnostic audio capture to %s", self.directory)

    def attach_client(self, client: SendspinClient) -> None:
        """Observe full metadata models, stream lifecycle, and encoded audio packets."""
        self.detach_client()
        self._client = client
        self.record("client_attached")
        self._unsubscribers = [
            client.add_audio_chunk_listener(self._on_audio),
            client.add_metadata_listener(partial(self._on_model, "metadata_current")),
            client.add_scheduled_metadata_listener(partial(self._on_model, "metadata_scheduled")),
            client.add_controller_state_listener(partial(self._on_model, "controller_state")),
            client.add_group_update_listener(partial(self._on_model, "group_update")),
            client.add_stream_start_listener(self._on_stream_start),
            client.add_stream_clear_listener(partial(self._on_roles, "stream_clear")),
            client.add_stream_end_listener(partial(self._on_roles, "stream_end")),
            client.add_disconnect_listener(partial(self.record, "disconnect")),
        ]

    def detach_client(self) -> None:
        """Remove capture listeners without stopping the capture worker."""
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._client = None

    def _context(self, kind: str) -> dict[str, object]:
        self._sequence += 1
        client = self._client
        local_us = client.now_us() if client is not None else None
        synced = client is not None and client.is_time_synchronized()
        server = client.server_info if client is not None else None
        delay_ms = client.output_delay_ms if client is not None else 0.0
        return {
            "type": kind,
            "sequence": self._sequence,
            "observed_at": datetime.now(UTC).isoformat(),
            "observed_monotonic_ns": time.monotonic_ns(),
            "client_time_us": local_us,
            "estimated_server_time_us": (
                # compute_server_time() includes the playback delay inverse.
                client.compute_server_time(local_us - round(delay_ms * 1000))
                if synced and client is not None and local_us is not None
                else None
            ),
            "clock_synchronized": synced,
            "output_delay_ms": delay_ms,
            "server_id": server.server_id if server is not None else None,
            "server_name": server.name if server is not None else None,
        }

    def record(self, kind: str, **data: object) -> None:
        """Queue a timestamped lifecycle event."""
        self._enqueue(_Record({**self._context(kind), **data}))

    def _on_model(self, kind: str, payload: SendspinModel | None) -> None:
        self.record(kind, payload=payload.to_dict() if payload is not None else None)

    def _on_stream_start(self, payload: StreamStartMessage) -> None:
        data = payload.to_dict()
        self.record(
            "stream_start", payload=data, player_boundary=payload.payload.player is not None
        )

    def _on_roles(self, kind: str, roles: list[str] | None) -> None:
        self.record(kind, roles=roles, player_boundary=roles is None or "player" in roles)

    def _on_audio(
        self, timestamp_us: int, audio: bytes, fmt: AudioFormat, send_ahead_us: int
    ) -> None:
        self._enqueue(
            _Record(
                {
                    **self._context("audio"),
                    "server_timestamp_us": timestamp_us,
                    "send_ahead_us": send_ahead_us,
                },
                bytes(audio),
                fmt,
            )
        )

    def _enqueue(self, record: _Record) -> None:
        if self._closing.is_set():
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._error = "Capture queue full: recording stopped; data is incomplete"
            logger.error("%s (%s)", self._error, self.directory)
            self._closing.set()

    def _run(self) -> None:
        try:
            while True:
                try:
                    record = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if self._closing.is_set():
                        break
                    continue
                self._writer.write(record)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            logger.exception("Diagnostic capture failed in %s", self.directory)
            self._closing.set()
        finally:
            try:
                self._writer.close(self._error)
            except (OSError, wave.Error):
                logger.exception("Failed to finalize diagnostic capture in %s", self.directory)

    async def close(self) -> None:
        """Stop accepting events and drain every accepted record before returning."""
        self.detach_client()
        self._closing.set()
        await asyncio.to_thread(self._thread.join)
