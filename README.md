# sendspin

[![pypi_badge](https://img.shields.io/pypi/v/sendspin.svg)](https://pypi.python.org/pypi/sendspin)

Connect to any [Sendspin](https://www.sendspin-audio.com) server and instantly turn your computer into an audio target that can participate in multi-room audio.

Sendspin CLI includes three apps:

- **[`sendspin`](#quick-start)** - Terminal client for interactive use
- **[`sendspin daemon`](#daemon-mode)** - Background daemon for headless devices
- **[`sendspin serve`](#sendspin-party)** - Host a Sendspin party to demo Sendspin

When using an explicit app (`daemon`, `serve`, or `player`), put it immediately after
`sendspin`. For example, use `sendspin daemon --name Kitchen`, not
`sendspin --name Kitchen daemon`.

<img width="1238" height="634" alt="Screenshot of the Sendspin terminal player" src="https://github.com/user-attachments/assets/2332a283-1994-4847-9265-4014f46abc56" />


[![A project from the Open Home Foundation](https://www.openhomefoundation.org/badges/ohf-project.png)](https://www.openhomefoundation.org/)

## Installation

**Install as daemon (Linux):**
```bash
curl -fsSL https://raw.githubusercontent.com/Sendspin/sendspin-cli/refs/heads/main/scripts/systemd/install-systemd.sh | sudo bash
```

**With uv:**
```bash
uv tool install sendspin
```

Support for Chromecast devices requires installation of extra dependencies:
```bash
uv tool install 'sendspin[cast]'
```

**With pip:**
```bash
pip install sendspin
```

## Try it without installation

**Run directly with [uv](https://docs.astral.sh/uv/getting-started/installation/):**

Start client

```bash
uvx sendspin
```

Host a Sendspin party

```bash
uvx sendspin serve --demo
uvx sendspin serve /path/to/media.mp3
uvx sendspin serve https://retro.dancewave.online/retrodance.mp3
```

## Updating

To update to the latest version of Sendspin:

**If installed with uv:**
```bash
uv tool upgrade sendspin
```

**If installed with pip:**
```bash
pip install --upgrade sendspin
```

**If installed as systemd daemon:**

The systemd daemon preserves your configuration during updates. Simply upgrade the package:

```bash
# Upgrade sendspin (the daemon installer uses uv by default)
uv tool upgrade sendspin

# Restart the service to use the new version
sudo systemctl restart sendspin
```

The installer may create a dedicated `sendspin` service account. In that case, the package is installed in that user's `uv` environment, not in your own shell. Upgrade the package as that user instead:

```bash
sudo -u sendspin bash -l -c "uv tool upgrade sendspin"
sudo systemctl restart sendspin
```

Your client name, audio device selection, and other settings in `~/.config/sendspin/settings-daemon.json` are preserved during the update.

> **Note:** You do **not** need to uninstall and reinstall when updating. Your configuration (client name, audio device, delay settings) is stored separately and will be preserved.

## Configuration Options

Sendspin stores settings in JSON configuration files that persist between sessions. All command-line arguments can also be set in the config file, with CLI arguments taking precedence over stored settings.

### Configuration File

Settings are stored in `~/.config/sendspin/`:
- `settings-tui.json` - Settings for the interactive TUI client
- `settings-daemon.json` - Settings for daemon mode
- `settings-serve.json` - Settings for serve mode

**Example configuration file (TUI/daemon):**
```json
{
  "player_volume": 50,
  "player_muted": false,
  "output_delay_ms": 0,
  "last_server_url": "ws://192.168.1.100:8927/sendspin",
  "name": "Living Room",
  "audio_device": "2",
  "audio_format": "flac:48000:24:2",
  "capture_dir": null,
  "log_level": "INFO",
  "listen_port": 8927,
  "use_mpris": true,
  "use_hardware_volume": true,
  "unpaired_access": true,
  "hook_set_volume": "/usr/local/bin/set-avr-volume",
  "manufacturer": "Acme Corp",
  "product_name": "Living Room Speaker",
  "interface": "192.168.1.5"
}
```

**Example configuration file (serve):**
```json
{
  "log_level": "INFO",
  "listen_port": 8927,
  "name": "My Sendspin Server",
  "source": "/path/to/music.mp3",
  "clients": ["ws://192.168.1.50:8927/sendspin", "ws://192.168.1.51:8927/sendspin"]
}
```

**Available settings:**

| Setting | Type | Mode | Description |
|---------|------|------|-------------|
| `player_volume` | integer (0-100) | TUI/daemon | Player output volume percentage |
| `player_muted` | boolean | TUI/daemon | Whether the player is muted |
| `output_delay_ms` | float | TUI/daemon | Extra playback delay in milliseconds (`--output-delay-ms`). Older settings files that use `static_delay_ms` are migrated automatically |
| `last_server_url` | string | TUI/daemon | Server URL (used as default for `--url`) |
| `name` | string | All | Friendly name for client or server (`--name`) |
| `audio_device` | string | TUI/daemon | Audio device index, name prefix, or ALSA device name (`--audio-device`) |
| `audio_format` | string | TUI/daemon | Preferred audio format (`--audio-format`, e.g., `flac:48000:24:2`) |
| `capture_dir` | string or null | TUI/daemon | Opt-in diagnostic PCM and metadata recording directory (`--capture-dir`). Disabled by default |
| `log_level` | string | All | Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `listen_port` | integer | daemon/serve | Listen port (`--port`, default: 8927) |
| `use_mpris` | boolean | TUI/daemon | Enable MPRIS integration (default: true) |
| `use_hardware_volume` | boolean | TUI/daemon | Control hardware/system output volume instead of software volume (`--hardware-volume true/false`). Default: on for daemon (if available), off for TUI |
| `unpaired_access` | boolean | TUI/daemon | Allow servers to play on this client without pairing (`--unpaired-access true/false`, default: true). See [Pairing](#pairing) |
| `hook_set_volume` | string | TUI/daemon | Script to run for external volume control (`--hook-set-volume`). Receives the effective volume 0-100 as the last argument |
| `hook_start` | string | TUI/daemon | Command to run when audio stream starts |
| `hook_stop` | string | TUI/daemon | Command to run when audio stream stops |
| `manufacturer` | string | TUI/daemon | Manufacturer name reported in the client hello (`--manufacturer`) |
| `product_name` | string | TUI/daemon | Product name reported in the client hello (`--product-name`); defaults to auto-detected OS/platform name |
| `interface` | string | TUI/daemon | IP address of the network interface to use (`--interface`) |
| `visualizer` | boolean | TUI | Render the `visualizer@v1` audio visualizer on launch (default: false). Toggle with `v` in the TUI |
| `source` | string | serve | Default audio source (file path or URL, ffmpeg input) |
| `source_format` | string | serve | ffmpeg container format for audio source |
| `clients` | array | serve | Client URLs to connect to (`--client`) |

Settings are automatically saved when changed through the TUI. You can also edit the JSON file directly while the client is not running.

### Diagnostic audio capture

To collect data for investigating sample-accurate track extraction, enable capture in either mode:

```powershell
sendspin --capture-dir .\recordings
sendspin daemon --capture-dir .\recordings --url ws://host:port/sendspin
```

Alternatively, set `"capture_dir": "recordings"` in the relevant settings file. The CLI
directory takes precedence. Remove the setting or set it to `null` to disable capture.
Each run creates a unique subdirectory containing:

- `session.json`: schema version, UTC start time, PCM representation and capture configuration.
- `audio-000001.wav`, etc.: unmodified decoded PCM, split at exactly 600 seconds of
  sample frames. Format changes, player stream starts/clears/ends and disconnects
  finish the current file early. The final file may also be shorter.
- `events.jsonl`: one JSON object per line, including full SDK-serialized current
  and scheduled metadata payloads, controller/group updates, stream lifecycle
  messages, audio formats (including hex codec headers), and an audio packet index.
  Metadata includes its original server-clock `timestamp`, progress and artwork URL
  when supplied; artwork image bytes are not downloaded.

Audio is independently decoded on a bounded capture worker, before volume, mute,
drift correction or playback buffering can modify or discard samples. It captures
**received audio**, including buffered audio later cleared by a seek, not necessarily
what was heard. No silence is inserted for timestamp gaps, and overlaps are not
removed. Playback continues normally; capture adds decoding and disk I/O overhead.

Each journal observation includes a sequence number, UTC and monotonic observation
times, client-clock time, synchronization status and, when synchronized, an estimated
server-clock time with playback delay excluded. The configured output delay and
server ID/name are also included when available. Observation time means
the SDK callback time, not necessarily the original network arrival time (especially
for current metadata that was previously scheduled).

Each `audio` index row maps a packet's original `server_timestamp_us` and
`send_ahead_us` to `file`, `file_frame_offset`, `frame_count`,
`packet_frame_offset` and `packet_frame_count`. Offsets/counts are **sample frames**
(one sample per channel), not bytes. A packet split across files has multiple rows.
The exact effective start time of a row is its packet timestamp plus
`packet_frame_offset * 1_000_000 / sample_rate` microseconds; retain this as a rational
value when exact timing matters. The preceding `format` event supplies the sample
rate, bit depth and channel count; each WAV header also contains these values.

Read the journal using Python's standard JSON parser:

```python
import json
from pathlib import Path

capture = Path("recordings") / "YOUR_CAPTURE_DIRECTORY"
with (capture / "events.jsonl").open(encoding="utf-8") as journal:
    for line in journal:
        event = json.loads(line)
        if event["type"] in {"metadata_scheduled", "metadata_current"}:
            print(event["payload"])
```

Stop normally to drain pending records and finalize WAV headers. The final
`capture_end` event has `complete: true` on success. A decoder error, invalid PCM or
queue overflow stops capture and logs an error; where the disk remains writable,
`capture_end` records `complete: false` and the error. A missing `capture_end` means
the capture was interrupted or could not be finalized, so treat it as incomplete.
Disk errors are logged and may prevent the failure marker from being written.

This is diagnostic data, **not yet guaranteed track-perfect extraction**. Metadata
effective times are not guaranteed track starts, and seeks/crossfades may need
additional server-provided boundary information. SDK-parsed metadata is preserved,
not raw WebSocket messages or unknown fields discarded by the SDK. There is no
retention limit: 48 kHz stereo 16-bit PCM uses about 115 MB per 10 minutes
(24-bit uses about 173 MB), plus journal data. Captures contain audio and potentially
private metadata/URLs; only record content you are authorized to save and review it
before sharing.

### Splitting a capture into tracks

Run the offline splitter after stopping capture (no server or audio output device is needed):

```bash
sendspin split-tracks recordings/20261119T141317Z-uz9mi3xu
```

By default this creates a new `tracks` directory inside the capture, with numbered
`Artist - Title.wav` files and a `tracks.json` report. Use `--output-dir PATH` to
choose a different **new** directory. Existing output directories are never overwritten,
and source recordings are never modified.

Choose lossless FLAC or AIFF output with `--format` (WAV remains the default):

```bash
sendspin split-tracks recordings/20261119T141317Z-uz9mi3xu --format flac --output-dir tracks-flac
sendspin split-tracks recordings/20261119T141317Z-uz9mi3xu --format aiff --output-dir tracks-aiff
```

FLAC exports include standard Vorbis comments; AIFF exports include ID3v2 text tags.
The available **title, artist, album, album artist, year and original album track
number** are embedded in each file. The output sequence number is not substituted
for the album track number. Unknown tags are left unset; the full captured metadata
remains in `tracks.json`. Export is fully offline: artwork URLs are not fetched and
cover art is not embedded. WAV output is unchanged and keeps metadata in the report.

Sample rate, channel count and PCM sample values are preserved. FLAC supports
16- and 24-bit capture audio with the bundled encoder; 32-bit FLAC export is rejected
rather than silently reducing precision (use AIFF or WAV for 32-bit audio). AIFF
supports 16-, 24- and 32-bit PCM. The existing PyAV dependency supplies the encoders;
no separate ffmpeg executable is needed. FLAC/AIFF export uses a temporary WAV per
track, removed after encoding, so allow additional disk space for the largest track.

The default **`--boundary-mode progress`** compensates for late metadata: on a
track-identity change it estimates the start as `metadata.timestamp -
track_progress * 1_000_000 / playback_speed` microseconds. Progress is media time
in milliseconds and playback speed is scaled by 1000. For example, normal-speed
metadata reporting 1510 ms of progress moves the cut 1.51 seconds earlier, without
applying a fixed offset to every track.

Paused, live/unknown-duration, missing or invalid progress falls back to the metadata
timestamp. An estimate that would cross the previous metadata update or an earlier
stream restart also falls back and records a warning. Updates to an unchanged track
do not backdate a new boundary. This is an estimate: it assumes uninterrupted playback
at the reported speed since track start; seeks, earlier pauses or speed changes can
invalidate it. It cannot restore audio never received by the capture.

Use **`--boundary-mode metadata`** to retain the original timestamp-only behavior:

```bash
sendspin split-tracks recordings/20261119T154607Z-ifbmas1g --boundary-mode metadata --output-dir tracks-timestamps
```

For a capture with consistently late cuts and sufficient silence between songs, use
**`--boundary-offset-ms 500`** to move track-identity boundaries an additional 500 ms
earlier. This non-negative integer option works with either boundary mode and defaults
to **0**, preserving existing behavior:

```bash
sendspin split-tracks recordings5/20261120T131719Z-wyada_q8 --format flac --boundary-offset-ms 500 --output-dir tracks-offset-500ms
```

The offset is not silence detection or a universal timing correction: it may include
the end of the previous song when gaps are too short. Same-track metadata updates are
not shifted. Shifts are limited at the previous metadata timestamp and, after a stream
restart, at that run's first received audio timestamp; limitations produce warnings.
The requested offset and each boundary's actual applied offset are saved in `tracks.json`.

Canceled scheduled updates are ignored. Cuts fall on the first complete sample frame
at or after the chosen boundary, using rational arithmetic to avoid rounding drift.
Tracks spanning multiple capture WAVs are joined without lossy encoding, gain changes,
silence insertion, or removal of overlapping audio. Progress-only and artwork-only
updates do not create a new track. Changes to title, artist, album, album artist,
track number or year do.

Stream restarts/clears/ends, connection/server changes, format changes, and timestamp
gaps/overlaps larger than one sample frame create separate numbered parts, even if the
track name stays the same. Audio without timestamped metadata is saved as `Unknown`.
All indexed frames are retained, including received audio that playback later cleared.
Consequently, a seek or pause/resume may produce partial or overlapping parts rather
than a single clean song.

`tracks.json` contains the selected boundary mode, full current-metadata observations,
each estimate's original timestamp, chosen timestamp, backdating amount and reason,
each output's metadata,
source WAV frame ranges and output offsets, exact rational server-clock start/end
times, and warnings. Its `complete` field means splitting completed, **not** that the
tracks have proven musical boundaries or that the input capture was complete. A failed
write leaves a report marked incomplete when possible; use a different output directory
for a retry.

This is a lossless diagnostic partition, not guaranteed track-perfect extraction:
inaccurate progress or timestamp-only fallbacks can still misplace musical boundaries;
crossfades and consecutive plays of the same metadata cannot be reliably separated.
The report preserves the source mapping so these decisions can be revisited.

### Server Connection

By default, the player automatically discovers Sendspin servers on your local network using mDNS. You can also connect directly to a specific server:

```bash
sendspin --url ws://192.168.1.100:8080/sendspin
```

The player follows the Sendspin 1.0 spec, so it needs a server built on it, such as
Music Assistant 2.10.7 or newer (or another server on aiosendspin 10). Older servers refuse
the connection.

**List available servers on the network:**
```bash
sendspin servers list
```

### Pairing

Sendspin connections are end-to-end encrypted. The first time a client connects to a
server that requires pairing, the server picks a pairing method and the client displays
a short pairing code — enter it on the server to approve the client. Once paired, the client's
credentials are persisted (see `--settings-dir` below), so subsequent connections to the
same server don't require pairing again.

- **TUI**: the pairing code appears in a "Pairing Required" panel that takes over the screen until
  pairing completes.
- **Daemon**: the pairing code is only logged (`Pairing required: enter pairing code ...`) — there's no other
  surface for it. You need to be watching the daemon's output at the moment it first
  connects to a new server. Under systemd, that means `journalctl -u sendspin -f`. For a
  headless install with unpaired access turned off, plan to watch the log during first setup
  for each new server.

If a pairing attempt fails (e.g. a mismatched pairing code), the client logs the reason and you
can retry by reconnecting.

Unpaired access is on by default, so servers can play on this client without a pairing code once
the server approves it (Music Assistant does this automatically). Unpaired connections are
still encrypted, but another device on the network could impersonate either side. To
require pairing, use `--unpaired-access false` (or `"unpaired_access": false` in the
settings file).

**Upgrading from a version before pairing support:** this client's protocol identity
(`client_id`) used to be a stable, often user-chosen string; it's now derived from a
generated cryptographic identity instead, so upgrading a previously-configured install
makes it look like a brand-new device to any server it talks to. You'll likely need to
re-add it to zones/groups (and re-pair, if the server requires it) after the first
upgrade. The client logs a one-time warning when this happens.

### Client Identification

Each client's cryptographic identity (used for encryption and pairing) and settings are
stored per `--settings-dir` (default: `~/.config/sendspin`). If you want to run multiple
players on the **same computer** as distinct clients — each with its own identity,
pairing records, and settings — give each one a separate settings directory:

```bash
sendspin --settings-dir ~/.config/sendspin-kitchen --name "Kitchen"
sendspin --settings-dir ~/.config/sendspin-bedroom --name "Bedroom"
```

- `--settings-dir`: Directory for this client's settings, identity, and pairing records (optional; defaults to `~/.config/sendspin`)
- `--name`: A friendly name displayed on the server (optional; defaults to hostname)

### Audio Output Device Selection

By default, the player uses your system's default audio output device. You can list available devices or select a specific device:

**List available audio devices:**
```bash
sendspin audio-devices list
```

This displays all audio output devices with their IDs, channel configurations, and sample rates. The default device is marked.

**Select a specific audio device by index:**
```bash
sendspin --audio-device 2
```

**Or by name prefix:**
```bash
sendspin --audio-device "MacBook"
```

**Or by raw ALSA device name (Linux):**
```bash
sendspin --audio-device dmixer
```

This is useful for ALSA plugin devices (dmix, plug, etc.) that may not appear in the numbered PortAudio device list (though they may be shown in the ALSA devices section on Linux). For example, in a dual mono setup where two daemons share a single sound card via dmix, each daemon can target a different ALSA device that routes to a specific channel:

```bash
# Room 1: left channel via dmix
sendspin daemon --name "Living Room" --audio-device living_room

# Room 2: right channel via dmix
sendspin daemon --name "Kitchen" --audio-device kitchen
```

This requires an `/etc/asound.conf` with dmix and plug devices that route to the appropriate channels. See your ALSA documentation for details on configuring dmix.

This is particularly useful when running `sendspin daemon` on headless devices or when you want to route audio to a specific output.

### Preferred Audio Format

By default, the player negotiates the best audio format with the server from the list of formats supported by your audio device (preferring FLAC over PCM). You can specify a preferred format to prioritize:

```bash
sendspin --audio-format flac:48000:24:2
```

The format string uses the pattern `codec:sample_rate:bit_depth:channels`:
- **codec**: `flac` (compressed, preferred) or `pcm` (uncompressed)
- **sample_rate**: Sample rate in Hz (e.g., `44100`, `48000`, `96000`)
- **bit_depth**: Bits per sample (`16` or `24`)
- **channels**: Channel count (`1` for mono, `2` for stereo)

The specified format is validated against the audio device on startup. If the device doesn't support it, the player will exit with an error.

### System Volume Control

On Linux with PulseAudio/PipeWire, Sendspin can control your system output volume directly. Volume adjustments (keyboard shortcuts, server commands) change the system volume. The current system volume is read on startup — the `player_volume` and `player_muted` settings are only used when hardware volume is disabled.

Hardware volume is **on by default in daemon mode** and **off by default in TUI mode**. To override:

```bash
sendspin --hardware-volume true             # Enable for TUI
sendspin daemon --hardware-volume false     # Disable for daemon
```

If your real volume control lives on another device, you can hand volume changes off to a script instead:

```bash
sendspin daemon --hook-set-volume /usr/local/bin/set-avr-volume
```

The script receives the effective output volume as its last argument in the range `0-100`. When the player is muted, Sendspin calls the script with `0` and keeps the last logical `player_volume` persisted separately so unmuting restores the previous level.

Because Sendspin cannot read back external device state from the hook, startup volume comes from the persisted `player_volume` and `player_muted` settings. Those settings are updated whenever Sendspin successfully applies a new volume through the hook. When `hook_set_volume` is configured, it takes precedence over PulseAudio/PipeWire hardware volume control.

### Adjusting Playback Delay

The player supports adjusting playback delay to compensate for audio hardware latency or achieve better synchronization across devices.

```bash
sendspin --output-delay-ms 50
```

`--static-delay-ms` still works as a deprecated alias for `--output-delay-ms`.

> **Note:** A delay of 0ms works well in most cases. If audio is playing slightly too late, a small positive delay (e.g., 50ms) can help compensate for audio hardware latency. On compatible servers, delay can be configured remotely per player, so you shouldn't need to set this locally.

### Daemon Mode

To run the player as a background daemon without the interactive TUI (useful for headless devices or scripts):

```bash
sendspin daemon
```

The daemon runs in the background and logs status messages to stdout. It accepts the same connection and audio options as the TUI client:

```bash
sendspin daemon --name "Kitchen" --audio-device 2
```

In daemon mode without `--url`, the client listens for incoming server connections and advertises itself via mDNS. The `--name` option (or `name` setting) is used as the friendly name in the mDNS advertisement, making it easy for servers to identify this client on the network.

Use `--manufacturer` and `--product-name` to override the device identity reported to the server in the client hello. This is useful when running the daemon in a container or on a custom device where the auto-detected OS name is not meaningful:

```bash
sendspin daemon --name "Living Room" --manufacturer "Acme" --product-name "Living Room Speaker"
```

### Hooks

You can run external commands when audio streams start or stop. This is useful for controlling amplifiers, lighting, or other home automation:

```bash
sendspin --hook-start "./turn_on_amp.sh" --hook-stop "./turn_off_amp.sh"
```

Or with inline commands:

```bash
sendspin daemon --hook-start "amixer set Master unmute" --hook-stop "amixer set Master mute"
```

`--hook-set-volume` is separate from these stream lifecycle hooks. It is intended for external volume controllers and receives the effective output volume as its last argument.

Hooks receive these environment variables:
- `SENDSPIN_EVENT` - Event type: "start" or "stop"
- `SENDSPIN_SERVER_ID` - Connected server identifier
- `SENDSPIN_SERVER_NAME` - Connected server friendly name
- `SENDSPIN_SERVER_URL` - Connected server URL. Only available if client initiated the connection to the server.
- `SENDSPIN_CLIENT_ID` - Client identifier
- `SENDSPIN_CLIENT_NAME` - Client friendly name

### Visualizer

The TUI includes a real-time audio visualizer driven by the `visualizer@v1` role. All analysis is computed on the server and streamed to the TUI, time-aligned to the audio playhead. It shows:

- **Spectrum bars** — frequency magnitude across the range, tinted by overall loudness (and by the album-artwork palette when the server provides one).
- **Beats timeline** — a `beats (NNN BPM):` strip with the estimated tempo; downbeats render differently from regular beats.
- **Peaks timeline** — a `peaks:` strip of energy onsets (transients like drum hits), independent of the beat grid, with glyph height scaled by onset strength.
- **Dominant frequency** — an `f_peak:` readout with an arrow marking the loudest frequency on the spectrum.

Lower rows are dropped first on short terminals, keeping the spectrum visible.

Toggle the visualizer by pressing `v` in the TUI. Your preference is saved in settings and remembered on next launch.

### Debugging & Troubleshooting

If you experience synchronization issues or audio glitches, you can enable detailed logging to help diagnose the problem:

```bash
sendspin --log-level DEBUG
```

This provides detailed information about time synchronization. The output can be helpful when reporting issues.

### Network Interface Binding

On machines with multiple network interfaces (e.g., a home server with both a LAN and a WAN/internet interface), you can restrict Sendspin to a specific interface using `--interface`:

```bash
sendspin --interface 192.168.1.5
sendspin daemon --interface 192.168.1.5
```

The `--interface` option takes the **IP address** of the interface to use. This affects:

- **mDNS discovery**: only servers advertising on that interface will be found.
- **Daemon listening mode** (no `--url`): the incoming-connection server binds only to that IP, so servers on other interfaces (e.g., the WAN) cannot connect.

This is useful when you want Sendspin to be accessible only on your LAN, not on the internet-facing interface.

## Install as Daemon (systemd, Linux)

For headless devices like Raspberry Pi, you can install `sendspin daemon` as a systemd service that starts automatically on boot.

**Install:**
```bash
curl -fsSL https://raw.githubusercontent.com/Sendspin/sendspin-cli/refs/heads/main/scripts/systemd/install-systemd.sh | sudo bash
```

The installer will:
- Check and offer to install dependencies (libportaudio2, uv)
- Install sendspin via `uv tool install`
- Prompt for client name and audio device selection
- Create systemd service and configuration

**Manage the service:**
```bash
sudo systemctl start sendspin    # Start the service
sudo systemctl stop sendspin     # Stop the service
sudo systemctl status sendspin   # Check status
journalctl -u sendspin -f        # View logs
```

**Configuration:** Edit `~/.config/sendspin/settings-daemon.json` to change client name, audio device, or other settings.

**Uninstall:**
```bash
curl -fsSL https://raw.githubusercontent.com/Sendspin/sendspin-cli/refs/heads/main/scripts/systemd/uninstall-systemd.sh | sudo bash
```

## Sendspin Party

The Sendspin client includes a mode to enable hosting a Sendspin Party. This will start a Sendspin server playing a specified audio file or URL in a loop, allowing nearby Sendspin clients to connect and listen together. It also hosts a web interface for easy playing and sharing. Fire up that home or office 🔥

```bash
# Demo mode
sendspin serve --demo
# Local file
sendspin serve /path/to/media.mp3
# Remote URL
sendspin serve https://retro.dancewave.online/retrodance.mp3
# Without pre-installing Sendspin
uvx sendspin serve /path/to/media.mp3
# Connect to specific clients
sendspin serve --demo --client ws://192.168.1.50:8927/sendspin --client ws://192.168.1.51:8927/sendspin
```

By default, only players that support encryption can connect. Add `--allow-unencrypted` to also accept older players from before the Sendspin 1.0 release candidate:

```bash
sendspin serve --demo --allow-unencrypted
```

### Multi-Worker Mode

For serving many concurrent listeners, use `--workers` to run multiple server processes behind a reverse proxy:

```bash
sendspin serve --demo --workers 4
```

This spawns 4 worker processes on consecutive ports starting from `--port` (default 8927), so ports 8927-8930. Place a reverse proxy (e.g., nginx, Caddy) in front with load balancing across these ports.

Note: `--client` is not supported with `--workers`.
