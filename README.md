# encode

Quick and dirty TUI for re-encoding Blu-ray remuxes on my media server to
HEVC (x265), 10-bit, in place. Not meant to be general-purpose - just scratches
my own itch.

## What it does

- Browse the filesystem, mark one or more directories (recursively picks up
  every `.mkv` under them).
- Skips anything already HEVC.
- Encodes with `libx265`, 10-bit, CRF 20, `slow` preset. Audio, subtitles,
  chapters, and metadata are copied untouched - only video is re-encoded.
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
- `s` starts encoding everything under the marked directories.
- `q` quits (cleanly cancels any in-flight encode).

## Files

- `hevc_common.py` - probing (codec/duration) and the actual ffmpeg
  invocation/progress parsing, shared by the TUI.
- `encode_tui.py` - the Textual app itself.
