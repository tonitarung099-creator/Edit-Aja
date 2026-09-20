from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .core import export_segments, probe_keyframes, probe_media

class AnalyzeWorker(QThread):
    ready = Signal(dict, list)
    failed = Signal(str)

    def __init__(self, ffprobe: str, source: Path):
        super().__init__()
        self.ffprobe = ffprobe
        self.source = source

    def run(self):
        try:
            metadata = probe_media(self.source, self.ffprobe)
            keyframes = probe_keyframes(self.source, self.ffprobe)
            self.ready.emit(metadata, keyframes)
        except Exception as exc:
            self.failed.emit(str(exc))

class ExportWorker(QThread):
    progress_changed = Signal(int, str)
    log_line = Signal(str)
    done = Signal(str, int, int, float)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, ffmpeg: str, source: Path, output_dir: Path, base_name: str, cuts: list[int], duration_ms: int):
        super().__init__()
        self.ffmpeg = ffmpeg
        self.source = source
        self.output_dir = output_dir
        self.base_name = base_name
        self.cuts = list(cuts)
        self.duration_ms = duration_ms
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        started = time.time()
        try:
            count, size = export_segments(
                self.ffmpeg,
                self.source,
                self.output_dir,
                self.base_name,
                self.cuts,
                self.duration_ms,
                progress=lambda p, t: self.progress_changed.emit(p, t),
                log=lambda s: self.log_line.emit(s),
                cancelled=lambda: self._cancel,
            )
            self.done.emit(str(self.output_dir), count, size, time.time() - started)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))

class AgentWorker(QThread):
    ready = Signal(dict)
    failed = Signal(str)

    def __init__(self, planner, endpoint: str, model: str, api_key: str, text: str, state: dict):
        super().__init__()
        self.planner = planner
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.text = text
        self.state = state

    def run(self):
        try:
            self.ready.emit(self.planner.remote_plan(self.endpoint, self.model, self.api_key, self.text, self.state))
        except Exception as exc:
            self.failed.emit(str(exc))
