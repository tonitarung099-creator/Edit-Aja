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
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget
)

from . import APP_TITLE
from .agent import AgentPlanner, MUTATING_TOOLS, ToolRegistry
from .bridge import BridgeCall, LocalBridge
from .core import (
    CutPoint, ProjectModel, SUPPORTED_VIDEO, clock_text, find_tool,
    load_project_file, parse_time_ms
)
from .gemini import DEFAULT_MODEL
from .gemini_keys import GeminiKeyStore, MAX_GEMINI_KEYS
from .workers import AgentWorker, AnalyzeWorker, ExportWorker, FilmCutWorker, GeminiTestWorker


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
        self.gemini_test_worker: GeminiTestWorker | None = None
        self.film_cut_worker: FilmCutWorker | None = None
        self.film_cut_results: list[dict] = []
        self.srt_path: Path | None = None
        self.gemini_keys = GeminiKeyStore()
        self._gemini_test_key_id: str | None = None
        self._film_active_key_id: str | None = None
        self._film_active_model: str = DEFAULT_MODEL
        self._film_usage_seen_requests = 0
        self._film_usage_seen_prompt_tokens = 0

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
        self.tabs.addTab(self._film_cut_tab(), "AI Film Cut")
        self.tabs.addTab(self._gemini_keys_tab(), "Gemini API")
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


    def _film_cut_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        intro = QLabel(
            "Mode hemat API: MiniCut mencari kandidat lokal dari visual + audio + SRT. "
            "Gemini hanya menerima frame kecil di sekitar kandidat, bukan film penuh."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        self.gemini_key_combo = QComboBox()
        self.gemini_manage_btn = QPushButton("Kelola API")
        key_layout.addWidget(self.gemini_key_combo, 1)
        key_layout.addWidget(self.gemini_manage_btn)
        self.gemini_model_combo = QComboBox()
        self.gemini_model_combo.addItems([
            DEFAULT_MODEL,
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash-lite",
        ])
        form.addRow("API aktif", key_row)
        form.addRow("Model", self.gemini_model_combo)

        srt_row = QWidget()
        srt_layout = QHBoxLayout(srt_row)
        srt_layout.setContentsMargins(0, 0, 0, 0)
        self.srt_edit = QLineEdit()
        self.srt_edit.setReadOnly(True)
        self.srt_edit.setPlaceholderText("Belum ada SRT")
        self.srt_btn = QPushButton("Pilih SRT")
        srt_layout.addWidget(self.srt_edit, 1)
        srt_layout.addWidget(self.srt_btn)
        form.addRow("Subtitle", srt_row)

        self.film_interval = QSpinBox()
        self.film_interval.setRange(5, 60)
        self.film_interval.setValue(15)
        self.film_interval.setSuffix(" menit")
        self.film_window = QSpinBox()
        self.film_window.setRange(1, 5)
        self.film_window.setValue(2)
        self.film_window.setSuffix(" menit")
        self.film_cache = QCheckBox("Simpan hasil parsial agar bisa dilanjutkan")
        self.film_cache.setChecked(True)
        form.addRow("Target part", self.film_interval)
        form.addRow("Cari sekitar target", self.film_window)
        form.addRow("Resume", self.film_cache)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.gemini_test_btn = QPushButton("Tes API")
        self.film_analyze_btn = QPushButton("Analisis Film")
        self.film_cancel_btn = QPushButton("Batalkan")
        self.film_apply_btn = QPushButton("Terapkan Semua Cut")
        self.film_cancel_btn.setEnabled(False)
        self.film_apply_btn.setEnabled(False)
        actions.addWidget(self.gemini_test_btn)
        actions.addWidget(self.film_analyze_btn)
        actions.addWidget(self.film_cancel_btn)
        actions.addWidget(self.film_apply_btn)
        layout.addLayout(actions)

        self.film_status_label = QLabel("Siap. Buka video, pilih SRT, lalu isi API Gemini.")
        self.film_status_label.setWordWrap(True)
        self.film_usage_label = QLabel("Pemakaian sesi: 0 request · 0 token")
        layout.addWidget(self.film_status_label)
        layout.addWidget(self.film_usage_label)

        self.film_table = QTableWidget(0, 5)
        self.film_table.setHorizontalHeaderLabels(
            ["Target", "Cut terpilih", "Confidence", "Status", "Alasan"]
        )
        self.film_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.film_table, 1)

        self.srt_btn.clicked.connect(self._choose_srt)
        self.gemini_manage_btn.clicked.connect(self._open_gemini_manager)
        self.gemini_key_combo.currentIndexChanged.connect(self._film_key_changed)
        self.gemini_model_combo.currentIndexChanged.connect(self._refresh_gemini_key_views)
        self.gemini_test_btn.clicked.connect(self._test_gemini)
        self.film_analyze_btn.clicked.connect(self._start_film_cut)
        self.film_cancel_btn.clicked.connect(self._cancel_film_cut)
        self.film_apply_btn.clicked.connect(self._apply_film_cut)
        self._refresh_gemini_key_views()
        return w


    def _gemini_keys_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        intro = QLabel(
            "Simpan hingga 100 Gemini API key. Key disimpan terenkripsi dengan Windows DPAPI. "
            "Persentase RPM/TPM/RPD adalah pemakaian yang dicatat MiniCut, bukan dashboard Google penuh."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.gemini_key_count_label = QLabel()
        layout.addWidget(self.gemini_key_count_label)

        self.gemini_keys_table = QTableWidget(0, 8)
        self.gemini_keys_table.setHorizontalHeaderLabels([
            "Aktif", "Nama", "Project", "API key", "Status", "RPM", "TPM", "RPD"
        ])
        self.gemini_keys_table.horizontalHeader().setStretchLastSection(True)
        self.gemini_keys_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.gemini_keys_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.gemini_keys_table, 1)

        actions = QHBoxLayout()
        self.gemini_add_key_btn = QPushButton("+ Tambah API")
        self.gemini_edit_key_btn = QPushButton("Edit")
        self.gemini_remove_key_btn = QPushButton("Hapus")
        self.gemini_activate_key_btn = QPushButton("Jadikan Aktif")
        self.gemini_test_selected_btn = QPushButton("Tes Terpilih")
        actions.addWidget(self.gemini_add_key_btn)
        actions.addWidget(self.gemini_edit_key_btn)
        actions.addWidget(self.gemini_remove_key_btn)
        actions.addWidget(self.gemini_activate_key_btn)
        actions.addWidget(self.gemini_test_selected_btn)
        layout.addLayout(actions)

        self.gemini_manager_note = QLabel(
            "Status hijau = request tes terakhir berhasil. Merah LIMIT = Google mengembalikan limit/quota. "
            "MiniCut tidak memindahkan key secara otomatis saat kuota habis."
        )
        self.gemini_manager_note.setWordWrap(True)
        layout.addWidget(self.gemini_manager_note)

        self.gemini_add_key_btn.clicked.connect(self._add_gemini_key)
        self.gemini_edit_key_btn.clicked.connect(self._edit_gemini_key)
        self.gemini_remove_key_btn.clicked.connect(self._remove_gemini_key)
        self.gemini_activate_key_btn.clicked.connect(self._activate_selected_gemini_key)
        self.gemini_test_selected_btn.clicked.connect(self._test_selected_gemini_key)
        self._refresh_gemini_key_views()
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
        self.film_cut_results = []
        if hasattr(self, 'film_table'):
            self.film_table.setRowCount(0)
            self.film_apply_btn.setEnabled(False)
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



    # ---------- Gemini API manager ----------
    def _current_gemini_model(self) -> str:
        if hasattr(self, "gemini_model_combo"):
            return self.gemini_model_combo.currentText().strip() or DEFAULT_MODEL
        return DEFAULT_MODEL

    def _refresh_gemini_key_views(self):
        summaries = self.gemini_keys.summaries()
        active_id = self.gemini_keys.active_id()
        model = self._current_gemini_model()

        if hasattr(self, "gemini_key_combo"):
            self.gemini_key_combo.blockSignals(True)
            self.gemini_key_combo.clear()
            if summaries:
                active_index = 0
                for i, item in enumerate(summaries):
                    label = item.name
                    if item.project:
                        label += f" · {item.project}"
                    self.gemini_key_combo.addItem(label, item.id)
                    if item.id == active_id:
                        active_index = i
                self.gemini_key_combo.setCurrentIndex(active_index)
            else:
                self.gemini_key_combo.addItem("Belum ada API key", None)
            self.gemini_key_combo.blockSignals(False)

        if hasattr(self, "gemini_key_count_label"):
            self.gemini_key_count_label.setText(
                f"Tersimpan: {len(summaries)} / {MAX_GEMINI_KEYS} API key · model status: {model}"
            )

        if hasattr(self, "gemini_keys_table"):
            self.gemini_keys_table.setRowCount(len(summaries))
            for row, item in enumerate(summaries):
                snap = self.gemini_keys.snapshot(item.id, model)
                status = snap.get("status", "unknown")
                status_text = {
                    "ready": "🟢 SIAP",
                    "limited": "🔴 LIMIT",
                    "error": "🟠 ERROR",
                    "unknown": "⚪ BELUM DICEK",
                }.get(status, status.upper())
                values = [
                    "●" if item.id == active_id else "",
                    item.name,
                    item.project or "-",
                    item.masked_key,
                    status_text,
                    f"{snap['rpm_used']}/{snap['rpm_limit']} ({snap['rpm_pct']:.1f}%)",
                    f"{snap['tpm_used']:,}/{snap['tpm_limit']:,} ({snap['tpm_pct']:.1f}%)",
                    f"{snap['rpd_used']}/{snap['rpd_limit']} ({snap['rpd_pct']:.1f}%)",
                ]
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    if col == 0:
                        cell.setData(Qt.ItemDataRole.UserRole, item.id)
                    self.gemini_keys_table.setItem(row, col, cell)

    def _selected_gemini_key_id(self) -> str | None:
        if not hasattr(self, "gemini_keys_table"):
            return None
        row = self.gemini_keys_table.currentRow()
        if row < 0:
            return None
        item = self.gemini_keys_table.item(row, 0)
        if not item:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _film_key_changed(self, index: int):
        if not hasattr(self, "gemini_key_combo"):
            return
        key_id = self.gemini_key_combo.itemData(index)
        if key_id:
            try:
                self.gemini_keys.set_active(str(key_id))
            except Exception as exc:
                QMessageBox.warning(self, APP_TITLE, str(exc))
        self._refresh_gemini_key_views()

    def _open_gemini_manager(self):
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == "Gemini API":
                self.tabs.setCurrentIndex(i)
                break

    def _gemini_key_dialog(self, key_id: str | None = None):
        existing = None
        if key_id:
            existing = next((x for x in self.gemini_keys.summaries() if x.id == key_id), None)
            if not existing:
                QMessageBox.warning(self, APP_TITLE, "API key tidak ditemukan.")
                return

        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Gemini API" if existing else "Tambah Gemini API")
        dialog.resize(520, 190)
        form = QFormLayout(dialog)

        name_edit = QLineEdit(existing.name if existing else "")
        project_edit = QLineEdit(existing.project if existing else "")
        key_edit = QLineEdit()
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText(
            "Kosongkan jika tidak ingin mengganti key" if existing else "Tempel Gemini API key"
        )

        form.addRow("Nama", name_edit)
        form.addRow("Project", project_edit)
        form.addRow("API key", key_edit)

        note = QLabel(
            "API key disimpan terenkripsi untuk user Windows ini dan tidak dimasukkan ke proyek GitHub."
        )
        note.setWordWrap(True)
        form.addRow(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            if existing:
                self.gemini_keys.update(
                    existing.id,
                    name_edit.text(),
                    project_edit.text(),
                    key_edit.text() or None,
                )
            else:
                self.gemini_keys.add(
                    name_edit.text(),
                    project_edit.text(),
                    key_edit.text(),
                )
            self._refresh_gemini_key_views()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def _add_gemini_key(self):
        if self.gemini_keys.count() >= MAX_GEMINI_KEYS:
            QMessageBox.information(
                self, APP_TITLE, f"Batas {MAX_GEMINI_KEYS} API key sudah tercapai."
            )
            return
        self._gemini_key_dialog()

    def _edit_gemini_key(self):
        key_id = self._selected_gemini_key_id()
        if not key_id:
            QMessageBox.information(self, APP_TITLE, "Pilih satu API key di tabel.")
            return
        self._gemini_key_dialog(key_id)

    def _remove_gemini_key(self):
        key_id = self._selected_gemini_key_id()
        if not key_id:
            QMessageBox.information(self, APP_TITLE, "Pilih satu API key di tabel.")
            return
        summary = next((x for x in self.gemini_keys.summaries() if x.id == key_id), None)
        name = summary.name if summary else "API ini"
        answer = QMessageBox.question(
            self, APP_TITLE, f"Hapus {name} dari penyimpanan MiniCut?"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.gemini_keys.remove(key_id)
            self._refresh_gemini_key_views()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def _activate_selected_gemini_key(self):
        key_id = self._selected_gemini_key_id()
        if not key_id:
            QMessageBox.information(self, APP_TITLE, "Pilih satu API key di tabel.")
            return
        try:
            self.gemini_keys.set_active(key_id)
            self._refresh_gemini_key_views()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))

    def _begin_gemini_test(self, key_id: str):
        if self.gemini_test_worker and self.gemini_test_worker.isRunning():
            QMessageBox.information(self, APP_TITLE, "Tes API sedang berjalan.")
            return
        try:
            key = self.gemini_keys.get_secret(key_id)
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return

        model = self._current_gemini_model()
        self._gemini_test_key_id = key_id
        self.gemini_test_btn.setEnabled(False)
        if hasattr(self, "gemini_test_selected_btn"):
            self.gemini_test_selected_btn.setEnabled(False)
        self.film_status_label.setText("Menguji Gemini API…")
        self.gemini_test_worker = GeminiTestWorker(key, model)
        self.gemini_test_worker.ready.connect(self._gemini_test_ready)
        self.gemini_test_worker.failed.connect(self._gemini_test_failed)
        self.gemini_test_worker.start()

    def _test_selected_gemini_key(self):
        key_id = self._selected_gemini_key_id()
        if not key_id:
            QMessageBox.information(self, APP_TITLE, "Pilih satu API key di tabel.")
            return
        self._begin_gemini_test(key_id)

    # ---------- AI Film Cut / Gemini ----------
    def _choose_srt(self):
        path, _ = QFileDialog.getOpenFileName(self, "Pilih subtitle SRT", "", "Subtitle (*.srt)")
        if path:
            self.srt_path = Path(path).resolve()
            self.srt_edit.setText(str(self.srt_path))
            self.film_status_label.setText("SRT siap. MiniCut akan menggunakannya untuk verifikasi dialog.")

    def _test_gemini(self):
        key_id = self.gemini_keys.active_id()
        if not key_id:
            QMessageBox.warning(self, APP_TITLE, "Tambahkan Gemini API key terlebih dahulu.")
            self._open_gemini_manager()
            return
        self._begin_gemini_test(key_id)

    def _gemini_test_ready(self, result: dict):
        self.gemini_test_btn.setEnabled(True)
        if hasattr(self, "gemini_test_selected_btn"):
            self.gemini_test_selected_btn.setEnabled(True)
        self.gemini_test_worker = None
        usage = result.get("usage") or {}
        model = str(result.get("model") or self._current_gemini_model())
        key_id = self._gemini_test_key_id
        if key_id:
            self.gemini_keys.record_usage(
                key_id,
                model,
                requests=int(usage.get("requests") or 0),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                status="ready",
                checked=True,
            )
        self._gemini_test_key_id = None
        self.film_usage_label.setText(
            f"Tes API: {usage.get('requests', 0)} request · "
            f"{usage.get('total_tokens', 0)} token"
        )
        self.film_status_label.setText("Gemini terhubung · " + model)
        self._refresh_gemini_key_views()

    def _gemini_test_failed(self, message: str):
        self.gemini_test_btn.setEnabled(True)
        if hasattr(self, "gemini_test_selected_btn"):
            self.gemini_test_selected_btn.setEnabled(True)
        self.gemini_test_worker = None
        key_id = self._gemini_test_key_id
        if key_id:
            self.gemini_keys.mark_error(key_id, self._current_gemini_model(), message)
        self._gemini_test_key_id = None
        self.film_status_label.setText("Tes Gemini gagal.")
        self._refresh_gemini_key_views()
        QMessageBox.critical(self, APP_TITLE, "Gemini API gagal:\n" + message)

    def _start_film_cut(self):
        if not self.model.source:
            QMessageBox.warning(self, APP_TITLE, "Buka video terlebih dahulu.")
            return
        if not self.srt_path or not self.srt_path.is_file():
            QMessageBox.warning(self, APP_TITLE, "Pilih file SRT yang sesuai dengan film.")
            return
        key_id = self.gemini_keys.active_id()
        if not key_id:
            QMessageBox.warning(self, APP_TITLE, "Tambahkan dan pilih Gemini API key terlebih dahulu.")
            self._open_gemini_manager()
            return
        try:
            key = self.gemini_keys.get_secret(key_id)
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            QMessageBox.critical(self, APP_TITLE, "FFmpeg tidak ditemukan.")
            return
        if self.film_cut_worker and self.film_cut_worker.isRunning():
            return

        self.film_cut_results = []
        self._film_active_key_id = key_id
        self._film_active_model = self._current_gemini_model()
        self._film_usage_seen_requests = 0
        self._film_usage_seen_prompt_tokens = 0
        self.film_table.setRowCount(0)
        self.film_apply_btn.setEnabled(False)
        self.film_analyze_btn.setEnabled(False)
        self.film_cancel_btn.setEnabled(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self.film_cut_worker = FilmCutWorker(
            ffmpeg=ffmpeg,
            source=self.model.source,
            duration_ms=self.model.duration_ms,
            srt_path=self.srt_path,
            api_key=key,
            model=self._film_active_model,
            interval_ms=self.film_interval.value() * 60_000,
            window_ms=self.film_window.value() * 60_000,
            top_n=3,
            use_cache=self.film_cache.isChecked(),
        )
        self.film_cut_worker.progress_changed.connect(self._film_cut_progress)
        self.film_cut_worker.target_result.connect(self._film_cut_target_result)
        self.film_cut_worker.usage_changed.connect(self._film_cut_usage)
        self.film_cut_worker.done.connect(self._film_cut_done)
        self.film_cut_worker.failed.connect(self._film_cut_failed)
        self.film_cut_worker.cancelled.connect(self._film_cut_cancelled)
        self.film_status_label.setText("Analisis dimulai. Film penuh tetap di komputer.")
        self._log("AI Film Cut dimulai: kandidat lokal → Gemini verifier.")
        self.film_cut_worker.start()

    def _film_cut_progress(self, index: int, total: int, stage: str):
        pct = int(((index - 1) / max(1, total)) * 100)
        self.progress.setValue(pct)
        self.status.setText(f"AI Film Cut {index}/{total} · {stage}")
        self.film_status_label.setText(f"Target {index}/{total} · {stage}")

    def _film_cut_target_result(self, result: dict):
        target_ms = int(result.get("target_ms") or 0)
        existing = next(
            (i for i, x in enumerate(self.film_cut_results)
             if int(x.get("target_ms") or 0) == target_ms),
            None,
        )
        if existing is None:
            self.film_cut_results.append(dict(result))
        else:
            self.film_cut_results[existing] = dict(result)
        self.film_cut_results.sort(key=lambda x: int(x.get("target_ms") or 0))

        row = self.film_table.rowCount()
        self.film_table.insertRow(row)
        confidence = float(result.get("confidence") or 0)
        review = bool(result.get("needs_review")) or confidence < 0.55
        values = [
            str(result.get("target") or clock_text(target_ms)),
            str(result.get("selected_time") or clock_text(int(result.get("selected_time_ms") or 0))),
            f"{confidence:.0%}",
            "REVIEW" if review else ("CACHE" if result.get("cached") else "OK"),
            str(result.get("reason") or ""),
        ]
        for col, value in enumerate(values):
            self.film_table.setItem(row, col, QTableWidgetItem(value))

        preview_marks = [int(x.get("selected_time_ms") or 0) for x in self.film_cut_results]
        self.timeline.set_marks(preview_marks)

    def _film_cut_usage(self, usage: dict):
        self.film_usage_label.setText(
            f"Pemakaian sesi: {usage.get('requests', 0)} request · "
            f"{usage.get('prompt_tokens', 0)} input token · "
            f"{usage.get('total_tokens', 0)} total token"
        )

    def _film_cut_done(self, results: list):
        self.film_cut_worker = None
        self.film_analyze_btn.setEnabled(True)
        self.film_cancel_btn.setEnabled(False)
        self.progress.setValue(100)
        self.film_cut_results = sorted(
            [dict(x) for x in results], key=lambda x: int(x.get("target_ms") or 0)
        )
        review_count = sum(
            1 for x in self.film_cut_results
            if bool(x.get("needs_review")) or float(x.get("confidence") or 0) < 0.55
        )
        self.film_apply_btn.setEnabled(bool(self.film_cut_results))
        if review_count:
            self.film_status_label.setText(
                f"Selesai · {len(results)} titik · {review_count} perlu review sebelum diterapkan."
            )
        else:
            self.film_status_label.setText(
                f"Selesai · {len(results)} titik potong siap dipreview dan diterapkan."
            )
        self.status.setText("AI Film Cut selesai · belum diterapkan ke timeline.")
        self._log(f"AI Film Cut selesai: {len(results)} titik.")

    def _film_cut_failed(self, message: str):
        self.film_cut_worker = None
        self.film_analyze_btn.setEnabled(True)
        self.film_cancel_btn.setEnabled(False)
        self.status.setText("AI Film Cut gagal.")
        self.film_status_label.setText("Analisis berhenti. Hasil yang sudah selesai disimpan di cache.")
        QMessageBox.critical(self, APP_TITLE, "AI Film Cut gagal:\n" + message)

    def _film_cut_cancelled(self):
        self.film_cut_worker = None
        self.film_analyze_btn.setEnabled(True)
        self.film_cancel_btn.setEnabled(False)
        self.status.setText("AI Film Cut dibatalkan.")
        self.film_status_label.setText("Dibatalkan. Hasil sebelumnya tetap tersimpan di cache.")

    def _cancel_film_cut(self):
        if self.film_cut_worker and self.film_cut_worker.isRunning():
            self.film_cut_worker.cancel()
            self.film_cancel_btn.setEnabled(False)
            self.film_status_label.setText("Membatalkan setelah langkah aktif selesai…")

    def _apply_film_cut(self):
        if not self.film_cut_results:
            return
        review_count = sum(
            1 for x in self.film_cut_results
            if bool(x.get("needs_review")) or float(x.get("confidence") or 0) < 0.55
        )
        if review_count:
            answer = QMessageBox.question(
                self,
                APP_TITLE,
                f"Ada {review_count} titik bertanda REVIEW. Tetap terapkan semua?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self.model.cuts:
            answer = QMessageBox.question(
                self,
                APP_TITLE,
                "Timeline sudah mempunyai cut. Ganti dengan hasil AI Film Cut?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        before = self._snapshot()
        exact = []
        for result in self.film_cut_results:
            t = int(result.get("selected_time_ms") or 0)
            if 0 < t < self.model.duration_ms:
                exact.append(CutPoint(t, t))
        exact.sort(key=lambda x: x.actual_ms)
        if not exact:
            QMessageBox.warning(self, APP_TITLE, "Tidak ada titik potong valid untuk diterapkan.")
            return
        self.undo_stack.append(before)
        self.model.cuts = exact
        self.model._normalize()
        self.model.dirty = True
        self._refresh()
        self.film_apply_btn.setEnabled(False)
        self.film_status_label.setText(
            f"{len(exact)} titik AI diterapkan ke timeline. Timestamp dipertahankan presisi."
        )
        self._log(f"AI Film Cut diterapkan: {len(exact)} cut presisi.")

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
        if self.film_cut_worker and self.film_cut_worker.isRunning():
            answer = QMessageBox.question(self, APP_TITLE, "AI Film Cut masih berjalan. Tetap keluar?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.film_cut_worker.cancel()
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
