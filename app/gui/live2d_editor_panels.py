"""Bindings that keep the mature MOD workbench inside an editor project."""
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QFileDialog

from app.core.live2dviewer_mod_project import PROJECT_FILE_NAME, create_empty_project, load_project, save_project
from app.gui.Live2DModPage import Live2DModPage, ProjectNameDialog
from app.i18n import tr


class ProjectBoundModPage(Live2DModPage):
    """Keep original model/skin tools while owning only workspace copies."""

    projectChanged = Signal(object)
    taskStateChanged = Signal(object)

    def __init__(self, session_provider, parent=None):
        self._session_provider = session_provider
        self._binding = False
        self._task_details = []
        self._task_failed = False
        self._feedback_task = "viewer"
        super().__init__(parent, compact=True)
        self.workflow_status.hide()

    def _emit_task_state(self, *, busy=None):
        busy = bool(self.worker and self.worker.isRunning()) if busy is None else bool(busy)
        self.taskStateChanged.emit({"task": self._feedback_task,
                                    "state": "running" if busy else "failed" if self._task_failed else "succeeded",
                                    "busy": busy, "progress": 0 if busy else 100,
                                    "phase": "ViewerEX", "message": getattr(self, "_last_log_message", ""),
                                    "details": "\n".join(self._task_details)})

    def start_worker(self, action, **kwargs):
        self._feedback_task = "viewer:" + str(action)
        self._task_details = []
        self._task_failed = False
        super().start_worker(action, **kwargs)

    def set_busy(self, busy):
        super().set_busy(busy)
        self._emit_task_state(busy=busy)

    def append_log(self, message):
        super().append_log(message)
        if message:
            self._task_details.append(str(message))
            self._task_details = self._task_details[-1000:]
            self._emit_task_state()

    def on_worker_error(self, error):
        self._task_failed = True
        super().on_worker_error(error)

    def show_error(self, message):
        self._task_failed = True
        self.append_log(message)
        super().show_error(message)

    def load_last_project(self, silent=False):
        # A standalone last-project preference must never replace this root.
        return

    def remember_project_file(self, project):
        return

    def discover_project_files(self):
        paths = [str(self.current_project.project_file)] if self.current_project else []
        session = self._session_provider()
        if session and (session.root / "mods").is_dir():
            paths.extend(str(path.resolve()) for path in sorted((session.root / "mods").rglob(PROJECT_FILE_NAME)))
        paths.extend(super().discover_project_files())
        return list(dict.fromkeys(paths))

    def recent_project_files(self):
        return super().recent_project_files()

    def refresh_project_combo(self, selected=""):
        super().refresh_project_combo(selected)
        self.project_combo.blockSignals(True)
        try:
            entries = []
            for index in range(self.project_combo.count()):
                path = self.project_combo.itemData(index)
                if path:
                    try:
                        entries.append((index, path, load_project(path).project_name))
                    except Exception:
                        pass
            for index, path, label in entries:
                if sum(name == label for _, _, name in entries) > 1:
                    label = f"{label} · {Path(path).parent.name}"
                self.project_combo.setItemText(index, label)
        finally:
            self.project_combo.blockSignals(False)

    def switch_to_project(self, path):
        if self.current_project and self.current_project.project_file.resolve() == Path(path).resolve():
            return True
        return self.load_project_file(path)

    def bind_project(self, project):
        self._binding = True
        try:
            if project is None:
                super().clear_current_project()
            else:
                super().set_current_project(project)
        finally:
            self._binding = False

    def set_current_project(self, project):
        session = self._session_provider()
        if session and not self._binding and project.project_dir.resolve() != (session.root / "mods").resolve():
            project = session.attach_mod_project(project.project_file)
        super().set_current_project(project)
        if not self._binding:
            self.projectChanged.emit(project)

    def mark_dirty(self, *_args):
        super().mark_dirty(*_args)
        if self._dirty and not self._binding:
            self.projectChanged.emit(self.current_project)

    def open_project_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("mod.project.open", default="导入 MOD 工程副本"), "",
            "Live2D MOD (*.json)",
        )
        if path:
            self.load_project_file(path)

    def load_project_file(self, path, silent=False):
        session = self._session_provider()
        if not session:
            self.show_warning(tr("editor.live2d.empty", default="先打开 Live2D 模型"))
            return False
        if not self.flush_pending_changes() and self.current_project:
            return False
        try:
            self.set_current_project(session.attach_mod_project(path))
            return True
        except Exception as exc:
            if not silent:
                self.show_error(str(exc))
            return False

    def start_create_project(self, project_name=""):
        session = self._session_provider()
        if not session:
            return
        source = self.source_edit.text().strip()
        if not source or not Path(source).exists():
            self.show_warning(tr("mod.warning.no_source"))
            return
        self.start_worker("create", source=source,
                          project_name=project_name.strip() or self.suggest_project_name(source),
                          output_root=session.root / "mod-imports",
                          temp_root=self.settings_manager.get_temp_dir())

    def create_new_project(self):
        session = self._session_provider()
        if not session or not self.confirm_discard_or_save():
            return
        dialog = ProjectNameDialog(tr("mod.project.new_title", default="新建 MOD 工程"),
                                   self.current_project.project_name if self.current_project else "", self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            project = create_empty_project(dialog.project_name, output_root=session.root / "mod-imports")
            self.set_current_project(project)
        except Exception as exc:
            self.show_error(str(exc))

    def rename_current_project(self):
        if not self.current_project or not self.flush_pending_changes():
            return
        dialog = ProjectNameDialog(tr("mod.project.rename_title", default="重命名 MOD 工程"),
                                   self.current_project.project_name, self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            data = dict(self.current_project.data)
            data["project_name"] = dialog.project_name
            # The anchored mods/ directory stays stable; rename its title.
            save_project(self.current_project.project_dir, data)
            super().load_project_file(str(self.current_project.project_file), silent=True)
        except Exception as exc:
            self.show_error(str(exc))

    def clear_current_project(self):
        super().clear_current_project()
        if not self._binding:
            session = self._session_provider()
            if session:
                session.detach_mod_project()
            self.projectChanged.emit(None)

    def forget_project_file(self, *_args, **_kwargs):
        # Only the independent working copy is removable here. Do not alter
        # the user's standalone recent/ignored-project preferences.
        return
