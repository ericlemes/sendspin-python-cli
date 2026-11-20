"""Tests for offline metadata-based PCM partitioning."""

from __future__ import annotations

import json
import wave
from fractions import Fraction
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
    output = split_tracks(directory, boundary_mode="metadata")
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


def _progress(position: int, *, duration: int = 100_000, speed: int = 1000) -> dict:
    return {"track_progress": position, "track_duration": duration, "playback_speed": speed}


def test_late_metadata_no_longer_cuts_off_next_track_intro(tmp_path: Path) -> None:
    """Use reported progress to recover a known transition instead of a blanket offset."""
    directory = tmp_path / "capture"
    old_pcm = b"\x11" * 1000 * 4
    new_pcm = b"\x22" * 3000 * 4
    _capture(
        directory,
        [
            _metadata(0, "Old", progress=_progress(0)),
            _audio("audio-000001.wav", 0, 4000, 0),
            _metadata(2_510_000, "New", progress=_progress(1510)),
            _metadata(3_500_000, "New", progress=_progress(2500)),
        ],
        {"audio-000001.wav": old_pcm + new_pcm},
        rate=1000,
    )
    manifest, audio = _result(split_tracks(directory, tmp_path / "progress"))
    assert audio == [old_pcm, new_pcm]
    assert manifest["boundary_mode"] == "progress"
    assert manifest["boundary_method"] == "progress_estimated_track_start"
    boundary = manifest["tracks"][1]["boundary_estimate"]
    assert boundary["metadata_timestamp_us"] == 2_510_000
    assert boundary["split_timestamp_us"] == {"numerator": 1_000_000, "denominator": 1}
    assert boundary["backdated_us"] == {"numerator": 1_510_000, "denominator": 1}
    assert boundary["reason"] == "progress_estimate"
    assert manifest["tracks"][1]["metadata"]["timestamp"] == 2_510_000
    assert manifest["metadata_events"][2]["boundary_estimate"]["reason"] == "same_track_update"
    raw_manifest, raw_audio = _result(
        split_tracks(directory, tmp_path / "metadata", boundary_mode="metadata")
    )
    assert [track["frame_count"] for track in raw_manifest["tracks"]] == [2510, 1490]
    assert b"".join(raw_audio) == old_pcm + new_pcm


def test_progress_speed_scaling_uses_exact_frame_rounding(tmp_path: Path) -> None:
    """A 1.5x playback speed requires a rational estimate, not a rounded millisecond shift."""
    directory = tmp_path / "capture"
    pcm = bytes(range(200))
    _capture(
        directory,
        [
            _metadata(0, "Old", progress=_progress(0)),
            _audio("audio-000001.wav", 0, 50, 1_999_000),
            _metadata(2_000_000, "New", progress=_progress(1, speed=1500)),
        ],
        {"audio-000001.wav": pcm},
        rate=44_100,
    )
    manifest, audio = _result(split_tracks(directory))
    assert audio == [pcm[: 15 * 4], pcm[15 * 4 :]]
    boundary = manifest["tracks"][1]["boundary_estimate"]
    expected = Fraction(2_000_000) - Fraction(2000, 3)
    assert boundary["split_timestamp_us"] == {
        "numerator": expected.numerator,
        "denominator": expected.denominator,
    }
    assert manifest["tracks"][1]["sources"][0]["file_frame_offset"] == 15


def test_very_late_track_update_is_not_limited_to_fixed_offset(tmp_path: Path) -> None:
    """A delayed notification can arrive minutes into a track while PCM keeps streaming."""
    directory = tmp_path / "capture"
    pcm = bytes(range(200)) * 8
    _capture(
        directory,
        [
            _metadata(0, "Old", progress=_progress(0, duration=121_000)),
            _audio("audio-000001.wav", 0, 400, 0),
            _metadata(300_000_000, "New", progress=_progress(179_000, duration=385_000)),
        ],
        {"audio-000001.wav": pcm},
        rate=1,
    )
    manifest, audio = _result(split_tracks(directory))
    assert [track["frame_count"] for track in manifest["tracks"]] == [121, 279]
    assert b"".join(audio) == pcm


@pytest.mark.parametrize(
    ("progress", "reason"),
    [
        (None, "missing_progress"),
        (_progress(1510, speed=0), "paused"),
        (_progress(1510, duration=0), "unknown_or_live_duration"),
        (_progress(-1), "invalid_progress"),
        (_progress(1510, speed=-1), "invalid_progress"),
        (_progress(1510, duration=1000), "invalid_progress"),
        ({**_progress(1510), "track_progress": True}, "invalid_progress"),
        (_progress(1510), "would_cross_previous_metadata"),
    ],
)
def test_unsafe_progress_falls_back_with_recorded_reason(
    tmp_path: Path, progress: dict | None, reason: str
) -> None:
    directory = tmp_path / "capture"
    pcm = bytes(range(80))
    _capture(
        directory,
        [
            _metadata(1_000_000, "Old", progress=_progress(0)),
            _metadata(2_000_000, "New", progress=progress),
            _audio("audio-000001.wav", 0, 20, 1_000_000),
        ],
        {"audio-000001.wav": pcm},
    )
    manifest, audio = _result(split_tracks(directory))
    assert audio == [pcm[:40], pcm[40:]]
    boundary = manifest["tracks"][1]["boundary_estimate"]
    assert boundary["method"] == "metadata_timestamp"
    assert boundary["reason"] == reason
    assert boundary["backdated_us"] == {"numerator": 0, "denominator": 1}
    assert any(reason in warning for warning in manifest["warnings"])


def test_seek_restart_prevents_backdating_into_previous_stream(tmp_path: Path) -> None:
    """Do not extrapolate a seeked media position into audio from an earlier playback run."""
    directory = tmp_path / "capture"
    old_pcm = b"\x11" * 10 * 4
    seek_pcm = b"\x22" * 20 * 4
    _capture(
        directory,
        [
            _metadata(0, "Old", progress=_progress(0)),
            _audio("audio-000001.wav", 0, 10, 0),
            {"type": "stream_clear", "player_boundary": True},
            _audio("audio-000002.wav", 0, 20, 10_000_000),
            _metadata(11_500_000, "New", progress=_progress(10_500)),
        ],
        {"audio-000001.wav": old_pcm, "audio-000002.wav": seek_pcm},
    )
    manifest, audio = _result(split_tracks(directory))
    assert b"".join(audio) == old_pcm + seek_pcm
    assert audio[0] == old_pcm
    boundary = manifest["tracks"][-1]["boundary_estimate"]
    assert boundary["reason"] == "would_cross_stream_restart"
    assert boundary["split_timestamp_us"] == {"numerator": 11_500_000, "denominator": 1}


def test_boundary_mode_cli_preserves_timestamp_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "capture"
    _capture(
        directory,
        [
            _metadata(0, "Old", progress=_progress(0)),
            _metadata(200_000, "New", progress=_progress(100)),
            _audio("audio-000001.wav", 0, 4, 0),
        ],
        {"audio-000001.wav": bytes(range(16))},
    )
    assert parse_args(["split-tracks", str(directory)]).boundary_mode == "progress"
    monkeypatch.setattr(
        "sys.argv", ["sendspin", "split-tracks", str(directory), "--boundary-mode", "metadata"]
    )
    assert main() == 0
    manifest, _ = _result(directory / "tracks")
    assert manifest["boundary_mode"] == "metadata"
    assert [track["frame_count"] for track in manifest["tracks"]] == [2, 2]


@pytest.mark.parametrize("mode", ["progress", "metadata"])
def test_earlier_offset_moves_only_identity_boundaries(tmp_path: Path, mode: str) -> None:
    """Apply the extra offset after estimation without shifting same-track observations."""
    directory = tmp_path / "capture"
    pcm = bytes(range(200))
    _capture(
        directory,
        [
            _metadata(0, "Old", progress=_progress(0)),
            _metadata(2_000_001, "New", progress=_progress(500)),
            _metadata(3_000_000, "New", progress=_progress(1500)),
            _audio("audio-000001.wav", 0, 50, 0),
        ],
        {"audio-000001.wav": pcm},
    )
    boundary_mode: track_splitter.BoundaryMode = "progress" if mode == "progress" else "metadata"
    manifest, audio = _result(
        split_tracks(directory, boundary_mode=boundary_mode, boundary_offset_ms=500)
    )
    cut_frame = 11 if mode == "progress" else 16
    assert audio == [pcm[: cut_frame * 4], pcm[cut_frame * 4 :]]
    assert manifest["boundary_offset_ms"] == 500
    boundary = manifest["tracks"][1]["boundary_estimate"]
    assert boundary["applied_offset_us"] == {"numerator": 500_000, "denominator": 1}
    assert boundary["offset_limit"] is None
    update = manifest["metadata_events"][2]["boundary_estimate"]
    assert update["split_timestamp_us"] == {"numerator": 3_000_000, "denominator": 1}
    assert update["applied_offset_us"] == {"numerator": 0, "denominator": 1}


@pytest.mark.parametrize("restart", [False, True])
def test_earlier_offset_respects_metadata_and_restart_limits(tmp_path: Path, restart: bool) -> None:
    """An excessive shift cannot reorder updates or label a preceding stream."""
    directory = tmp_path / "capture"
    events = [
        _metadata(0, "Old", progress=_progress(0)),
        _audio("audio-000001.wav", 0, 10, 0),
        _metadata(900_000, "Old", progress=_progress(900)),
    ]
    if restart:
        events.append({"type": "stream_clear", "player_boundary": True})
    events.extend(
        [
            _audio("audio-000002.wav", 0, 10, 1_000_000),
            _metadata(1_500_000, "New", progress=_progress(100)),
        ]
    )
    _capture(
        directory,
        events,
        {"audio-000001.wav": b"\x11" * 40, "audio-000002.wav": b"\x22" * 40},
    )
    manifest, audio = _result(split_tracks(directory, boundary_offset_ms=1000))
    assert b"".join(audio) == b"\x11" * 40 + b"\x22" * 40
    boundary = manifest["tracks"][-1]["boundary_estimate"]
    limit = "stream_restart" if restart else "previous_metadata"
    assert boundary["offset_limit"] == limit
    assert boundary["split_timestamp_us"] == {
        "numerator": 1_000_000 if restart else 900_000,
        "denominator": 1,
    }
    assert any(limit in warning for warning in manifest["warnings"])


@pytest.mark.parametrize("offset", [-1, 0.5, True])
def test_invalid_earlier_offset_does_not_create_output(tmp_path: Path, offset: object) -> None:
    """Reject invalid offsets before reading input or creating output."""
    with pytest.raises(ValueError, match="non-negative integer"):
        split_tracks(tmp_path / "absent", boundary_offset_ms=offset)
    assert not (tmp_path / "absent").exists()


def test_earlier_offset_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the requested offset through the CLI into the frame partition."""
    directory = tmp_path / "capture"
    _capture(
        directory,
        [
            _metadata(0, "Old"),
            _metadata(1_500_000, "New"),
            _audio("audio-000001.wav", 0, 20, 0),
        ],
        {"audio-000001.wav": bytes(range(80))},
    )
    assert parse_args(["split-tracks", str(directory)]).boundary_offset_ms == 0
    monkeypatch.setattr(
        "sys.argv",
        ["sendspin", "split-tracks", str(directory), "--boundary-offset-ms", "500"],
    )
    assert main() == 0
    manifest, _ = _result(directory / "tracks")
    assert manifest["boundary_offset_ms"] == 500
    assert [track["frame_count"] for track in manifest["tracks"]] == [10, 10]
