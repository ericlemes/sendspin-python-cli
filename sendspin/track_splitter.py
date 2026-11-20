"""Offline, lossless partitioning of diagnostic captures using metadata timestamps."""

from __future__ import annotations

import json
import re
import wave
from bisect import bisect_right
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO, Literal

import av

_IDENTITY_FIELDS = ("title", "artist", "album", "album_artist", "track", "year")
_BOUNDARIES = {"client_attached", "disconnect", "stream_start", "stream_clear", "stream_end"}
_COPY_FRAMES = 65_536
type ExportFormat = Literal["wav", "flac", "aiff"]
type BoundaryMode = Literal["progress", "metadata"]
_TEXT_TAGS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "album_artist",
    "year": "date",
    "track": "track",
}


def _export_track(
    source: Path, destination: Path, output_format: ExportFormat, metadata: dict[str, Any]
) -> None:
    """Encode losslessly and embed standard Vorbis comments or AIFF ID3v2 text tags."""
    options = {"write_id3v2": "1"} if output_format == "aiff" else {}
    try:
        with (
            destination.open("xb") as handle,
            av.open(str(source)) as input_file,
            av.open(handle, "w", format=output_format, options=options) as output_file,
        ):
            audio = input_file.streams.audio[0]
            # WAV's packed 24-bit PCM is decoded to left-aligned signed 32-bit samples.
            with wave.open(str(source), "rb") as wav:
                depth = wav.getsampwidth() * 8
            codec = "flac" if output_format == "flac" else f"pcm_s{depth}be"
            stream = output_file.add_stream(codec, rate=audio.rate)
            if not isinstance(stream, av.AudioStream):
                raise ValueError(f"{codec} did not create an audio encoder")
            stream.layout = "mono" if audio.channels == 1 else "stereo"
            stream.format = "s16" if depth == 16 else "s32"
            output_file.metadata.update(
                {
                    tag: str(metadata[key])
                    for key, tag in _TEXT_TAGS.items()
                    if metadata.get(key) is not None
                }
            )
            for frame in input_file.decode(audio):
                for packet in stream.encode(frame):
                    output_file.mux(packet)
            for packet in stream.encode(None):
                output_file.mux(packet)
    except av.FFmpegError as exc:
        raise ValueError(f"Failed to export {destination.name}: {exc}") from exc


def _integer(data: dict[str, Any], key: str, minimum: int | None = None) -> int:
    value = data.get(key)
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"Invalid {key}: expected an integer >= {minimum}, got {value!r}")
    return value


def _object(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {description}: expected a JSON object")
    return value


@dataclass(frozen=True)
class _Metadata:
    timestamp: Fraction
    data: dict[str, Any]
    reference: int
    run: int
    boundary: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[object, ...]:
        return tuple(self.data.get(key) for key in _IDENTITY_FIELDS)


@dataclass
class _Span:
    source: str
    offset: int
    count: int
    start: Fraction
    params: tuple[int, int, int]
    epoch: int
    run: int


@dataclass
class _Capture:
    spans: list[_Span] = field(default_factory=list)
    timelines: dict[int, list[_Metadata]] = field(default_factory=dict)
    metadata: list[dict[str, Any]] = field(default_factory=list)
    params: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    frames: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def source(self, directory: Path, name: str) -> tuple[int, int, int]:
        if name not in self.params:
            path = directory / name
            if (
                "/" in name
                or "\\" in name
                or path.suffix.lower() != ".wav"
                or path.resolve().parent != directory
            ):
                raise ValueError(f"Unsafe capture WAV filename: {name!r}")
            try:
                with wave.open(str(path), "rb") as wav:
                    self.params[name] = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
                    self.frames[name] = wav.getnframes()
            except (EOFError, wave.Error) as exc:
                raise ValueError(f"Invalid capture WAV {name}: {exc}") from exc
        return self.params[name]


def _read_capture(directory: Path) -> _Capture:  # noqa: PLR0912, PLR0915
    session = _object(
        json.loads((directory / "session.json").read_text(encoding="utf-8")), "session"
    )
    if session.get("schema_version") != 1:
        raise ValueError("Only capture schema_version 1 is supported")
    capture = _Capture()
    offsets: dict[str, int] = {}
    epoch = run = 0
    server: str | None = None
    ended = False
    complete = False
    with (directory / "events.jsonl").open(encoding="utf-8") as journal:
        for line_number, line in enumerate(journal, 1):
            try:
                event = _object(json.loads(line), "journal event")
                if ended:
                    raise ValueError("Journal contains records after capture_end")
                kind = event.get("type")
                if not isinstance(kind, str):
                    raise ValueError("Journal event has no string type")
                new_server = event.get("server_id")
                if kind in {"client_attached", "disconnect"} or (
                    new_server is not None and server is not None and new_server != server
                ):
                    epoch += 1
                    run += 1
                    server = None
                if new_server is not None:
                    server = new_server
                if kind in _BOUNDARIES and event.get("player_boundary", True):
                    run += 1
                if kind == "capture_end":
                    ended = True
                    complete = event.get("complete") is True
                elif kind == "metadata_current":
                    if event.get("payload") is None:
                        epoch += 1
                        run += 1
                    _read_metadata(capture, event, epoch, run, line_number)
                elif kind == "audio":
                    name = event.get("file")
                    if not isinstance(name, str):
                        raise ValueError("Audio index requires a WAV filename")
                    params = capture.source(directory, name)
                    offset = _integer(event, "file_frame_offset", 0)
                    count = _integer(event, "frame_count", 1)
                    if offset != offsets.get(name, 0):
                        raise ValueError(f"Non-contiguous or duplicate frame index for {name}")
                    if offset + count > capture.frames[name]:
                        raise ValueError(f"Frame index exceeds WAV length for {name}")
                    offsets[name] = offset + count
                    start = Fraction(_integer(event, "server_timestamp_us")) + Fraction(
                        _integer(event, "packet_frame_offset", 0) * 1_000_000, params[2]
                    )
                    capture.spans.append(_Span(name, offset, count, start, params, epoch, run))
            except (ValueError, wave.Error) as exc:
                raise ValueError(f"events.jsonl line {line_number}: {exc}") from exc
    if not capture.spans:
        raise ValueError("Capture contains no indexed audio")
    for name, count in capture.frames.items():
        if offsets[name] != count:
            raise ValueError(f"WAV {name} has unindexed frames; refusing to omit audio")
    unindexed = {p.name for p in directory.glob("audio-*.wav")} - capture.params.keys()
    if unindexed:
        raise ValueError(f"Capture contains unindexed WAV files: {', '.join(sorted(unindexed))}")
    if not complete:
        capture.warnings.append("Capture is incomplete or has no successful capture_end marker.")
    for timeline in capture.timelines.values():
        timeline.sort(key=lambda metadata: metadata.timestamp)
    return capture


def _read_metadata(
    capture: _Capture, event: dict[str, Any], epoch: int, run: int, line_number: int
) -> None:
    reference = len(capture.metadata)
    capture.metadata.append({"journal_line": line_number, "epoch": epoch, "event": event})
    payload = event.get("payload")
    if payload is None:
        # Role revocation has no effective timestamp: do not extend stale labels past it.
        capture.warnings.append(
            f"Metadata reset at journal line {line_number}; subsequent audio is unlabeled "
            "until another timestamped update."
        )
        return
    payload = _object(payload, "metadata payload")
    if "metadata" not in payload:
        return
    data = _object(payload["metadata"], "metadata")
    timestamp = _integer(data, "timestamp")
    capture.timelines.setdefault(epoch, []).append(
        _Metadata(Fraction(timestamp), data, reference, run)
    )


def _rational(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _progress_start(metadata: _Metadata) -> tuple[Fraction | None, str]:
    progress = metadata.data.get("progress")
    if not isinstance(progress, dict):
        return None, "missing_progress"
    position = progress.get("track_progress")
    duration = progress.get("track_duration")
    speed = progress.get("playback_speed")
    if type(position) is not int or type(duration) is not int or type(speed) is not int:
        return None, "invalid_progress"
    if speed == 0:
        return None, "paused"
    if duration == 0:
        return None, "unknown_or_live_duration"
    if speed < 0 or duration < 0 or position < 0 or position > duration:
        return None, "invalid_progress"
    # Progress is milliseconds of media time; speed is a multiplier scaled by 1000.
    return metadata.timestamp - Fraction(position * 1_000_000, speed), "progress_estimate"


def _estimate_boundaries(capture: _Capture, mode: BoundaryMode, offset_ms: int = 0) -> None:
    """Backdate identity changes, not progress updates, without crossing prior state."""
    run_starts: dict[tuple[int, int], Fraction] = {}
    first_runs: dict[int, int] = {}
    for span in capture.spans:
        key = (span.epoch, span.run)
        run_starts[key] = min(run_starts.get(key, span.start), span.start)
        first_runs[span.epoch] = min(first_runs.get(span.epoch, span.run), span.run)
    for epoch, timeline in capture.timelines.items():
        previous: _Metadata | None = None
        adjusted = []
        for metadata in timeline:
            timestamp = metadata.timestamp
            reason = "metadata_timestamp"
            is_change = previous is None or previous.identity != metadata.identity
            if mode == "progress" and is_change and metadata.data.get("title"):
                candidate, reason = _progress_start(metadata)
                if candidate is not None:
                    if previous is not None and candidate < previous.timestamp:
                        reason = "would_cross_previous_metadata"
                    elif (
                        metadata.run > first_runs.get(epoch, metadata.run)
                        and (run_start := run_starts.get((epoch, metadata.run))) is not None
                        and candidate < run_start
                    ):
                        reason = "would_cross_stream_restart"
                    else:
                        timestamp = candidate
                if reason != "progress_estimate":
                    capture.warnings.append(
                        f"{metadata.data['title']!r}: using metadata timestamp "
                        f"({reason}); its musical boundary remains uncertain."
                    )
            elif mode == "progress" and not is_change:
                reason = "same_track_update"
            before_offset = timestamp
            offset_limit = None
            if offset_ms and is_change and metadata.data.get("title"):
                timestamp -= offset_ms * 1000
                if previous is not None and timestamp < previous.timestamp:
                    timestamp = previous.timestamp
                    offset_limit = "previous_metadata"
                if (
                    metadata.run > first_runs.get(epoch, metadata.run)
                    and (run_start := run_starts.get((epoch, metadata.run))) is not None
                    and timestamp < run_start
                ):
                    timestamp = run_start
                    offset_limit = "stream_restart"
                if offset_limit is not None:
                    capture.warnings.append(
                        f"{metadata.data['title']!r}: earlier boundary offset limited by "
                        f"{offset_limit}."
                    )
            boundary = {
                "method": "progress_estimate"
                if reason == "progress_estimate"
                else "metadata_timestamp",
                "reason": reason,
                "metadata_timestamp_us": metadata.data["timestamp"],
                "split_timestamp_us": _rational(timestamp),
                "backdated_us": _rational(metadata.timestamp - timestamp),
                "applied_offset_us": _rational(before_offset - timestamp),
                "offset_limit": offset_limit,
            }
            capture.metadata[metadata.reference]["boundary_estimate"] = boundary
            adjusted.append(replace(metadata, timestamp=timestamp, boundary=boundary))
            previous = metadata
        timeline[:] = adjusted


class _TrackWriter:
    def __init__(self, directory: Path, output_format: ExportFormat = "wav") -> None:
        self.directory = directory
        self._output_format = output_format
        self.tracks: list[dict[str, Any]] = []
        self._wave: wave.Wave_write | None = None
        self._handle: BinaryIO | None = None
        self._key: tuple[object, ...] | None = None
        self._end: Fraction | None = None

    def append(
        self,
        source: wave.Wave_read,
        span: _Span,
        offset: int,
        count: int,
        metadata: _Metadata | None,
    ) -> None:
        start = span.start + Fraction(offset * 1_000_000, span.params[2])
        identity = metadata.identity if metadata is not None else (None,) * len(_IDENTITY_FIELDS)
        key = (span.epoch, span.run, span.params, identity)
        discontinuity = self._end is not None and abs(start - self._end) > Fraction(
            1_000_000, span.params[2]
        )
        if key != self._key or discontinuity:
            reason = "timestamp_discontinuity" if discontinuity else "metadata_or_stream_boundary"
            self._open(span, metadata, start, reason)
            self._key = key
        assert self._wave is not None
        track = self.tracks[-1]
        track["sources"].append(
            {
                "file": span.source,
                "file_frame_offset": span.offset + offset,
                "frame_count": count,
                "output_frame_offset": track["frame_count"],
                "server_start_us": _rational(start),
            }
        )
        if metadata is not None and metadata.reference not in track["metadata_refs"]:
            track["metadata_refs"].append(metadata.reference)
        source.setpos(span.offset + offset)
        remaining = count
        while remaining:
            frames = min(remaining, _COPY_FRAMES)
            data = source.readframes(frames)
            if len(data) != frames * span.params[0] * span.params[1]:
                raise ValueError(f"Truncated PCM data in {span.source}")
            self._wave.writeframesraw(data)
            remaining -= frames
        track["frame_count"] += count
        self._end = start + Fraction(count * 1_000_000, span.params[2])
        track["server_end_us"] = _rational(self._end)

    def _open(self, span: _Span, metadata: _Metadata | None, start: Fraction, reason: str) -> None:
        self.close()
        data = metadata.data if metadata is not None else {}
        label = " - ".join(str(data[key]) for key in ("artist", "title") if data.get(key))
        safe_label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label or "Unknown")
        safe_label = safe_label.encode("utf-8")[:160].decode("utf-8", errors="ignore").rstrip(" .")
        filename = f"{len(self.tracks) + 1:03d} - {safe_label}.{self._output_format}"
        destination = self.directory / filename
        # A temporary WAV keeps packed 24-bit PCM exact before lossless encoding.
        self._pcm_path = (
            destination
            if self._output_format == "wav"
            else destination.with_suffix(".temporary.wav")
        )
        # Exclusive creation also protects against accidental overwrite on a failed rerun.
        self._handle = self._pcm_path.open("xb")
        self._wave = wave.open(self._handle, "wb")
        self._wave.setparams((*span.params, 0, "NONE", "not compressed"))
        self.tracks.append(
            {
                "file": filename,
                "metadata": data,
                "boundary_estimate": metadata.boundary if metadata is not None else None,
                "metadata_refs": [],
                "frame_count": 0,
                "sample_rate": span.params[2],
                "channels": span.params[0],
                "bit_depth": span.params[1] * 8,
                "epoch": span.epoch,
                "stream_run": span.run,
                "start_reason": reason,
                "server_start_us": _rational(start),
                "sources": [],
            }
        )

    def close(self) -> None:
        has_audio = self._wave is not None
        try:
            if self._wave is not None:
                self._wave.close()
        finally:
            if self._handle is not None:
                self._handle.close()
            self._handle = None
            self._wave = None
        if has_audio and self._output_format != "wav":
            track = self.tracks[-1]
            try:
                _export_track(
                    self._pcm_path,
                    self.directory / track["file"],
                    self._output_format,
                    track["metadata"],
                )
            finally:
                self._pcm_path.unlink()


def split_tracks(
    capture_dir: Path,
    output_dir: Path | None = None,
    *,
    output_format: ExportFormat = "wav",
    boundary_mode: BoundaryMode = "progress",
    boundary_offset_ms: int = 0,
) -> Path:
    """Partition captured frames by current metadata and export lossless audio."""
    if output_format not in {"wav", "flac", "aiff"}:
        raise ValueError(f"Unsupported output format: {output_format}")
    if boundary_mode not in {"progress", "metadata"}:
        raise ValueError(f"Unsupported boundary mode: {boundary_mode}")
    if type(boundary_offset_ms) is not int or boundary_offset_ms < 0:
        raise ValueError("boundary_offset_ms must be a non-negative integer")
    directory = capture_dir.resolve()
    capture = _read_capture(directory)
    _estimate_boundaries(capture, boundary_mode, boundary_offset_ms)
    if output_format != "wav":
        for channels, width, _rate in capture.params.values():
            if channels not in (1, 2) or width not in (2, 3, 4):
                raise ValueError("FLAC/AIFF export requires mono/stereo 16-, 24- or 32-bit PCM")
            if output_format == "flac" and width == 4:
                raise ValueError(
                    "The FLAC encoder cannot preserve 32-bit PCM; use AIFF or WAV instead"
                )
    output = output_dir.resolve() if output_dir is not None else directory / "tracks"
    warnings = [
        (
            "Boundaries estimate track starts from metadata progress and current playback speed. "
            "This assumes constant playback speed since track start; seeks, pauses and crossfades "
            "can invalidate the estimate. Guarded or missing progress falls back to metadata time."
            if boundary_mode == "progress"
            else "Boundaries use metadata effective timestamps, not proven track starts. "
            "Progress is not subtracted; seeks, crossfades and late metadata may mislabel audio."
        ),
        "All received PCM is retained, including buffered audio later cleared by the player. "
        "Repeated tracks with identical metadata cannot reliably be distinguished.",
        *capture.warnings,
    ]
    if boundary_offset_ms:
        warnings.append(
            f"Track identity boundaries moved {boundary_offset_ms} ms earlier where safe. "
            "This fixed offset is not silence detection and may include the previous track's audio."
        )
    output.mkdir(parents=True, exist_ok=False)
    writer = _TrackWriter(output, output_format)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "capture_dir": str(directory),
        "boundary_method": (
            "progress_estimated_track_start"
            if boundary_mode == "progress"
            else "metadata_effective_timestamp"
        ),
        "boundary_mode": boundary_mode,
        "boundary_offset_ms": boundary_offset_ms,
        "output_format": output_format,
        "complete": False,
        "warnings": warnings,
        "metadata_events": capture.metadata,
        "tracks": writer.tracks,
    }
    try:
        with ExitStack() as stack:
            source: wave.Wave_read | None = None
            source_name: str | None = None
            for span in capture.spans:
                if source is None or source_name != span.source:
                    stack.close()
                    source = stack.enter_context(wave.open(str(directory / span.source), "rb"))
                    source_name = span.source
                timeline = capture.timelines.get(span.epoch, [])
                times = [entry.timestamp for entry in timeline]
                index = bisect_right(times, span.start) - 1
                offset = 0
                while offset < span.count:
                    metadata = timeline[index] if index >= 0 else None
                    stop = span.count
                    if index + 1 < len(timeline):
                        # The first sample at/after the timestamp belongs to the new metadata.
                        frames = (
                            (Fraction(times[index + 1]) - span.start) * span.params[2] / 1_000_000
                        )
                        stop = min(stop, -(-frames.numerator // frames.denominator))
                    if stop > offset:
                        writer.append(source, span, offset, stop - offset, metadata)
                        offset = stop
                    else:
                        index += 1
        writer.close()
        manifest["complete"] = True
    except Exception as exc:
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            writer.close()
        finally:
            (output / "tracks.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
            )
    return output
