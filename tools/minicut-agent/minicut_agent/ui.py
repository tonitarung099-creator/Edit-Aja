from __future__ import annotations

import json
import queue
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSlider, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget
)

from . import APP_TITLE
from .agent import AgentPlanner, MUTATING_TOOLS, ToolRegistry
from .bridge import BridgeCall, LocalBridge
from .core import (
    CutPoint, ProjectModel, SUPPORTED_VIDEO, clock_text, find_tool,
    load_project_file, parse_time_ms
)
from .workers import AgentWorker, AnalyzeWorker, ExportWorker


class TimelineSlider(QSlider):
    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.marks: list[int] = []

    def set_marks(self, marks: list[int]):
        self.marks = list(marks)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.marks or self.maximum() <= 0:
            return
        painter = QPainter(self)
        painter.setPen(QPen(QColor("#ffb547"), 2))
        width = max(1, self.width() - 12)
        for mark in self.marks:
            x = 6 + int(width * mark / self.maximum())
            painter.drawLine(x, 3, x, self.height() - 3)


class MiniCutWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 780)
        self.setAcceptDrops(True)

        self.model = ProjectModel()
        self.registry = ToolRegistry(self)
        self.planner = AgentPlanner(self.registry)
        self.undo_stack: list[list[CutPoint]] = []
        self.pending_plan: dict | None = None
        self.pending_load: tuple[Path, dict | None, Path | None] | None = None
        self.analyze_worker: AnalyzeWorker | None = None
        self.export_worker: ExportWorker | None = None
        self.agent_worker: AgentWorker | None = None

        self.bridge_queue: "queue.Queue[BridgeCall]" = queue.Queue()
        self.bridge_state: dict = self.model.state()
        self.bridge = LocalBridge(
            self.bridge_queue,
            state_provider=lambda: dict(self.bridge_state),
            manifest_provider=self.registry.manifest,
        )

        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)

        self._build_ui()
        self._connect_player()

        try:
            self.bridge.start()
            self.bridge_label.setText("Bridge aktif · 127.0.0.1:8765")
        except OSError as exc:
            self.bridge_label.setText("Bridge gagal: " + str(exc))

        self.bridge_timer = QTimer(self)
        self.bridge_timer.timeout.connect(self._drain_bridge)
        self.bridge_timer.start(120)
        self._refresh()

    # ---------- UI ----------
    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)

        toolbar = QHBoxLayout()
        self.open_video_btn = QPushButton("Buka Video")
        self.open_project_btn = QPushButton("Buka Proyek")
        self.save_btn = QPushButton("Simpan Proyek")
        self.export_btn = QPushButton("Ekspor Semua Part")
        for b in (self.open_video_btn, self.open_project_btn, self.save_btn, self.export_btn):
            toolbar.addWidget(b)
        toolbar.addStretch(1)
        outer.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)

        preview = QWidget()
        pv = QVBoxLayout(preview)
        self.video = QVideoWidget()
        self.video.setMinimumSize(520, 300)
        self.player.setVideoOutput(self.video)
        pv.addWidget(self.video, 1)

        self.timeline = TimelineSlider()
        self.timeline.setRange(0, 0)
        pv.addWidget(self.timeline)

        controls = QHBoxLayout()
        self.back_btn = QPushButton("◀ 1 Frame")
        self.play_btn = QPushButton("▶ Play")
        self.forward_btn = QPushButton("1 Frame ▶")
        self.position_label = QLabel("00:00:00.000 / 00:00:00.000")
        controls.addWidget(self.back_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.forward_btn)
        controls.addWidget(self.position_label, 1)
        pv.addLayout(controls)
        splitter.addWidget(preview)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._parts_tab(), "Timeline Part")
        self.tabs.addTab(self._agent_tab(), "AI Agent")
        self.tabs.addTab(self._log_tab(), "Log")
        splitter.addWidget(self.tabs)
        splitter.setSizes([820, 460])

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status = QLabel("Buka video untuk mulai.")
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.progress)
        outer.addLayout(bottom)

        self.setCentralWidget(root)

        self.open_video_btn.clicked.connect(self._choose_video)
        self.open_project_btn.clicked.connect(self._choose_project)
        self.save_btn.clicked.connect(lambda: self.tool_save_project())
        self.export_btn.clicked.connect(lambda: self.tool_export_all())
        self.play_btn.clicked.connect(self._toggle_play)
        self.back_btn.clicked.connect(lambda: self._step_frame(-1))
        self.forward_btn.clicked.connect(lambda: self._step_frame(1))
        self.timeline.sliderMoved.connect(self.tool_seek)

    def _parts_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        self.info_label = QLabel("Belum ada video.")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.parts_table = QTableWidget(0, 4)
        self.parts_table.setHorizontalHeaderLabels(["Part", "Mulai", "Selesai", "Durasi"])
        self.parts_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.parts_table, 1)

        row1 = QHBoxLayout()
        self.add_cut_btn = QPushButton("Tambah Cut di Playhead")
        self.remove_cut_btn = QPushButton("Hapus Cut Terpilih")
        self.clear_cut_btn = QPushButton("Hapus Semua Cut")
        row1.addWidget(self.add_cut_btn)
        row1.addWidget(self.remove_cut_btn)
        row1.addWidget(self.clear_cut_btn)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.equal_parts = QSpinBox()
        self.equal_parts.setRange(2, 999)
        self.equal_parts.setValue(4)
        self.divide_equal_btn = QPushButton("Bagi Sama")
        self.interval_edit = QLineEdit("00:10:00")
        self.divide_interval_btn = QPushButton("Bagi Tiap Interval")
        row2.addWidget(QLabel("Part:"))
        row2.addWidget(self.equal_parts)
        row2.addWidget(self.divide_equal_btn)
        row2.addSpacing(12)
        row2.addWidget(QLabel("Interval:"))
        row2.addWidget(self.interval_edit)
        row2.addWidget(self.divide_interval_btn)
        layout.addLayout(row2)

        self.add_cut_btn.clicked.connect(lambda: self._manual_mutation("add_cut", {"time_ms": self.model.playhead_ms}))
        self.remove_cut_btn.clicked.connect(self._remove_selected)
        self.clear_cut_btn.clicked.connect(lambda: self._manual_mutation("clear_cuts", {}))
        self.divide_equal_btn.clicked.connect(
            lambda: self._manual_mutation("divide_equal", {"parts": self.equal_parts.value()})
        )
        self.divide_interval_btn.clicked.connect(self._divide_interval_clicked)
        return w

    def _agent_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        self.agent_state = QLabel()
        self.agent_state.setWordWrap(True)
        layout.addWidget(self.agent_state)

        form = QFormLayout()
        self.agent_mode = QComboBox()
        self.agent_mode.addItems(["Lokal · tanpa API", "OpenAI-compatible API"])
        self.endpoint_edit = QLineEdit("http://127.0.0.1:1234/v1")
        self.model_edit = QLineEdit("local-model")
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Mode", self.agent_mode)
        form.addRow("Endpoint", self.endpoint_edit)
        form.addRow("Model", self.model_edit)
        form.addRow("API key", self.api_key_edit)
        layout.addLayout(form)

        self.agent_input = QPlainTextEdit()
        self.agent_input.setPlaceholderText(
            "Contoh: bagi jadi 8 part\n"
            "tiap 10 menit\n"
            "tambah cut di 00:12:30 dan 00:25:00"
        )
        self.agent_input.setMaximumHeight(125)
        layout.addWidget(self.agent_input)

        buttons = QHBoxLayout()
        self.plan_btn = QPushButton("Buat Rencana")
        self.apply_btn = QPushButton("Terapkan")
        self.undo_btn = QPushButton("Undo Agent")
        self.apply_btn.setEnabled(False)
        buttons.addWidget(self.plan_btn)
        buttons.addWidget(self.apply_btn)
        buttons.addWidget(self.undo_btn)
        layout.addLayout(buttons)

        self.plan_preview = QPlainTextEdit()
        self.plan_preview.setReadOnly(True)
        layout.addWidget(self.plan_preview, 1)

        self.bridge_label = QLabel("Bridge belum aktif")
        layout.addWidget(self.bridge_label)

        self.agent_mode.currentIndexChanged.connect(self._agent_mode_changed)
        self.plan_btn.clicked.connect(self._make_plan)
        self.apply_btn.clicked.connect(self._apply_plan)
        self.undo_btn.clicked.connect(lambda: self.tool_undo())
        self._agent_mode_changed()
        return w

    def _log_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        return w

    def _connect_player(self):
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.errorOccurred.connect(lambda _e, text: self._log("Player: " + text))

    # ---------- loading ----------
    def _choose_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Buka video", "", "Video (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.mts);;Semua file (*)"
        )
        if path:
            self._begin_load(Path(path))

    def _choose_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Buka proyek MiniCut", "", "MiniCut JSON (*.json)")
        if not path:
            return
        try:
            data, source = load_project_file(Path(path))
            if not source:
                chosen, _ = QFileDialog.getOpenFileName(self, "Cari video sumber", "", "Video (*.*)")
                if not chosen:
                    return
                source = Path(chosen)
            self._begin_load(source, data, Path(path))
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def _begin_load(self, source: Path, project_data: dict | None = None, project_path: Path | None = None):
        ffprobe = find_tool("ffprobe")
        if not ffprobe:
            QMessageBox.critical(self, APP_TITLE, "ffprobe tidak ditemukan. Pastikan FFmpeg tersedia.")
            return
        if not source.is_file():
            QMessageBox.warning(self, APP_TITLE, "File video tidak ditemukan.")
            return
        self.pending_load = (source.resolve(), project_data, project_path)
        self.status.setText("Menganalisis video dan keyframe…")
        self.progress.setRange(0, 0)
        self.analyze_worker = AnalyzeWorker(ffprobe, source.resolve())
        self.analyze_worker.ready.connect(self._analysis_ready)
        self.analyze_worker.failed.connect(self._analysis_failed)
        self.analyze_worker.start()

    def _analysis_ready(self, metadata: dict, keyframes: list):
        assert self.pending_load is not None
        source, data, project_path = self.pending_load
        self.model.reset(source, metadata)
        self.model.keyframes = keyframes
        if data:
            cuts = []
            for item in data.get("cuts", []):
                try:
                    req = int(item.get("requested_ms", item.get("actual_ms", 0)))
                    actual = int(item.get("actual_ms", req))
                    cuts.append(CutPoint(req, actual))
                except Exception:
                    pass
            self.model.cuts = cuts
            self.model._normalize()
            self.model.project_path = project_path
            self.model.dirty = False
        self.player.setSource(QUrl.fromLocalFile(str(source)))
        self.timeline.setRange(0, max(0, self.model.duration_ms))
        self.undo_stack.clear()
        self.pending_load = None
        self.analyze_worker = None
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText(f"Siap · {source.name} · {len(keyframes)} keyframe")
        self._log(f"Video dibuka: {source}")
        self._refresh()

    def _analysis_failed(self, message: str):
        self.pending_load = None
        self.analyze_worker = None
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("Analisis gagal.")
        QMessageBox.critical(self, APP_TITLE, "Analisis video gagal:\n" + message)

    # ---------- player ----------
    def _position_changed(self, ms: int):
        self.model.playhead_ms = int(ms)
        if not self.timeline.isSliderDown():
            self.timeline.setValue(int(ms))
        self.position_label.setText(f"{clock_text(ms)} / {clock_text(self.model.duration_ms)}")
        self.bridge_state = self.model.state()

    def _duration_changed(self, ms: int):
        if self.model.source and self.model.duration_ms <= 0 and ms > 0:
            self.model.duration_ms = int(ms)
            self.timeline.setRange(0, int(ms))
            self._refresh()

    def _playback_changed(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setText("⏸ Pause" if playing else "▶ Play")

    def _toggle_play(self):
        if not self.model.source:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _step_frame(self, direction: int):
        fps = self.model.fps if self.model.fps > 0 else 25.0
        step = max(1, round(1000 / fps))
        self.tool_seek(self.model.playhead_ms + direction * step)

    # ---------- manual operations ----------
    def _snapshot(self) -> list[CutPoint]:
        return [CutPoint(c.requested_ms, c.actual_ms) for c in self.model.cuts]

    def _manual_mutation(self, tool: str, args: dict):
        try:
            before = self._snapshot()
            self.registry.execute(tool, args)
            self.undo_stack.append(before)
            self._refresh()
        except Exception as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))

    def _remove_selected(self):
        row = self.parts_table.currentRow()
        if row < 0:
            return
        # Selecting Part-N removes the cut before the next part when possible.
        cut_index = min(row, len(self.model.cuts) - 1)
        if cut_index >= 0:
            self._manual_mutation("remove_cut", {"index": cut_index})

    def _divide_interval_clicked(self):
        try:
            interval = parse_time_ms(self.interval_edit.text())
            self._manual_mutation("divide_interval", {"interval_ms": interval})
        except Exception as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))

    # ---------- agent ----------
    def _agent_mode_changed(self):
        enabled = self.agent_mode.currentIndex() == 1
        for widget in (self.endpoint_edit, self.model_edit, self.api_key_edit):
            widget.setEnabled(enabled)

    def _make_plan(self):
        text = self.agent_input.toPlainText().strip()
        if not text:
            return
        self.pending_plan = None
        self.apply_btn.setEnabled(False)
        if self.agent_mode.currentIndex() == 0:
            try:
                self._set_plan(self.planner.local_plan(text))
            except Exception as exc:
                QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self.plan_btn.setEnabled(False)
        self.plan_preview.setPlainText("Menghubungi model…")
        self.agent_worker = AgentWorker(
            self.planner, self.endpoint_edit.text(), self.model_edit.text(),
            self.api_key_edit.text(), text, self.model.state()
        )
        self.agent_worker.ready.connect(self._remote_plan_ready)
        self.agent_worker.failed.connect(self._remote_plan_failed)
        self.agent_worker.start()

    def _remote_plan_ready(self, plan: dict):
        self.plan_btn.setEnabled(True)
        self.agent_worker = None
        self._set_plan(plan)

    def _remote_plan_failed(self, message: str):
        self.plan_btn.setEnabled(True)
        self.agent_worker = None
        self.plan_preview.setPlainText("Rencana gagal: " + message)

    def _set_plan(self, plan: dict):
        self.pending_plan = self.planner.validate(plan)
        self.plan_preview.setPlainText(json.dumps(self.pending_plan, ensure_ascii=False, indent=2))
        self.apply_btn.setEnabled(True)

    def _apply_plan(self):
        if not self.pending_plan:
            return
        before = self._snapshot()
        try:
            results = self.planner.apply(self.pending_plan)
            if any(step["tool"] in MUTATING_TOOLS for step in self.pending_plan["steps"]):
                self.undo_stack.append(before)
            self.plan_preview.setPlainText(json.dumps(
                {"plan": self.pending_plan, "results": results}, ensure_ascii=False, indent=2
            ))
            self._log("Agent menerapkan: " + self.pending_plan.get("summary", ""))
            self.pending_plan = None
            self.apply_btn.setEnabled(False)
            self._refresh()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, "Agent berhenti karena error:\n" + str(exc))

    # ---------- tool API ----------
    def tool_get_state(self):
        return {"ok": True, "state": self.model.state()}

    def tool_seek(self, time_ms):
        ms = self.model.clamp(parse_time_ms(time_ms))
        self.player.setPosition(ms)
        self.model.playhead_ms = ms
        return {"ok": True, "playhead_ms": ms}

    def tool_play(self):
        self.player.play()
        return {"ok": True}

    def tool_pause(self):
        self.player.pause()
        return {"ok": True}

    def tool_add_cut(self, time_ms):
        cut = self.model.add_cut(parse_time_ms(time_ms))
        self._refresh()
        return {"ok": True, "cut": asdict(cut), "parts": len(self.model.cuts) + 1}

    def tool_remove_cut(self, index):
        cut = self.model.remove_cut(int(index))
        self._refresh()
        return {"ok": True, "removed": asdict(cut), "parts": len(self.model.cuts) + 1}

    def tool_clear_cuts(self):
        self.model.clear_cuts()
        self._refresh()
        return {"ok": True, "parts": 1}

    def tool_divide_equal(self, parts):
        self.model.divide_equal(int(parts))
        self._refresh()
        return {"ok": True, "parts": len(self.model.cuts) + 1}

    def tool_divide_interval(self, interval_ms):
        self.model.divide_interval(parse_time_ms(interval_ms) if isinstance(interval_ms, str) else int(interval_ms))
        self._refresh()
        return {"ok": True, "parts": len(self.model.cuts) + 1}

    def tool_save_project(self):
        if not self.model.source:
            raise ValueError("Belum ada proyek.")
        path = self.model.project_path
        if not path:
            suggested = self.model.source.with_suffix(".minicut.json")
            selected, _ = QFileDialog.getSaveFileName(self, "Simpan proyek", str(suggested), "MiniCut JSON (*.json)")
            if not selected:
                return {"ok": False, "cancelled": True}
            path = Path(selected)
        self.model.save(path)
        self.status.setText("Proyek tersimpan · " + str(path))
        self._refresh()
        return {"ok": True, "path": str(path)}

    def tool_export_all(self):
        if not self.model.source:
            raise ValueError("Belum ada video.")
        if self.export_worker and self.export_worker.isRunning():
            raise RuntimeError("Ekspor sedang berjalan.")
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg tidak ditemukan. Pastikan FFmpeg tersedia.")
        parent = QFileDialog.getExistingDirectory(self, "Pilih folder hasil ekspor")
        if not parent:
            return {"ok": False, "cancelled": True}
        out_dir = Path(parent) / (self.model.source.stem + "_Parts")
        self.export_worker = ExportWorker(
            ffmpeg, self.model.source, out_dir, self.model.source.stem,
            [c.actual_ms for c in self.model.cuts], self.model.duration_ms
        )
        self.export_worker.progress_changed.connect(self._export_progress)
        self.export_worker.log_line.connect(self._log)
        self.export_worker.done.connect(self._export_done)
        self.export_worker.failed.connect(self._export_failed)
        self.export_worker.cancelled.connect(self._export_cancelled)
        self.progress.setValue(0)
        self.status.setText("Mengekspor part…")
        self.export_worker.start()
        return {"ok": True, "started": True, "output_dir": str(out_dir)}

    def tool_undo(self):
        if not self.undo_stack:
            return {"ok": False, "error": "Belum ada perubahan yang bisa di-undo."}
        self.model.cuts = self.undo_stack.pop()
        self.model.dirty = True
        self._refresh()
        return {"ok": True, "parts": len(self.model.cuts) + 1}

    # ---------- export ----------
    def _export_progress(self, pct: int, text: str):
        self.progress.setValue(pct)
        self.status.setText(f"Ekspor {pct}% · {text}")

    def _export_done(self, out_dir: str, count: int, size: int, elapsed: float):
        self.progress.setValue(100)
        self.status.setText(f"Selesai · {count} part · {elapsed:.1f}s")
        self._log(f"Ekspor selesai: {count} file, {size / 1024 / 1024:.1f} MiB → {out_dir}")
        self.export_worker = None
        QMessageBox.information(self, APP_TITLE, f"Ekspor selesai.\n{count} part\n{out_dir}")

    def _export_failed(self, message: str):
        self.status.setText("Ekspor gagal.")
        self.export_worker = None
        QMessageBox.critical(self, APP_TITLE, "Ekspor gagal:\n" + message)

    def _export_cancelled(self):
        self.status.setText("Ekspor dibatalkan.")
        self.export_worker = None

    # ---------- bridge ----------
    def _drain_bridge(self):
        self.bridge_state = self.model.state()
        for _ in range(20):
            try:
                call = self.bridge_queue.get_nowait()
            except queue.Empty:
                break
            try:
                before = self._snapshot()
                result = self.registry.execute(call.tool, call.args)
                if call.tool in MUTATING_TOOLS:
                    self.undo_stack.append(before)
                call.result.update(result)
                call.result.setdefault("ok", True)
                self._refresh()
            except Exception as exc:
                call.result.update({"ok": False, "error": str(exc)})
            finally:
                call.event.set()

    # ---------- refresh/log ----------
    def _refresh(self):
        state = self.model.state()
        self.bridge_state = state
        self.timeline.set_marks([c.actual_ms for c in self.model.cuts])
        self.position_label.setText(
            f"{clock_text(self.model.playhead_ms)} / {clock_text(self.model.duration_ms)}"
        )
        if not self.model.source:
            self.info_label.setText("Belum ada video.")
            self.agent_state.setText("Belum ada timeline untuk dikontrol agent.")
            self.parts_table.setRowCount(0)
            return
        self.info_label.setText(
            f"{self.model.source.name} · {clock_text(self.model.duration_ms)} · "
            f"{self.model.fps:.3f} fps · {len(self.model.cuts) + 1} part"
        )
        self.agent_state.setText(
            f"{self.model.source.name} · {len(self.model.cuts) + 1} part · "
            f"playhead {clock_text(self.model.playhead_ms)} · "
            f"keyframe {'siap' if self.model.keyframes else 'belum'}"
        )
        ranges = self.model.part_ranges()
        self.parts_table.setRowCount(len(ranges))
        for row, (start, end) in enumerate(ranges):
            values = [f"Part-{row + 1:02d}", clock_text(start), clock_text(end), clock_text(end - start)]
            for col, value in enumerate(values):
                self.parts_table.setItem(row, col, QTableWidgetItem(value))

    def _log(self, text: str):
        self.log.appendPlainText(text)

    # ---------- drag/drop/close ----------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = Path(urls[0].toLocalFile())
        if path.suffix.lower() == ".json":
            try:
                data, source = load_project_file(path)
                if source:
                    self._begin_load(source, data, path)
            except Exception as exc:
                QMessageBox.warning(self, APP_TITLE, str(exc))
        elif path.suffix.lower() in SUPPORTED_VIDEO:
            self._begin_load(path)

    def closeEvent(self, event):
        if self.export_worker and self.export_worker.isRunning():
            answer = QMessageBox.question(self, APP_TITLE, "Ekspor masih berjalan. Tetap keluar?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.export_worker.cancel()
        if self.model.dirty:
            answer = QMessageBox.question(self, APP_TITLE, "Perubahan cut belum disimpan. Tetap keluar?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.bridge.stop()
        self.player.stop()
        event.accept()
