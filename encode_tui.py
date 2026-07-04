#!/usr/bin/env python3
"""Textual TUI for batch HEVC re-encoding.

Navigate the filesystem, mark one or more directories to process (their .mkv
files are found recursively), then kick off the batch encode and watch a
per-file progress bar plus an overall progress bar - no raw ffmpeg output.

Usage: ./encode_tui.py [start_dir]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DirectoryTree, Footer, Header, Label, ProgressBar, RichLog
from textual.widgets.tree import TreeNode
from textual.widgets._directory_tree import DirEntry

import hevc_common as hc


class MarkableDirectoryTree(DirectoryTree):
    """A DirectoryTree restricted to directories, with toggleable marks."""

    BINDINGS = [
        Binding("space", "toggle_mark_cursor", "Mark/unmark", show=True),
    ]

    def __init__(self, path: Path, **kwargs) -> None:
        super().__init__(path, **kwargs)
        self.marked_paths: set[Path] = set()

    def action_toggle_mark_cursor(self) -> None:
        if self.cursor_node is not None:
            self.toggle_mark(self.cursor_node)

    def filter_paths(self, paths):
        return [p for p in paths if p.is_dir() and not p.name.startswith(".")]

    def render_label(self, node: TreeNode[DirEntry], base_style, style) -> Text:
        label = super().render_label(node, base_style, style)
        if node.data is not None and node.data.path in self.marked_paths:
            return Text("[x] ") + label
        return Text("[ ] ") + label

    def toggle_mark(self, node: TreeNode[DirEntry]) -> None:
        if node.data is None:
            return
        path = node.data.path
        if path in self.marked_paths:
            self.marked_paths.discard(path)
        else:
            self.marked_paths.add(path)
        node.refresh()


@dataclass
class FileToConvert:
    path: Path
    duration: float


@dataclass
class BatchSummary:
    converted: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[Path] = field(default_factory=list)


class PickScreen(Screen):
    BINDINGS = [
        Binding("s", "start", "Start encoding"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, start_path: Path) -> None:
        super().__init__()
        self.start_path = start_path

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(
            "Navigate with arrow keys. [b]Space[/b] marks/unmarks a directory "
            "(all .mkv files under it, recursively). [b]S[/b] starts encoding.",
            id="help",
        )
        yield MarkableDirectoryTree(self.start_path, id="tree")
        yield Footer()

    def action_start(self) -> None:
        tree = self.query_one(MarkableDirectoryTree)
        if not tree.marked_paths:
            self.notify("No directories marked. Press space to mark one first.", severity="warning")
            return
        self.app.push_screen(EncodeScreen(sorted(tree.marked_paths)))

    def action_quit(self) -> None:
        self.app.exit()


class EncodeScreen(Screen):
    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    SCAN_CONCURRENCY = 8

    def __init__(self, directories: list[Path]) -> None:
        super().__init__()
        self.directories = directories
        self.finished = False
        self.summary: BatchSummary | None = None

        self._scanning = True
        self._start_time = 0.0
        self._file_start_time: float | None = None
        self._current_duration = 1.0
        self._current_out_time = 0.0
        self._current_speed = 0.0
        self._current_name = ""
        self._idx = 0
        self._total_files = 0
        self._bytes_saved = 0
        self._tick_timer = None
        self._worker = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Label("Scanning...", id="overall_label")
            yield ProgressBar(id="overall_bar", show_eta=False)
            yield Label("", id="file_label")
            yield ProgressBar(id="file_bar", show_eta=False)
            yield RichLog(id="log", wrap=True, markup=True)
        yield Footer()

    async def action_quit(self) -> None:
        # Cancel and fully await the encode worker before exiting, so
        # ffmpeg's subprocess transport gets cleaned up while the event loop
        # is still alive - otherwise it can get garbage-collected after the
        # loop closes and print a harmless-but-noisy traceback.
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(Exception):
                await self._worker.wait()
        self.app.exit()

    def on_mount(self) -> None:
        self._start_time = time.monotonic()
        self._tick_timer = self.set_interval(1.0, self._refresh_labels)
        self._worker = self.run_encoding()

    def _refresh_labels(self) -> None:
        if self.finished:
            return
        overall_label = self.query_one("#overall_label", Label)
        now = time.monotonic()
        overall_elapsed = now - self._start_time

        if self._scanning:
            return  # scanning updates its own label text directly

        overall_label.update(
            f"Overall - file {self._idx}/{self._total_files} - "
            f"elapsed {hc.format_duration(overall_elapsed)} - "
            f"saved {hc.format_bytes(self._bytes_saved)}"
        )

        if self._file_start_time is not None:
            file_label = self.query_one("#file_label", Label)
            file_elapsed = now - self._file_start_time
            out_time = self._current_out_time
            duration = self._current_duration
            if out_time > 1.0 and duration > out_time:
                eta_str = hc.format_duration(file_elapsed * (duration - out_time) / out_time)
            else:
                eta_str = "--:--"
            file_label.update(
                f"[{self._idx}/{self._total_files}] {self._current_name} - "
                f"elapsed {hc.format_duration(file_elapsed)} / eta {eta_str} "
                f"({self._current_speed:.2f}x)"
            )

    def _log(self, message: str) -> None:
        self.query_one(RichLog).write(message)

    @work(exclusive=True)
    async def run_encoding(self) -> None:
        log = self.query_one(RichLog)
        overall_label = self.query_one("#overall_label", Label)
        overall_bar = self.query_one("#overall_bar", ProgressBar)
        file_label = self.query_one("#file_label", Label)
        file_bar = self.query_one("#file_bar", ProgressBar)

        # Clean up stale temp files from any previous interrupted run.
        for directory in self.directories:
            for stale in directory.rglob(".*.tmp.mkv"):
                log.write(f"Removing stale temp file: {stale}")
                stale.unlink(missing_ok=True)

        all_files: dict[Path, None] = {}
        for directory in self.directories:
            for f in hc.find_mkv_files(directory):
                all_files[f] = None
        paths = list(all_files.keys())

        summary = BatchSummary()
        to_convert: list[FileToConvert] = []

        # Probe every file concurrently (bounded) instead of one ffprobe call
        # at a time - serial probing of a large TV library can otherwise
        # block the UI for minutes before the encode screen shows anything.
        scanned = 0
        sem = asyncio.Semaphore(self.SCAN_CONCURRENCY)
        probe_results: list[tuple[Optional[str], float]] = [(None, 0.0)] * len(paths)

        async def scan_one(i: int, f: Path) -> None:
            nonlocal scanned
            async with sem:
                probe_results[i] = await hc.probe_codec_and_duration_async(f)
            scanned += 1
            overall_label.update(f"Scanning files... {scanned}/{len(paths)}")

        overall_label.update(f"Scanning files... 0/{len(paths)}")
        await asyncio.gather(*(scan_one(i, f) for i, f in enumerate(paths)))

        for f, (codec, duration) in zip(paths, probe_results):
            if codec == "hevc":
                summary.skipped.append(f)
            else:
                to_convert.append(FileToConvert(path=f, duration=duration))

        total_files = len(to_convert)
        total_duration = sum(item.duration for item in to_convert) or 1.0
        overall_bar.update(total=total_duration, progress=0)
        log.write(f"Found {len(all_files)} .mkv file(s): {total_files} to convert, "
                  f"{len(summary.skipped)} already HEVC.")

        self._scanning = False
        self._total_files = total_files
        completed_duration = 0.0

        for idx, item in enumerate(to_convert, start=1):
            path = item.path
            duration = item.duration or 1.0
            self._idx = idx
            self._current_name = path.name
            self._current_duration = duration
            self._current_out_time = 0.0
            self._current_speed = 0.0
            self._file_start_time = time.monotonic()

            file_label.update(f"[{idx}/{total_files}] {path.name}")
            file_bar.update(total=duration, progress=0)
            log.write(f"=== {path} ===")

            tmp_output = hc.tmp_output_path_for(path)
            tmp_output.unlink(missing_ok=True)
            size_before = path.stat().st_size

            base_completed = completed_duration

            def on_progress(t: float, speed: float, _duration=duration, _base=base_completed) -> None:
                self._current_out_time = t
                self._current_speed = speed
                file_bar.update(progress=min(t, _duration))
                overall_bar.update(progress=min(_base + t, total_duration))

            try:
                result = await hc.encode_async(path, tmp_output, on_progress)
            except asyncio.CancelledError:
                tmp_output.unlink(missing_ok=True)
                raise

            if result.returncode == 0:
                src_dur = duration
                out_dur = hc.probe_duration(tmp_output)
                if tmp_output.exists() and tmp_output.stat().st_size > 0 and hc.durations_match(src_dur, out_dur):
                    size_after = tmp_output.stat().st_size
                    path.unlink()
                    tmp_output.rename(path)
                    self._bytes_saved += size_before - size_after
                    log.write(f"[green]OK[/green] (src {src_dur:.1f}s vs out {out_dur:.1f}s, "
                              f"saved {hc.format_bytes(size_before - size_after)})")
                    summary.converted.append(path)
                else:
                    log.write(f"[red]VERIFICATION FAILED[/red] (src {src_dur:.1f}s vs out {out_dur:.1f}s) - "
                              f"keeping original, left {tmp_output} for inspection")
                    summary.failed.append(path)
            else:
                log.write(f"[red]ffmpeg failed[/red] - keeping original\n{result.stderr[-500:]}")
                tmp_output.unlink(missing_ok=True)
                summary.failed.append(path)

            completed_duration += duration
            file_bar.update(progress=duration)
            overall_bar.update(progress=min(completed_duration, total_duration))

        if self._tick_timer is not None:
            self._tick_timer.stop()

        total_elapsed = time.monotonic() - self._start_time
        overall_label.update(
            f"Done - elapsed {hc.format_duration(total_elapsed)} - "
            f"saved {hc.format_bytes(self._bytes_saved)}"
        )
        log.write("")
        log.write(f"Done. Converted: {len(summary.converted)}, "
                  f"Skipped (already HEVC): {len(summary.skipped)}, "
                  f"Failed: {len(summary.failed)}, "
                  f"Total saved: {hc.format_bytes(self._bytes_saved)}")
        if summary.failed:
            log.write("Failed files:")
            for f in summary.failed:
                log.write(f"  {f}")
        self.summary = summary
        self.finished = True


class HevcEncodeApp(App):
    CSS = """
    #help { padding: 0 1; }
    #tree { height: 1fr; }
    #overall_label, #file_label { padding: 1 1 0 1; }
    #overall_bar, #file_bar { padding: 0 1; }
    #log { height: 1fr; border: solid $accent; margin: 1; }
    """

    def __init__(self, start_dir: Path) -> None:
        super().__init__()
        self.start_dir = start_dir

    def on_mount(self) -> None:
        self.push_screen(PickScreen(self.start_dir))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start_dir", nargs="?", default=".", help="Directory to start browsing from")
    args = parser.parse_args()

    start_dir = Path(args.start_dir).expanduser().resolve()
    if not start_dir.is_dir():
        print(f"Not a directory: {start_dir}", file=sys.stderr)
        raise SystemExit(1)

    HevcEncodeApp(start_dir).run()


if __name__ == "__main__":
    main()
