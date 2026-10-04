"""Shared recovery status and recovery picker for Live2D and Spine editors."""
from __future__ import annotations

import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import uuid

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, PushButton

from app.core.editor_recovery import RecoveryStore, capture_recovery
from app.core.settings_manager import SettingsManager
from app.gui.editor_dialogs import ThemedEditorDialog
from app.gui.live2d_appearance import ElidedAppearanceLabel
from app.i18n import tr
from app.paths import RUNTIME_DIR


def text(key, default, **values):
    return tr("editor.recovery." + key, default=default, **values)


class _Signals(QObject):
    done = Signal(object, str)


class _Job(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.done.emit(self.operation(), "")
        except Exception as exc:
            self.signals.done.emit(None, str(exc))


class EditorRecoveryController(QObject):
    def __init__(self, page, kind, settings=None):
        super().__init__(page)
        self.page, self.kind = page, kind
        self.settings = settings or SettingsManager()
        self.store = RecoveryStore(Path(self.settings.get("runtime.dir", str(RUNTIME_DIR))) / "recovery")
        self.identity = uuid.uuid4().hex
        self.session = None
        self.saved_path = ""
        self.recovered = False
        self.origin = ""
        self.last_time = ""
        self.error = ""
        self._job = None
        self._token = None
        self._last_token = None
        self._last_attempt = 0.
        self._closed = False
        self._discard = None
        self._restore_directory = None
        self._restoring = None
        self._restored_identity = None
        self.bar = QWidget(page)
        self.bar.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        row = QHBoxLayout(self.bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.label = ElidedAppearanceLabel(self.bar)
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.button = PushButton(self.bar)
        self.button.clicked.connect(self.choose_restore)
        row.addWidget(self.label, 1)
        row.addWidget(self.button)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        page.sourceOpened.connect(self.source_opened)
        page.sourceFailed.connect(self.source_failed)
        self.refresh()

    def _busy(self):
        if self.kind == "spine":
            return self.page.loading
        return self.page._projects_busy() or bool(self.page._preview_workers)

    def _signature(self):
        if not self.session:
            return None
        token = self.session._signature()
        if self.kind == "live2d":
            projects = self._project_overrides()
            token = (token, json.dumps(projects, sort_keys=True, ensure_ascii=False),
                     tuple(sorted(self.session._parameter_preview.items())))
        return token

    def _project_overrides(self):
        if self.kind != "live2d":
            return {}
        projects = {}
        for key, panel in (("psd", self.page.psd_panel), ("mods", self.page.mod_panel)):
            project = panel.current_project
            if project:
                data = copy.deepcopy(project.data)
                if key == "psd":
                    data["ui_state"] = panel._current_ui_state()
                projects[key] = data
        return projects

    def dirty(self):
        session = self.page.session
        return bool(session and (session.dirty or (self.kind == "live2d" and
                    (self.page.psd_panel._project_dirty or self.page.mod_panel._dirty))))

    def source_opened(self, source):
        if self._discard is not None and self.session is not None:
            self.store.dismiss(self.identity)
            if self._restored_identity:
                self.store.dismiss(self._restored_identity)
        self._restored_identity = None
        self.session = self.page.session
        self.identity = uuid.uuid4().hex
        self._last_token = None
        self._last_attempt = time.monotonic()
        self.last_time = self.error = self.saved_path = ""
        self.origin = str(source)
        self.recovered = False
        self._discard = None
        if self._restoring:
            self._restored_identity = self._restoring["id"]
            self.origin = self._restoring["source"]
            self.recovered = True
            # A restored copy still requires an explicit user save.
            if self.kind == "live2d":
                self.session._saved_signature = "recovered-unsaved"
            else:
                self.session._saved = b"recovered-unsaved"
            self._restoring = None
        self.refresh()
        self.page._update_actions()

    def source_failed(self, *_args):
        if self._restoring:
            self._restoring = None
        self.refresh()

    def note_saved(self, model_path):
        self.saved_path = str(model_path)
        self.recovered = False
        self.store.dismiss(self.identity)
        if self._restored_identity:
            self.store.dismiss(self._restored_identity)
            self._restored_identity = None
        self.identity = uuid.uuid4().hex
        self.last_time = ""
        self._last_token = None
        self._discard = None
        self.refresh()

    def note_discard(self):
        self._discard = self._signature()

    def refresh(self):
        if self._closed:
            return
        self.button.setText(text("open", "恢复工程…"))
        self.button.setToolTip(text("hint", "恢复自动备份的副本，不覆盖原工程；恢复后请另存工程。"))
        self.button.setEnabled(self._job is None and not self._busy())
        if self.page.session is None:
            count = len({r["id"] for r in self.store.records(self.kind)})
            message = text("available", "可恢复工程：{count}", count=count) if count else text("empty", "尚未打开工程")
        else:
            source = self.saved_path or self.origin or str(getattr(self.page.session, "source_path", ""))
            state = text("dirty", "未保存修改") if self.dirty() else text("saved", "已保存状态")
            if self.recovered:
                state = text("restored", "恢复副本 · 请另存工程")
            if self._job and self._restoring:
                backup = text("restoring", "正在恢复工程…")
            elif self._job:
                backup = text("writing", "正在准备恢复点…")
            elif self.error:
                backup = text("failed", "恢复点失败（悬停查看）")
            elif not self.settings.get("editor.recovery.enabled", True):
                backup = text("disabled", "自动恢复已关闭")
            elif self.last_time:
                backup = text("latest", "恢复点 {time}", time=self.last_time)
            else:
                backup = text("waiting", "等待下一恢复点")
            message = f"{Path(source).name} · {state} · {backup}"
        self.label.setText(message)
        self.label.setToolTip(message + ("\n" + (self.saved_path or self.origin) if self.origin else "") +
                              ("\n" + self.error if self.error else ""))

    def tick(self, force=False):
        if self._closed:
            return
        self.settings.settings = self.settings.load_settings()
        self.refresh()
        if self._job or self._busy() or not self.page.session or not self.dirty():
            return
        if not self.settings.get("editor.recovery.enabled", True):
            return
        interval = max(15, min(600, int(self.settings.get("editor.recovery.interval", 60))))
        if not force and time.monotonic() - self._last_attempt < interval:
            return
        token = self._signature()
        if token == self._last_token and not force:
            return
        self._last_attempt = time.monotonic()
        try:
            capture = capture_recovery(self.page.session, self.kind, project_overrides=self._project_overrides())
            capture.source = self.origin or capture.source
        except Exception as exc:
            self.error = str(exc)
            self.refresh()
            return
        identity = self.identity
        self._token = (identity, token)
        self._job = _Job(lambda: self.store.prepare(identity, capture))
        self._job.signals.done.connect(self._checkpoint_done)
        QThreadPool.globalInstance().start(self._job)
        self.refresh()

    def _checkpoint_done(self, record, error):
        identity, token = self._token
        self._job = None
        if self._closed:
            return
        if self.identity != identity:
            self.refresh()
            return
        if error:
            self.error = error
        elif token == self._signature() and self.dirty():
            try:
                self.store.publish(record, self.settings.get("editor.recovery.keep", 3))
                self._last_token = token
                self.last_time = datetime.fromisoformat(record["created"]).astimezone().strftime("%H:%M:%S")
                self.error = ""
            except Exception as exc:
                self.error = str(exc)
        self.refresh()

    def choose_restore(self):
        records = self.store.records(self.kind)
        dialog = ThemedEditorDialog(self.page)
        dialog.setWindowTitle(text("open", "恢复工程…"))
        hint = CaptionLabel(text("hint", "恢复自动备份的副本，不覆盖原工程；恢复后请另存工程。"), dialog)
        hint.setWordWrap(True)
        dialog.viewLayout.addWidget(hint)
        choices = ComboBox(dialog)
        for record in records:
            stamp = datetime.fromisoformat(record["created"]).astimezone().strftime("%m-%d %H:%M:%S")
            choices.addItem(f"{stamp} · {Path(record['source']).name}")
        dialog.viewLayout.addWidget(choices)
        source_label = CaptionLabel(dialog)
        source_label.setWordWrap(True)
        choices.currentIndexChanged.connect(
            lambda index: source_label.setText(records[index]["source"] if 0 <= index < len(records) else ""))
        source_label.setText(records[0]["source"] if records else "")
        dialog.viewLayout.addWidget(source_label)
        dialog.yesButton.setEnabled(bool(records))
        if not records:
            hint.setText(text("none", "暂无可用恢复点。编辑后会按设置的间隔自动保存。"))
        try:
            if dialog.exec() and records:
                self.restore(records[choices.currentIndex()])
        finally:
            dialog.deleteLater()

    def restore(self, record):
        if self._job or self._busy():
            return False
        self._restoring = record
        temporary = tempfile.TemporaryDirectory(prefix="lpk-recovery-open-")
        self._job = _Job(lambda: (self.store.restore(record, Path(temporary.name) / "model"), temporary))
        self._job.signals.done.connect(self._restore_done)
        QThreadPool.globalInstance().start(self._job)
        self.refresh()
        return True

    def _restore_done(self, result, error):
        self._job = None
        if error or self._closed:
            self.error = error
            self._restoring = None
            if result:
                result[1].cleanup()
            self.refresh()
            return
        path, temporary = result
        # Spine opens asynchronously: keep the materialized source alive.
        previous = self._restore_directory
        self._restore_directory = temporary
        if not self.page.open_source(str(path)):
            self._restoring = None
            temporary.cleanup()
            self._restore_directory = previous
        elif previous:
            previous.cleanup()
        self.refresh()

    def shutdown(self):
        if self._discard is not None and self._discard == self._signature():
            self.store.dismiss(self.identity)
            if self._restored_identity:
                self.store.dismiss(self._restored_identity)
        self._closed = True
        self.timer.stop()
        if self._restore_directory:
            self._restore_directory.cleanup()
