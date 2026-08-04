from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, QThread, QTime, QTimer, Qt, Signal
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon, QPixmap, QTextCharFormat, QBrush
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QCalendarWidget,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QTextEdit,
    QTimeEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import load_settings
from .permissions import Permission, ROLE_LABELS, can
from .repository import AppError, SupabaseRepository
from .theme import APP_STYLESHEET, COLORS, STATUS_COLORS
from .updater import ReleaseInfo, download_installer, latest_release
from .version import APP_VERSION

STATUSES = ["Agendado", "Confirmado", "Atendido", "Falta com aviso", "Falta sem aviso", "Cancelado"]
SPECIALTIES = [
    "Psicologa",
    "Psicopedagoga",
    "Neuropsicopedagoga",
    "Fonoaudiologa",
    "Terapeuta ocupacional",
    "Outra",
]

DOCUMENT_FILE_FILTER = (
    "Documentos e imagens (*.pdf *.doc *.docx *.xls *.xlsx *.ppt *.pptx *.txt *.rtf *.csv "
    "*.png *.jpg *.jpeg *.webp *.gif *.bmp *.zip *.rar *.7z);;"
    "PDF (*.pdf);;"
    "Word (*.doc *.docx);;"
    "Planilhas (*.xls *.xlsx *.csv);;"
    "Apresentações (*.ppt *.pptx);;"
    "Imagens (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;"
    "Arquivos compactados (*.zip *.rar *.7z);;"
    "Todos os arquivos (*.*)"
)


def resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    if base.name != "agape_app":
        candidate = base / "agape_app" / "assets" / Path(*parts)
        if candidate.exists():
            return candidate
    return base / "assets" / Path(*parts)


def app_icon_path() -> Path:
    return resource_path("app_icon.ico")


def app_icon() -> QIcon:
    icon = QIcon(str(app_icon_path()))
    png_path = resource_path("app_icon.png")
    if png_path.exists():
        icon.addFile(str(png_path))
    return icon


def configure_windows_taskbar_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClinicaAgape.Desktop.App")
    except Exception:
        pass


def configure_logger() -> logging.Logger:
    logger = logging.getLogger("clinica_agape")
    if logger.handlers:
        return logger

    base_dir = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    log_dir = base_dir / "ClinicaAgape" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
    except Exception:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


LOGGER = configure_logger()
_LAST_ERROR_SIGNATURE = ""
_LAST_ERROR_AT: datetime | None = None


def apply_window_icon(window: QWidget) -> None:
    icon = app_icon()
    window.setWindowIcon(icon)
    if sys.platform != "win32":
        return
    try:
        hwnd = int(window.winId())
        icon_path = str(app_icon_path())
        image_icon = 1
        lr_loadfromfile = 0x00000010
        lr_defaultsize = 0x00000040
        wm_seticon = 0x0080
        icon_small = 0
        icon_big = 1
        user32 = ctypes.windll.user32
        big_icon = user32.LoadImageW(None, icon_path, image_icon, 0, 0, lr_loadfromfile | lr_defaultsize)
        small_icon = user32.LoadImageW(None, icon_path, image_icon, 16, 16, lr_loadfromfile)
        if big_icon:
            user32.SendMessageW(hwnd, wm_seticon, icon_big, big_icon)
        if small_icon:
            user32.SendMessageW(hwnd, wm_seticon, icon_small, small_icon)
        window._native_icon_handles = (big_icon, small_icon)
    except Exception:
        pass


def log_exception(context: str, exc: Exception) -> None:
    LOGGER.error(context, exc_info=(type(exc), exc, exc.__traceback__))


def friendly_error_message(exc: Exception) -> str:
    raw = str(exc)
    if "JSON could not be generated" in raw or "Bad Request" in raw:
        return (
            "Não foi possível atualizar alguns dados agora. "
            "O sistema continuara aberto; tente atualizar a tela em instantes."
        )
    return raw


def show_error(parent: QWidget, exc: Exception) -> None:
    global _LAST_ERROR_SIGNATURE, _LAST_ERROR_AT

    log_exception("Erro exibido ao usuário", exc)
    message = friendly_error_message(exc)
    signature = f"{type(exc).__name__}:{message}"
    now = datetime.now()
    if (
        _LAST_ERROR_SIGNATURE == signature
        and _LAST_ERROR_AT is not None
        and (now - _LAST_ERROR_AT).total_seconds() < 4
    ):
        return
    _LAST_ERROR_SIGNATURE = signature
    _LAST_ERROR_AT = now
    QMessageBox.warning(parent, "Atenção", message)


def show_page_error(parent: QWidget, page: QWidget, exc: Exception, show_popup: bool) -> None:
    context = f"Erro ao atualizar {page.__class__.__name__}"
    if show_popup:
        show_error(parent, exc)
    else:
        log_exception(context, exc)


class UpdateCheckThread(QThread):
    update_available = Signal(object)
    check_failed = Signal(str)

    def run(self) -> None:
        try:
            release = latest_release(APP_VERSION)
            if release:
                self.update_available.emit(release)
        except Exception as exc:
            self.check_failed.emit(str(exc))


class UpdateDownloadThread(QThread):
    download_ready = Signal(str)
    download_failed = Signal(str)

    def __init__(self, release: ReleaseInfo):
        super().__init__()
        self.release = release

    def run(self) -> None:
        try:
            self.download_ready.emit(str(download_installer(self.release)))
        except Exception as exc:
            self.download_failed.emit(str(exc))


def start_update_check(app: QApplication, parent: QWidget) -> None:
    if not getattr(sys, "frozen", False):
        return

    app._update_threads = getattr(app, "_update_threads", [])
    check_thread = UpdateCheckThread()
    app._update_threads.append(check_thread)

    def forget_thread(thread: QThread) -> None:
        if thread in app._update_threads:
            app._update_threads.remove(thread)
        thread.deleteLater()

    def offer_update(release: ReleaseInfo) -> None:
        notes = release.notes.strip()
        if len(notes) > 700:
            notes = f"{notes[:700].rstrip()}…"
        message = f"A versão {release.version} está disponível. Deseja atualizar agora?"
        if notes:
            message = f"{message}\n\n{notes}"
        answer = QMessageBox.question(
            parent,
            "Atualização disponível",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        QMessageBox.information(
            parent,
            "Baixando atualização",
            "A atualização será baixada em segundo plano. O aplicativo avisará quando estiver pronta.",
        )
        download_thread = UpdateDownloadThread(release)
        app._update_threads.append(download_thread)

        def install_update(installer_path: str) -> None:
            try:
                subprocess.Popen(
                    [installer_path, "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
                    close_fds=True,
                )
                app.quit()
            except Exception as exc:
                show_error(parent, exc)

        download_thread.download_ready.connect(install_update)
        download_thread.download_failed.connect(
            lambda message: QMessageBox.warning(parent, "Falha na atualização", message)
        )
        download_thread.finished.connect(lambda: forget_thread(download_thread))
        download_thread.start()

    check_thread.update_available.connect(offer_update)
    check_thread.check_failed.connect(lambda message: LOGGER.info("Verificação de atualização ignorada: %s", message))
    check_thread.finished.connect(lambda: forget_thread(check_thread))
    check_thread.start()


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget:
            widget.deleteLater()


def button(text: str, secondary: bool = False, danger: bool = False) -> QPushButton:
    btn = QPushButton(text)
    if secondary:
        btn.setProperty("secondary", True)
    if danger:
        btn.setProperty("danger", True)
    return btn


def card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("Card")
    return frame


def page_card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("PageCard")
    return frame


def style_card(frame: QFrame, background: str = "#FFFFFF", border: str = "#EADFD1") -> None:
    object_name = f"ToneCard_{id(frame)}"
    frame.setObjectName(object_name)
    frame.setStyleSheet(f"QFrame#{object_name} {{ background: {background}; border: 1px solid {border}; border-radius: 8px; }}")


def table_polish(table: QTableWidget) -> None:
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.setSelectionBehavior(QTableWidget.SelectRows)


def full_row_table_polish(table: QTableWidget) -> None:
    table_polish(table)
    table.setObjectName(f"FullRowTable_{id(table)}")
    table.setStyleSheet(
        table.styleSheet()
        + f"""
        QTableWidget#{table.objectName()} {{
            outline: 0;
        }}
        QTableWidget#{table.objectName()}::item {{
            border: none;
            border-radius: 0px;
            margin: 0px;
            padding: 7px 8px;
        }}
        QTableWidget#{table.objectName()}::item:selected {{
            background: {COLORS['primary_soft']};
            color: {COLORS['text']};
            border: none;
        }}
        """
    )


def time_field(hour: int, minute: int) -> QTimeEdit:
    field = QTimeEdit(QTime(hour, minute))
    field.setDisplayFormat("HH:mm")
    field.setAlignment(Qt.AlignCenter)
    field.setButtonSymbols(QTimeEdit.NoButtons)
    return field


class LoginWindow(QWidget):
    def __init__(self, repo: SupabaseRepository):
        super().__init__()
        self.repo = repo
        self.main_window: MainWindow | None = None
        self.setWindowTitle("Clínica Agape - Entrar")
        apply_window_icon(self)
        self.setMinimumSize(620, 600)

        root = QVBoxLayout(self)
        root.setContentsMargins(52, 44, 52, 44)
        root.setSpacing(18)

        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        logo.setStyleSheet("background: transparent;")
        logo_path = resource_path("clinica_agape_logo.jpeg")
        pixmap = QPixmap(str(logo_path))
        if not pixmap.isNull():
            logo.setPixmap(pixmap.scaled(360, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            logo.setText("Clínica Agape")
            logo.setObjectName("Title")

        self.email = QLineEdit()
        self.email.setPlaceholderText("E-mail")
        self.email.setMinimumHeight(44)
        self.password = QLineEdit()
        self.password.setPlaceholderText("Senha")
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setMinimumHeight(44)
        self.error = QLabel("")
        self.error.setMinimumHeight(22)
        self.error.setStyleSheet(f"background: transparent; color: {COLORS['red']};")
        self.login_button = button("Entrar")
        self.login_button.setMinimumHeight(44)
        self.login_button.clicked.connect(self.handle_login)
        self.password.returnPressed.connect(self.handle_login)

        panel = QFrame()
        panel.setObjectName("LoginPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(36, 34, 36, 34)
        panel_layout.setSpacing(15)
        panel_layout.addWidget(logo)
        panel_layout.addSpacing(10)
        panel_layout.addWidget(self.email)
        panel_layout.addWidget(self.password)
        panel_layout.addWidget(self.error)
        panel_layout.addWidget(self.login_button)

        root.addStretch()
        root.addWidget(panel)
        root.addStretch()
        version_label = QLabel(f"Versão {APP_VERSION}")
        version_label.setObjectName("Muted")
        version_label.setStyleSheet("background: transparent;")
        version_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        root.addWidget(version_label)

    def handle_login(self) -> None:
        self.error.clear()
        self.login_button.setEnabled(False)
        try:
            profile = self.repo.login(self.email.text().strip(), self.password.text())
            self.main_window = MainWindow(self.repo, profile)
            self.main_window.show()
            apply_window_icon(self.main_window)
            self.close()
        except Exception as exc:
            self.error.setText(str(exc))
        finally:
            self.login_button.setEnabled(True)


class MainWindow(QMainWindow):
    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any]):
        super().__init__()
        self.repo = repo
        self.profile = profile
        self.setWindowTitle("Clínica Agape")
        apply_window_icon(self)
        self.setMinimumSize(1120, 720)

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(250)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(18, 24, 18, 18)
        side_layout.setSpacing(10)

        brand = QLabel()
        brand.setAlignment(Qt.AlignCenter)
        brand.setMinimumHeight(96)
        brand.setStyleSheet("background: transparent;")
        sidebar_logo_path = resource_path("clinica_agape_logo_sidebar.png")
        sidebar_logo = QPixmap(str(sidebar_logo_path))
        if not sidebar_logo.isNull():
            brand.setPixmap(sidebar_logo.scaled(210, 90, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            brand.setText("Clínica Agape")
            brand.setStyleSheet("background: transparent; color: #FFFFFF; font-size: 22px; font-weight: 800;")
        profile_name = str(profile.get("full_name") or "Usuário")
        profile_role = ROLE_LABELS.get(profile.get("role"), profile.get("role") or "Perfil")
        initials = "".join(part[0] for part in profile_name.split()[:2] if part).upper() or "U"
        profile_card = QFrame()
        profile_card.setStyleSheet(
            "QFrame { background: rgba(255, 255, 255, 0.12); border: 1px solid rgba(255, 255, 255, 0.24); "
            "border-radius: 12px; }"
        )
        profile_layout = QHBoxLayout(profile_card)
        profile_layout.setContentsMargins(12, 11, 12, 11)
        profile_layout.setSpacing(10)
        avatar = QLabel(initials)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFixedSize(42, 42)
        avatar.setStyleSheet(
            "background: #FFFFFF; color: #B51F68; border: none; border-radius: 21px; "
            "font-size: 14px; font-weight: 900;"
        )
        profile_text = QVBoxLayout()
        profile_text.setSpacing(3)
        name_label = QLabel(profile_name)
        name_label.setToolTip(profile_name)
        name_label.setWordWrap(True)
        name_label.setStyleSheet("background: transparent; border: none; color: #FFFFFF; font-size: 14px; font-weight: 800;")
        role_label = QLabel(str(profile_role))
        role_label.setStyleSheet(
            "background: rgba(255, 255, 255, 0.16); border: none; border-radius: 6px; "
            "color: #FFFFFF; padding: 3px 7px; font-size: 11px; font-weight: 700;"
        )
        profile_text.addWidget(name_label)
        profile_text.addWidget(role_label, 0, Qt.AlignLeft)
        profile_layout.addWidget(avatar, 0, Qt.AlignTop)
        profile_layout.addLayout(profile_text, 1)
        side_layout.addWidget(brand)
        side_layout.addWidget(profile_card)
        side_layout.addSpacing(14)

        self.stack = QStackedWidget()
        self.nav_buttons: list[QPushButton] = []
        self.agenda_page = AgendaPage(repo, profile)
        self.dashboard_page = DashboardPage(repo, profile, self.show_page_by_label)
        self.pages: list[tuple[str, QWidget]] = [
            ("Início", self.dashboard_page),
            ("Agenda", self.agenda_page),
            ("Pacientes", PatientsPage(repo, profile)),
            ("Exportar sessões", SessionExportPage(repo)),
            ("Documentos", DocumentsPage(repo, profile)),
        ]
        if can(profile, Permission.VIEW_FINANCIALS):
            self.pages.append(("Financeiro", FinancePage(repo)))
        if can(profile, Permission.VIEW_SETTINGS):
            self.pages.append(("Configurações", SettingsPage(repo)))

        for index, (label, page) in enumerate(self.pages):
            self.stack.addWidget(page)
            nav = QPushButton(label)
            nav.clicked.connect(lambda checked=False, i=index: self.show_page(i))
            self.nav_buttons.append(nav)
            side_layout.addWidget(nav)

        side_layout.addStretch()
        if profile.get("role") == "admin":
            admin_mode = QCheckBox("Modo administrador")
            admin_mode.setCursor(Qt.PointingHandCursor)
            admin_mode.setStyleSheet(
                """
                QCheckBox {
                    background: transparent;
                    color: #FFFFFF;
                    font-weight: 700;
                    spacing: 8px;
                    padding: 8px 2px;
                }
                QCheckBox::indicator {
                    width: 18px;
                    height: 18px;
                    border-radius: 4px;
                    border: 2px solid #FFFFFF;
                    background: transparent;
                }
                QCheckBox::indicator:checked {
                    background: #FFFFFF;
                    border: 2px solid #FFFFFF;
                }
                """
            )
            admin_mode.stateChanged.connect(lambda state: self.set_admin_mode(state == Qt.Checked.value))
            side_layout.addWidget(admin_mode)
        logout = QPushButton("Sair")
        logout.clicked.connect(self.close)
        side_layout.addWidget(logout)

        root_layout.addWidget(sidebar)
        root_layout.addWidget(self.stack, 1)
        self.stack.setCurrentIndex(0)
        self.set_active_nav(0)
        QTimer.singleShot(0, self.refresh_initial_page)
        self.auto_refresh_timer = QTimer(self)
        self.auto_refresh_timer.setInterval(10_000)
        self.auto_refresh_timer.timeout.connect(self.auto_refresh_current_page)
        self.auto_refresh_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_window_icon(self)

    def show_page_by_label(self, label: str) -> None:
        for index, (page_label, _) in enumerate(self.pages):
            if page_label == label:
                self.show_page(index)
                return

    def set_admin_mode(self, enabled: bool) -> None:
        self.agenda_page.set_admin_mode(enabled)
        self.dashboard_page.set_admin_mode(enabled)

    def set_active_nav(self, index: int) -> None:
        for idx, nav in enumerate(self.nav_buttons):
            nav.setProperty("active", idx == index)
            nav.style().unpolish(nav)
            nav.style().polish(nav)

    def safe_refresh_page(self, page: QWidget, show_popup: bool = True) -> None:
        if not hasattr(page, "refresh"):
            return
        try:
            page.refresh()
        except Exception as exc:
            show_page_error(self, page, exc, show_popup)

    def refresh_initial_page(self) -> None:
        self.safe_refresh_page(self.stack.currentWidget(), show_popup=False)

    def show_page(self, index: int) -> None:
        page = self.stack.widget(index)
        self.stack.setCurrentIndex(index)
        self.set_active_nav(index)
        self.safe_refresh_page(page, show_popup=True)

    def auto_refresh_current_page(self) -> None:
        if QApplication.activeModalWidget():
            return
        index = self.stack.currentIndex()
        if not (0 <= index < len(self.pages)):
            return
        label, page = self.pages[index]
        if label not in {"Início", "Agenda"} or not hasattr(page, "refresh"):
            return
        self.safe_refresh_page(page, show_popup=False)


class DashboardPage(QWidget):
    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any], navigate=None):
        super().__init__()
        self.repo = repo
        self.profile = profile
        self.navigate = navigate
        self.admin_mode_enabled = profile.get("role") == "reception"
        self.professionals: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(18)

        stats_panel = page_card()
        stats_layout = QVBoxLayout(stats_panel)
        stats_layout.setContentsMargins(18, 18, 18, 18)
        self.grid = QGridLayout()
        self.grid.setSpacing(14)
        stats_layout.addLayout(self.grid)

        today_card = page_card()
        today_layout = QVBoxLayout(today_card)
        today_layout.setContentsMargins(18, 18, 18, 18)
        today_header = QHBoxLayout()
        today_title = QLabel("Atendimentos de hoje")
        today_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        self.today_badge = QLabel("")
        self.today_badge.setObjectName("Badge")
        today_header.addWidget(today_title)
        today_header.addStretch()
        today_header.addWidget(self.today_badge)
        self.today_list = QListWidget()
        self.today_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.today_list.setSpacing(8)
        today_layout.addLayout(today_header)
        today_layout.addWidget(self.today_list, 1)
        self.today_list.setStyleSheet("QListWidget { background: transparent; border: none; }")

        layout.addWidget(stats_panel)
        layout.addWidget(today_card, 1)

    def set_admin_mode(self, enabled: bool) -> None:
        if self.profile.get("role") != "admin":
            return
        self.admin_mode_enabled = enabled
        self.refresh()

    def can_view_all_agendas(self) -> bool:
        return self.profile.get("role") == "reception" or (
            self.profile.get("role") == "admin" and self.admin_mode_enabled
        )

    def own_professional_id(self) -> str | None:
        profile_id = self.profile.get("id")
        for professional in self.professionals:
            if professional.get("profile_id") == profile_id:
                return professional.get("id")
        return None

    def dashboard_counts(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "Atendimentos": len(rows),
            "Confirmados": sum(1 for row in rows if row["status"] == "Confirmado"),
            "Atendidos": sum(1 for row in rows if row["status"] == "Atendido"),
            "Faltas": sum(1 for row in rows if row["status"] in {"Falta com aviso", "Falta sem aviso"}),
        }

    def refresh(self) -> None:
        today = date.today()
        clear_layout(self.grid)
        try:
            self.professionals = self.repo.professionals(active_only=True)
            professional_id = None if self.can_view_all_agendas() else self.own_professional_id()
            rows = [] if not self.can_view_all_agendas() and professional_id is None else self.repo.appointments(today, professional_id)
            counts = self.dashboard_counts(rows)
        except Exception:
            counts = {"Atendimentos": 0, "Confirmados": 0, "Atendidos": 0, "Faltas": 0}
            rows = []
        for col, (label, count) in enumerate(counts.items()):
            item = card()
            item.setMinimumHeight(110)
            style_card(item, COLORS["surface"], COLORS["line"])
            item_layout = QVBoxLayout(item)
            item_layout.setContentsMargins(18, 16, 18, 16)
            number = QLabel(str(count))
            number.setObjectName("StatNumber")
            stat_colors = [COLORS["primary"], COLORS["green"], COLORS["blue"], COLORS["red"]]
            number.setStyleSheet(f"color: {stat_colors[col % len(stat_colors)]}; background: transparent;")
            text = QLabel(label)
            text.setObjectName("StatLabel")
            item_layout.addWidget(number)
            item_layout.addWidget(text)
            self.grid.addWidget(item, 0, col)
        self.fill_today_list(rows)

    def fill_today_list(self, rows: list[dict[str, Any]]) -> None:
        self.today_list.clear()
        self.today_badge.setText(f"{len(rows)} atendimentos")
        if not rows:
            item = QListWidgetItem()
            item.setSizeHint(QSize(10, 92))
            self.today_list.addItem(item)
            self.today_list.setItemWidget(item, self.empty_today_card())
            return
        for row in rows[:8]:
            item = QListWidgetItem()
            item.setSizeHint(QSize(10, 92))
            self.today_list.addItem(item)
            self.today_list.setItemWidget(item, self.today_appointment_card(row))

    def empty_today_card(self) -> QWidget:
        frame = QFrame()
        style_card(frame, COLORS["bg2"], COLORS["line"])
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        title = QLabel("Nenhum atendimento hoje")
        title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-weight: 800;")
        subtitle = QLabel("Abra a agenda para criar ou consultar outros dias.")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent; font-size: 12px;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def today_appointment_card(self, row: dict[str, Any]) -> QWidget:
        patient = (row.get("patients") or {}).get("full_name", "Paciente não informado")
        professional = (row.get("professionals") or {}).get("full_name", "Profissional não informado")
        status_text = row.get("status", "")
        bg, fg = STATUS_COLORS.get(status_text, (COLORS["bg2"], COLORS["text"]))
        marker = fg if fg != COLORS["text"] else COLORS["primary"]

        frame = QFrame()
        style_card(frame, COLORS["surface"], COLORS["line"])
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(14)

        time_box = QLabel(f"{row.get('start_time', '')[:5]}\n{row.get('end_time', '')[:5]}")
        time_box.setFixedWidth(76)
        time_box.setMinimumHeight(62)
        time_box.setAlignment(Qt.AlignCenter)
        time_box.setStyleSheet(
            f"background: {COLORS['bg2']}; color: {COLORS['text']}; border: 1px solid {COLORS['line']}; "
            "border-radius: 8px; padding: 5px 7px; font-size: 16px; font-weight: 800;"
        )

        details = QVBoxLayout()
        details.setSpacing(4)
        patient_label = QLabel(patient)
        patient_label.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 15px; font-weight: 800;")
        professional_label = QLabel(f"Profissional: {professional}")
        professional_label.setStyleSheet(f"background: transparent; color: {COLORS['muted']};")
        details.addWidget(patient_label)
        details.addWidget(professional_label)

        status = QLabel(status_text)
        status.setAlignment(Qt.AlignCenter)
        status.setMinimumWidth(130)
        status.setStyleSheet(
            f"background: {bg}; color: {fg}; border: 1px solid {marker}; border-radius: 8px; "
            "padding: 8px 12px; font-weight: 800;"
        )

        layout.addWidget(time_box)
        layout.addLayout(details, 1)
        layout.addWidget(status)
        return frame


class PatientsPage(QWidget):
    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any]):
        super().__init__()
        self.repo = repo
        self.profile = profile
        self.rows: list[dict[str, Any]] = []
        self.build("Pacientes")

    def build(self, title_text: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)
        title = QLabel(title_text)
        title.setObjectName("PageTitle")
        subtitle = QLabel("Pacientes registrados na clínica")
        subtitle.setObjectName("Muted")

        panel = page_card()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 18, 18, 18)
        panel_layout.setSpacing(12)
        card_header = QHBoxLayout()
        self.card_title = QLabel("Pacientes registrados (0)")
        self.card_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar paciente...")
        self.search.textChanged.connect(self.refresh)
        self.search.setFixedWidth(360)
        self.active_only = QCheckBox("Somente ativos")
        self.active_only.setChecked(True)
        self.active_only.setCursor(Qt.PointingHandCursor)
        self.active_only.setMinimumHeight(44)
        self.active_only.setStyleSheet(
            f"""
            QCheckBox {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['line']};
                border-radius: 8px;
                padding: 10px 14px;
                color: {COLORS['text']};
                font-weight: 700;
            }}
            QCheckBox:hover {{
                border: 1px solid {COLORS['primary']};
                background: {COLORS['primary_soft']};
            }}
            QCheckBox::indicator {{
                width: 20px;
                height: 20px;
                border-radius: 5px;
                border: 2px solid {COLORS['primary']};
                background: #FFFFFF;
                margin-right: 8px;
            }}
            QCheckBox::indicator:checked {{
                background: {COLORS['primary']};
                border: 2px solid {COLORS['primary']};
            }}
            """
        )
        self.active_only.stateChanged.connect(self.refresh)
        can_manage_patients = can(self.profile, Permission.MANAGE_PATIENTS)
        self.new_btn = button("Novo paciente")
        self.new_btn.clicked.connect(self.create_record)
        self.new_btn.setVisible(can_manage_patients)
        self.delete_btn = button("Excluir paciente", danger=True)
        self.delete_btn.clicked.connect(self.delete_record)
        self.delete_btn.setVisible(can_manage_patients)
        card_header.addWidget(self.card_title)
        card_header.addStretch()
        card_header.addWidget(self.search)
        card_header.addWidget(self.active_only)
        card_header.addWidget(self.delete_btn)
        card_header.addWidget(self.new_btn)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Nome", "Documento", "Responsável", "Telefone", "Status", "Motivo"])
        full_row_table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(lambda row, col: self.edit_record(row))
        layout.addWidget(title)
        layout.addWidget(subtitle)
        panel_layout.addLayout(card_header)
        panel_layout.addWidget(self.table, 1)
        layout.addWidget(panel, 1)

    def refresh(self) -> None:
        try:
            self.rows = self.repo.patients(self.search.text().strip(), self.active_only.isChecked())
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
        self.card_title.setText(f"Pacientes registrados ({len(self.rows)})")
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            values = [
                row.get("full_name", ""),
                row.get("document", ""),
                row.get("guardian_name", ""),
                row.get("guardian_phone", ""),
                "Ativo" if row.get("is_active") else "Inativo",
                row.get("reason_for_care") or row.get("general_notes", ""),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row_index, col, item)
        widths = [220, 120, 220, 130, 90, 320]
        for col, width in enumerate(widths):
            self.table.setColumnWidth(col, width)

    def current(self, row: int | None = None) -> dict[str, Any] | None:
        idx = self.table.currentRow() if row is None else row
        return self.rows[idx] if 0 <= idx < len(self.rows) else None

    def create_record(self) -> None:
        if not can(self.profile, Permission.MANAGE_PATIENTS):
            return
        dialog = PatientDialog(self, self.repo)
        if dialog.exec():
            try:
                self.repo.save_patient(dialog.values())
                self.refresh()
            except Exception as exc:
                show_error(self, exc)

    def edit_record(self, row: int) -> None:
        record = self.current(row)
        if not record or not can(self.profile, Permission.MANAGE_PATIENTS):
            return
        dialog = PatientDialog(self, self.repo, record)
        if dialog.exec():
            try:
                self.repo.save_patient(dialog.values(), record["id"])
                self.refresh()
            except Exception as exc:
                show_error(self, exc)

    def set_active(self, active: bool) -> None:
        record = self.current()
        if not record or not can(self.profile, Permission.MANAGE_PATIENTS):
            return
        try:
            self.repo.set_patient_active(record["id"], active)
            self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def delete_record(self) -> None:
        record = self.current()
        if not record or not can(self.profile, Permission.MANAGE_PATIENTS):
            return
        answer = QMessageBox.question(
            self,
            "Excluir paciente",
            f"Deseja apagar o paciente {record.get('full_name', '')}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.repo.delete_patient(record["id"])
            self.refresh()
        except Exception as exc:
            show_error(self, exc)


class PatientDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, record: dict[str, Any] | None = None):
        super().__init__(parent)
        self.repo = repo
        self.record = record
        self.setWindowTitle("Paciente")
        self.setMinimumSize(820, 680)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(14)

        header = QFrame()
        style_card(header, COLORS["surface_warm"])
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 14, 18, 14)
        title = QLabel(record.get("full_name", "Novo paciente") if record else "Novo paciente")
        title.setObjectName("PageTitle")
        title.setStyleSheet("font-size: 21px; background: transparent;")
        subtitle = QLabel("Dados pessoais, residência, motivo do atendimento e histórico.")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)

        tabs = QTabWidget()
        personal_tab = QWidget()
        address_tab = QWidget()
        history_tab = QWidget()
        tabs.addTab(personal_tab, "Dados pessoais")
        tabs.addTab(address_tab, "Endereço")
        tabs.addTab(history_tab, "Atendimentos")

        personal_layout = QGridLayout(personal_tab)
        personal_layout.setContentsMargins(12, 16, 12, 12)
        personal_layout.setHorizontalSpacing(14)
        personal_layout.setVerticalSpacing(10)
        self.name = QLineEdit(record.get("full_name", "") if record else "")
        self.guardian = QLineEdit(record.get("guardian_name", "") if record else "")
        self.phone = QLineEdit(record.get("guardian_phone", "") if record else "")
        self.patient_phone = QLineEdit(record.get("patient_phone", "") if record else "")
        self.email = QLineEdit(record.get("email", "") if record else "")
        self.birth_date = QLineEdit(record.get("birth_date", "") if record else "")
        self.birth_date.setPlaceholderText("AAAA-MM-DD")
        self.document = QLineEdit(record.get("document", "") if record else "")
        self.reason = QTextEdit(record.get("reason_for_care", "") if record else "")
        self.reason.setPlaceholderText("Ex.: avaliação inicial, acompanhamento terapêutico, dificuldade de fala...")
        self.notes = QTextEdit(record.get("general_notes", "") if record else "")
        self.reason.setMinimumHeight(120)
        self.notes.setMinimumHeight(120)
        self.active = QCheckBox("Paciente ativo")
        self.active.setChecked(record.get("is_active", True) if record else True)
        self.active.stateChanged.connect(self.update_active_style)
        self.update_active_style()

        personal_layout.addWidget(QLabel("Nome completo"), 0, 0)
        personal_layout.addWidget(self.name, 1, 0)
        personal_layout.addWidget(QLabel("Data de nascimento"), 0, 1)
        personal_layout.addWidget(self.birth_date, 1, 1)
        personal_layout.addWidget(QLabel("Documento"), 0, 2)
        personal_layout.addWidget(self.document, 1, 2)
        personal_layout.addWidget(QLabel("Responsável"), 2, 0)
        personal_layout.addWidget(self.guardian, 3, 0)
        personal_layout.addWidget(QLabel("Telefone responsável"), 2, 1)
        personal_layout.addWidget(self.phone, 3, 1)
        personal_layout.addWidget(QLabel("Telefone paciente"), 2, 2)
        personal_layout.addWidget(self.patient_phone, 3, 2)
        personal_layout.addWidget(QLabel("E-mail"), 4, 0)
        personal_layout.addWidget(self.email, 5, 0, 1, 3)
        personal_layout.addWidget(QLabel("Motivo do atendimento"), 6, 0, 1, 3)
        personal_layout.addWidget(self.reason, 7, 0, 1, 3)
        personal_layout.addWidget(QLabel("Observações gerais"), 8, 0, 1, 3)
        personal_layout.addWidget(self.notes, 9, 0, 1, 3)

        address_layout = QGridLayout(address_tab)
        address_layout.setContentsMargins(12, 16, 12, 12)
        address_layout.setHorizontalSpacing(14)
        address_layout.setVerticalSpacing(10)
        self.zip_code = QLineEdit(record.get("zip_code", "") if record else "")
        self.street = QLineEdit(record.get("street", "") if record else "")
        self.address_number = QLineEdit(record.get("address_number", "") if record else "")
        self.address_complement = QLineEdit(record.get("address_complement", "") if record else "")
        self.neighborhood = QLineEdit(record.get("neighborhood", "") if record else "")
        self.city = QLineEdit(record.get("city", "") if record else "")
        self.state = QLineEdit(record.get("state", "") if record else "")
        self.state.setMaxLength(2)
        address_layout.addWidget(QLabel("CEP"), 0, 0)
        address_layout.addWidget(self.zip_code, 1, 0)
        address_layout.addWidget(QLabel("Rua"), 0, 1)
        address_layout.addWidget(self.street, 1, 1, 1, 2)
        address_layout.addWidget(QLabel("Número"), 2, 0)
        address_layout.addWidget(self.address_number, 3, 0)
        address_layout.addWidget(QLabel("Complemento"), 2, 1)
        address_layout.addWidget(self.address_complement, 3, 1, 1, 2)
        address_layout.addWidget(QLabel("Bairro"), 4, 0)
        address_layout.addWidget(self.neighborhood, 5, 0)
        address_layout.addWidget(QLabel("Cidade"), 4, 1)
        address_layout.addWidget(self.city, 5, 1)
        address_layout.addWidget(QLabel("UF"), 4, 2)
        address_layout.addWidget(self.state, 5, 2)
        address_layout.setRowStretch(6, 1)

        history_layout = QVBoxLayout(history_tab)
        history_layout.setContentsMargins(12, 16, 12, 12)
        self.history_summary = QLabel("")
        self.history_summary.setObjectName("Muted")
        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(["Data", "Horário", "Profissional", "Status", "Situação"])
        table_polish(self.history_table)
        history_layout.addWidget(self.history_summary)
        history_layout.addWidget(self.history_table)
        self.load_patient_history()

        actions = QHBoxLayout()
        status_box = QFrame()
        status_box.setObjectName("PatientStatusBox")
        status_box.setStyleSheet("QFrame#PatientStatusBox { background: transparent; border: none; }")
        status_layout = QVBoxLayout(status_box)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.addWidget(self.active)
        actions.addWidget(status_box)
        actions.addStretch()
        save = button("Salvar")
        save.clicked.connect(self.accept)
        actions.addWidget(save)
        root.addWidget(header)
        root.addWidget(tabs, 1)
        root.addLayout(actions)

    def update_active_style(self) -> None:
        if self.active.isChecked():
            self.active.setText("Paciente ativo")
            self.active.setStyleSheet(
                f"QCheckBox {{ background: {COLORS['green_card']}; color: {COLORS['green']}; "
                "border: 1px solid #B9D9B8; border-radius: 8px; padding: 12px 18px; font-weight: 800; }}"
                "QCheckBox::indicator { width: 18px; height: 18px; }"
            )
        else:
            self.active.setText("Paciente inativo")
            self.active.setStyleSheet(
                f"QCheckBox {{ background: #FFE5E3; color: {COLORS['red']}; "
                "border: 1px solid #F2B3AE; border-radius: 8px; padding: 12px 18px; font-weight: 800; }}"
                "QCheckBox::indicator { width: 18px; height: 18px; }"
            )

    def load_patient_history(self) -> None:
        if not self.record:
            self.history_summary.setText("Salve o paciente para visualizar os atendimentos.")
            return
        try:
            rows = self.repo.patient_appointments(self.record["id"])
        except Exception as exc:
            self.history_summary.setText(f"Não foi possível carregar os atendimentos: {exc}")
            rows = []
        completed = sum(1 for row in rows if row.get("status") == "Atendido")
        pending = sum(1 for row in rows if row.get("status") not in {"Atendido", "Cancelado"})
        self.history_summary.setText(f"{completed} atendimentos realizados | {pending} pendentes")
        self.history_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            professional = (row.get("professionals") or {}).get("full_name", "")
            status = row.get("status", "")
            situation = "Realizado" if status == "Atendido" else "Pendente" if status != "Cancelado" else "Cancelado"
            values = [
                row.get("appointment_date", ""),
                f"{str(row.get('start_time', ''))[:5]} - {str(row.get('end_time', ''))[:5]}",
                professional,
                status,
                situation,
            ]
            for col, value in enumerate(values):
                self.history_table.setItem(row_index, col, QTableWidgetItem(str(value or "")))
        self.history_table.resizeColumnsToContents()

    def values(self) -> dict[str, Any]:
        return {
            "full_name": self.name.text().strip(),
            "guardian_name": self.guardian.text().strip(),
            "guardian_phone": self.phone.text().strip(),
            "patient_phone": self.patient_phone.text().strip(),
            "email": self.email.text().strip(),
            "birth_date": self.birth_date.text().strip() or None,
            "document": self.document.text().strip(),
            "reason_for_care": self.reason.toPlainText().strip(),
            "general_notes": self.notes.toPlainText().strip(),
            "zip_code": self.zip_code.text().strip(),
            "street": self.street.text().strip(),
            "address_number": self.address_number.text().strip(),
            "address_complement": self.address_complement.text().strip(),
            "neighborhood": self.neighborhood.text().strip(),
            "city": self.city.text().strip(),
            "state": self.state.text().strip().upper(),
            "is_active": self.active.isChecked(),
        }


class SessionExportPage(QWidget):
    def __init__(self, repo: SupabaseRepository):
        super().__init__()
        self.repo = repo
        self.patients: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)

        title = QLabel("Exportar sessões")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Reúna observações por paciente para pareceres e relatórios.")
        subtitle.setObjectName("Muted")

        filters_panel = page_card()
        filters = QHBoxLayout(filters_panel)
        filters.setContentsMargins(18, 14, 18, 14)
        filters.setSpacing(14)
        self.patient = QComboBox()
        self.patient.setMinimumWidth(320)
        self.start_date = QDateEdit(QDate.currentDate().addMonths(-1))
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("dd/MM/yyyy")
        self.start_date.setMinimumWidth(150)
        self.end_date = QDateEdit(QDate.currentDate())
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("dd/MM/yyyy")
        self.end_date.setMinimumWidth(150)
        load_btn = button("Carregar sessões")
        load_btn.clicked.connect(self.load_sessions)
        filters.addLayout(self.filter_field("Paciente", self.patient))
        filters.addLayout(self.filter_field("Início", self.start_date))
        filters.addLayout(self.filter_field("Fim", self.end_date))
        filters.addStretch()
        filters.addWidget(load_btn, 0, Qt.AlignBottom)

        content = QHBoxLayout()
        content.setSpacing(16)

        sessions_card = page_card()
        sessions_layout = QVBoxLayout(sessions_card)
        sessions_layout.setContentsMargins(18, 18, 18, 18)
        sessions_header = QHBoxLayout()
        sessions_title = QLabel("Sessões encontradas")
        sessions_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        self.sessions_badge = QLabel("0 sessões")
        self.sessions_badge.setObjectName("Badge")
        sessions_header.addWidget(sessions_title)
        sessions_header.addStretch()
        sessions_header.addWidget(self.sessions_badge)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Usar", "Data", "Horário", "Profissional", "Status"])
        table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self.update_preview)
        sessions_layout.addLayout(sessions_header)
        sessions_layout.addWidget(self.table, 1)

        preview_card = page_card()
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(18, 18, 18, 18)
        preview_title = QLabel("Texto para relatório")
        preview_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        self.preview = QTextEdit()
        self.preview.setPlaceholderText("Selecione um paciente, carregue as sessões e marque as observações que deseja exportar.")
        actions = QHBoxLayout()
        copy_btn = button("Copiar texto", secondary=True)
        copy_btn.clicked.connect(self.copy_text)
        save_btn = button("Salvar TXT")
        save_btn.clicked.connect(self.save_txt)
        actions.addWidget(copy_btn)
        actions.addWidget(save_btn)
        preview_layout.addWidget(preview_title)
        preview_layout.addWidget(self.preview, 1)
        preview_layout.addLayout(actions)

        content.addWidget(sessions_card, 1)
        content.addWidget(preview_card, 1)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(filters_panel)
        layout.addLayout(content, 1)
        self.load_patients()

    def filter_field(self, label: str, widget: QWidget) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(6)
        text = QLabel(label)
        text.setObjectName("Muted")
        text.setStyleSheet("background: transparent;")
        box.addWidget(text)
        box.addWidget(widget)
        return box

    def load_patients(self) -> None:
        self.patient.clear()
        try:
            self.patients = self.repo.patients(active_only=False)
            for patient in self.patients:
                self.patient.addItem(patient.get("full_name", ""), patient.get("id"))
        except Exception as exc:
            show_error(self, exc)
            self.patients = []

    def refresh(self) -> None:
        if not self.patients:
            self.load_patients()

    def load_sessions(self) -> None:
        patient_id = self.patient.currentData()
        if not patient_id:
            show_error(self, AppError("Selecione um paciente antes de carregar as sessões."))
            return
        start = self.start_date.date().toPython()
        end = self.end_date.date().toPython()
        if end < start:
            show_error(self, AppError("A data final deve ser maior ou igual a inicial."))
            return
        try:
            self.rows = self.repo.patient_session_exports(patient_id, start, end)
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
        self.fill_table()
        self.update_preview()

    def fill_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            use_item = QTableWidgetItem()
            use_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            use_item.setCheckState(Qt.Checked)
            self.table.setItem(row_index, 0, use_item)
            values = [
                self.format_date(row.get("appointment_date", "")),
                f"{str(row.get('start_time', ''))[:5]} - {str(row.get('end_time', ''))[:5]}",
                (row.get("professionals") or {}).get("full_name", ""),
                row.get("status", ""),
            ]
            for col, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(row_index, col, item)
        self.table.blockSignals(False)
        self.sessions_badge.setText(f"{len(self.rows)} sessões")
        self.table.resizeColumnsToContents()

    def selected_rows(self) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for row_index, row in enumerate(self.rows):
            item = self.table.item(row_index, 0)
            if item and item.checkState() == Qt.Checked:
                selected.append(row)
        return selected

    def session_note_text(self, row: dict[str, Any]) -> str:
        notes = row.get("session_notes") or []
        if isinstance(notes, list):
            return "\n".join(note.get("note", "") for note in notes if note.get("note")).strip()
        return str(notes.get("note", "") if isinstance(notes, dict) else "").strip()

    def build_export_text(self) -> str:
        patient_name = self.patient.currentText()
        rows = self.selected_rows()
        lines = [
            f"Paciente: {patient_name}",
            f"Período: {self.start_date.date().toString('dd/MM/yyyy')} a {self.end_date.date().toString('dd/MM/yyyy')}",
            "",
            "Sessões selecionadas",
            "",
        ]
        if not rows:
            lines.append("Nenhuma sessão selecionada.")
            return "\n".join(lines)
        for row in rows:
            professional = (row.get("professionals") or {}).get("full_name", "Não informado")
            note = self.session_note_text(row) or "Sem relatório registrado."
            lines.extend(
                [
                    f"{self.format_date(row.get('appointment_date', ''))} - {str(row.get('start_time', ''))[:5]} às {str(row.get('end_time', ''))[:5]}",
                    f"Profissional: {professional}",
                    f"Status: {row.get('status', '')}",
                    "Relatório da sessão:",
                    note,
                    "",
                ]
            )
        return "\n".join(lines).strip()

    def update_preview(self) -> None:
        if not self.rows:
            self.preview.clear()
            return
        self.preview.setPlainText(self.build_export_text())

    def copy_text(self) -> None:
        QApplication.clipboard().setText(self.preview.toPlainText())
        QMessageBox.information(self, "Exportar sessões", "Texto copiado.")

    def save_txt(self) -> None:
        text = self.preview.toPlainText().strip()
        if not text:
            show_error(self, AppError("Carregue as sessões antes de salvar."))
            return
        filename = f"sessões-{self.patient.currentText().replace(' ', '-').lower()}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "Salvar sessões", filename, "Texto (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(text, encoding="utf-8")
            QMessageBox.information(self, "Exportar sessões", "Arquivo salvo com sucesso.")
        except Exception as exc:
            show_error(self, exc)

    def format_date(self, value: str) -> str:
        try:
            return date.fromisoformat(str(value)).strftime("%d/%m/%Y")
        except ValueError:
            return str(value or "")


class ProfessionalsPage(QWidget):
    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any]):
        super().__init__()
        self.repo = repo
        self.profile = profile
        self.rows: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)
        title = QLabel("Profissionais")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Profissionais vinculados a usuários do tipo Profissional")
        subtitle.setObjectName("Muted")
        panel = page_card()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 18, 18, 18)
        panel_layout.setSpacing(12)
        actions = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por nome")
        self.search.textChanged.connect(self.refresh)
        self.active_only = QCheckBox("Somente ativos")
        self.active_only.setChecked(True)
        self.active_only.stateChanged.connect(self.refresh)
        self.new_btn = button("Vincular profissional")
        self.new_btn.clicked.connect(self.create_record)
        actions.addWidget(self.search)
        actions.addWidget(self.active_only)
        actions.addWidget(self.new_btn)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Nome", "Especialidade", "Telefone", "Status"])
        table_polish(self.table)
        self.table.cellDoubleClicked.connect(lambda row, col: self.edit_record(row))
        row_actions = QHBoxLayout()
        edit = button("Editar", secondary=True)
        edit.clicked.connect(lambda: self.edit_record(self.table.currentRow()))
        inactive = button("Inativar", danger=True)
        inactive.clicked.connect(lambda: self.set_active(False))
        row_actions.addWidget(edit)
        row_actions.addWidget(inactive)
        row_actions.addStretch()
        layout.addWidget(title)
        layout.addWidget(subtitle)
        panel_layout.addLayout(actions)
        panel_layout.addWidget(self.table, 1)
        panel_layout.addLayout(row_actions)
        layout.addWidget(panel, 1)

    def refresh(self) -> None:
        try:
            self.rows = self.repo.professionals(self.search.text().strip(), self.active_only.isChecked())
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            values = [row.get("full_name", ""), row.get("specialty", ""), row.get("phone", ""), "Ativo" if row.get("is_active") else "Inativo"]
            for col, value in enumerate(values):
                self.table.setItem(row_index, col, QTableWidgetItem(str(value or "")))
        self.table.resizeColumnsToContents()

    def current(self, row: int | None = None) -> dict[str, Any] | None:
        idx = self.table.currentRow() if row is None else row
        return self.rows[idx] if 0 <= idx < len(self.rows) else None

    def create_record(self) -> None:
        dialog = ProfessionalDialog(self, self.repo)
        if dialog.exec():
            try:
                self.repo.save_professional(dialog.values())
                self.refresh()
            except Exception as exc:
                show_error(self, exc)

    def edit_record(self, row: int) -> None:
        record = self.current(row)
        if not record:
            return
        dialog = ProfessionalDialog(self, self.repo, record)
        if dialog.exec():
            try:
                self.repo.save_professional(dialog.values(), record["id"])
                self.refresh()
            except Exception as exc:
                show_error(self, exc)

    def set_active(self, active: bool) -> None:
        record = self.current()
        if not record:
            return
        try:
            self.repo.set_professional_active(record["id"], active)
            self.refresh()
        except Exception as exc:
            show_error(self, exc)


class ProfessionalDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, record: dict[str, Any] | None = None):
        super().__init__(parent)
        self.repo = repo
        self.profiles: list[dict[str, Any]] = []
        self.setWindowTitle("Vincular profissional" if record is None else "Profissional")
        layout = QFormLayout(self)
        self.name = QLineEdit(record.get("full_name", "") if record else "")
        self.name.setReadOnly(record is None)
        self.specialty = QComboBox()
        self.specialty.addItems(SPECIALTIES)
        if record and record.get("specialty") in SPECIALTIES:
            self.specialty.setCurrentText(record["specialty"])
        self.phone = QLineEdit(record.get("phone", "") if record else "")
        self.profile = QComboBox()
        try:
            self.profiles = repo.available_professional_profiles(record.get("profile_id") if record else None)
            for profile in self.profiles:
                self.profile.addItem(profile["full_name"], profile["id"])
        except Exception:
            pass
        if not self.profiles:
            self.profile.addItem("Nenhum usuário profissional disponível", None)
        self.profile.currentIndexChanged.connect(self.sync_name_from_profile)
        if record and record.get("profile_id"):
            idx = self.profile.findData(record["profile_id"])
            if idx >= 0:
                self.profile.setCurrentIndex(idx)
        if record is None:
            self.sync_name_from_profile()
        self.active = QCheckBox("Ativo")
        self.active.setChecked(record.get("is_active", True) if record else True)
        save = button("Salvar")
        save.clicked.connect(self.accept)
        layout.addRow("Nome completo", self.name)
        layout.addRow("Especialidade", self.specialty)
        layout.addRow("Telefone", self.phone)
        layout.addRow("Usuário", self.profile)
        layout.addRow("", self.active)
        layout.addRow("", save)

    def sync_name_from_profile(self) -> None:
        profile_id = self.profile.currentData()
        profile = next((item for item in self.profiles if item.get("id") == profile_id), None)
        if profile:
            self.name.setText(profile.get("full_name", ""))

    def values(self) -> dict[str, Any]:
        if not self.profile.currentData():
            raise AppError("Crie primeiro um usuário com permissão Profissional em Configurações.")
        return {
            "full_name": self.name.text().strip(),
            "specialty": self.specialty.currentText(),
            "phone": self.phone.text().strip(),
            "profile_id": self.profile.currentData(),
            "is_active": self.active.isChecked(),
        }


class AgendaPage(QWidget):
    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any]):
        super().__init__()
        self.repo = repo
        self.profile = profile
        self.admin_mode_enabled = profile.get("role") == "reception"
        self.rows: list[dict[str, Any]] = []
        self.professionals: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(18)
        title = QLabel("Agenda")
        title.setObjectName("PageTitle")

        top_grid = QGridLayout()
        top_grid.setSpacing(18)

        filters_panel = page_card()
        filters_panel.setMinimumWidth(370)
        filters = QVBoxLayout(filters_panel)
        filters.setContentsMargins(18, 18, 18, 20)
        filters.setSpacing(12)
        filter_title = QLabel("Selecionar data")
        filter_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.dateChanged.connect(self.refresh)
        self.date_edit.hide()
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(False)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.calendar.setHorizontalHeaderFormat(QCalendarWidget.ShortDayNames)
        self.calendar.setSelectedDate(QDate.currentDate())
        self.selected_calendar_date = QDate.currentDate()
        self.calendar.selectionChanged.connect(self.sync_calendar_date)
        self.calendar.setStyleSheet(
            f"QCalendarWidget {{ background: {COLORS['surface']}; border: 1px solid {COLORS['line']}; border-radius: 8px; }}"
            f"QCalendarWidget QWidget {{ alternate-background-color: {COLORS['bg2']}; }}"
            f"QCalendarWidget QToolButton {{ color: {COLORS['text']}; background: transparent; font-weight: 800; padding: 7px; }}"
            f"QCalendarWidget QAbstractItemView {{ background: {COLORS['surface']}; selection-background-color: {COLORS['primary']}; "
            "selection-color: #FFFFFF; outline: 0; border: none; font-size: 13px; }}"
        )
        today = button("Hoje", secondary=True)
        today.setMinimumHeight(42)
        today.clicked.connect(self.go_today)
        self.professional_filter = QComboBox()
        self.professional_filter.setObjectName("AgendaProfessional")
        self.professional_filter.setMinimumHeight(46)
        self.professional_filter.currentIndexChanged.connect(self.refresh)
        self.new_appointment = button("Novo atendimento")
        self.new_appointment.setMinimumHeight(40)
        self.new_appointment.clicked.connect(self.create_appointment)
        self.new_recurring = button("Nova agenda fixa", secondary=True)
        self.new_recurring.setMinimumHeight(40)
        self.new_recurring.clicked.connect(self.create_recurring)
        self.bulk_edit = button("Alterar atendimentos", secondary=True)
        self.bulk_edit.setMinimumHeight(40)
        self.bulk_edit.clicked.connect(self.edit_appointments_bulk)
        allowed = can(profile, Permission.MANAGE_AGENDA)
        self.new_appointment.setEnabled(allowed)
        self.new_recurring.setEnabled(allowed)
        self.bulk_edit.setVisible(allowed)
        self.day_stat_labels: dict[str, QLabel] = {}
        stats_panel = QFrame()
        style_card(stats_panel, COLORS["bg2"], COLORS["line"])
        stats_layout = QGridLayout(stats_panel)
        stats_layout.setContentsMargins(12, 10, 12, 10)
        stats_layout.setHorizontalSpacing(12)
        stats_layout.setVerticalSpacing(8)
        for index, (key, label) in enumerate(
            [
                ("total", "Total"),
                ("confirmed", "Confirmados"),
                ("completed", "Atendidos"),
                ("pending", "Pendentes"),
            ]
        ):
            item = QVBoxLayout()
            number = QLabel("0")
            number.setAlignment(Qt.AlignCenter)
            number.setStyleSheet(
                f"background: transparent; color: {COLORS['primary']}; font-size: 20px; font-weight: 900;"
            )
            text = QLabel(label)
            text.setAlignment(Qt.AlignCenter)
            text.setStyleSheet(f"background: transparent; color: {COLORS['muted']}; font-size: 11px; font-weight: 700;")
            item.addWidget(number)
            item.addWidget(text)
            stats_layout.addLayout(item, index // 2, index % 2)
            self.day_stat_labels[key] = number
        professional_label = QLabel("Profissional")
        professional_label.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-weight: 700;")
        filters.addWidget(filter_title)
        filters.addWidget(self.calendar)
        filters.addWidget(today)
        filters.addWidget(professional_label)
        filters.addWidget(self.professional_filter)
        filters.addWidget(stats_panel)
        filters.addStretch()

        canvas = page_card()
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(18, 18, 18, 18)
        canvas_layout.setSpacing(12)
        canvas_header = QHBoxLayout()
        canvas_title_box = QVBoxLayout()
        canvas_title = QLabel("Atendimentos do dia")
        canvas_title.setObjectName("PageTitle")
        canvas_title.setStyleSheet("font-size: 18px; background: transparent;")
        self.canvas_subtitle = QLabel("")
        self.canvas_subtitle.setObjectName("Muted")
        self.canvas_subtitle.setStyleSheet("background: transparent;")
        canvas_title_box.addWidget(canvas_title)
        canvas_title_box.addWidget(self.canvas_subtitle)
        self.summary_badge = QLabel("")
        self.summary_badge.setObjectName("Badge")
        self.summary_badge.setStyleSheet(
            f"background: {COLORS['primary_soft']}; color: {COLORS['primary']}; "
            "border-radius: 8px; padding: 7px 12px; font-weight: 800;"
        )
        canvas_header.addLayout(canvas_title_box)
        canvas_header.addStretch()
        canvas_header.addWidget(self.new_appointment)
        canvas_header.addWidget(self.new_recurring)
        canvas_header.addWidget(self.bulk_edit)
        self.list = QListWidget()
        self.list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.list.setSpacing(8)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: 0; }"
            "QListWidget::item { border: none; margin: 0px; padding: 0px; }"
        )
        self.list.itemDoubleClicked.connect(self.open_selected)
        canvas_layout.addLayout(canvas_header)
        canvas_layout.addWidget(self.summary_badge, 0, Qt.AlignLeft)
        canvas_layout.addWidget(self.list, 1)

        layout.addWidget(title)
        top_grid.addWidget(filters_panel, 0, 0)
        top_grid.addWidget(canvas, 0, 1)
        top_grid.setColumnStretch(0, 1)
        top_grid.setColumnStretch(1, 2)
        layout.addLayout(top_grid, 1)
        self.professionals_loaded = False
        self.update_calendar_selection_style()

    def sync_calendar_date(self) -> None:
        self.calendar.setDateTextFormat(self.selected_calendar_date, QTextCharFormat())
        self.selected_calendar_date = self.calendar.selectedDate()
        self.date_edit.setDate(self.calendar.selectedDate())
        self.update_calendar_selection_style()

    def update_calendar_selection_style(self) -> None:
        selected_format = QTextCharFormat()
        selected_format.setBackground(QBrush(QColor(COLORS["primary"])))
        selected_format.setForeground(QBrush(QColor("#FFFFFF")))
        selected_format.setFontWeight(800)
        self.calendar.setDateTextFormat(self.selected_calendar_date, selected_format)

    def shift_date(self, days: int) -> None:
        self.date_edit.setDate(self.date_edit.date().addDays(days))

    def go_today(self) -> None:
        self.calendar.setSelectedDate(QDate.currentDate())
        self.date_edit.setDate(QDate.currentDate())

    def set_admin_mode(self, enabled: bool) -> None:
        if self.profile.get("role") != "admin":
            return
        self.admin_mode_enabled = enabled
        self.professionals_loaded = False
        self.load_professionals()
        self.refresh()

    def can_view_all_agendas(self) -> bool:
        return self.profile.get("role") == "reception" or (
            self.profile.get("role") == "admin" and self.admin_mode_enabled
        )

    def own_professionals(self) -> list[dict[str, Any]]:
        profile_id = self.profile.get("id")
        return [professional for professional in self.professionals if professional.get("profile_id") == profile_id]

    def load_professionals(self) -> None:
        self.professional_filter.blockSignals(True)
        self.professional_filter.clear()
        try:
            self.professionals = self.repo.professionals(active_only=True)
            self.professionals_loaded = True
        except Exception as exc:
            log_exception("Erro ao carregar profissionais da agenda", exc)
            self.professionals = []
            self.professionals_loaded = False
        if self.can_view_all_agendas():
            self.professional_filter.addItem("Todos os profissionais", None)
            visible_professionals = self.professionals
        else:
            visible_professionals = self.own_professionals()
            if not visible_professionals:
                self.professional_filter.addItem("Nenhuma agenda vinculada", None)
        for professional in visible_professionals:
            self.professional_filter.addItem(professional["full_name"], professional["id"])
        self.professional_filter.setEnabled(self.can_view_all_agendas() or len(visible_professionals) > 1)
        self.professional_filter.blockSignals(False)

    def refresh(self) -> None:
        if not self.professionals_loaded:
            self.load_professionals()
        selected = self.date_edit.date().toPython()
        professional_id = self.professional_filter.currentData()
        try:
            if not self.can_view_all_agendas() and professional_id is None:
                self.rows = []
            else:
                self.rows = self.repo.appointments(selected, professional_id)
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
        confirmed = sum(1 for row in self.rows if row.get("status") == "Confirmado")
        completed = sum(1 for row in self.rows if row.get("status") == "Atendido")
        pending = sum(1 for row in self.rows if row.get("status") not in {"Atendido", "Cancelado"})
        professional_text = self.professional_filter.currentText()
        self.canvas_subtitle.setText(f"{selected.strftime('%d/%m/%Y')} | {professional_text}")
        self.summary_badge.setText(f"{len(self.rows)} total  |  {confirmed} confirmados  |  {completed} atendidos  |  {pending} pendentes")
        self.day_stat_labels["total"].setText(str(len(self.rows)))
        self.day_stat_labels["confirmed"].setText(str(confirmed))
        self.day_stat_labels["completed"].setText(str(completed))
        self.day_stat_labels["pending"].setText(str(pending))
        self.list.clear()
        if not self.rows:
            empty = QListWidgetItem()
            empty.setSizeHint(QSize(10, 150))
            self.list.addItem(empty)
            self.list.setItemWidget(empty, self.empty_agenda_card())
            return
        for row in self.rows:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, row)
            item.setSizeHint(QSize(10, 96))
            self.list.addItem(item)
            self.list.setItemWidget(item, self.appointment_card(row))

    def empty_agenda_card(self) -> QWidget:
        frame = QFrame()
        style_card(frame, COLORS["surface_warm"], COLORS["line"])
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 18, 18, 18)
        title = QLabel("Nenhum atendimento nesta data")
        title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        subtitle = QLabel("Use os filtros acima ou crie um novo atendimento.")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def appointment_card(self, row: dict[str, Any]) -> QWidget:
        patient = (row.get("patients") or {}).get("full_name", "")
        professional = (row.get("professionals") or {}).get("full_name", "")
        bg, fg = STATUS_COLORS.get(row["status"], ("#FFFFFF", COLORS["text"]))
        frame = QFrame()
        border_color = fg if fg != COLORS["text"] else COLORS["primary"]
        style_card(frame, COLORS["surface"], COLORS["line"])
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(16)

        marker = QFrame()
        marker.setFixedWidth(5)
        marker.setStyleSheet(f"background: {border_color}; border-radius: 2px;")

        time_label = QLabel(f"{row['start_time'][:5]}\n{row['end_time'][:5]}")
        time_label.setMinimumWidth(66)
        time_label.setAlignment(Qt.AlignCenter)
        time_label.setStyleSheet(
            f"background: {COLORS['bg2']}; color: {COLORS['text']}; border: 1px solid {COLORS['line']}; "
            "border-radius: 8px; padding: 8px; font-size: 18px; font-weight: 800;"
        )
        details = QVBoxLayout()
        patient_label = QLabel(patient or "Paciente não informado")
        patient_label.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 16px; font-weight: 800;")
        professional_label = QLabel(professional or "Profissional não informado")
        professional_label.setStyleSheet(f"background: transparent; color: {COLORS['muted']};")
        details.addWidget(patient_label)
        details.addWidget(professional_label)

        if can(self.profile, Permission.CHANGE_STATUS):
            status = QComboBox()
            status.addItems(STATUSES)
            status.setCurrentText(row["status"])
            status.setCursor(Qt.PointingHandCursor)
            status.setMinimumWidth(164)
            status.setStyleSheet(
                f"QComboBox {{ background: {bg}; color: {fg}; border: 1px solid {border_color}; "
                "border-radius: 8px; padding: 7px 28px 7px 10px; font-weight: 800; }}"
                f"QComboBox::drop-down {{ border: none; width: 24px; }}"
                f"QComboBox QAbstractItemView {{ background: {COLORS['surface']}; color: {COLORS['text']}; "
                f"selection-background-color: {COLORS['primary_soft']}; selection-color: {COLORS['primary']}; }}"
            )
            status.currentTextChanged.connect(lambda value, appointment_id=row["id"]: self.change_status(appointment_id, value))
        else:
            status = QLabel(row["status"])
            status.setAlignment(Qt.AlignCenter)
            status.setMinimumWidth(132)
            status.setStyleSheet(
                f"background: {bg}; color: {fg}; border: 1px solid {border_color}; "
                "border-radius: 8px; padding: 7px 10px; font-weight: 800;"
            )
        layout.addWidget(marker)
        layout.addWidget(time_label)
        layout.addLayout(details, 1)
        layout.addWidget(status)
        return frame

    def change_status(self, appointment_id: str, status: str) -> None:
        try:
            self.repo.update_appointment_status(appointment_id, status)
            self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def selected_row(self) -> dict[str, Any] | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def open_selected(self) -> None:
        row = self.selected_row()
        if not row:
            return
        dialog = AppointmentDetailsDialog(self, self.repo, self.profile, row)
        if dialog.exec():
            self.refresh()

    def create_appointment(self) -> None:
        dialog = AppointmentDialog(self, self.repo, self.date_edit.date().toPython())
        if dialog.exec():
            try:
                self.repo.save_appointment(dialog.values())
                self.refresh()
            except Exception as exc:
                show_error(self, exc)

    def create_recurring(self) -> None:
        dialog = RecurringDialog(self, self.repo)
        if dialog.exec():
            try:
                count = self.repo.save_recurring_schedule(dialog.values())
                QMessageBox.information(self, "Agenda fixa", f"{count} atendimentos gerados.")
                self.refresh()
            except Exception as exc:
                show_error(self, exc)

    def edit_appointments_bulk(self) -> None:
        dialog = BulkAppointmentEditDialog(self, self.repo)
        if dialog.exec():
            self.refresh()


class AppointmentDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, selected_date: date):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Atendimento")
        self.patients = repo.patients(active_only=True)
        self.professionals = repo.professionals(active_only=True)
        layout = QFormLayout(self)
        self.patient = QComboBox()
        for item in self.patients:
            self.patient.addItem(item["full_name"], item["id"])
        self.professional = QComboBox()
        for item in self.professionals:
            self.professional.addItem(item["full_name"], item["id"])
        self.date_edit = QDateEdit(QDate(selected_date.year, selected_date.month, selected_date.day))
        self.date_edit.setCalendarPopup(True)
        self.start = time_field(8, 0)
        self.end = time_field(9, 0)
        self.status = QComboBox()
        self.status.addItems(STATUSES)
        self.consultation_fee = QLineEdit()
        self.consultation_fee.setPlaceholderText("Ex.: 150,00")
        save = button("Salvar")
        save.clicked.connect(self.accept)
        layout.addRow("Paciente", self.patient)
        layout.addRow("Profissional", self.professional)
        layout.addRow("Data", self.date_edit)
        layout.addRow("Início", self.start)
        layout.addRow("Fim", self.end)
        layout.addRow("Status", self.status)
        layout.addRow("Valor da sessão", self.consultation_fee)
        layout.addRow("", save)

    def values(self) -> dict[str, Any]:
        return {
            "patient_id": self.patient.currentData(),
            "professional_id": self.professional.currentData(),
            "appointment_date": self.date_edit.date().toPython().isoformat(),
            "start_time": self.start.time().toString("HH:mm:ss"),
            "end_time": self.end.time().toString("HH:mm:ss"),
            "status": self.status.currentText(),
            "consultation_fee": self.consultation_fee.text(),
        }


class RecurringDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Agenda fixa")
        self.patients = repo.patients(active_only=True)
        self.professionals = repo.professionals(active_only=True)
        layout = QFormLayout(self)
        self.patient = QComboBox()
        for item in self.patients:
            self.patient.addItem(item["full_name"], item["id"])
        self.professional = QComboBox()
        for item in self.professionals:
            self.professional.addItem(item["full_name"], item["id"])
        self.weekday = QComboBox()
        for idx, label in enumerate(["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]):
            self.weekday.addItem(label, idx)
        self.start = time_field(8, 0)
        self.end = time_field(9, 0)
        today = QDate.currentDate()
        self.start_date = QDateEdit(today)
        self.start_date.setCalendarPopup(True)
        self.end_date = QDateEdit(today.addMonths(1))
        self.end_date.setCalendarPopup(True)
        self.consultation_fee = QLineEdit()
        self.consultation_fee.setPlaceholderText("Ex.: 150,00")
        save = button("Gerar atendimentos")
        save.clicked.connect(self.accept)
        layout.addRow("Paciente", self.patient)
        layout.addRow("Profissional", self.professional)
        layout.addRow("Dia da semana", self.weekday)
        layout.addRow("Início", self.start)
        layout.addRow("Fim", self.end)
        layout.addRow("Valor da sessão", self.consultation_fee)
        layout.addRow("Data inicial", self.start_date)
        layout.addRow("Data final", self.end_date)
        layout.addRow("", save)

    def values(self) -> dict[str, Any]:
        return {
            "patient_id": self.patient.currentData(),
            "professional_id": self.professional.currentData(),
            "weekday": self.weekday.currentData(),
            "start_time": self.start.time().toString("HH:mm:ss"),
            "end_time": self.end.time().toString("HH:mm:ss"),
            "start_date": self.start_date.date().toPython().isoformat(),
            "end_date": self.end_date.date().toPython().isoformat(),
            "consultation_fee": self.consultation_fee.text(),
            "is_active": True,
        }


class BulkAppointmentEditDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository):
        super().__init__(parent)
        self.repo = repo
        self.rows: list[dict[str, Any]] = []
        self.appointment_checks: list[QCheckBox] = []
        self.updating_checks = False
        self.setWindowTitle("Alterar atendimentos em lote")
        self.setMinimumSize(980, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)

        title = QLabel("Selecionar atendimentos")
        title.setObjectName("PageTitle")
        hint = QLabel("Filtre, selecione uma ou mais linhas e escolha as alterações que deseja aplicar.")
        hint.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(hint)

        filters = QGridLayout()
        today = QDate.currentDate()
        self.start_date = QDateEdit(today.addMonths(-1))
        self.end_date = QDateEdit(today.addMonths(1))
        self.start_date.setCalendarPopup(True)
        self.end_date.setCalendarPopup(True)
        self.weekday_filter = QComboBox()
        self.weekday_filter.addItem("Todos os dias", None)
        for weekday, label in enumerate(
            ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
        ):
            self.weekday_filter.addItem(label, weekday)
        self.professional = QComboBox()
        self.professional.addItem("Todos os profissionais", None)
        professionals = repo.professionals(active_only=False)
        for item in professionals:
            self.professional.addItem(item["full_name"], item["id"])
        self.patient = QComboBox()
        self.patient.addItem("Todos os pacientes", None)
        patients = repo.patients(active_only=False)
        for item in patients:
            self.patient.addItem(item["full_name"], item["id"])
        search = button("Buscar", secondary=True)
        search.clicked.connect(self.load_rows)
        for column, (label, widget) in enumerate(
            [("Data inicial", self.start_date), ("Data final", self.end_date), ("Dia da semana", self.weekday_filter),
             ("Profissional", self.professional), ("Paciente", self.patient)]
        ):
            filters.addWidget(QLabel(label), 0, column)
            filters.addWidget(widget, 1, column)
        filters.addWidget(search, 1, 5)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Selecionar", "Data", "Horário", "Paciente", "Profissional", "Status", "Valor"])
        table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionMode(QAbstractItemView.MultiSelection)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        check_icon = resource_path("check_white.svg").as_posix()
        self.table.setStyleSheet(
            self.table.styleSheet()
            + f"""
            QTableWidget::item {{ border: none; border-radius: 0px; padding: 0px; }}
            QTableWidget::item:selected {{ background: {COLORS['primary_soft']}; color: {COLORS['text']}; }}
            QCheckBox {{ background: transparent; spacing: 0px; }}
            QCheckBox::indicator {{
                width: 22px; height: 22px; border-radius: 5px;
                border: 2px solid {COLORS['primary']}; background: #FFFFFF;
            }}
            QCheckBox::indicator:checked {{
                background: {COLORS['primary']}; border: 2px solid {COLORS['primary']};
                image: url("{check_icon}");
            }}
            """
        )
        layout.addWidget(self.table, 1)

        selection_bar = QHBoxLayout()
        self.result_label = QLabel("0 atendimento(s)")
        self.result_label.setObjectName("Muted")
        select_all = button("Selecionar todos", secondary=True)
        select_all.clicked.connect(self.select_all)
        clear_selection = button("Limpar seleção", secondary=True)
        clear_selection.clicked.connect(self.clear_selection)
        selection_bar.addWidget(self.result_label)
        selection_bar.addStretch()
        selection_bar.addWidget(select_all)
        selection_bar.addWidget(clear_selection)
        layout.addLayout(selection_bar)

        changes = page_card()
        changes_layout = QGridLayout(changes)
        changes_layout.setContentsMargins(16, 14, 16, 14)
        self.start_time = QLineEdit()
        self.start_time.setInputMask("00:00;_")
        self.start_time.setPlaceholderText("HH:MM")
        self.start_time.setMinimumHeight(44)
        self.end_time = QLineEdit()
        self.end_time.setInputMask("00:00;_")
        self.end_time.setPlaceholderText("HH:MM")
        self.end_time.setMinimumHeight(44)
        self.status = QComboBox()
        self.status.addItem("Não alterar", None)
        for status in STATUSES:
            self.status.addItem(status, status)
        self.fee = QLineEdit()
        self.fee.setPlaceholderText("Ex.: 150,00")
        self.target_professional = QComboBox()
        self.target_professional.addItem("Não alterar", None)
        for item in professionals:
            self.target_professional.addItem(item["full_name"], item["id"])
        self.target_patient = QComboBox()
        self.target_patient.addItem("Não alterar", None)
        for item in patients:
            self.target_patient.addItem(item["full_name"], item["id"])
        time_box = QVBoxLayout()
        time_fields = QHBoxLayout()
        start_box = QVBoxLayout()
        start_label = QLabel("Início")
        start_label.setStyleSheet("background: transparent;")
        start_box.addWidget(start_label)
        start_box.addWidget(self.start_time)
        end_box = QVBoxLayout()
        end_label = QLabel("Fim")
        end_label.setStyleSheet("background: transparent;")
        end_box.addWidget(end_label)
        end_box.addWidget(self.end_time)
        time_fields.addLayout(start_box)
        time_fields.addLayout(end_box)
        time_title = QLabel("Alterar horário")
        time_title.setStyleSheet("background: transparent;")
        time_box.addWidget(time_title)
        time_box.addLayout(time_fields)
        changes_layout.addLayout(time_box, 0, 0, 2, 1)
        status_title = QLabel("Alterar status")
        status_title.setStyleSheet("background: transparent;")
        changes_layout.addWidget(status_title, 0, 1)
        changes_layout.addWidget(self.status, 1, 1)
        fee_title = QLabel("Alterar valor")
        fee_title.setStyleSheet("background: transparent;")
        changes_layout.addWidget(fee_title, 0, 2)
        changes_layout.addWidget(self.fee, 1, 2)
        professional_title = QLabel("Alterar profissional")
        professional_title.setStyleSheet("background: transparent;")
        changes_layout.addWidget(professional_title, 2, 0)
        changes_layout.addWidget(self.target_professional, 3, 0)
        patient_title = QLabel("Alterar paciente")
        patient_title.setStyleSheet("background: transparent;")
        changes_layout.addWidget(patient_title, 2, 1)
        changes_layout.addWidget(self.target_patient, 3, 1, 1, 2)
        changes_layout.setColumnStretch(0, 2)
        changes_layout.setColumnStretch(1, 1)
        changes_layout.setColumnStretch(2, 2)
        layout.addWidget(changes)

        actions = QHBoxLayout()
        cancel = button("Cancelar", secondary=True)
        cancel.clicked.connect(self.reject)
        apply_button = button("Aplicar alterações")
        apply_button.clicked.connect(self.apply_changes)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(apply_button)
        layout.addLayout(actions)
        self.load_rows()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self.fit_to_available_screen)

    def fit_to_available_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return
        if self.isMaximized():
            self.showNormal()
        available = screen.availableGeometry()
        horizontal_margin = max(18, int(available.width() * 0.015))
        top_margin = 18
        bottom_margin = 28
        self.setGeometry(
            available.left() + horizontal_margin,
            available.top() + top_margin,
            available.width() - (horizontal_margin * 2),
            available.height() - top_margin - bottom_margin,
        )

    def load_rows(self) -> None:
        if self.end_date.date() < self.start_date.date():
            QMessageBox.warning(self, "Período inválido", "A data final deve ser maior ou igual à data inicial.")
            return
        try:
            self.rows = self.repo.financial_appointments(
                self.start_date.date().toPython(), self.end_date.date().toPython(),
                patient_id=self.patient.currentData(), professional_id=self.professional.currentData(),
            )
            weekday = self.weekday_filter.currentData()
            if weekday is not None:
                self.rows = [
                    row for row in self.rows
                    if date.fromisoformat(row["appointment_date"]).weekday() == weekday
                ]
        except Exception as exc:
            show_error(self, exc)
            return
        self.updating_checks = True
        self.appointment_checks = []
        self.table.clearContents()
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            financial = self.repo.financial_row(row)
            fee = financial.get("consultation_fee")
            values = [
                date.fromisoformat(row["appointment_date"]).strftime("%d/%m/%Y"),
                f"{row['start_time'][:5]} - {row['end_time'][:5]}",
                (row.get("patients") or {}).get("full_name", ""),
                (row.get("professionals") or {}).get("full_name", ""),
                row.get("status", ""),
                "" if fee is None else f"R$ {float(fee):.2f}".replace(".", ","),
            ]
            check = QCheckBox()
            check.setCursor(Qt.PointingHandCursor)
            check.stateChanged.connect(self.update_selection)
            holder = QWidget()
            holder.setStyleSheet(f"background: {COLORS['surface']};")
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignCenter)
            holder_layout.addWidget(check)
            self.table.setCellWidget(index, 0, holder)
            self.appointment_checks.append(check)
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter if column != 3 and column != 4 else Qt.AlignLeft | Qt.AlignVCenter)
                self.table.setItem(index, column, item)
        widths = [90, 110, 130, 270, 250, 130, 110]
        for column, width in enumerate(widths):
            self.table.setColumnWidth(column, width)
        self.updating_checks = False
        self.update_selection()

    def select_all(self) -> None:
        self.updating_checks = True
        for check in self.appointment_checks:
            check.setChecked(True)
        self.updating_checks = False
        self.update_selection()

    def clear_selection(self) -> None:
        self.updating_checks = True
        for check in self.appointment_checks:
            check.setChecked(False)
        self.updating_checks = False
        self.update_selection()

    def update_selection(self) -> None:
        if self.updating_checks:
            return
        selected_count = 0
        for row_index, check in enumerate(self.appointment_checks):
            checked = check.isChecked()
            selected_count += int(checked)
            holder = self.table.cellWidget(row_index, 0)
            if holder:
                holder.setStyleSheet(f"background: {COLORS['primary_soft'] if checked else COLORS['surface']};")
            for column in range(1, self.table.columnCount()):
                item = self.table.item(row_index, column)
                if item:
                    item.setSelected(checked)
        self.result_label.setText(
            f"{len(self.rows)} atendimento(s) encontrado(s)  |  {selected_count} selecionado(s)"
        )

    def selected_appointments(self) -> list[dict[str, Any]]:
        return [
            row for index, row in enumerate(self.rows)
            if index < len(self.appointment_checks) and self.appointment_checks[index].isChecked()
        ]

    def apply_changes(self) -> None:
        selected = self.selected_appointments()
        if not selected:
            QMessageBox.warning(self, "Nenhuma seleção", "Selecione pelo menos um atendimento na tabela.")
            return
        start_digits = "".join(character for character in self.start_time.text() if character.isdigit())
        end_digits = "".join(character for character in self.end_time.text() if character.isdigit())
        if start_digits and len(start_digits) != 4 or end_digits and len(end_digits) != 4:
            QMessageBox.warning(self, "Horário inválido", "Informe o horário completo no formato HH:MM.")
            return
        start_time = f"{start_digits[:2]}:{start_digits[2:]}" if start_digits else ""
        end_time = f"{end_digits[:2]}:{end_digits[2:]}" if end_digits else ""
        if bool(start_time) != bool(end_time):
            QMessageBox.warning(self, "Horário incompleto", "Preencha os horários de início e fim ou deixe ambos vazios.")
            return
        if start_time and (
            not QTime.fromString(start_time, "HH:mm").isValid()
            or not QTime.fromString(end_time, "HH:mm").isValid()
        ):
            QMessageBox.warning(self, "Horário inválido", "Informe horários válidos no formato HH:MM.")
            return
        answer = QMessageBox.question(
            self, "Confirmar alterações",
            f"Aplicar as alterações em {len(selected)} atendimento(s)?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            count = self.repo.update_appointments_bulk(
                selected,
                start_time=f"{start_time}:00" if start_time else None,
                end_time=f"{end_time}:00" if end_time else None,
                status=self.status.currentData(),
                consultation_fee=self.fee.text().strip() or None,
                professional_id=self.target_professional.currentData(),
                patient_id=self.target_patient.currentData(),
            )
            QMessageBox.information(self, "Atendimentos alterados", f"{count} atendimento(s) atualizado(s).")
            self.accept()
        except Exception as exc:
            show_error(self, exc)


class AppointmentDetailsDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, profile: dict[str, Any], appointment: dict[str, Any]):
        super().__init__(parent)
        self.repo = repo
        self.profile = profile
        self.appointment = appointment
        self.setWindowTitle("Detalhes do atendimento")
        self.setMinimumSize(760, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        patient_data = appointment.get("patients") or {}
        patient = patient_data.get("full_name", "")
        professional = (appointment.get("professionals") or {}).get("full_name", "")

        header = QFrame()
        style_card(header, COLORS["surface_warm"])
        header_layout = QGridLayout(header)
        header_layout.setContentsMargins(18, 16, 18, 16)
        title = QLabel(patient or "Paciente não informado")
        title.setObjectName("PageTitle")
        title.setStyleSheet("font-size: 21px; background: transparent;")
        subtitle = QLabel(f"{appointment['appointment_date']} - {appointment['start_time'][:5]} às {appointment['end_time'][:5]}")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        professional_label = QLabel(f"Profissional: {professional or 'Não informado'}")
        professional_label.setStyleSheet("background: transparent; font-weight: 700;")
        header_layout.addWidget(title, 0, 0)
        header_layout.addWidget(subtitle, 1, 0)
        header_layout.addWidget(professional_label, 0, 1, 2, 1, Qt.AlignRight | Qt.AlignVCenter)

        patient_card = QFrame()
        style_card(patient_card)
        patient_layout = QGridLayout(patient_card)
        patient_layout.setContentsMargins(18, 16, 18, 16)
        patient_layout.setHorizontalSpacing(18)
        patient_layout.setVerticalSpacing(10)
        guardian = patient_data.get("guardian_name") or "Não informado"
        phone = patient_data.get("guardian_phone") or "Não informado"
        reason = patient_data.get("reason_for_care") or "Motivo do atendimento ainda não informado."
        general_notes = patient_data.get("general_notes") or "Sem observações gerais."
        patient_layout.addWidget(self.info_label("Responsável", guardian), 0, 0)
        patient_layout.addWidget(self.info_label("Telefone", phone), 0, 1)
        patient_layout.addWidget(self.info_label("Motivo do atendimento", reason), 1, 0, 1, 2)
        patient_layout.addWidget(self.info_label("Observações gerais", general_notes), 2, 0, 1, 2)

        self.status = QComboBox()
        self.status.addItems(STATUSES)
        self.status.setCurrentText(appointment["status"])
        self.status.setEnabled(can(profile, Permission.CHANGE_STATUS))

        can_manage = can(profile, Permission.MANAGE_AGENDA)
        appointment_date = date.fromisoformat(appointment["appointment_date"])
        self.appointment_date = QDateEdit(QDate(appointment_date.year, appointment_date.month, appointment_date.day))
        self.appointment_date.setCalendarPopup(True)
        self.appointment_date.setEnabled(can_manage)
        start_parts = [int(value) for value in appointment["start_time"][:5].split(":")]
        end_parts = [int(value) for value in appointment["end_time"][:5].split(":")]
        self.start_time = time_field(*start_parts)
        self.end_time = time_field(*end_parts)
        self.start_time.setEnabled(can_manage)
        self.end_time.setEnabled(can_manage)
        self.consultation_fee = QLineEdit()
        self.consultation_fee.setPlaceholderText("Ex.: 150,00")
        self.consultation_fee.setEnabled(can_manage)
        try:
            financial = repo.appointment_financials(appointment["id"]) or {}
            fee = financial.get("consultation_fee")
            if fee is not None:
                self.consultation_fee.setText(f"{float(fee):.2f}".replace(".", ","))
        except Exception:
            pass

        is_professional = profile.get("role") == "professional"
        edit_panel = QWidget()
        edit_panel_layout = QVBoxLayout(edit_panel)
        edit_panel_layout.setContentsMargins(0, 0, 0, 0)
        edit_panel_layout.setSpacing(14)
        admin_fields = QWidget()
        edit_grid = QGridLayout(admin_fields)
        edit_grid.setContentsMargins(0, 0, 0, 0)
        edit_grid.addWidget(QLabel("Data"), 0, 0)
        edit_grid.addWidget(self.appointment_date, 1, 0)
        edit_grid.addWidget(QLabel("Início"), 0, 1)
        edit_grid.addWidget(self.start_time, 1, 1)
        edit_grid.addWidget(QLabel("Fim"), 0, 2)
        edit_grid.addWidget(self.end_time, 1, 2)
        fee_label = QLabel("Valor da consulta")
        fee_label.setVisible(can_manage)
        self.consultation_fee.setVisible(can_manage)
        edit_grid.addWidget(fee_label, 0, 3)
        edit_grid.addWidget(self.consultation_fee, 1, 3)

        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Status"))
        status_row.addWidget(self.status, 1)
        admin_fields.setVisible(not is_professional and can_manage)
        edit_panel_layout.addWidget(admin_fields)
        edit_panel_layout.addLayout(status_row)
        edit_panel.setVisible(can_manage or can(profile, Permission.CHANGE_STATUS))

        self.note = QTextEdit()
        self.note.setPlaceholderText("Relatório da sessão")
        self.note.setMinimumHeight(340)
        try:
            existing = repo.session_note_for(appointment["id"])
            self.note.setPlainText(existing.get("note", "") if existing else "")
        except Exception:
            pass
        self.note.setEnabled(can(profile, Permission.WRITE_SESSION_NOTE))

        edit_buttons = QHBoxLayout()
        if not is_professional and (can_manage or can(profile, Permission.CHANGE_STATUS)):
            save_changes_button = button("Salvar alterações")
            save_changes_button.setMinimumWidth(170)
            save_changes_button.clicked.connect(self.save_appointment_changes)
            edit_buttons.addWidget(save_changes_button)
        edit_buttons.addStretch()
        if can(profile, Permission.MANAGE_AGENDA):
            delete_btn = button("Excluir atendimento", danger=True)
            delete_btn.clicked.connect(self.delete_appointment)
            edit_buttons.addWidget(delete_btn)

        note_buttons = QHBoxLayout()
        if can(profile, Permission.WRITE_SESSION_NOTE):
            save_note_button = button("Salvar status e relatório" if is_professional else "Salvar relatório")
            save_note_button.setMinimumWidth(170)
            save_note_button.clicked.connect(self.save_session_report)
            note_buttons.addWidget(save_note_button)
        note_buttons.addStretch()

        layout.addWidget(header)
        layout.addWidget(patient_card)
        layout.addWidget(edit_panel)
        layout.addLayout(edit_buttons)
        layout.addWidget(QLabel("Relatório da sessão"))
        layout.addWidget(self.note, 1)
        layout.addLayout(note_buttons)

    def info_label(self, title: str, value: str) -> QLabel:
        label = QLabel(f"{title}\n{value}")
        label.setWordWrap(True)
        label.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-weight: 600;")
        return label

    def save_appointment_changes(self) -> None:
        try:
            if can(self.profile, Permission.MANAGE_AGENDA):
                self.repo.parse_consultation_fee(self.consultation_fee.text())
                values = {
                    "patient_id": self.appointment["patient_id"],
                    "professional_id": self.appointment["professional_id"],
                    "appointment_date": self.appointment_date.date().toPython().isoformat(),
                    "start_time": self.start_time.time().toString("HH:mm:ss"),
                    "end_time": self.end_time.time().toString("HH:mm:ss"),
                    "status": self.status.currentText(),
                }
                self.repo.save_appointment(values, self.appointment["id"])
                existing = self.repo.appointment_financials(self.appointment["id"]) or {}
                self.repo.save_appointment_financials(
                    self.appointment["id"],
                    {
                        "consultation_fee": self.consultation_fee.text(),
                        "payment_status": existing.get("payment_status", "Pendente"),
                        "payment_method": existing.get("payment_method", ""),
                        "financial_notes": existing.get("financial_notes", ""),
                    },
                )
            elif can(self.profile, Permission.CHANGE_STATUS):
                self.repo.update_appointment_status(self.appointment["id"], self.status.currentText())
            self.accept()
        except Exception as exc:
            show_error(self, exc)

    def save_session_report(self) -> None:
        try:
            if not can(self.profile, Permission.WRITE_SESSION_NOTE):
                raise AppError("Você não possui permissão para salvar o relatório da sessão.")
            if self.profile.get("role") == "professional" and can(self.profile, Permission.CHANGE_STATUS):
                self.repo.update_appointment_status(self.appointment["id"], self.status.currentText())
            self.repo.save_session_note(self.appointment, self.note.toPlainText().strip())
            self.accept()
        except Exception as exc:
            show_error(self, exc)

    def delete_appointment(self) -> None:
        patient = (self.appointment.get("patients") or {}).get("full_name", "este paciente")
        date_text = self.appointment.get("appointment_date", "")
        start_text = self.appointment.get("start_time", "")[:5]
        answer = QMessageBox.question(
            self,
            "Excluir atendimento",
            f"Deseja excluir o atendimento de {patient} em {date_text} às {start_text}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.repo.delete_appointment(self.appointment["id"])
            self.accept()
        except Exception as exc:
            show_error(self, exc)


class FinancePage(QWidget):
    def __init__(self, repo: SupabaseRepository):
        super().__init__()
        self.repo = repo
        self.rows: list[dict[str, Any]] = []
        self.appointment_rows: list[dict[str, Any]] = []
        self.open_receipt_rows: list[dict[str, Any]] = []
        self.patient_payment_rows: list[dict[str, Any]] = []
        self.patient_payment_item_rows: list[dict[str, Any]] = []
        self.professional_payout_rows: list[dict[str, Any]] = []
        self.professional_payout_item_rows: list[dict[str, Any]] = []
        self.summary_rows: list[dict[str, Any]] = []
        self.summary_payment_item_rows: list[dict[str, Any]] = []
        self.summary_payout_item_rows: list[dict[str, Any]] = []
        self.receipt_history_rows: list[dict[str, Any]] = []
        self.payout_history_rows: list[dict[str, Any]] = []
        self.professionals: list[dict[str, Any]] = []
        self.patients: list[dict[str, Any]] = []
        self.updating_financial_table = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)

        title = QLabel("Financeiro")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Resumo, recebimentos de pacientes, repasses profissionais e atendimentos.")
        subtitle.setObjectName("Muted")

        filters_panel = page_card()
        filters = QHBoxLayout(filters_panel)
        filters.setContentsMargins(18, 14, 18, 14)
        filters.setSpacing(14)
        today = QDate.currentDate()
        self.use_date_filter = QCheckBox("Usar período")
        self.use_date_filter.setCursor(Qt.PointingHandCursor)
        self.use_date_filter.setText("Usar período")
        self.use_date_filter.setMinimumWidth(150)
        self.use_date_filter.setMinimumHeight(44)
        self.use_date_filter.setStyleSheet(
            f"""
            QCheckBox {{
                background: {COLORS['surface']};
                color: {COLORS['text']};
                border: 1px solid {COLORS['line']};
                border-radius: 8px;
                padding: 9px 12px;
                font-weight: 700;
                spacing: 8px;
            }}
            QCheckBox:hover {{
                background: {COLORS['primary_soft']};
                color: {COLORS['primary_dark']};
            }}
            QCheckBox::indicator {{
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 2px solid {COLORS['primary']};
                background: #FFFFFF;
            }}
            QCheckBox::indicator:checked {{
                background: {COLORS['primary']};
                border: 2px solid {COLORS['primary']};
            }}
            """
        )
        self.use_date_filter.stateChanged.connect(lambda _state=0: self.sync_finance_filters())
        self.start_date = QDateEdit(QDate(today.year(), today.month(), 1))
        self.start_date.setCalendarPopup(True)
        self.start_date.setMinimumWidth(130)
        self.start_date.dateChanged.connect(self.refresh)
        self.end_date = QDateEdit(today)
        self.end_date.setCalendarPopup(True)
        self.end_date.setMinimumWidth(130)
        self.end_date.dateChanged.connect(self.refresh)
        self.status_filter = QComboBox()
        self.status_filter.setMinimumWidth(140)
        self.status_filter.addItem("Abertos", "open")
        self.status_filter.addItem("Fechados", "closed")
        self.status_filter.addItem("Todos", "all")
        self.status_filter.currentIndexChanged.connect(self.refresh)
        self.patient_filter = QComboBox()
        self.patient_filter.setMinimumWidth(170)
        self.patient_filter.addItem("Todos os pacientes", None)
        self.patient_filter.currentIndexChanged.connect(self.refresh)
        self.professional_filter = QComboBox()
        self.professional_filter.setMinimumWidth(190)
        self.professional_filter.addItem("Todos os profissionais", None)
        self.professional_filter.currentIndexChanged.connect(self.refresh)
        self.overdue_only = QCheckBox("Pagamentos atrasados")
        self.overdue_only.setCursor(Qt.PointingHandCursor)
        self.overdue_only.setMinimumWidth(250)
        self.overdue_only.setVisible(False)
        self.overdue_only.setStyleSheet(
            f"""
            QCheckBox {{
                background: {COLORS['primary_soft']};
                color: {COLORS['primary_dark']};
                border: 1px solid #E9B3CF;
                border-radius: 8px;
                padding: 9px 12px;
                font-weight: 800;
                spacing: 10px;
            }}
            QCheckBox::indicator {{
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 2px solid {COLORS['primary']};
                background: #FFFFFF;
            }}
            QCheckBox::indicator:checked {{
                background: {COLORS['primary']};
                border: 2px solid {COLORS['primary']};
            }}
            """
        )
        refresh_btn = button("Atualizar")
        refresh_btn.setMinimumWidth(100)
        refresh_btn.clicked.connect(self.refresh)
        filters.addWidget(self.use_date_filter, 0, Qt.AlignBottom)
        filters.addLayout(self.filter_field("Início", self.start_date))
        filters.addLayout(self.filter_field("Fim", self.end_date))
        filters.addLayout(self.filter_field("Situação", self.status_filter))
        filters.addLayout(self.filter_field("Paciente", self.patient_filter))
        filters.addLayout(self.filter_field("Profissional", self.professional_filter))
        filters.addStretch()
        filters.addWidget(refresh_btn, 0, Qt.AlignBottom)

        self.tabs = QTabWidget()
        self.patient_financial_tabs: dict[str, QWidget] = {}
        self.professional_financial_tabs: dict[str, QWidget] = {}

        summary_tab = QWidget()
        summary_layout = QVBoxLayout(summary_tab)
        summary_layout.setContentsMargins(0, 10, 0, 0)
        summary_layout.setSpacing(10)
        self.summary_grid = QGridLayout()
        self.summary_grid.setSpacing(10)
        summary_layout.addLayout(self.summary_grid)
        summary_lists = QHBoxLayout()
        summary_lists.setSpacing(10)
        self.patient_summary_table, patient_summary_panel = self.summary_table_panel(
            "A receber por paciente",
            ["Paciente", "Atend.", "Total", "Recebido", "Aberto"],
        )
        full_row_table_polish(self.patient_summary_table)
        self.patient_summary_table.cellDoubleClicked.connect(self.open_patient_financial_tab)
        self.professional_summary_table, professional_summary_panel = self.summary_table_panel(
            "A pagar por funcionário",
            ["Funcionário", "Atend.", "Bruto", "Repasse 70%", "Pago", "Aberto"],
        )
        full_row_table_polish(self.professional_summary_table)
        self.professional_summary_table.cellDoubleClicked.connect(self.open_professional_financial_tab)
        summary_lists.addWidget(patient_summary_panel, 1)
        summary_lists.addWidget(professional_summary_panel, 1)
        summary_layout.addLayout(summary_lists, 1)

        receipts_tab = QWidget()
        receipts_layout = QVBoxLayout(receipts_tab)
        receipts_layout.setContentsMargins(0, 14, 0, 0)
        receipts_layout.setSpacing(12)
        receipts_actions = QHBoxLayout()
        register_receipt = button("Registrar recebimento")
        register_receipt.clicked.connect(self.register_receipt)
        receipts_actions.addStretch()
        receipts_actions.addWidget(register_receipt)
        self.receipts_table, receipts_panel = self.summary_table_panel(
            "Recebimentos de pacientes",
            ["Paciente", "Atendimentos", "Valor faturado", "Recebido", "A receber"],
        )
        receipts_layout.addLayout(receipts_actions)
        receipts_layout.addWidget(receipts_panel, 1)

        payouts_tab = QWidget()
        payouts_layout = QVBoxLayout(payouts_tab)
        payouts_layout.setContentsMargins(0, 14, 0, 0)
        payouts_layout.setSpacing(12)
        payouts_actions = QHBoxLayout()
        register_payout = button("Registrar repasse")
        register_payout.clicked.connect(self.register_payout)
        payouts_actions.addStretch()
        payouts_actions.addWidget(register_payout)
        self.payouts_table, payouts_panel = self.summary_table_panel(
            "Repasses de funcionários",
            ["Funcionário", "Atendimentos", "Valor bruto", "Percentual", "Repassado", "A pagar"],
        )
        payouts_layout.addLayout(payouts_actions)
        payouts_layout.addWidget(payouts_panel, 1)

        appointments_tab = QWidget()
        appointments_layout = QVBoxLayout(appointments_tab)
        appointments_layout.setContentsMargins(0, 14, 0, 0)
        appointments_layout.setSpacing(12)

        table_panel = page_card()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(18, 18, 18, 18)
        table_layout.setSpacing(12)
        table_header = QHBoxLayout()
        table_title = QLabel("Atendimentos financeiros")
        table_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        self.table_badge = QLabel("")
        self.table_badge.setObjectName("Badge")
        table_header.addWidget(table_title)
        table_header.addStretch()
        table_header.addWidget(self.table_badge)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Data", "Horário", "Paciente", "Profissional", "Atendimento", "Valor (R$)", "Pagamento", "Forma"])
        full_row_table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(self.edit_financial_appointment)
        self.table.cellChanged.connect(self.save_table_fee)
        table_layout.addLayout(table_header)
        table_layout.addWidget(self.table, 1)
        appointments_layout.addWidget(table_panel, 1)

        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)
        history_layout.setContentsMargins(0, 14, 0, 0)
        history_layout.setSpacing(12)
        history_hint = QLabel("Movimentações registradas conforme os filtros financeiros selecionados.")
        history_hint.setObjectName("Muted")
        history_hint.setStyleSheet("background: transparent;")
        history_sections = QTabWidget()
        receipt_history_page = QWidget()
        receipt_history_layout = QVBoxLayout(receipt_history_page)
        receipt_history_layout.setContentsMargins(0, 10, 0, 0)
        self.receipt_history_table, receipt_history_panel = self.summary_table_panel(
            "Histórico de recebimentos",
            ["Data", "Paciente", "Valor", "Forma", "Atendimentos", "Observação"],
        )
        full_row_table_polish(self.receipt_history_table)
        receipt_history_layout.addWidget(receipt_history_panel, 1)
        payout_history_page = QWidget()
        payout_history_layout = QVBoxLayout(payout_history_page)
        payout_history_layout.setContentsMargins(0, 10, 0, 0)
        self.payout_history_table, payout_history_panel = self.summary_table_panel(
            "Histórico de repasses",
            ["Data", "Funcionário", "Valor", "Forma", "Atendimentos", "Observação"],
        )
        full_row_table_polish(self.payout_history_table)
        payout_history_layout.addWidget(payout_history_panel, 1)
        history_sections.addTab(receipt_history_page, "Recebimentos")
        history_sections.addTab(payout_history_page, "Repasses")
        history_layout.addWidget(history_hint)
        history_layout.addWidget(history_sections, 1)

        self.tabs.addTab(summary_tab, "Resumo")
        self.tabs.addTab(receipts_tab, "Recebimentos")
        self.tabs.addTab(payouts_tab, "Repasses")
        self.tabs.addTab(appointments_tab, "Atendimentos")
        self.tabs.addTab(history_tab, "Histórico")

        layout.addWidget(title)
        layout.addWidget(filters_panel)
        layout.addWidget(self.tabs, 1)
        self.lookups_loaded = False
        self.sync_finance_filters(refresh=False)

    def summary_table_panel(self, title: str, headers: list[str]) -> tuple[QTableWidget, QFrame]:
        panel = page_card()
        box = QVBoxLayout(panel)
        box.setContentsMargins(18, 18, 18, 18)
        box.setSpacing(12)
        label = QLabel(title)
        label.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table_polish(table)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        box.addWidget(label)
        box.addWidget(table, 1)
        return table, panel

    def register_receipt(self) -> None:
        try:
            dialog = PatientReceiptDialog(self, self.repo, self.money)
            if dialog.exec():
                self.repo.save_patient_payment(dialog.values(), dialog.selected_items())
                QMessageBox.information(self, "Recebimento", "Recebimento registrado com sucesso.")
                self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def register_payout(self) -> None:
        try:
            dialog = ProfessionalPayoutDialog(self, self.repo, self.money)
            if dialog.exec():
                self.repo.save_professional_payout(dialog.values(), dialog.selected_items())
                QMessageBox.information(self, "Repasse", "Repasse registrado com sucesso.")
                self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def edit_financial_appointment(self, row: int, _col: int) -> None:
        if not (0 <= row < len(self.appointment_rows)):
            return
        appointment = self.appointment_rows[row]
        dialog = FinancialAppointmentDialog(self, appointment, self.repo, self.money_edit_text)
        if dialog.exec():
            try:
                self.repo.save_appointment_financials(appointment["id"], dialog.values())
                QMessageBox.information(self, "Atendimento", "Dados financeiros atualizados com sucesso.")
                self.refresh()
            except Exception as exc:
                show_error(self, exc)

    def open_patient_financial_tab(self, row: int, _col: int) -> None:
        item = self.patient_summary_table.item(row, 0)
        if not item:
            return
        patient_id = item.data(Qt.UserRole)
        if not patient_id:
            return
        patient_name = item.text()
        existing = self.patient_financial_tabs.get(patient_id)
        if existing:
            index = self.tabs.indexOf(existing)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
                return
            self.patient_financial_tabs.pop(patient_id, None)

        tab = self.build_patient_financial_tab(patient_id, patient_name)
        self.patient_financial_tabs[patient_id] = tab
        title = patient_name if len(patient_name) <= 18 else f"{patient_name[:17]}..."
        index = self.tabs.addTab(tab, f"Paciente: {title}")
        self.tabs.setCurrentIndex(index)

    def build_patient_financial_tab(self, patient_id: str, patient_name: str) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(12)

        header = page_card()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 16, 18, 16)
        text_box = QVBoxLayout()
        title = QLabel(patient_name)
        title.setObjectName("SectionTitle")
        subtitle = QLabel("Atendimentos financeiros deste paciente.")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        text_box.addWidget(title)
        text_box.addWidget(subtitle)
        header_layout.addLayout(text_box)
        header_layout.addStretch()
        close_btn = button("Fechar aba", secondary=True)
        close_btn.clicked.connect(lambda: self.close_patient_financial_tab(patient_id))
        header_layout.addWidget(close_btn)
        layout.addWidget(header)

        table = QTableWidget(0, 9)
        table.setHorizontalHeaderLabels(
            ["Data", "Horário", "Profissional", "Atendimento", "Valor", "Recebido", "Aberto", "Pagamento", "Forma"]
        )
        full_row_table_polish(table)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.cellDoubleClicked.connect(lambda row, col, source=table: self.edit_patient_financial_appointment(source, row, col))

        panel = page_card()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 18, 18, 18)
        top = QHBoxLayout()
        section = QLabel("Atendimentos do paciente")
        section.setObjectName("SectionTitle")
        badge = QLabel("0 atendimentos")
        badge.setObjectName("Badge")
        top.addWidget(section)
        top.addStretch()
        top.addWidget(badge)
        panel_layout.addLayout(top)
        panel_layout.addWidget(table, 1)
        layout.addWidget(panel, 1)

        tab.patient_id = patient_id
        tab.patient_table = table
        tab.patient_badge = badge
        self.fill_patient_financial_table(table, badge, patient_id)
        return tab

    def close_patient_financial_tab(self, patient_id: str) -> None:
        tab = self.patient_financial_tabs.pop(patient_id, None)
        if not tab:
            return
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.removeTab(index)

    def edit_patient_financial_appointment(self, table: QTableWidget, row: int, _col: int) -> None:
        rows = getattr(table, "appointment_rows", [])
        if not (0 <= row < len(rows)):
            return
        appointment = rows[row]
        dialog = FinancialAppointmentDialog(self, appointment, self.repo, self.money_edit_text)
        if dialog.exec():
            try:
                self.repo.save_appointment_financials(appointment["id"], dialog.values())
                QMessageBox.information(self, "Atendimento", "Dados financeiros atualizados com sucesso.")
                self.refresh()
                patient_id = getattr(table, "patient_id", None)
                badge = getattr(table, "patient_badge", None)
                if patient_id and badge:
                    self.fill_patient_financial_table(table, badge, patient_id)
            except Exception as exc:
                show_error(self, exc)

    def fill_patient_financial_table(self, table: QTableWidget, badge: QLabel, patient_id: str) -> None:
        try:
            rows = self.repo.financial_appointments(None, None, patient_id=patient_id)
            payment_items = self.repo.patient_payment_items_for_appointments([row["id"] for row in rows])
            rows = self.filter_appointment_rows_by_situation(rows, payment_items, "all")
        except Exception as exc:
            show_error(self, exc)
            rows = []

        table.patient_id = patient_id
        table.patient_badge = badge
        table.appointment_rows = rows
        badge.setText(f"{len(rows)} atendimentos")
        table.clearContents()
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            financial = self.repo.financial_row(row)
            values = [
                row.get("appointment_date", ""),
                f"{row.get('start_time', '')[:5]} às {row.get('end_time', '')[:5]}",
                self.professional_name_for(row),
                row.get("status", ""),
                self.money_edit_text(financial.get("consultation_fee")),
                self.money(row.get("_paid_amount", 0)),
                self.money(row.get("_open_amount", 0)),
                self.payment_status_text(financial.get("payment_status")),
                financial.get("payment_method") or "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter)
                table.setItem(row_index, col, item)
        table.resizeColumnsToContents()

    def open_professional_financial_tab(self, row: int, _col: int) -> None:
        item = self.professional_summary_table.item(row, 0)
        if not item:
            return
        professional_id = item.data(Qt.UserRole)
        if not professional_id:
            return
        professional_name = item.text()
        existing = self.professional_financial_tabs.get(professional_id)
        if existing:
            index = self.tabs.indexOf(existing)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
                return
            self.professional_financial_tabs.pop(professional_id, None)

        tab = self.build_professional_financial_tab(professional_id, professional_name)
        self.professional_financial_tabs[professional_id] = tab
        title = professional_name if len(professional_name) <= 18 else f"{professional_name[:17]}..."
        index = self.tabs.addTab(tab, f"Funcionário: {title}")
        self.tabs.setCurrentIndex(index)

    def build_professional_financial_tab(self, professional_id: str, professional_name: str) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(12)

        header = page_card()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 16, 18, 16)
        text_box = QVBoxLayout()
        title = QLabel(professional_name)
        title.setObjectName("SectionTitle")
        subtitle = QLabel("Atendimentos e repasses financeiros deste funcionário.")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        text_box.addWidget(title)
        text_box.addWidget(subtitle)
        header_layout.addLayout(text_box)
        header_layout.addStretch()
        close_btn = button("Fechar aba", secondary=True)
        close_btn.clicked.connect(lambda: self.close_professional_financial_tab(professional_id))
        header_layout.addWidget(close_btn)
        layout.addWidget(header)

        table = QTableWidget(0, 9)
        table.setHorizontalHeaderLabels(
            ["Data", "Horário", "Paciente", "Atendimento", "Bruto", "Repasse 70%", "Pago", "Aberto", "Pagamento"]
        )
        full_row_table_polish(table)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.cellDoubleClicked.connect(lambda row, col, source=table: self.edit_professional_financial_appointment(source, row, col))

        panel = page_card()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 18, 18, 18)
        top = QHBoxLayout()
        section = QLabel("Atendimentos do funcionário")
        section.setObjectName("SectionTitle")
        badge = QLabel("0 atendimentos")
        badge.setObjectName("Badge")
        top.addWidget(section)
        top.addStretch()
        top.addWidget(badge)
        panel_layout.addLayout(top)
        panel_layout.addWidget(table, 1)
        layout.addWidget(panel, 1)

        tab.professional_id = professional_id
        tab.professional_table = table
        tab.professional_badge = badge
        self.fill_professional_financial_table(table, badge, professional_id)
        return tab

    def close_professional_financial_tab(self, professional_id: str) -> None:
        tab = self.professional_financial_tabs.pop(professional_id, None)
        if not tab:
            return
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.removeTab(index)

    def edit_professional_financial_appointment(self, table: QTableWidget, row: int, _col: int) -> None:
        rows = getattr(table, "appointment_rows", [])
        if not (0 <= row < len(rows)):
            return
        appointment = rows[row]
        dialog = FinancialAppointmentDialog(self, appointment, self.repo, self.money_edit_text)
        if dialog.exec():
            try:
                self.repo.save_appointment_financials(appointment["id"], dialog.values())
                QMessageBox.information(self, "Atendimento", "Dados financeiros atualizados com sucesso.")
                self.refresh()
                professional_id = getattr(table, "professional_id", None)
                badge = getattr(table, "professional_badge", None)
                if professional_id and badge:
                    self.fill_professional_financial_table(table, badge, professional_id)
            except Exception as exc:
                show_error(self, exc)

    def fill_professional_financial_table(self, table: QTableWidget, badge: QLabel, professional_id: str) -> None:
        try:
            rows = self.repo.financial_appointments(None, None, professional_id=professional_id)
            payout_items = self.repo.professional_payout_items_for_appointments([row["id"] for row in rows])
        except Exception as exc:
            show_error(self, exc)
            rows = []
            payout_items = []

        paid_by_appointment: dict[str, float] = {}
        for item in payout_items:
            appointment_id = item.get("appointment_id")
            if appointment_id:
                paid_by_appointment[appointment_id] = paid_by_appointment.get(appointment_id, 0.0) + float(item.get("amount") or 0)

        table.professional_id = professional_id
        table.professional_badge = badge
        table.appointment_rows = rows
        badge.setText(f"{len(rows)} atendimentos")
        table.clearContents()
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            financial = self.repo.financial_row(row)
            gross = self.fee_for(row) if self.is_billable(row) else 0.0
            payout = gross * 0.70
            paid = paid_by_appointment.get(row.get("id"), 0.0)
            open_amount = max(payout - paid, 0.0)
            values = [
                row.get("appointment_date", ""),
                f"{row.get('start_time', '')[:5]} às {row.get('end_time', '')[:5]}",
                self.patient_name_for(row),
                row.get("status", ""),
                self.money(gross),
                self.money(payout),
                self.money(paid),
                self.money(open_amount),
                self.payment_status_text(financial.get("payment_status")),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter)
                table.setItem(row_index, col, item)
        table.resizeColumnsToContents()

    def filter_field(self, label: str, widget: QWidget) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(6)
        text = QLabel(label)
        text.setObjectName("Muted")
        text.setStyleSheet("background: transparent; font-size: 12px; font-weight: 700;")
        box.addWidget(text)
        box.addWidget(widget)
        return box

    def load_professionals(self) -> None:
        try:
            self.professionals = self.repo.professionals(active_only=True)
        except Exception:
            self.professionals = []
        self.professional_filter.blockSignals(True)
        self.professional_filter.clear()
        self.professional_filter.addItem("Todos os profissionais", None)
        for professional in self.professionals:
            self.professional_filter.addItem(professional.get("full_name", ""), professional.get("id"))
        self.professional_filter.blockSignals(False)

    def load_patients(self) -> None:
        try:
            self.patients = self.repo.patients(active_only=False)
        except Exception:
            self.patients = []
        self.patient_filter.blockSignals(True)
        self.patient_filter.clear()
        self.patient_filter.addItem("Todos os pacientes", None)
        for patient in self.patients:
            self.patient_filter.addItem(patient.get("full_name", ""), patient.get("id"))
        self.patient_filter.blockSignals(False)

    def sync_finance_filters(self, refresh: bool = True) -> None:
        use_period = self.use_date_filter.isChecked()
        self.start_date.setEnabled(use_period)
        self.end_date.setEnabled(use_period)
        if refresh:
            self.refresh()

    def refresh(self) -> None:
        if not self.lookups_loaded:
            self.load_patients()
            self.load_professionals()
            self.lookups_loaded = True
        try:
            use_period = self.use_date_filter.isChecked()
            start = self.start_date.date().toPython() if use_period else None
            end = self.end_date.date().toPython() if use_period else None
            patient_id = self.patient_filter.currentData()
            professional_id = self.professional_filter.currentData()
            self.rows = self.repo.patient_payment_balance_appointments(
                patient_id=patient_id,
                professional_id=professional_id,
                start_date=start,
                end_date=end,
                balance_filter=self.status_filter.currentData() or "open",
            )
            self.patient_payment_rows = self.repo.patient_payments(start, end)
            self.professional_payout_rows = self.repo.professional_payouts(start, end, professional_id)
            appointment_ids = [row.get("id") for row in self.rows if row.get("id")]
            self.patient_payment_item_rows = self.repo.patient_payment_items_for_appointments(appointment_ids)
            self.professional_payout_item_rows = self.repo.professional_payout_items_for_appointments(appointment_ids)
            self.open_receipt_rows = self.repo.patient_payment_balance_appointments(
                patient_id=patient_id,
                professional_id=professional_id,
                start_date=start,
                end_date=end,
                balance_filter=self.status_filter.currentData() or "open",
            )
            appointment_rows = self.repo.financial_appointments(
                start_date=start,
                end_date=end,
                patient_id=patient_id,
                professional_id=professional_id,
            )
            appointment_payment_items = self.repo.patient_payment_items_for_appointments(
                [row.get("id") for row in appointment_rows if row.get("id")]
            )
            self.summary_rows = appointment_rows
            self.summary_payment_item_rows = appointment_payment_items
            self.summary_payout_item_rows = self.repo.professional_payout_items_for_appointments(
                [row.get("id") for row in appointment_rows if row.get("id")]
            )
            try:
                self.receipt_history_rows = self.repo.patient_payment_history(
                    start, end, patient_id, professional_id
                )
                self.payout_history_rows = self.repo.professional_payout_history(
                    start, end, patient_id, professional_id
                )
            except Exception as history_exc:
                log_exception("Erro ao carregar histórico financeiro", history_exc)
                self.receipt_history_rows = []
                self.payout_history_rows = []
            self.appointment_rows = self.filter_appointment_rows_by_situation(
                appointment_rows,
                appointment_payment_items,
                self.status_filter.currentData() or "open",
            )
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
            self.appointment_rows = []
            self.open_receipt_rows = []
            self.patient_payment_rows = []
            self.patient_payment_item_rows = []
            self.professional_payout_rows = []
            self.professional_payout_item_rows = []
            self.summary_rows = []
            self.summary_payment_item_rows = []
            self.summary_payout_item_rows = []
            self.receipt_history_rows = []
            self.payout_history_rows = []
        self.fill_summary()
        self.fill_table()
        self.fill_financial_history()

    def fill_financial_history(self) -> None:
        self.receipt_history_table.setRowCount(len(self.receipt_history_rows))
        for row_index, row in enumerate(self.receipt_history_rows):
            items = row.get("patient_payment_items") or []
            raw_date = str(row.get("payment_date") or "")
            try:
                date_text = date.fromisoformat(raw_date[:10]).strftime("%d/%m/%Y")
            except ValueError:
                date_text = raw_date
            values = [
                date_text,
                (row.get("patients") or {}).get("full_name", "Paciente não informado"),
                self.money(row.get("amount")),
                row.get("payment_method") or "Não informado",
                str(len(items)),
                row.get("notes") or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter if column != 1 and column != 5 else Qt.AlignLeft | Qt.AlignVCenter)
                self.receipt_history_table.setItem(row_index, column, item)
        self.receipt_history_table.resizeColumnsToContents()
        self.receipt_history_table.horizontalHeader().setStretchLastSection(True)

        self.payout_history_table.setRowCount(len(self.payout_history_rows))
        for row_index, row in enumerate(self.payout_history_rows):
            items = row.get("professional_payout_items") or []
            raw_date = str(row.get("payout_date") or "")
            try:
                date_text = date.fromisoformat(raw_date[:10]).strftime("%d/%m/%Y")
            except ValueError:
                date_text = raw_date
            values = [
                date_text,
                (row.get("professionals") or {}).get("full_name", "Funcionário não informado"),
                self.money(row.get("amount")),
                row.get("payment_method") or "Não informado",
                str(len(items)),
                row.get("notes") or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter if column != 1 and column != 5 else Qt.AlignLeft | Qt.AlignVCenter)
                self.payout_history_table.setItem(row_index, column, item)
        self.payout_history_table.resizeColumnsToContents()
        self.payout_history_table.horizontalHeader().setStretchLastSection(True)

    def money(self, value: Any) -> str:
        if value in (None, ""):
            return "R$ 0,00"
        return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def money_edit_text(self, value: Any) -> str:
        if value in (None, ""):
            return "0,00"
        return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def fee_for(self, row: dict[str, Any]) -> float:
        financial = self.repo.financial_row(row)
        value = financial.get("consultation_fee")
        return float(value or 0)

    def payment_status_for(self, row: dict[str, Any]) -> str:
        financial = self.repo.financial_row(row)
        if "_paid_amount" not in row and "_open_amount" not in row:
            return financial.get("payment_status") or "Pendente"
        fee = self.fee_for(row)
        paid = float(row.get("_paid_amount") or 0)
        open_amount = float(row.get("_open_amount") or 0)
        if fee > 0 and open_amount <= 0.009:
            return "Pago"
        if paid > 0:
            return "Pendente"
        return financial.get("payment_status") or "Pendente"

    def payment_status_text(self, status: Any) -> str:
        return "Não informado" if not status or status == "Nao informado" else str(status)

    def filter_appointment_rows_by_situation(
        self,
        rows: list[dict[str, Any]],
        payment_items: list[dict[str, Any]],
        situation: str,
    ) -> list[dict[str, Any]]:
        paid_by_appointment: dict[str, float] = {}
        for item in payment_items:
            appointment_id = item.get("appointment_id")
            if not appointment_id:
                continue
            paid_by_appointment[appointment_id] = paid_by_appointment.get(appointment_id, 0.0) + float(item.get("amount") or 0)

        filtered: list[dict[str, Any]] = []
        for row in rows:
            fee = self.fee_for(row)
            paid = paid_by_appointment.get(row.get("id"), 0.0)
            open_amount = max(fee - paid, 0)
            row["_paid_amount"] = paid
            row["_open_amount"] = open_amount

            if situation == "all":
                filtered.append(row)
                continue

            is_closed = self.is_billable(row) and open_amount <= 0.009
            is_open = self.is_billable(row) and open_amount > 0.009
            if situation == "open" and is_open:
                filtered.append(row)
            elif situation == "closed" and is_closed:
                filtered.append(row)
        return filtered

    def is_realized(self, row: dict[str, Any]) -> bool:
        return row.get("status") in {"Atendido", "Falta sem aviso"}

    def is_future(self, row: dict[str, Any]) -> bool:
        return row.get("status") in {"Agendado", "Confirmado"}

    def is_billable(self, row: dict[str, Any]) -> bool:
        return self.repo.appointment_is_chargeable(row)

    def patient_name_for(self, row: dict[str, Any]) -> str:
        return (row.get("patients") or {}).get("full_name", "Paciente não informado")

    def professional_name_for(self, row: dict[str, Any]) -> str:
        return (row.get("professionals") or {}).get("full_name", "Funcionário não informado")

    def payment_totals_by_appointment(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.patient_payment_item_rows:
            appointment_id = row.get("appointment_id")
            if not appointment_id:
                continue
            totals[appointment_id] = totals.get(appointment_id, 0.0) + float(row.get("amount") or 0)
        return totals

    def payment_totals_by_patient(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        rows_by_id = {row.get("id"): row for row in self.rows if row.get("id")}
        for row in self.patient_payment_item_rows:
            appointment = rows_by_id.get(row.get("appointment_id"))
            if not appointment or not self.is_billable(appointment):
                continue
            patient_id = appointment.get("patient_id")
            if not patient_id:
                continue
            totals[patient_id] = totals.get(patient_id, 0.0) + float(row.get("amount") or 0)
        return totals

    def payout_totals_by_appointment(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.professional_payout_item_rows:
            appointment_id = row.get("appointment_id")
            if not appointment_id:
                continue
            totals[appointment_id] = totals.get(appointment_id, 0.0) + float(row.get("amount") or 0)
        return totals

    def payout_totals_by_professional(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        rows_by_id = {row.get("id"): row for row in self.rows if row.get("id")}
        for row in self.professional_payout_item_rows:
            appointment = rows_by_id.get(row.get("appointment_id"))
            if not appointment or not self.is_billable(appointment):
                continue
            professional_id = appointment.get("professional_id")
            if not professional_id:
                continue
            totals[professional_id] = totals.get(professional_id, 0.0) + float(row.get("amount") or 0)
        return totals

    def payment_totals_by_payment_date(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.patient_payment_rows:
            patient_id = row.get("patient_id")
            if not patient_id:
                continue
            totals[patient_id] = totals.get(patient_id, 0.0) + float(row.get("amount") or 0)
        return totals

    def payout_totals_by_payout_date(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.professional_payout_rows:
            professional_id = row.get("professional_id")
            if not professional_id:
                continue
            totals[professional_id] = totals.get(professional_id, 0.0) + float(row.get("amount") or 0)
        return totals

    def fill_summary(self) -> None:
        clear_layout(self.summary_grid)
        billable_rows = [row for row in self.summary_rows if self.is_billable(row)]
        billable_ids = {row.get("id") for row in billable_rows}
        realized = round(sum(self.fee_for(row) for row in billable_rows), 2)
        future = round(sum(self.fee_for(row) for row in billable_rows if self.is_future(row)), 2)
        received_for_appointments = round(
            sum(float(item.get("amount") or 0) for item in self.summary_payment_item_rows if item.get("appointment_id") in billable_ids),
            2,
        )
        patient_open = round(max(realized - received_for_appointments, 0), 2)
        professional_total = round(sum(self.repo.professional_share(self.fee_for(row)) for row in billable_rows), 2)
        paid_for_appointments = round(
            sum(float(item.get("amount") or 0) for item in self.summary_payout_item_rows if item.get("appointment_id") in billable_ids),
            2,
        )
        professional_due = round(max(professional_total - paid_for_appointments, 0), 2)
        clinic_share = round(sum(round(self.fee_for(row) * 0.30, 2) for row in billable_rows), 2)
        received_cash = round(sum(float(row.get("amount") or 0) for row in self.patient_payment_rows), 2)
        paid_cash = round(sum(float(row.get("amount") or 0) for row in self.professional_payout_rows), 2)
        cash_result = round(received_cash - paid_cash, 2)
        cards = [
            ("Faturado", self.money(realized), COLORS["primary"]),
            ("Faturamento futuro", self.money(future), COLORS["blue"]),
            ("Recebido de pacientes", self.money(received_cash), COLORS["green"]),
            ("A receber", self.money(patient_open), COLORS["red"]),
            ("A pagar funcionários", self.money(professional_due), COLORS["primary_dark"]),
            ("Repasses pagos", self.money(paid_cash), COLORS["blue"]),
            ("Parte da clínica 30%", self.money(clinic_share), COLORS["text"]),
            ("Resultado em caixa", self.money(cash_result), COLORS["green"] if cash_result >= 0 else COLORS["red"]),
        ]
        for col, (label, value, color) in enumerate(cards):
            frame = card()
            frame.setMinimumHeight(84)
            style_card(frame, COLORS["surface"], COLORS["line"])
            box = QVBoxLayout(frame)
            box.setContentsMargins(18, 12, 18, 12)
            number = QLabel(value)
            number.setStyleSheet(f"background: transparent; color: {color}; font-size: 23px; font-weight: 800;")
            text = QLabel(label)
            text.setObjectName("Muted")
            text.setStyleSheet("background: transparent; font-weight: 700;")
            box.addWidget(number)
            box.addWidget(text)
            self.summary_grid.addWidget(frame, col // 4, col % 4)
        self.fill_group_tables()

    def fill_group_tables(self) -> None:
        patient_rows = self.group_by_patient()
        receipt_rows = self.group_open_receipts_by_patient()
        professional_rows = self.group_by_professional()
        self.fill_plain_table(
            self.patient_summary_table,
            patient_rows,
            ["name", "count", "total", "received", "open"],
            money_keys={"total", "received", "open"},
        )
        self.fill_plain_table(
            self.receipts_table,
            receipt_rows,
            ["name", "count", "total", "received", "open"],
            money_keys={"total", "received", "open"},
        )
        self.fill_plain_table(
            self.professional_summary_table,
            professional_rows,
            ["name", "count", "gross", "total_payout", "paid", "open"],
            money_keys={"gross", "total_payout", "paid", "open"},
        )
        self.fill_plain_table(
            self.payouts_table,
            professional_rows,
            ["name", "count", "gross", "percent", "paid", "open"],
            money_keys={"gross", "paid", "open"},
        )

    def group_by_patient(self) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        payments = self.payment_totals_by_patient()
        for row in self.rows:
            if not self.is_billable(row):
                continue
            patient_id = row.get("patient_id") or self.patient_name_for(row)
            item = grouped.setdefault(
                patient_id,
                {
                    "id": patient_id,
                    "name": self.patient_name_for(row),
                    "count": 0,
                    "total": 0.0,
                    "received": 0.0,
                    "open": 0.0,
                },
            )
            fee = self.fee_for(row)
            item["count"] += 1
            item["total"] += fee
        for item in grouped.values():
            patient_id = next((key for key, value in grouped.items() if value is item), "")
            item["received"] = payments.get(patient_id, 0.0)
            item["open"] = max(item["total"] - item["received"], 0)
        return sorted(grouped.values(), key=lambda item: item["open"], reverse=True)

    def group_open_receipts_by_patient(self) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in self.open_receipt_rows:
            patient_id = row.get("patient_id") or self.patient_name_for(row)
            item = grouped.setdefault(
                patient_id,
                {
                    "id": patient_id,
                    "name": self.patient_name_for(row),
                    "count": 0,
                    "total": 0.0,
                    "received": 0.0,
                    "open": 0.0,
                },
            )
            item["count"] += 1
            item["total"] += self.fee_for(row)
            item["received"] += float(row.get("_paid_amount") or 0)
            item["open"] += float(row.get("_open_amount") or 0)
        return sorted(grouped.values(), key=lambda item: item["open"], reverse=True)

    def group_by_professional(self) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        payouts = self.payout_totals_by_professional()
        for row in self.rows:
            if not self.is_billable(row):
                continue
            professional_id = row.get("professional_id") or self.professional_name_for(row)
            item = grouped.setdefault(
                professional_id,
                {
                    "id": professional_id,
                    "name": self.professional_name_for(row),
                    "count": 0,
                    "gross": 0.0,
                    "percent": "70%",
                    "total_payout": 0.0,
                    "paid": 0.0,
                    "open": 0.0,
                },
            )
            fee = self.fee_for(row)
            item["count"] += 1
            item["gross"] += fee
            item["total_payout"] = round(item["total_payout"] + self.repo.professional_share(fee), 2)
        for professional_id, item in grouped.items():
            item["paid"] = payouts.get(professional_id, 0.0)
            item["open"] = max(item["total_payout"] - item["paid"], 0)
        return sorted(grouped.values(), key=lambda item: item["open"], reverse=True)

    def fill_plain_table(
        self,
        table: QTableWidget,
        rows: list[dict[str, Any]],
        keys: list[str],
        money_keys: set[str] | None = None,
    ) -> None:
        money_keys = money_keys or set()
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col, key in enumerate(keys):
                value = self.money(row.get(key)) if key in money_keys else row.get(key, "")
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col == 0 and row.get("id"):
                    item.setData(Qt.UserRole, row.get("id"))
                table.setItem(row_index, col, item)
        table.resizeColumnsToContents()

    def fill_table(self) -> None:
        self.updating_financial_table = True
        self.table_badge.setText(f"{len(self.appointment_rows)} atendimentos")
        self.table.clearContents()
        self.table.setRowCount(len(self.appointment_rows))
        for row_index, row in enumerate(self.appointment_rows):
            financial = self.repo.financial_row(row)
            patient = self.patient_name_for(row)
            professional = self.professional_name_for(row)
            values = [
                row.get("appointment_date", ""),
                f"{row.get('start_time', '')[:5]} às {row.get('end_time', '')[:5]}",
                patient,
                professional,
                row.get("status", ""),
                self.money_edit_text(financial.get("consultation_fee")),
                self.payment_status_for(row),
                financial.get("payment_method") or "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if col != 5:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 6:
                    bg, fg = self.payment_colors(str(value))
                    item.setBackground(QColor(bg))
                    item.setForeground(QColor(fg))
                self.table.setItem(row_index, col, item)
        widths = [110, 110, 220, 180, 130, 110, 140, 160]
        for col, width in enumerate(widths):
            self.table.setColumnWidth(col, width)
        self.updating_financial_table = False

    def save_table_fee(self, row: int, col: int) -> None:
        if self.updating_financial_table or col != 5 or not (0 <= row < len(self.appointment_rows)):
            return
        appointment = self.appointment_rows[row]
        item = self.table.item(row, col)
        if not item:
            return
        financial = self.repo.financial_row(appointment)
        try:
            self.repo.save_appointment_financials(
                appointment["id"],
                {
                    "consultation_fee": item.text().strip(),
                    "payment_status": financial.get("payment_status", "Nao informado"),
                    "payment_method": financial.get("payment_method", ""),
                },
            )
            updated = self.repo.appointment_financials(appointment["id"]) or {}
            appointment["appointment_financials"] = [updated]
            self.updating_financial_table = True
            item.setText(self.money_edit_text(updated.get("consultation_fee")))
            self.updating_financial_table = False
            self.refresh()
        except Exception as exc:
            self.updating_financial_table = True
            item.setText(self.money_edit_text(financial.get("consultation_fee")))
            self.updating_financial_table = False
            show_error(self, exc)

    def payment_colors(self, status: str) -> tuple[str, str]:
        return {
            "Pago": (COLORS["green_card"], COLORS["green"]),
            "Pendente": (COLORS["yellow_card"], COLORS["text"]),
            "Cortesia": (COLORS["blue_soft"], COLORS["blue"]),
            "Cancelado": (COLORS["gray"], COLORS["muted"]),
            "Nao informado": (COLORS["primary_soft"], COLORS["primary_dark"]),
        }.get(status, (COLORS["bg2"], COLORS["text"]))


class FinancialAppointmentDialog(QDialog):
    PAYMENT_STATUSES = [
        ("Não informado", "Nao informado"),
        ("Pendente", "Pendente"),
        ("Pago", "Pago"),
        ("Cortesia", "Cortesia"),
        ("Cancelado", "Cancelado"),
    ]
    PAYMENT_METHODS = ["", "PIX", "Dinheiro", "Crédito", "Débito", "Unimed", "Outro"]

    def __init__(self, parent: QWidget, appointment: dict[str, Any], repo: SupabaseRepository, money_edit_formatter):
        super().__init__(parent)
        self.appointment = appointment
        self.repo = repo
        self.money_edit = money_edit_formatter
        self.setWindowTitle("Editar financeiro do atendimento")
        self.setMinimumSize(560, 430)

        financial = repo.financial_row(appointment)
        patient = (appointment.get("patients") or {}).get("full_name", "Paciente não informado")
        professional = (appointment.get("professionals") or {}).get("full_name", "Funcionário não informado")
        date_text = appointment.get("appointment_date", "")
        time_text = f"{appointment.get('start_time', '')[:5]} às {appointment.get('end_time', '')[:5]}"
        status_text = appointment.get("status", "")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)

        header = QFrame()
        style_card(header, COLORS["surface_warm"])
        header_layout = QGridLayout(header)
        header_layout.setContentsMargins(18, 14, 18, 14)
        title = QLabel(patient)
        title.setObjectName("PageTitle")
        title.setStyleSheet("font-size: 20px; background: transparent;")
        subtitle = QLabel(f"{date_text} - {time_text}")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        professional_label = QLabel(f"Profissional: {professional}")
        professional_label.setStyleSheet("background: transparent; font-weight: 700;")
        status_label = QLabel(f"Atendimento: {status_text}")
        status_label.setStyleSheet("background: transparent; font-weight: 700;")
        header_layout.addWidget(title, 0, 0)
        header_layout.addWidget(subtitle, 1, 0)
        header_layout.addWidget(professional_label, 0, 1, Qt.AlignRight)
        header_layout.addWidget(status_label, 1, 1, Qt.AlignRight)

        form_panel = page_card()
        form = QGridLayout(form_panel)
        form.setContentsMargins(18, 18, 18, 18)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)

        self.consultation_fee = QLineEdit(self.money_edit(financial.get("consultation_fee")))
        self.consultation_fee.setPlaceholderText("Ex.: 150,00")
        self.payment_status = QComboBox()
        for label, value in self.PAYMENT_STATUSES:
            self.payment_status.addItem(label, value)
        payment_status_index = self.payment_status.findData(financial.get("payment_status") or "Nao informado")
        self.payment_status.setCurrentIndex(max(payment_status_index, 0))
        self.payment_method = QComboBox()
        self.payment_method.addItems(self.PAYMENT_METHODS)
        self.payment_method.setEditable(True)
        current_method = financial.get("payment_method") or ""
        method_index = self.payment_method.findText(current_method)
        if method_index >= 0:
            self.payment_method.setCurrentIndex(method_index)
        else:
            self.payment_method.setEditText(current_method)

        self.add_field(form, "Valor do atendimento", self.consultation_fee, 0, 0)
        self.add_field(form, "Status do pagamento", self.payment_status, 0, 1)
        self.add_field(form, "Forma de pagamento", self.payment_method, 1, 0, 1, 2)

        actions = QHBoxLayout()
        cancel = button("Cancelar", secondary=True)
        cancel.clicked.connect(self.reject)
        save = button("Salvar")
        save.setMinimumWidth(150)
        save.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save)

        root.addWidget(header)
        root.addWidget(form_panel)
        root.addStretch()
        root.addLayout(actions)

    def add_field(self, layout: QGridLayout, label: str, widget: QWidget, row: int, col: int, row_span: int = 1, col_span: int = 1) -> None:
        box = QVBoxLayout()
        box.setSpacing(6)
        text = QLabel(label)
        text.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-weight: 600;")
        widget.setMinimumHeight(44)
        box.addWidget(text)
        box.addWidget(widget)
        layout.addLayout(box, row, col, row_span, col_span)

    def values(self) -> dict[str, Any]:
        return {
            "consultation_fee": self.consultation_fee.text().strip(),
            "payment_status": self.payment_status.currentData(),
            "payment_method": self.payment_method.currentText().strip(),
        }


class PatientReceiptDialog(QDialog):
    PAYMENT_METHODS = ["PIX", "Dinheiro", "Crédito", "Débito", "Unimed", "Outro"]

    def __init__(self, parent: QWidget, repo: SupabaseRepository, money_formatter):
        super().__init__(parent)
        self.repo = repo
        self.money = money_formatter
        self.rows: list[dict[str, Any]] = []
        self.receipt_checks: list[QCheckBox] = []
        self.updating_checks = False
        self.setWindowTitle("Registrar recebimento")
        self.setMinimumSize(1120, 820)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        header = QFrame()
        style_card(header, COLORS["surface_warm"])
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 18, 12)
        title = QLabel("Recebimento de paciente")
        title.setObjectName("PageTitle")
        title.setStyleSheet("font-size: 21px; background: transparent;")
        subtitle = QLabel("Selecione os atendimentos realizados que este pagamento cobre.")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)

        form_panel = page_card()
        form = QGridLayout(form_panel)
        form.setContentsMargins(24, 16, 24, 16)
        form.setHorizontalSpacing(22)
        form.setVerticalSpacing(10)
        form.setColumnStretch(0, 1)
        form.setColumnStretch(1, 1)

        self.patient = QComboBox()
        self.patient.setMinimumWidth(260)
        self.patient.setMinimumHeight(44)
        self.payment_date = QDateEdit(QDate.currentDate())
        self.payment_date.setCalendarPopup(True)
        self.payment_date.setMinimumHeight(44)
        self.payment_method = QComboBox()
        self.payment_method.addItems(self.PAYMENT_METHODS)
        self.payment_method.setMinimumHeight(44)
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("Ex.: 150,00")
        self.amount.setMinimumHeight(44)
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("Observação do recebimento")
        self.notes.setMinimumHeight(46)
        self.notes.setMaximumHeight(52)

        self.add_field(form, "Paciente", self.patient, 0, 0)
        self.add_field(form, "Data", self.payment_date, 0, 1)
        self.add_field(form, "Forma de pagamento", self.payment_method, 1, 0)
        self.add_field(form, "Valor recebido", self.amount, 1, 1)
        form.addWidget(QLabel("Observação"), 2, 0, 1, 2)
        form.addWidget(self.notes, 3, 0, 1, 2)

        list_panel = page_card()
        list_panel.setMinimumHeight(390)
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(24, 18, 24, 18)
        list_layout.setSpacing(14)
        list_header = QHBoxLayout()
        list_title = QLabel("Atendimentos em aberto")
        list_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        self.total_label = QLabel("Selecionado: R$ 0,00")
        self.total_label.setObjectName("Badge")
        list_header.addWidget(list_title)
        list_header.addStretch()
        list_header.addWidget(self.total_label)

        period_panel = QFrame()
        period_panel.setStyleSheet(
            f"""
            QFrame {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['line']};
                border-radius: 8px;
            }}
            QLabel {{
                background: transparent;
                color: {COLORS['text']};
                font-weight: 600;
            }}
            """
        )
        period_layout = QHBoxLayout(period_panel)
        period_layout.setContentsMargins(14, 10, 14, 10)
        period_layout.setSpacing(12)
        period_title = QLabel("Selecionar por período")
        self.period_start = QDateEdit(QDate.currentDate())
        self.period_start.setCalendarPopup(True)
        self.period_start.setDisplayFormat("dd/MM/yyyy")
        self.period_start.setMinimumHeight(38)
        self.period_start.setMinimumWidth(130)
        self.period_end = QDateEdit(QDate.currentDate())
        self.period_end.setCalendarPopup(True)
        self.period_end.setDisplayFormat("dd/MM/yyyy")
        self.period_end.setMinimumHeight(38)
        self.period_end.setMinimumWidth(130)
        select_period = button("Selecionar período")
        select_period.setMinimumHeight(38)
        select_period.clicked.connect(self.select_period_appointments)
        period_layout.addWidget(period_title)
        period_layout.addStretch()
        period_layout.addWidget(QLabel("Início"))
        period_layout.addWidget(self.period_start)
        period_layout.addWidget(QLabel("Fim"))
        period_layout.addWidget(self.period_end)
        period_layout.addWidget(select_period)

        self.table = QTableWidget(0, 7)
        self.table.setMinimumHeight(300)
        self.table.setHorizontalHeaderLabels(["Receber", "Data", "Horário", "Status", "Valor", "Já pago", "Em aberto"])
        table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionMode(QAbstractItemView.MultiSelection)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        check_icon = resource_path("check_white.svg").as_posix()
        self.table.setStyleSheet(
            self.table.styleSheet()
            + f"""
            QTableWidget::item {{
                border: none;
                border-radius: 0px;
                padding: 0px;
            }}
            QTableWidget::item:selected {{
                background: {COLORS['primary_soft']};
                color: {COLORS['text']};
            }}
            QCheckBox {{
                background: transparent;
                spacing: 0px;
            }}
            QCheckBox::indicator {{
                width: 22px;
                height: 22px;
                border-radius: 5px;
                border: 2px solid {COLORS['primary']};
                background: #FFFFFF;
            }}
            QCheckBox::indicator:checked {{
                background: {COLORS['primary']};
                border: 2px solid {COLORS['primary']};
                image: url("{check_icon}");
            }}
            """
        )
        list_layout.addLayout(list_header)
        list_layout.addWidget(period_panel)
        list_layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(14)
        cancel = button("Cancelar", secondary=True)
        cancel.clicked.connect(self.reject)
        save = button("Salvar recebimento")
        save.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save)

        root.addWidget(header)
        root.addWidget(form_panel)
        root.addWidget(list_panel, 1)
        root.addLayout(actions)

        self.load_patients()
        self.patient.currentIndexChanged.connect(self.load_open_appointments)
        self.load_open_appointments()

    def add_field(self, layout: QGridLayout, label: str, widget: QWidget, row: int, col: int) -> None:
        box = QVBoxLayout()
        box.setSpacing(8)
        text = QLabel(label)
        text.setStyleSheet("background: transparent;")
        box.addWidget(text)
        box.addWidget(widget)
        layout.addLayout(box, row, col)

    def load_patients(self) -> None:
        self.patient.clear()
        try:
            patients = self.repo.patients(active_only=False)
        except Exception:
            patients = []
        for patient in patients:
            self.patient.addItem(patient.get("full_name", ""), patient.get("id"))

    def load_open_appointments(self) -> None:
        patient_id = self.patient.currentData()
        if not patient_id:
            self.rows = []
            self.fill_table()
            return
        try:
            self.rows = self.repo.patient_open_payment_appointments(patient_id)
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
        self.fill_table()

    def fill_table(self) -> None:
        self.updating_checks = True
        self.receipt_checks = []
        self.table.clearContents()
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            financial = self.repo.financial_row(row)
            selectable = float(row.get("_open_amount") or 0) > 0.009
            values = [
                row.get("appointment_date", ""),
                f"{row.get('start_time', '')[:5]} às {row.get('end_time', '')[:5]}",
                row.get("status", "Não informado"),
                self.money(financial.get("consultation_fee")),
                self.money(row.get("_paid_amount")),
                self.money(row.get("_open_amount")),
            ]
            check = QCheckBox()
            check.setCursor(Qt.PointingHandCursor)
            check.setEnabled(selectable)
            check.stateChanged.connect(self.update_selected_total)
            check_holder = QWidget()
            check_holder.setStyleSheet(f"background: {COLORS['surface'] if selectable else COLORS['bg2']};")
            check_layout = QHBoxLayout(check_holder)
            check_layout.setContentsMargins(0, 0, 0, 0)
            check_layout.setAlignment(Qt.AlignCenter)
            check_layout.addWidget(check)
            self.table.setCellWidget(row_index, 0, check_holder)
            self.receipt_checks.append(check)

            for col, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter)
                if not selectable:
                    item.setBackground(QColor(COLORS["bg2"]))
                    item.setForeground(QColor(COLORS["muted"]))
                self.table.setItem(row_index, col, item)
        widths = [80, 130, 140, 150, 120, 120, 150]
        for col, width in enumerate(widths):
            self.table.setColumnWidth(col, width)
        for row_index in range(len(self.rows)):
            self.table.setRowHeight(row_index, 44)
        self.updating_checks = False
        self.update_selected_total()

    def appointment_date(self, row: dict[str, Any]) -> date | None:
        raw_date = row.get("appointment_date")
        if not raw_date:
            return None
        if isinstance(raw_date, date):
            return raw_date
        try:
            return date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            return None

    def select_period_appointments(self) -> None:
        start = self.period_start.date().toPython()
        end = self.period_end.date().toPython()
        if start > end:
            start, end = end, start

        self.updating_checks = True
        try:
            for row_index, row in enumerate(self.rows):
                appointment_date = self.appointment_date(row)
                has_balance = float(row.get("_open_amount") or 0) > 0.009
                checked = has_balance and appointment_date is not None and start <= appointment_date <= end
                if row_index < len(self.receipt_checks):
                    self.receipt_checks[row_index].setChecked(checked)
        finally:
            self.updating_checks = False
        self.update_selected_total()

    def selected_total(self) -> float:
        total = 0.0
        for row_index, row in enumerate(self.rows):
            if (
                row_index < len(self.receipt_checks)
                and self.receipt_checks[row_index].isEnabled()
                and self.receipt_checks[row_index].isChecked()
            ):
                total += float(row.get("_open_amount") or 0)
        return total

    def update_selected_total(self) -> None:
        if self.updating_checks:
            return
        self.updating_checks = True
        try:
            for row_index in range(self.table.rowCount()):
                checked = row_index < len(self.receipt_checks) and self.receipt_checks[row_index].isChecked()
                enabled = row_index < len(self.receipt_checks) and self.receipt_checks[row_index].isEnabled()
                holder = self.table.cellWidget(row_index, 0)
                if holder:
                    background = COLORS["primary_soft"] if checked else COLORS["surface"] if enabled else COLORS["bg2"]
                    holder.setStyleSheet(f"background: {background};")
                for col in range(self.table.columnCount()):
                    item = self.table.item(row_index, col)
                    if not item:
                        continue
                    item.setSelected(checked)
        finally:
            self.updating_checks = False
        total = self.selected_total()
        self.total_label.setText(f"Selecionado: {self.money(total)}")
        self.amount.setText(self.money(total).replace("R$ ", ""))

    def selected_items(self) -> list[dict[str, Any]]:
        items = []
        for row_index, row in enumerate(self.rows):
            if (
                row_index < len(self.receipt_checks)
                and self.receipt_checks[row_index].isEnabled()
                and self.receipt_checks[row_index].isChecked()
            ):
                items.append({"appointment_id": row["id"], "open_amount": float(row.get("_open_amount") or 0)})
        return items

    def values(self) -> dict[str, Any]:
        return {
            "patient_id": self.patient.currentData(),
            "payment_date": self.payment_date.date().toPython().isoformat(),
            "payment_method": self.payment_method.currentText(),
            "amount": self.amount.text().strip(),
            "notes": self.notes.toPlainText().strip(),
        }


class ProfessionalPayoutDialog(QDialog):
    PAYMENT_METHODS = ["PIX", "Dinheiro", "Crédito", "Débito", "Unimed", "Outro"]

    def __init__(self, parent: QWidget, repo: SupabaseRepository, money_formatter):
        super().__init__(parent)
        self.repo = repo
        self.money = money_formatter
        self.rows: list[dict[str, Any]] = []
        self.payout_checks: list[QCheckBox] = []
        self.updating_checks = False
        self.setWindowTitle("Registrar repasse")
        self.setMinimumSize(1080, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        header = QFrame()
        style_card(header, COLORS["surface_warm"])
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 18, 12)
        title = QLabel("Repasse de funcionário")
        title.setObjectName("PageTitle")
        title.setStyleSheet("font-size: 21px; background: transparent;")
        subtitle = QLabel("Selecione os atendimentos realizados que entram neste repasse.")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)

        form_panel = page_card()
        form = QGridLayout(form_panel)
        form.setContentsMargins(24, 16, 24, 16)
        form.setHorizontalSpacing(22)
        form.setVerticalSpacing(10)
        form.setColumnStretch(0, 1)
        form.setColumnStretch(1, 1)

        self.professional = QComboBox()
        self.professional.setMinimumWidth(260)
        self.professional.setMinimumHeight(44)
        self.payout_date = QDateEdit(QDate.currentDate())
        self.payout_date.setCalendarPopup(True)
        self.payout_date.setMinimumHeight(44)
        self.payment_method = QComboBox()
        self.payment_method.addItems(self.PAYMENT_METHODS)
        self.payment_method.setMinimumHeight(44)
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("Ex.: 105,00")
        self.amount.setMinimumHeight(44)
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("Observação do repasse")
        self.notes.setMinimumHeight(54)
        self.notes.setMaximumHeight(62)

        self.add_field(form, "Funcionário", self.professional, 0, 0)
        self.add_field(form, "Data", self.payout_date, 0, 1)
        self.add_field(form, "Forma de pagamento", self.payment_method, 1, 0)
        self.add_field(form, "Valor do repasse", self.amount, 1, 1)
        form.addWidget(QLabel("Observação"), 2, 0, 1, 2)
        form.addWidget(self.notes, 3, 0, 1, 2)

        list_panel = page_card()
        list_panel.setMinimumHeight(300)
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(24, 18, 24, 18)
        list_layout.setSpacing(14)
        list_header = QHBoxLayout()
        list_title = QLabel("Atendimentos a repassar")
        list_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        self.total_label = QLabel("Selecionado: R$ 0,00")
        self.total_label.setObjectName("Badge")
        list_header.addWidget(list_title)
        list_header.addStretch()
        list_header.addWidget(self.total_label)

        period_panel = QFrame()
        period_panel.setStyleSheet(
            f"""
            QFrame {{
                background: {COLORS['surface']};
                border: 1px solid {COLORS['line']};
                border-radius: 8px;
            }}
            QLabel {{
                background: transparent;
                color: {COLORS['text']};
                font-weight: 600;
            }}
            """
        )
        period_layout = QHBoxLayout(period_panel)
        period_layout.setContentsMargins(14, 10, 14, 10)
        period_layout.setSpacing(12)
        period_title = QLabel("Selecionar por período")
        self.period_start = QDateEdit(QDate.currentDate())
        self.period_start.setCalendarPopup(True)
        self.period_start.setDisplayFormat("dd/MM/yyyy")
        self.period_start.setMinimumHeight(38)
        self.period_start.setMinimumWidth(130)
        self.period_end = QDateEdit(QDate.currentDate())
        self.period_end.setCalendarPopup(True)
        self.period_end.setDisplayFormat("dd/MM/yyyy")
        self.period_end.setMinimumHeight(38)
        self.period_end.setMinimumWidth(130)
        select_period = button("Selecionar período")
        select_period.setMinimumHeight(38)
        select_period.clicked.connect(self.select_period_appointments)
        period_layout.addWidget(period_title)
        period_layout.addStretch()
        period_layout.addWidget(QLabel("Início"))
        period_layout.addWidget(self.period_start)
        period_layout.addWidget(QLabel("Fim"))
        period_layout.addWidget(self.period_end)
        period_layout.addWidget(select_period)

        self.table = QTableWidget(0, 7)
        self.table.setMinimumHeight(210)
        self.table.setHorizontalHeaderLabels(["Pagar", "Data", "Horário", "Paciente", "Bruto", "Já pago", "A pagar"])
        table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionMode(QAbstractItemView.MultiSelection)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        check_icon = resource_path("check_white.svg").as_posix()
        self.table.setStyleSheet(
            self.table.styleSheet()
            + f"""
            QTableWidget::item {{
                border: none;
                border-radius: 0px;
                padding: 0px;
            }}
            QTableWidget::item:selected {{
                background: {COLORS['primary_soft']};
                color: {COLORS['text']};
            }}
            QCheckBox {{
                background: transparent;
                spacing: 0px;
            }}
            QCheckBox::indicator {{
                width: 22px;
                height: 22px;
                border-radius: 5px;
                border: 2px solid {COLORS['primary']};
                background: #FFFFFF;
            }}
            QCheckBox::indicator:checked {{
                background: {COLORS['primary']};
                border: 2px solid {COLORS['primary']};
                image: url("{check_icon}");
            }}
            """
        )
        list_layout.addLayout(list_header)
        list_layout.addWidget(period_panel)
        list_layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(14)
        cancel = button("Cancelar", secondary=True)
        cancel.clicked.connect(self.reject)
        save = button("Salvar repasse")
        save.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save)

        root.addWidget(header)
        root.addWidget(form_panel)
        root.addWidget(list_panel, 1)
        root.addLayout(actions)

        self.load_professionals()
        self.professional.currentIndexChanged.connect(self.load_open_appointments)
        self.load_open_appointments()

    def add_field(self, layout: QGridLayout, label: str, widget: QWidget, row: int, col: int) -> None:
        box = QVBoxLayout()
        box.setSpacing(8)
        text = QLabel(label)
        text.setStyleSheet("background: transparent;")
        box.addWidget(text)
        box.addWidget(widget)
        layout.addLayout(box, row, col)

    def load_professionals(self) -> None:
        self.professional.clear()
        try:
            professionals = self.repo.professionals(active_only=False)
        except Exception:
            professionals = []
        for professional in professionals:
            self.professional.addItem(professional.get("full_name", ""), professional.get("id"))

    def load_open_appointments(self) -> None:
        professional_id = self.professional.currentData()
        if not professional_id:
            self.rows = []
            self.fill_table()
            return
        try:
            self.rows = self.repo.professional_open_payout_appointments(professional_id)
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
        self.fill_table()

    def fill_table(self) -> None:
        self.updating_checks = True
        self.payout_checks = []
        self.table.clearContents()
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            patient = (row.get("patients") or {}).get("full_name", "")
            selectable = float(row.get("_open_payout_amount") or 0) > 0.009
            values = [
                row.get("appointment_date", ""),
                f"{row.get('start_time', '')[:5]} às {row.get('end_time', '')[:5]}",
                patient,
                self.money(row.get("_gross_amount")),
                self.money(row.get("_paid_payout_amount")),
                self.money(row.get("_open_payout_amount")),
            ]

            check = QCheckBox()
            check.setCursor(Qt.PointingHandCursor)
            check.setEnabled(selectable)
            check.stateChanged.connect(self.update_selected_total)
            check_holder = QWidget()
            check_holder.setStyleSheet(f"background: {COLORS['surface'] if selectable else COLORS['bg2']};")
            check_layout = QHBoxLayout(check_holder)
            check_layout.setContentsMargins(0, 0, 0, 0)
            check_layout.setAlignment(Qt.AlignCenter)
            check_layout.addWidget(check)
            self.table.setCellWidget(row_index, 0, check_holder)
            self.payout_checks.append(check)

            for col, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter)
                if not selectable:
                    item.setBackground(QColor(COLORS["bg2"]))
                    item.setForeground(QColor(COLORS["muted"]))
                self.table.setItem(row_index, col, item)
        widths = [70, 110, 120, 220, 110, 110, 120]
        for col, width in enumerate(widths):
            self.table.setColumnWidth(col, width)
        for row_index in range(len(self.rows)):
            self.table.setRowHeight(row_index, 44)
        self.updating_checks = False
        self.update_selected_total()

    def appointment_date(self, row: dict[str, Any]) -> date | None:
        raw_date = row.get("appointment_date")
        if not raw_date:
            return None
        if isinstance(raw_date, date):
            return raw_date
        try:
            return date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            return None

    def select_period_appointments(self) -> None:
        start = self.period_start.date().toPython()
        end = self.period_end.date().toPython()
        if start > end:
            start, end = end, start

        self.updating_checks = True
        try:
            for row_index, row in enumerate(self.rows):
                appointment_date = self.appointment_date(row)
                has_balance = float(row.get("_open_payout_amount") or 0) > 0.009
                checked = has_balance and appointment_date is not None and start <= appointment_date <= end
                if row_index < len(self.payout_checks):
                    self.payout_checks[row_index].setChecked(checked)
        finally:
            self.updating_checks = False
        self.update_selected_total()

    def selected_total(self) -> float:
        total = 0.0
        for row_index, row in enumerate(self.rows):
            if (
                row_index < len(self.payout_checks)
                and self.payout_checks[row_index].isEnabled()
                and self.payout_checks[row_index].isChecked()
            ):
                total += float(row.get("_open_payout_amount") or 0)
        return total

    def update_selected_total(self) -> None:
        if self.updating_checks:
            return
        self.updating_checks = True
        try:
            for row_index in range(self.table.rowCount()):
                checked = row_index < len(self.payout_checks) and self.payout_checks[row_index].isChecked()
                enabled = row_index < len(self.payout_checks) and self.payout_checks[row_index].isEnabled()
                holder = self.table.cellWidget(row_index, 0)
                if holder:
                    background = COLORS["primary_soft"] if checked else COLORS["surface"] if enabled else COLORS["bg2"]
                    holder.setStyleSheet(f"background: {background};")
                for col in range(self.table.columnCount()):
                    item = self.table.item(row_index, col)
                    if not item:
                        continue
                    item.setSelected(checked)
        finally:
            self.updating_checks = False
        total = self.selected_total()
        self.total_label.setText(f"Selecionado: {self.money(total)}")
        self.amount.setText(self.money(total).replace("R$ ", ""))

    def selected_items(self) -> list[dict[str, Any]]:
        items = []
        for row_index, row in enumerate(self.rows):
            if (
                row_index < len(self.payout_checks)
                and self.payout_checks[row_index].isEnabled()
                and self.payout_checks[row_index].isChecked()
            ):
                items.append({"appointment_id": row["id"], "open_amount": float(row.get("_open_payout_amount") or 0)})
        return items

    def values(self) -> dict[str, Any]:
        return {
            "professional_id": self.professional.currentData(),
            "payout_date": self.payout_date.date().toPython().isoformat(),
            "payment_method": self.payment_method.currentText(),
            "amount": self.amount.text().strip(),
            "notes": self.notes.toPlainText().strip(),
        }


class DocumentUploadDialog(QDialog):
    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any], professionals: list[dict[str, Any]], parent=None):
        super().__init__(parent)
        self.repo = repo
        self.profile = profile
        self.professionals = professionals
        self.setWindowTitle("Enviar documento")
        self.setMinimumSize(720, 430)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 20)
        layout.setSpacing(16)

        header = page_card()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 14, 18, 14)
        title = QLabel("Enviar documento")
        title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 20px; font-weight: 800;")
        subtitle = QLabel("Escolha o arquivo e o profissional responsável pelo documento.")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        layout.addWidget(header)

        form_card = page_card()
        form = QGridLayout(form_card)
        form.setContentsMargins(18, 18, 18, 18)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)

        self.professional = QComboBox()
        self.professional.setMinimumHeight(48)
        can_view_all = profile.get("role") in {"admin", "reception"}
        if can_view_all:
            for professional in professionals:
                self.professional.addItem(professional.get("full_name", ""), professional.get("id"))
        else:
            self.professional.addItem(profile.get("full_name", "Meu perfil"), profile.get("id"))
            self.professional.setEnabled(False)

        self.file_path = QLineEdit()
        self.file_path.setReadOnly(True)
        self.file_path.setPlaceholderText("Nenhum arquivo selecionado")
        self.file_path.setMinimumHeight(48)
        choose_file = button("Selecionar arquivo", secondary=True)
        choose_file.clicked.connect(self.choose_file)

        self.description = QTextEdit()
        self.description.setPlaceholderText("Descrição breve para identificar o documento")
        self.description.setFixedHeight(96)

        form.addLayout(self.field("Profissional", self.professional), 0, 0)
        file_row = QHBoxLayout()
        file_row.setSpacing(10)
        file_row.addWidget(self.file_path, 1)
        file_row.addWidget(choose_file)
        form.addLayout(self.field("Arquivo", file_row), 0, 1)
        form.addLayout(self.field("Descrição", self.description), 1, 0, 1, 2)
        form.setColumnStretch(0, 1)
        form.setColumnStretch(1, 1)
        layout.addWidget(form_card, 1)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = button("Cancelar", secondary=True)
        cancel.clicked.connect(self.reject)
        save = button("Enviar documento")
        save.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def field(self, label: str, widget_or_layout) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(6)
        text = QLabel(label)
        text.setObjectName("Muted")
        text.setStyleSheet("background: transparent;")
        box.addWidget(text)
        if isinstance(widget_or_layout, QVBoxLayout) or isinstance(widget_or_layout, QHBoxLayout):
            box.addLayout(widget_or_layout)
        else:
            box.addWidget(widget_or_layout)
        return box

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Selecionar documento", "", DOCUMENT_FILE_FILTER)
        if path:
            self.file_path.setText(path)

    def values(self) -> dict[str, Any]:
        return {
            "local_path": self.file_path.text().strip(),
            "professional_id": self.professional.currentData(),
            "description": self.description.toPlainText().strip(),
        }


class DocumentsPage(QWidget):
    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any]):
        super().__init__()
        self.repo = repo
        self.profile = profile
        self.rows: list[dict[str, Any]] = []
        self.professionals: list[dict[str, Any]] = []
        self.can_view_all = profile.get("role") in {"admin", "reception"}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)

        title = QLabel("Documentos")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Arquivos dos profissionais organizados em nuvem.")
        subtitle.setObjectName("Muted")

        filters_panel = page_card()
        filters = QHBoxLayout(filters_panel)
        filters.setContentsMargins(18, 14, 18, 14)
        filters.setSpacing(14)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar documento...")
        self.search.setMinimumWidth(360)
        self.search.textChanged.connect(lambda _text="": self.refresh())
        self.professional_filter = QComboBox()
        self.professional_filter.setMinimumWidth(260)
        self.professional_filter.currentIndexChanged.connect(lambda _index=0: self.refresh())
        upload_btn = button("Enviar documento")
        upload_btn.clicked.connect(self.upload_document)
        download_btn = button("Baixar", secondary=True)
        download_btn.clicked.connect(self.download_selected)
        delete_btn = button("Excluir documento", danger=True)
        delete_btn.clicked.connect(self.delete_selected)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.professional_filter)
        filters.addStretch()
        filters.addWidget(upload_btn)
        filters.addWidget(download_btn)
        filters.addWidget(delete_btn)

        panel = page_card()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 18, 18, 18)
        panel_layout.setSpacing(12)
        header = QHBoxLayout()
        table_title = QLabel("Documentos enviados")
        table_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        self.badge = QLabel("0 documentos")
        self.badge.setObjectName("Badge")
        header.addWidget(table_title)
        header.addStretch()
        header.addWidget(self.badge)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Documento", "Profissional", "Tipo", "Tamanho", "Enviado em", "Descrição"])
        full_row_table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(lambda item: self.download_selected())
        panel_layout.addLayout(header)
        panel_layout.addWidget(self.table, 1)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(filters_panel)
        layout.addWidget(panel, 1)
        self.professionals_loaded = False

    def load_professionals(self) -> None:
        try:
            self.professionals = self.repo.document_professionals()
            self.professionals_loaded = True
        except Exception as exc:
            show_error(self, exc)
            self.professionals = []
            self.professionals_loaded = False
        self.professional_filter.blockSignals(True)
        self.professional_filter.clear()
        if self.can_view_all:
            self.professional_filter.addItem("Todos os profissionais", None)
            for professional in self.professionals:
                self.professional_filter.addItem(professional.get("full_name", ""), professional.get("id"))
        else:
            self.professional_filter.addItem(self.profile.get("full_name", "Meus documentos"), self.profile.get("id"))
            self.professional_filter.setEnabled(False)
        self.professional_filter.blockSignals(False)

    def refresh(self) -> None:
        if not self.professionals_loaded:
            self.load_professionals()
        try:
            professional_id = self.professional_filter.currentData() if self.can_view_all else self.profile.get("id")
            self.rows = self.repo.documents(self.search.text().strip(), professional_id)
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
        self.fill_table()

    def fill_table(self) -> None:
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            values = [
                row.get("file_name", ""),
                row.get("professional_name", ""),
                self.format_type(row.get("file_type", ""), row.get("file_name", "")),
                self.format_size(row.get("file_size")),
                self.format_datetime(row.get("created_at", "")),
                row.get("description", ""),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if col == 0:
                    item.setData(Qt.UserRole, row.get("id"))
                self.table.setItem(row_index, col, item)
            self.table.setRowHeight(row_index, 42)
        self.badge.setText(f"{len(self.rows)} documentos")
        self.table.resizeColumnsToContents()

    def current_document(self) -> dict[str, Any] | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.rows):
            return None
        return self.rows[row]

    def upload_document(self) -> None:
        dialog = DocumentUploadDialog(self.repo, self.profile, self.professionals, self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        try:
            self.repo.upload_document(values["local_path"], values["professional_id"], values["description"])
            QMessageBox.information(self, "Documentos", "Documento enviado com sucesso.")
            self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def download_selected(self) -> None:
        document = self.current_document()
        if not document:
            show_error(self, AppError("Selecione um documento para baixar."))
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Salvar documento",
            self.download_name(document),
            self.download_filter(document),
        )
        if not path:
            return
        path = self.ensure_download_extension(path, document.get("file_name", ""))
        try:
            self.repo.download_document(document["id"], path)
            QMessageBox.information(self, "Documentos", "Documento baixado com sucesso.")
        except Exception as exc:
            show_error(self, exc)

    def delete_selected(self) -> None:
        document = self.current_document()
        if not document:
            show_error(self, AppError("Selecione um documento para excluir."))
            return
        answer = QMessageBox.question(
            self,
            "Excluir documento",
            "Este arquivo será removido definitivamente e não poderá ser recuperado. Deseja continuar?",
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.repo.delete_document(document["id"])
            QMessageBox.information(self, "Documentos", "Documento excluido com sucesso.")
            self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def format_size(self, value: Any) -> str:
        try:
            size = float(value or 0)
        except (TypeError, ValueError):
            return ""
        units = ["B", "KB", "MB", "GB"]
        index = 0
        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1
        if index == 0:
            return f"{int(size)} {units[index]}"
        return f"{size:.1f} {units[index]}"

    def download_name(self, document: dict[str, Any]) -> str:
        name = str(document.get("file_name") or "").strip()
        return name or "documento"

    def download_filter(self, document: dict[str, Any]) -> str:
        suffix = Path(str(document.get("file_name") or "")).suffix.lower()
        filters = {
            ".pdf": "PDF (*.pdf)",
            ".doc": "Word (*.doc)",
            ".docx": "Word (*.docx)",
            ".xls": "Excel (*.xls)",
            ".xlsx": "Excel (*.xlsx)",
            ".csv": "CSV (*.csv)",
            ".ppt": "PowerPoint (*.ppt)",
            ".pptx": "PowerPoint (*.pptx)",
            ".txt": "Texto (*.txt)",
            ".rtf": "RTF (*.rtf)",
            ".png": "PNG (*.png)",
            ".jpg": "JPEG (*.jpg *.jpeg)",
            ".jpeg": "JPEG (*.jpg *.jpeg)",
            ".webp": "WEBP (*.webp)",
            ".gif": "GIF (*.gif)",
            ".bmp": "BMP (*.bmp)",
            ".zip": "ZIP (*.zip)",
            ".rar": "RAR (*.rar)",
            ".7z": "7Z (*.7z)",
        }
        if suffix in filters:
            return f"{filters[suffix]};;Todos os arquivos (*.*)"
        return "Todos os arquivos (*.*)"

    def ensure_download_extension(self, target_path: str, original_name: str) -> str:
        target = Path(target_path)
        original_suffix = Path(original_name or "").suffix
        if original_suffix and not target.suffix:
            return str(target.with_suffix(original_suffix))
        return str(target)

    def format_type(self, value: str, file_name: str = "") -> str:
        suffix = Path(file_name or "").suffix.lower()
        extension_labels = {
            ".pdf": "PDF",
            ".doc": "Word",
            ".docx": "Word",
            ".xls": "Excel",
            ".xlsx": "Excel",
            ".csv": "CSV",
            ".ppt": "PowerPoint",
            ".pptx": "PowerPoint",
            ".txt": "Texto",
            ".rtf": "RTF",
            ".png": "Imagem PNG",
            ".jpg": "Imagem JPG",
            ".jpeg": "Imagem JPG",
            ".webp": "Imagem WEBP",
            ".gif": "Imagem GIF",
            ".bmp": "Imagem BMP",
            ".zip": "ZIP",
            ".rar": "RAR",
            ".7z": "7Z",
        }
        if suffix in extension_labels:
            return extension_labels[suffix]
        if not value:
            return "Arquivo"
        if value.startswith("image/"):
            return f"Imagem {value.split('/')[-1].upper()}"
        if value.startswith("text/"):
            return "Texto"
        return value.split("/")[-1].upper()

    def format_datetime(self, value: str) -> str:
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return str(value)[:16]


class SettingsPage(QWidget):
    def __init__(self, repo: SupabaseRepository):
        super().__init__()
        self.repo = repo
        self.profile_rows: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)
        title = QLabel("Configurações")
        title.setObjectName("PageTitle")
        text = QLabel("Usuários, permissões por perfil, status e backup simples.")
        text.setObjectName("Muted")

        users_actions = QHBoxLayout()
        new_user = button("Novo usuário")
        new_user.clicked.connect(self.create_user)
        edit_user = button("Editar permissões", secondary=True)
        edit_user.clicked.connect(self.edit_selected_user)
        delete_user = button("Excluir funcionário", danger=True)
        delete_user.clicked.connect(self.delete_selected_user)
        users_actions.addWidget(new_user)
        users_actions.addWidget(edit_user)
        users_actions.addWidget(delete_user)
        users_actions.addStretch()

        users_title = QLabel("Usuários do sistema")
        users_title.setObjectName("PageTitle")
        users_title.setStyleSheet("font-size: 18px;")
        self.users_table = QTableWidget(0, 9)
        self.users_table.setHorizontalHeaderLabels(["Nome", "E-mail", "Permissão", "Status", "Atende", "Especialidade", "CPF", "Telefone", "Cidade"])
        table_polish(self.users_table)
        self.users_table.cellDoubleClicked.connect(lambda row, col: self.edit_user(row))

        permissions_title = QLabel("Resumo das permissões")
        permissions_title.setObjectName("PageTitle")
        permissions_title.setStyleSheet("font-size: 18px;")
        export = button("Exportar backup JSON")
        export.clicked.connect(self.export_backup)
        self.table = QTableWidget(3, 4)
        self.table.setHorizontalHeaderLabels(["Perfil", "Pacientes", "Agenda", "Configurações"])
        table_polish(self.table)
        rows = [
            ("Administrador", "Sim", "Sim", "Sim"),
            ("Recepção", "Sim", "Sim", "Sim"),
            ("Profissional", "Não", "Própria agenda", "Não"),
        ]
        for row_idx, row in enumerate(rows):
            for col_idx, value in enumerate(row):
                self.table.setItem(row_idx, col_idx, QTableWidgetItem(value))
        layout.addWidget(title)
        layout.addWidget(text)
        layout.addWidget(users_title)
        layout.addLayout(users_actions)
        layout.addWidget(self.users_table)
        layout.addWidget(permissions_title)
        layout.addWidget(self.table)
        layout.addWidget(export)
        layout.addStretch()

    def refresh(self) -> None:
        try:
            self.profile_rows = self.repo.profiles()
        except Exception as exc:
            show_error(self, exc)
            self.profile_rows = []
        self.users_table.setRowCount(len(self.profile_rows))
        for row_index, row in enumerate(self.profile_rows):
            values = [
                row.get("full_name", ""),
                row.get("email", ""),
                ROLE_LABELS.get(row.get("role"), row.get("role", "")),
                "Ativo" if row.get("is_active") else "Inativo",
                "Sim" if row.get("can_attend") else "Não",
                row.get("specialty", ""),
                row.get("cpf", ""),
                row.get("phone", ""),
                row.get("city", ""),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.users_table.setItem(row_index, col, item)
        self.users_table.resizeColumnsToContents()

    def current_profile(self, row: int | None = None) -> dict[str, Any] | None:
        index = self.users_table.currentRow() if row is None else row
        return self.profile_rows[index] if 0 <= index < len(self.profile_rows) else None

    def create_user(self) -> None:
        dialog = NewUserDialog(self)
        if dialog.exec():
            try:
                self.repo.create_system_user(dialog.values())
                QMessageBox.information(self, "Usuário", "Usuário criado com sucesso.")
                self.refresh()
            except Exception as exc:
                show_error(self, exc)

    def edit_selected_user(self) -> None:
        self.edit_user(self.users_table.currentRow())

    def delete_selected_user(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        answer = QMessageBox.question(
            self,
            "Excluir funcionário",
            f"Deseja apagar o funcionário {profile.get('full_name', '')}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.repo.delete_profile(profile["id"])
            self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def edit_user(self, row: int) -> None:
        profile = self.current_profile(row)
        if not profile:
            return
        dialog = ProfilePermissionsDialog(self, self.repo, profile)
        if dialog.exec():
            try:
                self.repo.update_profile_permissions(profile["id"], dialog.values())
                self.refresh()
            except Exception as exc:
                show_error(self, exc)

    def export_backup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Salvar backup", "backup-agape.json", "JSON (*.json)")
        if not path:
            return
        try:
            data = self.repo.export_backup()
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            QMessageBox.information(self, "Backup", "Backup exportado com sucesso.")
        except Exception as exc:
            show_error(self, exc)


class NewUserDialog(QDialog):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Novo usuário")
        self.setMinimumSize(860, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(14)
        tabs = QTabWidget()
        personal_tab = QWidget()
        personal_layout = QGridLayout(personal_tab)
        personal_layout.setHorizontalSpacing(18)
        personal_layout.setVerticalSpacing(10)
        address_tab = QWidget()
        address_layout = QGridLayout(address_tab)
        address_layout.setHorizontalSpacing(18)
        address_layout.setVerticalSpacing(10)
        attendance_tab = QWidget()
        attendance_layout = QGridLayout(attendance_tab)
        attendance_layout.setHorizontalSpacing(18)
        attendance_layout.setVerticalSpacing(10)
        self.full_name = QLineEdit()
        self.email = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.role = QComboBox()
        self.role.addItem("Administrador", "admin")
        self.role.addItem("Recepção", "reception")
        self.role.addItem("Profissional", "professional")
        self.active = QCheckBox("Ativo")
        self.active.setChecked(True)
        self.cpf = QLineEdit()
        self.rg = QLineEdit()
        self.phone = QLineEdit()
        self.zip_code = QLineEdit()
        self.street = QLineEdit()
        self.address_number = QLineEdit()
        self.address_complement = QLineEdit()
        self.neighborhood = QLineEdit()
        self.city = QLineEdit()
        self.state = QLineEdit()
        self.state.setMaxLength(2)
        self.can_attend = QCheckBox("Pode realizar atendimentos")
        self.specialty = QComboBox()
        self.specialty.addItems(SPECIALTIES)
        self.can_attend.stateChanged.connect(self.sync_attendance_fields)
        self.role.currentIndexChanged.connect(self.sync_role_defaults)
        save = button("Criar usuário")
        save.clicked.connect(self.accept)
        hint = QLabel("A senha deve ter pelo menos 6 caracteres.")
        hint.setObjectName("Muted")
        self.add_field(personal_layout, "Nome completo", self.full_name, 0, 0)
        self.add_field(personal_layout, "E-mail", self.email, 0, 1)
        self.add_field(personal_layout, "Senha inicial", self.password, 1, 0)
        self.add_field(personal_layout, "Permissão", self.role, 1, 1)
        self.add_field(personal_layout, "CPF", self.cpf, 2, 0)
        self.add_field(personal_layout, "RG", self.rg, 2, 1)
        self.add_field(personal_layout, "Telefone", self.phone, 3, 0)
        self.add_field(personal_layout, "Especialidade", self.specialty, 3, 1)
        personal_layout.addWidget(self.can_attend, 4, 1)
        personal_layout.addWidget(self.active, 4, 0)
        personal_layout.addWidget(hint, 5, 0, 1, 2)
        personal_layout.setRowStretch(5, 1)

        self.add_field(address_layout, "CEP", self.zip_code, 0, 0)
        self.add_field(address_layout, "Rua", self.street, 0, 1)
        self.add_field(address_layout, "Número", self.address_number, 1, 0)
        self.add_field(address_layout, "Complemento", self.address_complement, 1, 1)
        self.add_field(address_layout, "Bairro", self.neighborhood, 2, 0)
        self.add_field(address_layout, "Cidade", self.city, 2, 1)
        self.add_field(address_layout, "UF", self.state, 3, 0)
        address_layout.setRowStretch(4, 1)

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["Data", "Horário", "Paciente", "Status"])
        table_polish(self.history_table)
        self.fill_history([])
        attendance_layout.addWidget(QLabel("Histórico de atendimentos"), 0, 0)
        attendance_layout.addWidget(self.history_table, 1, 0, 1, 2)
        attendance_layout.setRowStretch(1, 1)

        tabs.addTab(personal_tab, "Dados pessoais")
        tabs.addTab(address_tab, "Endereço")
        tabs.addTab(attendance_tab, "Atendimento")
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(save)
        root.addWidget(tabs, 1)
        root.addLayout(actions)
        self.sync_attendance_fields()

    def add_field(self, layout: QGridLayout, label: str, widget: QWidget, row: int, col: int) -> None:
        box = QVBoxLayout()
        box.setSpacing(6)
        box.addWidget(QLabel(label))
        box.addWidget(widget)
        layout.addLayout(box, row, col)

    def fill_history(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            self.history_table.setRowCount(1)
            self.history_table.setSpan(0, 0, 1, 4)
            self.history_table.setItem(0, 0, QTableWidgetItem("Histórico disponível depois que o usuário tiver atendimentos."))
            return
        self.history_table.setRowCount(len(rows))

    def sync_role_defaults(self) -> None:
        if self.role.currentData() == "professional":
            self.can_attend.setChecked(True)

    def sync_attendance_fields(self) -> None:
        enabled = self.can_attend.isChecked()
        self.specialty.setEnabled(enabled)
        self.can_attend.setCursor(Qt.PointingHandCursor)
        self.can_attend.setStyleSheet(
            f"""
            QCheckBox {{
                color: {COLORS['text']};
                background: transparent;
                spacing: 10px;
            }}
            QCheckBox::indicator {{
                width: 20px;
                height: 20px;
                border-radius: 4px;
                border: 2px solid {COLORS['primary']};
                background: #FFFFFF;
            }}
            QCheckBox::indicator:checked {{
                background: {COLORS['primary']};
                border: 2px solid {COLORS['primary']};
            }}
            """
        )

    def values(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name.text().strip(),
            "email": self.email.text().strip(),
            "password": self.password.text(),
            "role": self.role.currentData(),
            "is_active": self.active.isChecked(),
            "cpf": self.cpf.text().strip(),
            "rg": self.rg.text().strip(),
            "phone": self.phone.text().strip(),
            "zip_code": self.zip_code.text().strip(),
            "street": self.street.text().strip(),
            "address_number": self.address_number.text().strip(),
            "address_complement": self.address_complement.text().strip(),
            "neighborhood": self.neighborhood.text().strip(),
            "city": self.city.text().strip(),
            "state": self.state.text().strip().upper(),
            "can_attend": self.can_attend.isChecked(),
            "specialty": self.specialty.currentText() if self.can_attend.isChecked() else "",
        }


class ProfilePermissionsDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, profile: dict[str, Any]):
        super().__init__(parent)
        self.repo = repo
        self.profile = profile
        self.setWindowTitle("Funcionário do sistema")
        self.setMinimumSize(860, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(14)
        tabs = QTabWidget()
        personal_tab = QWidget()
        personal_layout = QGridLayout(personal_tab)
        personal_layout.setHorizontalSpacing(18)
        personal_layout.setVerticalSpacing(10)
        address_tab = QWidget()
        address_layout = QGridLayout(address_tab)
        address_layout.setHorizontalSpacing(18)
        address_layout.setVerticalSpacing(10)
        attendance_tab = QWidget()
        attendance_layout = QGridLayout(attendance_tab)
        attendance_layout.setHorizontalSpacing(18)
        attendance_layout.setVerticalSpacing(10)
        self.full_name = QLineEdit(profile.get("full_name", ""))
        self.email = QLineEdit(profile.get("email", "") or "")
        self.email.setPlaceholderText("E-mail de login")
        password_box = QWidget()
        password_layout = QHBoxLayout(password_box)
        password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.setSpacing(8)
        self.new_password = QLineEdit()
        self.new_password.setEchoMode(QLineEdit.Password)
        self.new_password.setPlaceholderText("Nova senha")
        self.new_password.setToolTip("Informe uma nova senha com pelo menos 6 caracteres.")
        self.change_password_btn = button("Trocar senha", secondary=True)
        self.change_password_btn.clicked.connect(self.change_password)
        password_layout.addWidget(self.new_password, 1)
        password_layout.addWidget(self.change_password_btn)
        self.role = QComboBox()
        self.role.addItem("Administrador", "admin")
        self.role.addItem("Recepção", "reception")
        self.role.addItem("Profissional", "professional")
        role_index = self.role.findData(profile.get("role"))
        if role_index >= 0:
            self.role.setCurrentIndex(role_index)
        self.active = QCheckBox("Ativo")
        self.active.setChecked(profile.get("is_active", True))
        self.cpf = QLineEdit(profile.get("cpf", "") or "")
        self.rg = QLineEdit(profile.get("rg", "") or "")
        self.phone = QLineEdit(profile.get("phone", "") or "")
        self.zip_code = QLineEdit(profile.get("zip_code", "") or "")
        self.street = QLineEdit(profile.get("street", "") or "")
        self.address_number = QLineEdit(profile.get("address_number", "") or "")
        self.address_complement = QLineEdit(profile.get("address_complement", "") or "")
        self.neighborhood = QLineEdit(profile.get("neighborhood", "") or "")
        self.city = QLineEdit(profile.get("city", "") or "")
        self.state = QLineEdit(profile.get("state", "") or "")
        self.state.setMaxLength(2)
        self.can_attend = QCheckBox("Pode realizar atendimentos")
        self.can_attend.setChecked(profile.get("can_attend", False))
        self.specialty = QComboBox()
        self.specialty.addItems(SPECIALTIES)
        if profile.get("specialty") in SPECIALTIES:
            self.specialty.setCurrentText(profile["specialty"])
        self.can_attend.stateChanged.connect(self.sync_attendance_fields)
        save = button("Salvar funcionário")
        save.clicked.connect(self.accept)
        self.add_field(personal_layout, "Nome completo", self.full_name, 0, 0)
        self.add_field(personal_layout, "Permissão", self.role, 0, 1)
        self.add_field(personal_layout, "E-mail de login", self.email, 1, 0)
        self.add_field(personal_layout, "Senha", password_box, 1, 1)
        self.add_field(personal_layout, "CPF", self.cpf, 2, 0)
        self.add_field(personal_layout, "RG", self.rg, 2, 1)
        self.add_field(personal_layout, "Telefone", self.phone, 3, 0)
        self.add_field(personal_layout, "Especialidade", self.specialty, 3, 1)
        personal_layout.addWidget(self.can_attend, 4, 1)
        personal_layout.addWidget(self.active, 4, 0)
        personal_layout.setRowStretch(5, 1)

        self.add_field(address_layout, "CEP", self.zip_code, 0, 0)
        self.add_field(address_layout, "Rua", self.street, 0, 1)
        self.add_field(address_layout, "Número", self.address_number, 1, 0)
        self.add_field(address_layout, "Complemento", self.address_complement, 1, 1)
        self.add_field(address_layout, "Bairro", self.neighborhood, 2, 0)
        self.add_field(address_layout, "Cidade", self.city, 2, 1)
        self.add_field(address_layout, "UF", self.state, 3, 0)
        address_layout.setRowStretch(4, 1)

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["Data", "Horário", "Paciente", "Status"])
        table_polish(self.history_table)
        self.fill_history()
        attendance_layout.addWidget(QLabel("Histórico de atendimentos"), 0, 0)
        attendance_layout.addWidget(self.history_table, 1, 0, 1, 2)
        attendance_layout.setRowStretch(1, 1)

        tabs.addTab(personal_tab, "Dados pessoais")
        tabs.addTab(address_tab, "Endereço")
        tabs.addTab(attendance_tab, "Atendimento")
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(save)
        root.addWidget(tabs, 1)
        root.addLayout(actions)
        self.sync_attendance_fields()

    def add_field(self, layout: QGridLayout, label: str, widget: QWidget, row: int, col: int) -> None:
        box = QVBoxLayout()
        box.setSpacing(6)
        box.addWidget(QLabel(label))
        box.addWidget(widget)
        layout.addLayout(box, row, col)

    def change_password(self) -> None:
        new_password = self.new_password.text()
        if len(new_password) < 6:
            show_error(self, AppError("A nova senha deve ter pelo menos 6 caracteres."))
            return
        auth_user_id = self.profile.get("auth_user_id", "")
        if not auth_user_id:
            show_error(self, AppError("Este funcionário não possui Auth ID vinculado."))
            return
        email = self.email.text().strip() or "e-mail não informado"
        answer = QMessageBox.question(
            self,
            "Trocar senha",
            f"Deseja trocar a senha do usuário {email}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.repo.update_user_password(auth_user_id, new_password)
            self.new_password.clear()
            QMessageBox.information(
                self,
                "Trocar senha",
                "Senha alterada com sucesso. Informe a nova senha ao funcionário.",
            )
        except Exception as exc:
            show_error(self, exc)

    def fill_history(self) -> None:
        try:
            rows = self.repo.profile_appointments(self.profile["id"])
        except Exception:
            rows = []
        if not rows:
            self.history_table.setRowCount(1)
            self.history_table.setSpan(0, 0, 1, 4)
            self.history_table.setItem(0, 0, QTableWidgetItem("Nenhum atendimento vinculado a este funcionário."))
            return
        self.history_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            patient = (row.get("patients") or {}).get("full_name", "")
            values = [
                row.get("appointment_date", ""),
                f"{row.get('start_time', '')[:5]} às {row.get('end_time', '')[:5]}",
                patient,
                row.get("status", ""),
            ]
            for col, value in enumerate(values):
                self.history_table.setItem(row_index, col, QTableWidgetItem(str(value or "")))
        self.history_table.resizeColumnsToContents()

    def sync_attendance_fields(self) -> None:
        enabled = self.can_attend.isChecked()
        self.specialty.setEnabled(enabled)
        self.can_attend.setCursor(Qt.PointingHandCursor)
        self.can_attend.setStyleSheet(
            f"""
            QCheckBox {{
                color: {COLORS['text']};
                background: transparent;
                spacing: 10px;
            }}
            QCheckBox::indicator {{
                width: 20px;
                height: 20px;
                border-radius: 4px;
                border: 2px solid {COLORS['primary']};
                background: #FFFFFF;
            }}
            QCheckBox::indicator:checked {{
                background: {COLORS['primary']};
                border: 2px solid {COLORS['primary']};
            }}
            """
        )

    def values(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name.text().strip(),
            "email": self.email.text().strip(),
            "role": self.role.currentData(),
            "is_active": self.active.isChecked(),
            "cpf": self.cpf.text().strip(),
            "rg": self.rg.text().strip(),
            "phone": self.phone.text().strip(),
            "zip_code": self.zip_code.text().strip(),
            "street": self.street.text().strip(),
            "address_number": self.address_number.text().strip(),
            "address_complement": self.address_complement.text().strip(),
            "neighborhood": self.neighborhood.text().strip(),
            "city": self.city.text().strip(),
            "state": self.state.text().strip().upper(),
            "can_attend": self.can_attend.isChecked(),
            "specialty": self.specialty.currentText() if self.can_attend.isChecked() else "",
        }


def main() -> int:
    configure_windows_taskbar_id()
    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    app.setStyleSheet(APP_STYLESHEET)
    settings = load_settings()
    if not settings.is_configured:
        QMessageBox.warning(
            None,
            "Configuração necessária",
            "Configure SUPABASE_URL e SUPABASE_ANON_KEY no arquivo .env antes de entrar.",
        )
    try:
        repo = SupabaseRepository(settings)
        login = LoginWindow(repo)
        login.show()
        QTimer.singleShot(1500, lambda: start_update_check(app, login))
        return app.exec()
    except AppError as exc:
        QMessageBox.critical(None, "Clínica Agape", str(exc))
        return 1
