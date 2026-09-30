import os

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLayout, QSizePolicy
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import FluentWindow, NavigationItemPosition, InfoBar, InfoBarPosition
from qfluentwidgets import isDarkTheme

from app.core.font_helper import apply_application_font
from app.core.settings_manager import SettingsManager
from app.gui.theme import apply_application_theme
from app.i18n import get_i18n, normalize_language_code, tr
from app.gui.Live2DEditorPage import Live2DEditorPage
from app.gui.SpineEditorPage import SpineEditorPage


try:
    from app.gui.ExtractorPage import ExtractorPage
except Exception as e:
    import traceback
    print(f"Error importing ExtractorPage: {e}")
    traceback.print_exc()

    class ExtractorPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("extractorPage")
            QHBoxLayout(self).addWidget(QFrame(self))


try:
    from app.gui.UnityExtractorPage import UnityExtractorPage
except Exception as e:
    import traceback
    print(f"Error importing UnityExtractorPage: {e}")
    traceback.print_exc()

    class UnityExtractorPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("unityExtractorPage")
            QHBoxLayout(self).addWidget(QFrame(self))


_DISABLE_NATIVE_PREVIEW = os.environ.get("LPK_DISABLE_NATIVE_PREVIEW", "0") == "1"
if not _DISABLE_NATIVE_PREVIEW:
    try:
        from app.gui.PreviewPage import PreviewPage
    except Exception as e:
        import traceback
        print(f"Error importing PreviewPage: {e}")
        traceback.print_exc()

        class PreviewPage(QFrame):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setObjectName("previewPage")
                QHBoxLayout(self).addWidget(QFrame(self))
else:
    class PreviewPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("previewPage")
            QHBoxLayout(self).addWidget(QFrame(self))


try:
    from app.gui.EncryptionPage import EncryptionPage
except Exception as e:
    import traceback
    print(f"Error importing EncryptionPage: {e}")
    traceback.print_exc()

    class EncryptionPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("encryptionPage")
            QHBoxLayout(self).addWidget(QFrame(self))


try:
    from app.gui.SteamWorkshopPage import SteamWorkshopPage
except Exception as e:
    import traceback
    print(f"Error importing SteamWorkshopPage: {e}")
    traceback.print_exc()

    class SteamWorkshopPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("steamWorkshopPage")
            QHBoxLayout(self).addWidget(QFrame(self))


try:
    from app.gui.Live2DModPage import Live2DModPage
except Exception as e:
    import traceback
    print(f"Error importing Live2DModPage: {e}")
    traceback.print_exc()

    class Live2DModPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("live2dModPage")
            QHBoxLayout(self).addWidget(QFrame(self))


try:
    from app.gui.PsdReconstructionPage import PsdReconstructionPage
except Exception as e:
    import traceback
    print(f"Error importing PsdReconstructionPage: {e}")
    traceback.print_exc()

    class PsdReconstructionPage(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("psdReconstructionPage")
            QHBoxLayout(self).addWidget(QFrame(self))


try:
    from app.gui.SpineConverterPage import SpineConverterPage
except Exception as e:
    import traceback
    print(f"Error importing SpineConverterPage: {e}")
    traceback.print_exc()

    class SpineConverterPage(QFrame):
        previewRequested = None

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("spineConverterPage")
            QHBoxLayout(self).addWidget(QFrame(self))


try:
    from app.gui.SettingsPage import SettingsPage
except Exception as e:
    import traceback
    print(f"Error importing SettingsPage: {e}")
    traceback.print_exc()

    class SettingsPage(QFrame):
        languageChanged = None
        themeChanged = None
        fontChanged = None

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("settingsPage")
            QHBoxLayout(self).addWidget(QFrame(self))


class MainWindow(FluentWindow):
    """Main Window with Navigation."""

    def __init__(self):
        super().__init__()
        self.setMicaEffectEnabled(False)
        # A stacked layout otherwise combines the size hints of every hidden
        # page, so opening an inspector can enlarge unrelated pages as well.
        # Each page keeps its own scrolling/splitter layout within the viewport.
        self.stackedWidget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        # Wrapped labels in nested tabs also report a preferred height for
        # width. On Windows, a default top-level constraint treats that as a
        # minimum and grows the window even though the page can scroll/shrink.
        self.hBoxLayout.setSizeConstraint(QLayout.SetNoConstraint)
        self.setMinimumSize(1040, 760)

        self.settings_manager = SettingsManager()
        self.i18n = get_i18n()
        self._applied_font_preferences = None
        self.i18n.set_language(
            normalize_language_code(self.settings_manager.get("language", "en_US"))
        )

        self.extractorPage = self._create_page(ExtractorPage, "extractorPage")
        self.unityExtractorPage = None
        self.previewPage = self._create_page(PreviewPage, "previewPage")
        self.encryptionPage = None
        self.steamWorkshopPage = None
        self.live2dEditorPage = self._create_page(Live2DEditorPage, "live2dEditorPage")
        self.spineEditorPage = self._create_page(SpineEditorPage, "spineEditorPage")
        self.live2dModPage = getattr(self.live2dEditorPage, "mod_panel", None)
        self._editor_source_leases = {}
        self._opening_editor_request = None
        self.psdReconstructionPage = getattr(self.live2dEditorPage, "psd_panel", None)
        self._embedded_psd_workspace = self.psdReconstructionPage is not None
        if self.psdReconstructionPage is None:
            self.psdReconstructionPage = self._create_page(PsdReconstructionPage, "psdReconstructionPage")
        self.spineConverterPage = self._create_page(SpineConverterPage, "spineConverterPage")
        self.settingsPage = self._create_page(SettingsPage, "settingsPage")

        unified_preview_requested = getattr(
            self.psdReconstructionPage,
            "unifiedPreviewRequested",
            None,
        )
        if not self._embedded_psd_workspace and unified_preview_requested is not None and hasattr(unified_preview_requested, "connect"):
            unified_preview_requested.connect(self.open_psd_preview)
        # The Live2D editor owns its MOD panel's preview signal. Connecting it
        # again here would load the same model twice and switch pages midway.
        editor_requested = getattr(self.previewPage, "editorRequested", None)
        if editor_requested is not None and hasattr(editor_requested, "connect"):
            editor_requested.connect(self.open_model_editor)
        for page in (self.live2dEditorPage, self.spineEditorPage):
            for signal_name in ("sourceOpened", "sourceFailed"):
                signal = getattr(page, signal_name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(lambda source, editor=page, failed=signal_name == "sourceFailed":
                                   self._editor_source_completed(editor, source, failed))
        pose_scheme_requested = getattr(self.previewPage, "poseSchemeRequested", None)
        pose_scheme_handler = getattr(
            self.psdReconstructionPage,
            "create_pose_scheme_from_preview",
            None,
        )
        if (
            pose_scheme_requested is not None
            and hasattr(pose_scheme_requested, "connect")
            and callable(pose_scheme_handler)
        ):
            pose_scheme_requested.connect(self.save_psd_pose_scheme)
        converter_preview_requested = getattr(
            self.spineConverterPage,
            "previewRequested",
            None,
        )
        if (
            converter_preview_requested is not None
            and hasattr(converter_preview_requested, "connect")
        ):
            converter_preview_requested.connect(self.open_spine_converter_preview)

        language_changed = getattr(self.settingsPage, "languageChanged", None)
        theme_changed = getattr(self.settingsPage, "themeChanged", None)
        font_changed = getattr(self.settingsPage, "fontChanged", None)
        if language_changed is not None and hasattr(language_changed, "connect"):
            language_changed.connect(self.on_language_changed)
        if theme_changed is not None and hasattr(theme_changed, "connect"):
            theme_changed.connect(self.on_theme_changed)
        if font_changed is not None and hasattr(font_changed, "connect"):
            font_changed.connect(self.on_font_changed)

        self.initWindow()
        self.initNavigation()
        self.stackedWidget.currentChanged.connect(self._on_page_changed)
        self._on_page_changed()
        self.apply_theme()
        self.updateFontSize()

        self.i18n.languageChanged.connect(self.retranslate_ui)
        self.retranslate_ui()
        self.installEventFilter(self)

    def _create_page(self, page_cls, object_name: str):
        try:
            return page_cls(self)
        except Exception as e:
            print(f"Error creating {page_cls.__name__}: {e}")
            page = QFrame(self)
            page.setObjectName(object_name)
            page._creation_error = str(e)
            return page

    def initWindow(self):
        geometry = dict(self.settings_manager.get("window_geometry", {}) or {})
        self.setGeometry(
            int(geometry.get("x", 100)),
            int(geometry.get("y", 100)),
            max(800, int(geometry.get("width", 1000))),
            max(600, int(geometry.get("height", 700))),
        )
        self.setWindowTitle(tr("main.window_title"))
        if geometry.get("maximized", False):
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def closeEvent(self, event):
        settings_page = getattr(self, "settingsPage", None)
        installing = getattr(settings_page, "is_tool_install_running", None)
        if callable(installing) and installing():
            settings_page.notify_close_while_installing()
            event.ignore()
            return
        preview_page = getattr(self, "previewPage", None)
        spine_installing = getattr(
            preview_page, "is_spine_runtime_install_running", None
        )
        if callable(spine_installing) and spine_installing():
            notify_close = getattr(
                preview_page,
                "notify_close_while_spine_runtime_installing",
                None,
            )
            if callable(notify_close):
                notify_close()
            # The preview installer is a QThread child of PreviewPage.  Keep
            # the window alive until its cancellation reaches ``finished``;
            # destroying a running QThread would terminate the process.
            event.ignore()
            return
        converter_page = getattr(self, "spineConverterPage", None)
        is_conversion_running = getattr(converter_page, "is_conversion_running", None)
        if callable(is_conversion_running) and is_conversion_running():
            notify_close = getattr(converter_page, "notify_close_while_running", None)
            if callable(notify_close):
                notify_close()
            # Let the converter finish instead of destroying a running QThread
            # while the main window is closing.
            event.ignore()
            return
        prepare_preview = getattr(preview_page, "prepare_shutdown", None)
        if callable(prepare_preview) and prepare_preview() is False:
            event.ignore()
            return
        for page in (getattr(self, "live2dEditorPage", None), getattr(self, "spineEditorPage", None)):
            confirm = getattr(page, "confirm_discard_or_save", None)
            if callable(confirm) and not confirm():
                event.ignore()
                return
        shutdown_preview = getattr(preview_page, "shutdown", None)
        if callable(shutdown_preview) and shutdown_preview() is False:
            event.ignore()
            return
        shutdown_mcp = getattr(settings_page, "shutdown_animation_mcp", None)
        if callable(shutdown_mcp) and shutdown_mcp() is False:
            event.ignore()
            return
        for page in (getattr(self, "live2dEditorPage", None), getattr(self, "spineEditorPage", None)):
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()
            self._release_editor_source_leases(page)
        # Store the restore rectangle, not the maximized monitor rectangle.
        # Otherwise opening the next session looks maximized but its titlebar
        # and Windows restore state disagree.
        maximized = self.isMaximized()
        rect = self.normalGeometry() if maximized or self.isFullScreen() else self.geometry()
        self.settings_manager.set(
            "window_geometry",
            {
                "width": rect.width(),
                "height": rect.height(),
                "x": rect.x(),
                "y": rect.y(),
                "maximized": maximized,
            },
        )
        super().closeEvent(event)

    def initNavigation(self):
        try:
            self.addSubInterface(self.extractorPage, FIF.ZIP_FOLDER, tr("main.nav.extractor"))
        except Exception as e:
            print(f"Error adding ExtractorPage to navigation: {e}")

        try:
            if not _DISABLE_NATIVE_PREVIEW:
                self.addSubInterface(self.previewPage, FIF.MOVIE, tr("main.nav.preview_native"))
        except Exception as e:
            print(f"Error adding PreviewPage to navigation: {e}")

        try:
            self.addSubInterface(
                self.live2dEditorPage,
                FIF.PEOPLE,
                tr("main.nav.live2d_editor"),
                NavigationItemPosition.SCROLL,
            )
        except Exception as e:
            print(f"Error adding Live2DEditorPage to navigation: {e}")

        self.addSubInterface(
            self.spineEditorPage, FIF.IOT, tr("main.nav.spine_editor"), NavigationItemPosition.SCROLL,
        )

        try:
            self.theme_toggle_button = self.navigationInterface.addItem(
                "themeToggle", FIF.CONSTRACT, "", onClick=self.toggle_theme,
                selectable=False, position=NavigationItemPosition.BOTTOM,
            )
            self.addSubInterface(
                self.spineConverterPage,
                FIF.SYNC,
                tr("main.nav.spine_converter"),
                NavigationItemPosition.SCROLL,
            )
        except Exception as e:
            print(f"Error adding SpineConverterPage to navigation: {e}")

        try:
            self.addSubInterface(
                self.settingsPage,
                FIF.SETTING,
                tr("main.nav.settings"),
                NavigationItemPosition.BOTTOM,
            )
        except Exception as e:
            print(f"Error adding SettingsPage to navigation: {e}")

    def _on_page_changed(self, _index=None):
        current = self.stackedWidget.currentWidget()
        for page in (self.previewPage, self.live2dEditorPage, self.spineEditorPage):
            activate = getattr(page, "set_active", None)
            if callable(activate):
                activate(page is current)
            elif page is not current:
                pause = getattr(page, "pause_playback", None)
                if callable(pause):
                    pause()

    def toggle_theme(self):
        self.on_theme_changed("light" if isDarkTheme() else "dark")

    def _update_theme_toggle(self):
        button = getattr(self, "theme_toggle_button", None)
        if button is not None:
            text = tr("main.theme.switch_light" if isDarkTheme() else "main.theme.switch_dark")
            button.setText(text)
            button.setToolTip(text)

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QEvent.Resize:
            self.updateFontSize()

        return super().eventFilter(obj, event)

    def updateFontSize(self):
        # Keep the user's chosen point size stable while retaining the
        # existing page-level resize hooks below.  The old width heuristic
        # silently overwrote a persisted custom size on every resize.
        get_family = getattr(self.settings_manager, "get_font_family", None)
        get_size = getattr(self.settings_manager, "get_font_size", None)
        family = get_family() if callable(get_family) else ""
        size = get_size() if callable(get_size) else 10
        preferences = (str(family or ""), int(size))
        if preferences != self._applied_font_preferences:
            apply_application_font(QApplication.instance(), family, size, root=self)
            self._applied_font_preferences = preferences

        pages = [
            self.extractorPage,
            self.unityExtractorPage,
            self.previewPage,
            self.encryptionPage,
            self.steamWorkshopPage,
            self.live2dEditorPage,
            self.spineEditorPage,
            self.psdReconstructionPage,
            self.spineConverterPage,
            self.settingsPage,
        ]
        for page in filter(None, pages):
            if hasattr(page, "updateUIScale"):
                page.updateUIScale(self.width(), self.height())

    def apply_theme(self):
        try:
            theme_setting = str(self.settings_manager.get("theme", "auto") or "auto").lower()
            selected = apply_application_theme(theme_setting, self)
            print(f"Applied theme: {selected.value.lower()}")
        except Exception as e:
            print(f"Error applying theme: {e}")
            apply_application_theme("light", self)
        self._update_theme_toggle()

    def retranslate_ui(self):
        self.setWindowTitle(tr("main.window_title"))

        for page in [
            self.extractorPage,
            self.unityExtractorPage,
            self.previewPage,
            self.encryptionPage,
            self.steamWorkshopPage,
            self.live2dEditorPage,
            self.spineEditorPage,
            self.psdReconstructionPage,
            self.spineConverterPage,
            self.settingsPage,
        ]:
            if page is not None and hasattr(page, "retranslate_ui"):
                try:
                    page.retranslate_ui()
                except Exception as e:
                    print(f"Error re-translating {page.objectName()}: {e}")
        for page, key in (
            (self.extractorPage, "main.nav.extractor"),
            (self.previewPage, "main.nav.preview_native"),
            (self.live2dEditorPage, "main.nav.live2d_editor"),
            (self.spineEditorPage, "main.nav.spine_editor"),
            (self.spineConverterPage, "main.nav.spine_converter"),
            (self.settingsPage, "main.nav.settings"),
        ):
            if page is self.previewPage and _DISABLE_NATIVE_PREVIEW:
                continue
            item = self.navigationInterface.widget(page.objectName())
            if item is not None:
                item.setText(tr(key))
        self._update_theme_toggle()

    def on_language_changed(self, language: str):
        normalized = normalize_language_code(language)
        self.settings_manager.set("language", normalized)
        self.i18n.set_language(normalized)

    def on_theme_changed(self, theme: str):
        self.settings_manager.set("theme", str(theme).lower())
        sync = getattr(self.settingsPage, "sync_theme_selection", None)
        if callable(sync):
            sync(str(theme).lower())
        self.apply_theme()

    def on_font_changed(self, family: str, size: int):
        """Apply a settings-page font change to every existing text widget."""

        setter = getattr(self.settings_manager, "set_font_preferences", None)
        if callable(setter):
            setter(family, size)
        apply_application_font(
            QApplication.instance(),
            family,
            size,
            # Include preview/dialog top-level windows that are not children
            # of the Fluent main window when a preference changes live.
            root=None,
        )
        self._applied_font_preferences = (str(family or ""), int(size))
        for page in (
            self.extractorPage,
            self.unityExtractorPage,
            self.previewPage,
            self.encryptionPage,
            self.steamWorkshopPage,
            self.live2dEditorPage,
            self.spineEditorPage,
            self.psdReconstructionPage,
            self.spineConverterPage,
            self.settingsPage,
        ):
            if page is not None and hasattr(page, "updateUIScale"):
                try:
                    page.updateUIScale(self.width(), self.height())
                except Exception as exc:
                    print(f"Error refreshing {page.objectName()} after font change: {exc}")

    def open_psd_preview(self, model_json_path: str, project_file: str):
        if not self.open_psd_workspace(project_file=project_file):
            return False
        handler = getattr(self.live2dEditorPage, "_open_psd_preview", None)
        if callable(handler):
            handler(model_json_path, project_file)
            return True
        return False

    def _release_editor_source_leases(self, page, source=None, token=None):
        leases = getattr(self, "_editor_source_leases", {}).get(page, {})
        if token is not None:
            selected = [token] if token in leases else []
        elif source is not None:
            path = os.path.normcase(os.path.abspath(source))
            selected = [key for key, value in leases.items() if value == path][:1]
            active = self._opening_editor_request
            if active and active[0] is page and active[2] == path and active[1] in leases:
                selected = [active[1]]
        else:
            selected = list(leases)
        release = getattr(getattr(self, "previewPage", None), "release_editor_source", None)
        for lease in selected:
            leases.pop(lease, None)
            if callable(release):
                release(lease)
        if not leases:
            self._editor_source_leases.pop(page, None)

    def _editor_source_completed(self, page, source, failed):
        self._release_editor_source_leases(page, source)
        # Synchronous failures are reported by open_model_editor once it has
        # the return value. Cancelled opens deliberately carry no error text.
        if failed and not (self._opening_editor_request and self._opening_editor_request[0] is page):
            error = str(getattr(page, "last_open_error", "") or "")
            if error:
                self._show_editor_open_error(error)

    def _show_editor_open_error(self, error):
        reporter = getattr(self.previewPage, "show_error", None)
        if self.stackedWidget.currentWidget() is self.previewPage and callable(reporter):
            reporter(tr("editor.navigation.open_failed"), str(error))
        else:
            InfoBar.error(title=tr("editor.navigation.open_failed"), content=str(error),
                          isClosable=True, position=InfoBarPosition.TOP, duration=6000, parent=self)

    def open_model_editor(self, kind: str, model_path: str) -> bool:
        """Open the resolved preview model after the editor accepts its new source."""
        page = {"live2d": self.live2dEditorPage, "spine": self.spineEditorPage}.get(kind)
        if page is None or not os.path.isfile(model_path):
            self._show_editor_open_error(model_path)
            return False
        opener = getattr(page, "open_source", None)
        if not callable(opener):
            self._show_editor_open_error(getattr(page, "_creation_error", tr("editor.navigation.open_failed")))
            return False
        acquire = getattr(self.previewPage, "acquire_editor_source", None)
        lease = acquire(model_path) if callable(acquire) else None
        if lease is not None:
            self._editor_source_leases.setdefault(page, {})[lease] = os.path.normcase(os.path.abspath(model_path))
        self._opening_editor_request = (page, lease, os.path.normcase(os.path.abspath(model_path)))
        error = ""
        try:
            accepted = bool(opener(model_path))
            if not accepted:
                error = str(getattr(page, "last_open_error", "") or "")
        except Exception as exc:
            error = str(exc)
            accepted = False
        finally:
            self._opening_editor_request = None
        asynchronous = hasattr(page, "sourceOpened") and hasattr(page, "sourceFailed")
        if lease is not None and (not accepted or not asynchronous):
            self._release_editor_source_leases(page, token=lease)
        if error:
            self._show_editor_open_error(error)
        if accepted:
            self.switchTo(page)
        return accepted

    def open_live2d_mod_preview(self, model_json_path: str):
        self.switchTo(self.previewPage)
        handler = getattr(self.previewPage, "open_model_preview_source", None)
        if callable(handler):
            handler(model_json_path)

    def open_spine_converter_preview(self, skeleton_path: str):
        """Open converted Spine data in the existing Spine preview stage."""

        self.switchTo(self.previewPage)
        handler = getattr(self.previewPage, "start_spine_preview_import", None)
        if callable(handler):
            handler(skeleton_path)

    def save_psd_pose_scheme(self, payload: dict):
        handler = getattr(
            self.psdReconstructionPage,
            "create_pose_scheme_from_preview",
            None,
        )
        if not callable(handler):
            return
        if self.open_psd_workspace(payload.get("model_json_path"), payload.get("project_file")):
            copied_payload = dict(payload)
            project = getattr(self.psdReconstructionPage, "current_project", None)
            copied_project_file = getattr(project, "project_file", None)
            if copied_project_file:
                copied_payload["project_file"] = str(copied_project_file)
            handler(copied_payload)

    def open_psd_workspace(self, model_path=None, project_file=None):
        opener = getattr(self.live2dEditorPage, "open_psd_workspace", None)
        if not callable(opener):
            return False
        if opener(model_path=model_path, project_file=project_file):
            self.switchTo(self.live2dEditorPage)
            return True
        return False

    def switchTo(self, interface):  # noqa: N802
        if interface is getattr(self, "psdReconstructionPage", None):
            self.open_psd_workspace()
            return
        super().switchTo(interface)
