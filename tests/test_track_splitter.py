"""Tests for offline metadata-based PCM partitioning."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import av
import pytest

import sendspin.track_splitter as track_splitter
from sendspin.cli import main, parse_args
from sendspin.track_splitter import ExportFormat, split_tracks


def _capture(
    directory: Path,
    events: list[dict],
    files: dict[str, bytes],
    *,
    rate: int = 10,
    width: int = 2,
    channels: int = 2,
    complete: bool = True,
) -> None:
    directory.mkdir()
    (directory / "session.json").write_text(json.dumps({"schema_version": 1}))
    with (directory / "events.jsonl").open("w") as journal:
        for event in [*events, {"type": "capture_end", "complete": complete}]:
            journal.write(json.dumps(event) + "\n")
    for name, pcm in files.items():
        with wave.open(str(directory / name), "wb") as wav:
            wav.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)


def _audio(name: str, offset: int, count: int, timestamp: int, packet_offset: int = 0) -> dict:
    return {
        "type": "audio",
        "file": name,
        "file_frame_offset": offset,
        "frame_count": count,
        "server_timestamp_us": timestamp,
        "packet_frame_offset": packet_offset,
    }


def _metadata(timestamp: int, title: str, **extra: object) -> dict:
    return {
        "type": "metadata_current",
        "payload": {
            "metadata": {
                "timestamp": timestamp,
                "title": title,
                "artist": "Artist",
                **extra,
            }
        },
    }


def _result(output: Path) -> tuple[dict, list[bytes]]:
    manifest = json.loads((output / "tracks.json").read_text())
    pcm = []
    for track in manifest["tracks"]:
        with wave.open(str(output / track["file"]), "rb") as wav:
            assert wav.getnframes() == track["frame_count"]
            pcm.append(wav.readframes(wav.getnframes()))
    return manifest, pcm


@pytest.mark.parametrize("width", [2, 3, 4])
def test_split_inside_packet_and_join_rotated_files(tmp_path: Path, width: int) -> None:
    """Retroactively split a buffered packet, keep whole frames, and join rotated WAVs."""
    directory = tmp_path / "capture"
    pcm = bytes(range(12 * 2 * width))
    frame_size = 2 * width
    _capture(
        directory,
        [
            _metadata(0, "First"),
            _audio("audio-000001.wav", 0, 5, 0),
            # This packet straddles the capture file boundary.
            _audio("audio-000001.wav", 5, 2, 500_000),
            _audio("audio-000002.wav", 0, 5, 500_000, packet_offset=2),
            # Delivered after buffered audio: use effective time, not journal order/progress.
            _metadata(
                250_001,
                "Second",
                progress={"track_progress": 200, "track_duration": 2000, "playback_speed": 1000},
            ),
        ],
        {
            "audio-000001.wav": pcm[: 7 * frame_size],
            "audio-000002.wav": pcm[7 * frame_size :],
        },
        width=width,
    )
    output = split_tracks(directory)
    manifest, audio = _result(output)
    assert audio == [pcm[: 3 * frame_size], pcm[3 * frame_size :]]
    assert manifest["complete"] is True
    assert manifest["boundary_method"] == "metadata_effective_timestamp"
    assert [track["frame_count"] for track in manifest["tracks"]] == [3, 9]
    assert manifest["tracks"][1]["sources"][0]["file_frame_offset"] == 3
    assert manifest["tracks"][1]["metadata"]["timestamp"] == 250_001
    assert (
        manifest["metadata_events"][1]["event"]["payload"]["metadata"]["progress"]["track_progress"]
        == 200
    )
    # Originals and their bytes remain available after splitting.
    with wave.open(str(directory / "audio-000001.wav"), "rb") as wav:
        assert wav.readframes(7) == pcm[: 7 * frame_size]


def test_exact_boundary_and_repeated_metadata_do_not_add_cuts(tmp_path: Path) -> None:
    """Ignore progress/artwork updates and canceled schedules; retain the latest state."""
    directory = tmp_path / "capture"
    canceled = _metadata(100_000, "Canceled")
    canceled["type"] = "metadata_scheduled"
    pcm = bytes(range(24))
    _capture(
        directory,
        [
            _metadata(0, "First"),
            canceled,
            _metadata(100_000, "First", artwork_url="https://example.com/art"),
            _metadata(200_000, "Second"),
            _audio("audio-000001.wav", 0, 6, 0),
        ],
        {"audio-000001.wav": pcm},
    )
    manifest, audio = _result(split_tracks(directory))
    assert audio == [pcm[:8], pcm[8:]]
    assert manifest["tracks"][0]["metadata_refs"] == [0, 1]


def test_rational_frame_rounding_at_44100_hz(tmp_path: Path) -> None:
    """A one-microsecond boundary cuts after, not inside, the first 44.1 kHz frame."""
    directory = tmp_path / "capture"
    pcm = bytes(range(16))
    _capture(
        directory,
        [_metadata(0, "First"), _metadata(1, "Second"), _audio("audio-000001.wav", 0, 4, 0)],
        {"audio-000001.wav": pcm},
        rate=44_100,
    )
    manifest, audio = _result(split_tracks(directory))
    assert audio == [pcm[:4], pcm[4:]]
    assert manifest["tracks"][1]["server_start_us"] == {
        "numerator": 10_000,
        "denominator": 441,
    }


@pytest.mark.parametrize(
    "boundary",
    [
        {"type": "stream_start", "player_boundary": True},
        {"type": "stream_clear", "player_boundary": True},
        {"type": "stream_end", "player_boundary": True},
    ],
)
def test_stream_boundaries_keep_repeated_track_parts(tmp_path: Path, boundary: dict) -> None:
    """Never glue separate playback runs into one apparent continuous song."""
    directory = tmp_path / "capture"
    _capture(
        directory,
        [
            _metadata(0, "Same"),
            _audio("audio-000001.wav", 0, 1, 0),
            boundary,
            _audio("audio-000002.wav", 0, 1, 100_000),
        ],
        {"audio-000001.wav": b"\x01" * 4, "audio-000002.wav": b"\x02" * 4},
    )
    _, audio = _result(split_tracks(directory))
    assert audio == [b"\x01" * 4, b"\x02" * 4]


def test_unknown_metadata_reset_and_connection_clock_reset(tmp_path: Path) -> None:
    """Do not apply future metadata to unlabeled frames or carry labels across connections."""
    directory = tmp_path / "capture"
    _capture(
        directory,
        [
            _audio("audio-000001.wav", 0, 2, 0),
            _metadata(100_000, "Known"),
            {"type": "metadata_current", "payload": None},
            _audio("audio-000001.wav", 2, 1, 200_000),
            {"type": "disconnect"},
            _metadata(0, "New server"),
            _audio("audio-000001.wav", 3, 1, 0),
        ],
        {"audio-000001.wav": bytes(range(16))},
    )
    manifest, audio = _result(split_tracks(directory))
    assert [track["metadata"].get("title") for track in manifest["tracks"]] == [
        None,
        "Known",
        None,
        "New server",
    ]
    assert b"".join(audio) == bytes(range(16))
    assert any("Metadata reset" in warning for warning in manifest["warnings"])


def test_timestamp_gaps_overlaps_and_non_player_events(tmp_path: Path) -> None:
    """Preserve every received frame without padding gaps or deleting overlap."""
    directory = tmp_path / "capture"
    _capture(
        directory,
        [
            _metadata(0, "Same"),
            _audio("audio-000001.wav", 0, 2, 0),
            {"type": "stream_end", "player_boundary": False},
            _audio("audio-000001.wav", 2, 1, 200_000),
            _audio("audio-000001.wav", 3, 1, 900_000),
            _audio("audio-000001.wav", 4, 1, 500_000),
        ],
        {"audio-000001.wav": bytes(range(20))},
        complete=False,
    )
    manifest, audio = _result(split_tracks(directory))
    assert audio == [bytes(range(12)), bytes(range(12, 16)), bytes(range(16, 20))]
    assert manifest["tracks"][1]["start_reason"] == "timestamp_discontinuity"
    assert any("Capture is incomplete" in warning for warning in manifest["warnings"])


def test_format_change_keeps_compatible_wav_headers(tmp_path: Path) -> None:
    """Keep the original PCM representation when one song changes formats."""
    directory = tmp_path / "capture"
    _capture(
        directory,
        [
            _metadata(0, "Same"),
            _audio("audio-000001.wav", 0, 1, 0),
            _audio("audio-000002.wav", 0, 1, 100_000),
        ],
        {"audio-000001.wav": b"\x01" * 4, "audio-000002.wav": b"\x02" * 4},
    )
    with wave.open(str(directory / "audio-000002.wav"), "wb") as wav:
        wav.setparams((2, 3, 20, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x03" * 6)
    manifest, audio = _result(split_tracks(directory))
    assert audio == [b"\x01" * 4, b"\x03" * 6]
    assert [track["bit_depth"] for track in manifest["tracks"]] == [16, 24]
    assert [track["sample_rate"] for track in manifest["tracks"]] == [10, 20]


def test_server_change_does_not_reuse_prior_metadata(tmp_path: Path) -> None:
    directory = tmp_path / "capture"
    _capture(
        directory,
        [
            {**_metadata(0, "Old server track"), "server_id": "old"},
            {**_audio("audio-000001.wav", 0, 1, 0), "server_id": "old"},
            {**_audio("audio-000001.wav", 1, 1, 100_000), "server_id": "new"},
        ],
        {"audio-000001.wav": bytes(range(8))},
    )
    manifest, audio = _result(split_tracks(directory))
    assert [track["metadata"].get("title") for track in manifest["tracks"]] == [
        "Old server track",
        None,
    ]
    assert b"".join(audio) == bytes(range(8))


@pytest.mark.parametrize(
    "corruption", ["unsafe_path", "unindexed", "duplicate", "missing", "bad_json", "bad_wav"]
)
def test_invalid_capture_does_not_create_output(tmp_path: Path, corruption: str) -> None:
    """Reject inconsistent source indexes before producing apparently successful tracks."""
    directory = tmp_path / "capture"
    event = _audio("audio-000001.wav", 0, 2, 0)
    if corruption == "unsafe_path":
        event["file"] = "../outside.wav"
    elif corruption == "unindexed":
        event["frame_count"] = 1
    elif corruption == "duplicate":
        event["file_frame_offset"] = 1
    elif corruption == "missing":
        event["file"] = "audio-missing.wav"
    _capture(directory, [event], {"audio-000001.wav": bytes(range(8))})
    if corruption == "bad_json":
        (directory / "events.jsonl").write_text("{invalid")
    elif corruption == "bad_wav":
        (directory / "audio-000001.wav").write_bytes(b"RIFF")
    with pytest.raises((ValueError, OSError)):
        split_tracks(directory)
    assert not (directory / "tracks").exists()


def test_failed_copy_marks_output_incomplete(tmp_path: Path) -> None:
    """A truncated WAV must not leave a success-shaped report."""
    directory = tmp_path / "capture"
    _capture(
        directory,
        [_metadata(0, "Track"), _audio("audio-000001.wav", 0, 2, 0)],
        {"audio-000001.wav": bytes(range(8))},
    )
    path = directory / "audio-000001.wav"
    path.write_bytes(path.read_bytes()[:-4])
    with pytest.raises(ValueError, match="Truncated PCM"):
        split_tracks(directory)
    manifest = json.loads((directory / "tracks" / "tracks.json").read_text())
    assert manifest["complete"] is False
    assert "Truncated PCM" in manifest["error"]


def test_output_directory_is_never_overwritten(tmp_path: Path) -> None:
    directory = tmp_path / "capture"
    _capture(
        directory,
        [_audio("audio-000001.wav", 0, 1, 0)],
        {"audio-000001.wav": bytes(range(4))},
    )
    output = split_tracks(directory, tmp_path / "custom")
    before = (output / "tracks.json").read_bytes()
    with pytest.raises(FileExistsError):
        split_tracks(directory, output)
    assert (output / "tracks.json").read_bytes() == before


def test_splitter_cli_routes_without_starting_player(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The offline command needs neither a live server nor an audio device."""
    directory = tmp_path / "capture"
    _capture(
        directory,
        [_audio("audio-000001.wav", 0, 1, 0)],
        {"audio-000001.wav": bytes(range(4))},
    )
    monkeypatch.setattr("sys.argv", ["sendspin", "split-tracks", str(directory)])
    assert parse_args(["split-tracks", str(directory)]).command == "split-tracks"
    assert main() == 0
    assert (directory / "tracks" / "tracks.json").exists()
    assert "Saved tracks" in capsys.readouterr().out
    assert main() == 1
    assert "failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("output_format", "width"), [("flac", 2), ("flac", 3), ("aiff", 2), ("aiff", 3), ("aiff", 4)]
)
@pytest.mark.parametrize("channels", [1, 2])
def test_lossless_export_with_standard_text_tags(
    tmp_path: Path, output_format: ExportFormat, width: int, channels: int
) -> None:
    """Round-trip low bits, signed extrema, frame cuts, mono/stereo and Unicode text tags."""
    directory = tmp_path / "capture"
    values = [0, 1, -1, 123, -123, (1 << (width * 8 - 1)) - 1, -(1 << (width * 8 - 1)), 0]
    pcm = b"".join(value.to_bytes(width, "little", signed=True) for value in values * channels)
    metadata = {
        "album": "Album \u00e9",
        "album_artist": "Album artist \u4e00",
        "year": 2026,
        "track": 7,
    }
    _capture(
        directory,
        [
            _metadata(0, "First \u00e9", **metadata),
            # At 44.1 kHz this timestamp cuts after frame 1.
            _metadata(1, "Second", **metadata),
            _audio("audio-000001.wav", 0, 8, 0),
        ],
        {"audio-000001.wav": pcm},
        rate=44_100,
        width=width,
        channels=channels,
    )
    output = split_tracks(directory, output_format=output_format)
    manifest = json.loads((output / "tracks.json").read_text())
    assert manifest["complete"] is True
    assert manifest["output_format"] == output_format
    assert [track["frame_count"] for track in manifest["tracks"]] == [1, 7]
    decoded = bytearray()
    for track in manifest["tracks"]:
        path = output / track["file"]
        assert path.suffix == f".{output_format}"
        with av.open(str(path)) as audio_file:
            assert audio_file.metadata["title"] == track["metadata"]["title"]
            assert audio_file.metadata["artist"] == "Artist"
            assert audio_file.metadata["album"] == metadata["album"]
            assert audio_file.metadata["album_artist"] == metadata["album_artist"]
            assert audio_file.metadata["date"] == "2026"
            assert audio_file.metadata["track"] == "7"
            stream = audio_file.streams.audio[0]
            assert stream.rate == 44_100
            assert stream.channels == channels
            if output_format == "aiff":
                assert stream.codec_context.name == f"pcm_s{width * 8}be"
            for frame in audio_file.decode(audio=0):
                raw = frame.to_ndarray().astype("<i2" if width == 2 else "<i4").tobytes()
                if width == 3:
                    raw = b"".join(raw[index + 1 : index + 4] for index in range(0, len(raw), 4))
                decoded.extend(raw)
        raw_file = path.read_bytes()
        if output_format == "aiff":
            assert raw_file[8:12] == b"AIFF"
            for tag in (b"ID3", b"TIT2", b"TPE1", b"TALB", b"TPE2", b"TDRC", b"TRCK"):
                assert tag in raw_file
        else:
            assert raw_file[:4] == b"fLaC"
            assert b"ALBUMARTIST=Album artist" in raw_file
            assert b"TRACKNUMBER=7" in raw_file
            # STREAMINFO's bit depth must agree with the source, not just its decoded values.
            assert ((int.from_bytes(raw_file[18:26], "big") >> 36) & 31) + 1 == width * 8
    assert decoded == pcm
    assert not list(output.glob("*.temporary.wav"))


def test_flac_rejects_32_bit_instead_of_truncating_samples(tmp_path: Path) -> None:
    """The FFmpeg FLAC encoder uses at most 24 bits; never silently lose low bits."""
    directory = tmp_path / "capture"
    _capture(
        directory,
        [_metadata(0, "Track"), _audio("audio-000001.wav", 0, 1, 0)],
        {"audio-000001.wav": (1).to_bytes(4, "little", signed=True) * 2},
        width=4,
        rate=44_100,
    )
    with pytest.raises(ValueError, match="cannot preserve 32-bit"):
        split_tracks(directory, output_format="flac")
    assert not (directory / "tracks").exists()


@pytest.mark.parametrize("output_format", ["flac", "aiff"])
def test_untagged_audio_does_not_invent_song_metadata(
    tmp_path: Path, output_format: ExportFormat
) -> None:
    directory = tmp_path / "capture"
    _capture(
        directory,
        [_audio("audio-000001.wav", 0, 1, 0)],
        {"audio-000001.wav": b"\x01" * 4},
        rate=44_100,
    )
    output = split_tracks(directory, output_format=output_format)
    path = next(output.glob(f"*.{output_format}"))
    with av.open(str(path)) as audio_file:
        assert "title" not in audio_file.metadata
        assert "track" not in audio_file.metadata


def test_failed_export_has_incomplete_report_and_no_temporary_wav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "capture"
    _capture(
        directory,
        [_metadata(0, "Track"), _audio("audio-000001.wav", 0, 1, 0)],
        {"audio-000001.wav": b"\x01" * 4},
        rate=44_100,
    )

    def fail_export(*args: object) -> None:
        raise ValueError("Encoder failed")

    monkeypatch.setattr(track_splitter, "_export_track", fail_export)
    with pytest.raises(ValueError, match="Encoder failed"):
        split_tracks(directory, output_format="flac")
    manifest = json.loads((directory / "tracks" / "tracks.json").read_text())
    assert manifest["complete"] is False
    assert manifest["error"] == "ValueError: Encoder failed"
    assert not list((directory / "tracks").glob("*.temporary.wav"))


@pytest.mark.parametrize("output_format", ["wav", "flac", "aiff"])
def test_output_format_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_format: ExportFormat,
) -> None:
    directory = tmp_path / "capture"
    _capture(
        directory,
        [_metadata(0, "Track"), _audio("audio-000001.wav", 0, 1, 0)],
        {"audio-000001.wav": b"\x01" * 4},
        rate=44_100,
    )
    assert parse_args(["split-tracks", str(directory)]).output_format == "wav"
    monkeypatch.setattr(
        "sys.argv", ["sendspin", "split-tracks", str(directory), "--format", output_format]
    )
    assert main() == 0
    assert list((directory / "tracks").glob(f"*.{output_format}"))
