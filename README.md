# Encode TUI 

Quick and dirty TUI for re-encoding Blu-ray remuxes on my media server to
HEVC (x265), 10-bit, in place. Not meant to be general-purpose - just scratches
my own itch.

## What it does

- Browse the filesystem, mark one or more directories (recursively picks up
  every `.mkv` under them).
- Skips anything already HEVC.
- Encodes with software `libx265`, 10-bit. Preset and CRF are selectable
  right in the TUI before starting (defaults: `medium` / CRF 20). Audio,
  subtitles, chapters, and metadata are copied untouched - only video is
  re-encoded.
- Encoder dropdown in the options panel also offers two hardware backends
  (preset is ignored for both - no such knob on hardware encoders):
  - **VAAPI** (Intel/Linux) - uses the CRF field (same lower=better scale as
    software). Whether this actually works depends entirely on the specific
    iGPU/driver: confirmed working on a 12th-gen Alder Lake (Iris Xe)
    laptop, confirmed **not** available (decode-only) on an older Coffee
    Lake (Gen9.5) box, per `vainfo`. Check with `vainfo` if unsure before
    relying on it.
  - **VideoToolbox** (macOS) - the numeric field becomes a 1-100 quality
    value instead (higher = better). Untested on real hardware (this tool
    was built on Linux) - if `-q:v` constant-quality mode isn't supported by
    your ffmpeg/macOS version, it'll fail loudly and need switching to
    bitrate-based control instead.
  - Both hardware paths trade some compression efficiency/quality-per-bit
    for a large speed win over software `libx265`.
- Verifies the output's duration matches the source (within a couple
  seconds) before replacing the original. If verification fails, the
  original is left alone and the encoded file is kept around (as
  `.<name>.tmp.mkv`) for inspection instead of being silently discarded.
- Shows a live per-file progress bar (elapsed/ETA/speed) and an overall
  progress bar (file N/M, elapsed, storage saved) - no raw ffmpeg log spam.

## Requirements

- `ffmpeg` / `ffprobe` on `PATH`.
- Python 3.10+.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```
source .venv/bin/activate
python encode_tui.py [start_dir]
```

- Arrow keys to navigate.
- `space` marks/unmarks the highlighted directory.
- Pick a preset/CRF from the dropdown/input at the top before starting.
- `s` starts encoding everything under the marked directories.
- `q` quits (cleanly cancels any in-flight encode).

## Files

- `hevc_common.py` - probing (codec/duration) and the actual ffmpeg
  invocation/progress parsing, shared by the TUI.
- `encode_tui.py` - the Textual app itself.
