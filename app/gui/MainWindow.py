import os

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import FluentWindow, NavigationItemPosition

from app.core.settings_manager import SettingsManager
from app.gui.theme import apply_application_theme
from app.i18n import get_i18n, normalize_language_code, tr


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

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("settingsPage")
            QHBoxLayout(self).addWidget(QFrame(self))


class MainWindow(FluentWindow):
    """Main Window with Navigation."""

    def __init__(self):
        super().__init__()
        self.setMicaEffectEnabled(False)

        self.settings_manager = SettingsManager()
        self.i18n = get_i18n()
        self.i18n.set_language(
            normalize_language_code(self.settings_manager.get("language", "en_US"))
        )

        self.extractorPage = self._create_page(ExtractorPage, "extractorPage")
        self.unityExtractorPage = None
        self.previewPage = self._create_page(PreviewPage, "previewPage")
        self.encryptionPage = None
        self.steamWorkshopPage = None
        self.live2dModPage = self._create_page(Live2DModPage, "live2dModPage")
        self.psdReconstructionPage = self._create_page(
            PsdReconstructionPage, "psdReconstructionPage"
        )
        self.spineConverterPage = self._create_page(SpineConverterPage, "spineConverterPage")
        self.settingsPage = self._create_page(SettingsPage, "settingsPage")

        unified_preview_requested = getattr(
            self.psdReconstructionPage,
            "unifiedPreviewRequested",
            None,
        )
        if unified_preview_requested is not None and hasattr(unified_preview_requested, "connect"):
            unified_preview_requested.connect(self.open_psd_preview)
        mod_preview_requested = getattr(
            self.live2dModPage,
            "previewModelRequested",
            None,
        )
        if mod_preview_requested is not None and hasattr(mod_preview_requested, "connect"):
            mod_preview_requested.connect(self.open_live2d_mod_preview)
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
        if language_changed is not None and hasattr(language_changed, "connect"):
            language_changed.connect(self.on_language_changed)
        if theme_changed is not None and hasattr(theme_changed, "connect"):
            theme_changed.connect(self.on_theme_changed)

        self.initWindow()
        self.initNavigation()
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
                self.psdReconstructionPage,
                FIF.IMAGE_EXPORT,
                tr("main.nav.psd_reconstruction"),
                NavigationItemPosition.SCROLL,
            )
        except Exception as e:
            print(f"Error adding PsdReconstructionPage to navigation: {e}")

        try:
            self.addSubInterface(
                self.live2dModPage,
                FIF.EDIT,
                tr("main.nav.live2d_mod"),
                NavigationItemPosition.SCROLL,
            )
        except Exception as e:
            print(f"Error adding Live2DModPage to navigation: {e}")

        try:
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

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QEvent.Resize:
            self.updateFontSize()

        return super().eventFilter(obj, event)

    def updateFontSize(self):
        width = self.width()

        base_size = 9
        if width > 1600:
            font_size = base_size + 3
        elif width > 1200:
            font_size = base_size + 2
        elif width > 800:
            font_size = base_size + 1
        else:
            font_size = base_size

        app = QApplication.instance()
        font = app.font()
        font.setPointSize(font_size)
        app.setFont(font)

        pages = [
            self.extractorPage,
            self.unityExtractorPage,
            self.previewPage,
            self.encryptionPage,
            self.steamWorkshopPage,
            self.live2dModPage,
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

    def retranslate_ui(self):
        self.setWindowTitle(tr("main.window_title"))

        for page in [
            self.extractorPage,
            self.unityExtractorPage,
            self.previewPage,
            self.encryptionPage,
            self.steamWorkshopPage,
            self.live2dModPage,
            self.psdReconstructionPage,
            self.spineConverterPage,
            self.settingsPage,
        ]:
            if page is not None and hasattr(page, "retranslate_ui"):
                try:
                    page.retranslate_ui()
                except Exception as e:
                    print(f"Error re-translating {page.objectName()}: {e}")

    def on_language_changed(self, language: str):
        normalized = normalize_language_code(language)
        self.settings_manager.set("language", normalized)
        self.i18n.set_language(normalized)

    def on_theme_changed(self, theme: str):
        self.settings_manager.set("theme", str(theme).lower())
        self.apply_theme()

    def open_psd_preview(self, model_json_path: str, project_file: str):
        self.switchTo(self.previewPage)
        handler = getattr(self.previewPage, "open_psd_project_preview", None)
        if callable(handler):
            handler(model_json_path, project_file)

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
        self.switchTo(self.psdReconstructionPage)
        handler(payload)
