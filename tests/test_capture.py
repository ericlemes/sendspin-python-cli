"""Tests for frame-exact diagnostic PCM capture and its metadata journal."""

from __future__ import annotations

import json
import queue
import wave
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import av
import pytest
from aiosendspin.client import AudioFormat, SendspinClient
from aiosendspin.client.models import PCMFormat, ServerInfo
from aiosendspin.models.core import ServerStatePayload
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, Roles
from aiosendspin.noise import Identity, InMemoryClientPairingStore

from sendspin.audio_connector import AudioStreamHandler
from sendspin.audio_devices import AudioDevice
from sendspin.capture import AudioCapture, _CaptureWriter, _Record
from sendspin.cli import parse_args
from sendspin.decoder import FlacDecoder


def _format(bit_depth: int = 16, sample_rate: int = 10) -> AudioFormat:
    return AudioFormat(AudioCodec.PCM, PCMFormat(sample_rate, 2, bit_depth))


def _audio(data: bytes, fmt: AudioFormat, timestamp: int = 1_234_567) -> _Record:
    return _Record({"type": "audio", "server_timestamp_us": timestamp}, data, fmt)


def _events(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.parametrize("bit_depth", [16, 24, 32])
def test_ten_minute_split_preserves_every_frame(tmp_path: Path, bit_depth: int) -> None:
    """Split inside packets at exactly 600 seconds without altering any PCM bytes."""
    fmt = _format(bit_depth)
    frame_size = fmt.pcm_format.frame_size
    # A low sample rate keeps this test small while exercising the real 600-second limit.
    frames_per_file = fmt.pcm_format.sample_rate * 600
    data = bytes(range(256)) * ((frames_per_file * 2 + 7) * frame_size // 256 + 1)
    data = data[: (frames_per_file * 2 + 7) * frame_size]
    writer = _CaptureWriter(tmp_path)
    writer.write(_audio(data[: 3 * frame_size], fmt))
    writer.write(_audio(data[3 * frame_size :], fmt, timestamp=9_876_543))
    writer.close(None)

    reconstructed = bytearray()
    frame_counts = []
    for path in sorted(tmp_path.glob("*.wav")):
        with wave.open(str(path), "rb") as wav:
            assert wav.getsampwidth() == bit_depth // 8
            assert wav.getnchannels() == 2
            assert wav.getframerate() == fmt.pcm_format.sample_rate
            frame_counts.append(wav.getnframes())
            reconstructed.extend(wav.readframes(wav.getnframes()))
    assert frame_counts == [frames_per_file, frames_per_file, 7]
    assert reconstructed == data
    index = [row for row in _events(tmp_path) if row["type"] == "audio"]
    assert [row["file_frame_offset"] for row in index] == [0, 3, 0, 0]
    assert [row["packet_frame_offset"] for row in index] == [
        0,
        0,
        frames_per_file - 3,
        frames_per_file * 2 - 3,
    ]
    assert [row["server_timestamp_us"] for row in index] == [
        1_234_567,
        9_876_543,
        9_876_543,
        9_876_543,
    ]
    assert sum(row["frame_count"] for row in index) == len(data) // frame_size


def test_format_and_player_boundaries_rotate_without_padding(tmp_path: Path) -> None:
    """Non-player events and timestamp gaps leave PCM alone; player clears rotate."""
    writer = _CaptureWriter(tmp_path)
    fmt = _format()
    writer.write(_audio(b"\x01" * 8, fmt))
    writer.write(_Record({"type": "stream_end", "player_boundary": False}))
    writer.write(_audio(b"\x02" * 4, fmt, timestamp=99_000_000))
    writer.write(_Record({"type": "stream_clear", "player_boundary": True}))
    writer.write(_audio(b"\x03" * 4, fmt, timestamp=100))
    writer.write(_audio(b"\x04" * 6, _format(24)))
    writer.close(None)

    data = []
    for path in sorted(tmp_path.glob("*.wav")):
        with wave.open(str(path), "rb") as wav:
            data.append(wav.readframes(wav.getnframes()))
    assert data == [b"\x01" * 8 + b"\x02" * 4, b"\x03" * 4, b"\x04" * 6]


async def test_metadata_round_trip_and_shutdown_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture full metadata, including progress and scheduled timestamps, before closing."""
    client = SendspinClient(
        Identity.generate(),
        "Capture test",
        [Roles.PLAYER, Roles.METADATA],
        pairing_store=InMemoryClientPairingStore(),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[SupportedAudioFormat(AudioCodec.PCM, 2, 48_000, 16)],
            buffer_capacity=32_000_000,
        ),
    )
    worker = Mock(stop=AsyncMock())
    monkeypatch.setattr("sendspin.audio_connector._AudioSyncWorker", Mock(return_value=worker))
    handler = AudioStreamHandler(
        AudioDevice(0, "Test device", 2, 48_000, True), capture_dir=str(tmp_path)
    )
    handler.attach_client(client)
    capture = handler._capture
    assert capture is not None
    payload = ServerStatePayload.from_dict(
        {
            "metadata": {
                "timestamp": 123_456_789,
                "title": "Diagnostic track",
                "artist": "Artist",
                "album_artist": "Album artist",
                "album": "Album",
                "year": 2026,
                "track": 7,
                "artwork_url": "https://example.com/art.jpg",
                "progress": {
                    "track_progress": 12_345,
                    "track_duration": 300_000,
                    "playback_speed": 1000,
                },
            },
            "_diagnostic": {"extra": [1, 2, 3]},
        }
    )
    # Exercise the actual SDK listener registrations, not just serialization helpers.
    for callback in client._scheduled_metadata_callbacks:
        callback(payload)
    for callback in client._metadata_callbacks:
        callback(payload)
        callback(None)
    for callback in client._audio_chunk_callbacks:
        callback(123_456_789, b"\x01\x00\x02\x00", _format(), 543_210)
    await handler.shutdown()

    events = _events(capture.directory)
    metadata = [row for row in events if row["type"].startswith("metadata_")]
    assert [row["type"] for row in metadata] == [
        "metadata_scheduled",
        "metadata_current",
        "metadata_current",
    ]
    assert ServerStatePayload.from_dict(metadata[0]["payload"]) == payload
    assert metadata[0]["payload"]["metadata"]["timestamp"] == 123_456_789
    assert metadata[0]["payload"]["metadata"]["progress"]["track_progress"] == 12_345
    assert metadata[2]["payload"] is None
    assert metadata[0]["estimated_server_time_us"] is None
    audio = next(row for row in events if row["type"] == "audio")
    assert audio["send_ahead_us"] == 543_210
    assert audio["frame_count"] == 1
    assert events[-1] == {"type": "capture_end", "complete": True, "error": None}
    assert not client._metadata_callbacks
    assert not client._audio_chunk_callbacks
    with wave.open(str(capture.directory / audio["file"]), "rb") as wav:
        assert wav.readframes(1) == b"\x01\x00\x02\x00"


def test_flac_capture_matches_decoded_pcm(tmp_path: Path) -> None:
    """Save decoded FLAC samples with the original packet timestamp and codec header."""
    fixture = Path(__file__).parent / "fixtures" / "almost_silent.flac"
    with av.open(str(fixture)) as container:
        stream = container.streams.audio[0]
        packet = next(packet for packet in container.demux(stream) if packet.dts is not None)
        fmt = AudioFormat(
            AudioCodec.FLAC,
            PCMFormat(stream.codec_context.sample_rate, stream.codec_context.channels, 16),
            b"fLaC\x80\x00\x00\x22" + (stream.codec_context.extradata or b""),
        )
        encoded = bytes(packet)
    expected = FlacDecoder(fmt).decode(encoded)
    writer = _CaptureWriter(tmp_path)
    writer.write(_audio(encoded, fmt))
    writer.close(None)
    with wave.open(str(tmp_path / "audio-000001.wav"), "rb") as wav:
        assert wav.readframes(wav.getnframes()) == expected
    assert _events(tmp_path)[0]["codec_header_hex"] == fmt.codec_header.hex()


async def test_capture_failure_is_explicit_and_finalizes_prior_audio(tmp_path: Path) -> None:
    """Malformed PCM cannot silently create a successful but incomplete capture."""
    capture = AudioCapture(str(tmp_path))
    capture._on_audio(1, b"\x00" * 4, _format(), 0)
    capture._on_audio(2, b"\x00" * 3, _format(), 0)
    await capture.close()
    end = _events(capture.directory)[-1]
    assert end["complete"] is False
    assert "complete sample frames" in end["error"]
    with wave.open(str(capture.directory / "audio-000001.wav"), "rb") as wav:
        assert wav.getnframes() == 1


async def test_queue_overflow_marks_capture_incomplete(tmp_path: Path) -> None:
    """Overload stops recording rather than silently dropping packets and claiming success."""
    capture = AudioCapture(str(tmp_path))
    capture._queue.put_nowait = Mock(side_effect=queue.Full)
    capture.record("test")
    await capture.close()
    end = _events(capture.directory)[-1]
    assert end["complete"] is False
    assert "queue full" in end["error"]


async def test_server_clock_observations_exclude_playback_delay(tmp_path: Path) -> None:
    """Timing diagnostics must not confuse configured output delay with server-clock offset."""
    client = Mock(
        spec=SendspinClient,
        output_delay_ms=125.0,
        server_info=ServerInfo("test-server", "Test server"),
    )
    client.now_us.return_value = 1_000_000
    client.is_time_synchronized.return_value = True
    client.compute_server_time.side_effect = lambda value: value + 125_000 + 5_000_000
    capture = AudioCapture(str(tmp_path))
    capture.attach_client(client)
    await capture.close()
    event = _events(capture.directory)[0]
    assert event["client_time_us"] == 1_000_000
    assert event["estimated_server_time_us"] == 6_000_000
    assert event["output_delay_ms"] == 125.0
    assert event["server_id"] == "test-server"


@pytest.mark.parametrize("prefix", [[], ["daemon"]])
def test_capture_cli_option_in_both_modes(prefix: list[str]) -> None:
    """Capture is disabled by default and can be enabled in either player mode."""
    assert parse_args(prefix).capture_dir is None
    assert parse_args([*prefix, "--capture-dir", "recordings"]).capture_dir == "recordings"
