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

import httpx
from PySide6.QtCore import QDate, QObject, QRunnable, QThread, QThreadPool, QTime, QTimer, Qt, Signal, Slot
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon, QPixmap, QTextCharFormat, QBrush, QIntValidator
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
    QScrollArea,
    QStackedWidget,
    QStyledItemDelegate,
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
from .financial_rules import appointment_is_selectable, current_month_period
from .permissions import Permission, ROLE_LABELS, can
from .repository import AppError, SupabaseRepository
from .theme import APP_STYLESHEET, COLORS, STATUS_COLORS
from .updater import ReleaseInfo, download_installer, latest_release
from .version import APP_VERSION

STATUSES = ["Agendado", "Confirmado", "Atendido", "Falta com aviso", "Falta sem aviso", "Cancelado"]
FINANCIAL_ROW_SELECTED_ROLE = Qt.UserRole + 20
AGENDA_KIND = "_agenda_kind"
AGENDA_KEY = "_agenda_key"


def agenda_time_to_minutes(value: Any, fallback: str) -> int:
    text = str(value or fallback)[:5]
    try:
        hour, minute = (int(part) for part in text.split(":"))
    except (TypeError, ValueError):
        hour, minute = (int(part) for part in fallback[:5].split(":"))
    return hour * 60 + minute


def agenda_minutes_to_time(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}:00"


def build_agenda_grid_rows(
    professionals: list[dict[str, Any]],
    appointments: list[dict[str, Any]],
    professional_id: str | None = None,
    include_free_slots: bool = True,
) -> list[dict[str, Any]]:
    """Combina a grade configurada com atendimentos reais sem persistir horários livres."""
    visible_professionals = [
        row for row in professionals if professional_id is None or row.get("id") == professional_id
    ]
    known_ids = {row.get("id") for row in visible_professionals}
    for appointment in appointments:
        appointment_professional_id = appointment.get("professional_id")
        if (
            appointment_professional_id not in known_ids
            and (professional_id is None or appointment_professional_id == professional_id)
        ):
            professional = appointment.get("professionals") or {}
            visible_professionals.append(
                {
                    "id": appointment_professional_id,
                    "full_name": professional.get("full_name", "Profissional não informado"),
                }
            )
            known_ids.add(appointment_professional_id)

    grid_rows: list[dict[str, Any]] = []
    for professional in visible_professionals:
        current_professional_id = professional.get("id")
        try:
            slot_minutes = int(
                professional.get("agenda_slot_minutes") or SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES
            )
            step_minutes = int(
                professional.get("agenda_step_minutes")
                or max(SupabaseRepository.DEFAULT_AGENDA_STEP_MINUTES, slot_minutes)
            )
        except (TypeError, ValueError):
            slot_minutes = SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES
            step_minutes = SupabaseRepository.DEFAULT_AGENDA_STEP_MINUTES
        periods = professional_agenda_periods(professional)
        if slot_minutes < 5 or step_minutes < slot_minutes:
            periods = SupabaseRepository.DEFAULT_AGENDA_PERIODS
            slot_minutes = SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES
            step_minutes = SupabaseRepository.DEFAULT_AGENDA_STEP_MINUTES

        professional_appointments = [
            row
            for row in appointments
            if row.get("professional_id") == current_professional_id
        ]
        for period in periods:
            start_minutes = agenda_time_to_minutes(period.get("start"), SupabaseRepository.DEFAULT_AGENDA_START_TIME)
            end_minutes = agenda_time_to_minutes(period.get("end"), SupabaseRepository.DEFAULT_AGENDA_END_TIME)
            if end_minutes <= start_minutes:
                continue
            slot_start = start_minutes
            # Os períodos limitam somente a geração automática de horários
            # livres. Atendimentos reais, inclusive fora deles, são preservados.
            while include_free_slots and slot_start < end_minutes:
                slot_end = slot_start + slot_minutes
                has_appointment = any(
                    agenda_time_to_minutes(row.get("start_time"), "00:00:00") < slot_end
                    and agenda_time_to_minutes(row.get("end_time"), "00:00:00") > slot_start
                    for row in professional_appointments
                )
                if not has_appointment:
                    slot_start_text = agenda_minutes_to_time(slot_start)
                    slot_end_text = agenda_minutes_to_time(slot_end)
                    row_key = f"{current_professional_id}:free:{slot_start_text}:{slot_end_text}"
                    grid_rows.append({
                        AGENDA_KIND: "free", AGENDA_KEY: row_key,
                        "professional_id": current_professional_id,
                        "professionals": {"full_name": professional.get("full_name", "")},
                        "start_time": slot_start_text, "end_time": slot_end_text,
                        "_agenda_slot_start": slot_start_text, "_agenda_slot_end": slot_end_text,
                    })
                slot_start += step_minutes

        # Cada atendimento real aparece uma única vez e sempre conserva seus
        # horários próprios, mesmo quando ocupa mais de uma célula teórica.
        for appointment in professional_appointments:
            grid_rows.append(
                {
                    **appointment,
                    AGENDA_KIND: "appointment",
                    AGENDA_KEY: f"{current_professional_id}:appointment:{appointment.get('id')}",
                    "_agenda_appointments": [appointment],
                    "_agenda_slot_start": appointment.get("start_time", ""),
                    "_agenda_slot_end": appointment.get("end_time", ""),
                }
            )

    return sorted(
        grid_rows,
        key=lambda row: (
            row.get("_agenda_slot_start", ""),
            (row.get("professionals") or {}).get("full_name", "").casefold(),
            row.get(AGENDA_KEY, ""),
        ),
    )


class NoWheelComboBox(QComboBox):
    """Mantém a roda do mouse disponível para rolar a tela, não o valor."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelTimeEdit(QTimeEdit):
    """Impede alterações acidentais de horário com a roda do mouse."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class AgendaStatusComboBox(NoWheelComboBox):
    """Evita mudanças acidentais de status ao rolar a agenda."""


class FinancialRowDelegate(QStyledItemDelegate):
    """Paint checkbox-selected rows consistently without using table selection."""

    def paint(self, painter, option, index) -> None:
        selected = bool(index.data(FINANCIAL_ROW_SELECTED_ROLE))
        background = COLORS["primary_soft"] if selected else "#FBFCFE" if index.row() % 2 else COLORS["surface"]
        foreground = index.data(Qt.ForegroundRole)
        text_color = foreground.color() if isinstance(foreground, QBrush) else QColor(COLORS["text"])
        painter.save()
        painter.fillRect(option.rect, QColor(background))
        painter.setPen(text_color)
        painter.setFont(option.font)
        painter.drawText(
            option.rect.adjusted(6, 2, -6, -2),
            Qt.AlignCenter | Qt.TextWordWrap,
            str(index.data(Qt.DisplayRole) or ""),
        )
        painter.restore()
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


class WorkerSignals(QObject):
    completed = Signal(object, object, object)


class FunctionWorker(QRunnable):
    """Executa uma função sem bloquear o event loop e devolve tudo por signal."""

    _next_task_id = 0

    def __init__(self, function, *args, **kwargs):
        super().__init__()
        FunctionWorker._next_task_id += 1
        self.task_id = FunctionWorker._next_task_id
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        result = None
        error = None
        try:
            result = self.function(*self.args, **self.kwargs)
        except Exception as exc:
            error = exc
        self.signals.completed.emit(self.task_id, result, error)


def start_worker(owner: QWidget, callback, function, *args, **kwargs) -> int:
    """Mantém o QRunnable vivo até a entrega do resultado ao objeto Qt receptor."""

    workers = getattr(owner, "_background_workers", None)
    if workers is None:
        workers = {}
        owner._background_workers = workers
    worker = FunctionWorker(function, *args, **kwargs)
    workers[worker.task_id] = worker
    worker.signals.completed.connect(callback)
    QThreadPool.globalInstance().start(worker)
    return worker.task_id


def forget_worker(owner: QWidget, task_id: int) -> None:
    workers = getattr(owner, "_background_workers", None)
    if workers is not None:
        workers.pop(task_id, None)


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
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException)):
        return "O servidor demorou para responder. Verifique sua conexão e tente novamente."
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "Não foi possível conectar ao servidor. Verifique sua conexão e tente novamente."
    if isinstance(exc, httpx.TransportError):
        return "A conexão com o servidor foi interrompida. O sistema tentará novamente automaticamente."
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
    field = NoWheelTimeEdit(QTime(hour, minute))
    field.setDisplayFormat("HH:mm")
    field.setAlignment(Qt.AlignCenter)
    field.setButtonSymbols(QTimeEdit.NoButtons)
    return field


def agenda_time_field(value: Any, fallback: str) -> QTimeEdit:
    minutes = agenda_time_to_minutes(value, fallback)
    return time_field(minutes // 60, minutes % 60)


def agenda_duration_field(value: Any = None) -> QComboBox:
    field = NoWheelComboBox()
    duration_options = [15, 20, 30, 40, 45, 50, 60, 90, 120]
    try:
        current_duration = int(value or SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES)
    except (TypeError, ValueError):
        current_duration = SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES
    if current_duration not in duration_options:
        duration_options.append(current_duration)
        duration_options.sort()
    for minutes in duration_options:
        field.addItem(f"{minutes} minutos", minutes)
    field.setCurrentIndex(field.findData(current_duration))
    return field


def bind_agenda_duration_fields(duration_field: QComboBox, step_field: QComboBox) -> None:
    def keep_step_valid(_index: int = -1) -> None:
        duration = int(duration_field.currentData() or SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES)
        step = int(step_field.currentData() or SupabaseRepository.DEFAULT_AGENDA_STEP_MINUTES)
        if step >= duration:
            return
        target_index = step_field.findData(duration)
        if target_index < 0:
            step_field.addItem(f"{duration} minutos", duration)
            target_index = step_field.findData(duration)
        step_field.setCurrentIndex(target_index)

    duration_field.currentIndexChanged.connect(keep_step_valid)
    keep_step_valid()


class AgendaTimingEditor(QWidget):
    """Um campo simples por padrão, com intervalo independente sob demanda."""

    def __init__(self, duration: Any = None, step: Any = None):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.duration = agenda_duration_field(duration)
        self.step = agenda_duration_field(step if step is not None else duration)
        bind_agenda_duration_fields(self.duration, self.step)
        self.advanced = QCheckBox("Usar intervalo diferente entre os horários")
        self.step_label = QLabel("Novo horário a cada")
        current_duration = int(self.duration.currentData() or SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES)
        current_step = int(self.step.currentData() or current_duration)
        self.advanced.setChecked(current_step != current_duration)
        layout.addWidget(self.duration)
        layout.addWidget(self.advanced)
        layout.addWidget(self.step_label)
        layout.addWidget(self.step)
        self.advanced.toggled.connect(self.sync_advanced)
        self.duration.currentIndexChanged.connect(self.sync_duration)
        self.sync_advanced(self.advanced.isChecked())

    def sync_duration(self, _index: int = -1) -> None:
        if not self.advanced.isChecked():
            self._copy_duration_to_step()

    def sync_advanced(self, enabled: bool) -> None:
        if not enabled:
            self._copy_duration_to_step()
        self.step_label.setVisible(enabled)
        self.step.setVisible(enabled)

    def _copy_duration_to_step(self) -> None:
        duration = int(self.duration.currentData() or SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES)
        index = self.step.findData(duration)
        if index < 0:
            self.step.addItem(f"{duration} minutos", duration)
            index = self.step.findData(duration)
        self.step.setCurrentIndex(index)


def professional_agenda_periods(professional: dict[str, Any]) -> list[dict[str, str]]:
    periods = professional.get("agenda_periods")
    if isinstance(periods, list) and periods:
        return periods
    return [{
        "start": str(professional.get("agenda_start_time") or SupabaseRepository.DEFAULT_AGENDA_START_TIME),
        "end": str(professional.get("agenda_end_time") or SupabaseRepository.DEFAULT_AGENDA_END_TIME),
    }]


class AgendaPeriodsEditor(QWidget):
    """Editor compacto para uma ou mais faixas diárias de disponibilidade."""

    def __init__(self, periods: Any = None):
        super().__init__()
        self.setObjectName("AgendaPeriodsEditor")
        self.rows: list[tuple[QWidget, QTimeEdit, QTimeEdit]] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self.rows_container = QWidget()
        self.rows_container.setObjectName("AgendaPeriodsRows")
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(4)
        self.rows_layout.addStretch()
        root.addWidget(self.rows_container)
        self.add_button = button("+ Adicionar período", secondary=True)
        self.add_button.setToolTip("Adicionar outra faixa de atendimento no mesmo dia")
        self.add_button.clicked.connect(self.add_suggested_period)
        root.addWidget(self.add_button, 0, Qt.AlignLeft)
        initial = periods if isinstance(periods, list) and periods else SupabaseRepository.DEFAULT_AGENDA_PERIODS
        for period in initial:
            self.add_period(period.get("start"), period.get("end"))

    def add_suggested_period(self) -> None:
        last_end = max((end.time() for _row, _start, end in self.rows), default=QTime(12, 0))
        start = QTime(13, 0) if last_end <= QTime(13, 0) else last_end
        end = start.addSecs(5 * 60 * 60)
        if end <= start:
            end = QTime(23, 59)
        self.add_period(start.toString("HH:mm:ss"), end.toString("HH:mm:ss"))

    def add_period(self, start: Any, end: Any) -> None:
        row = QFrame()
        row.setObjectName("AgendaPeriodRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)
        number = QLabel(f"Período {len(self.rows) + 1}")
        number.setMinimumWidth(70)
        start_field = agenda_time_field(start, SupabaseRepository.DEFAULT_AGENDA_START_TIME)
        end_field = agenda_time_field(end, SupabaseRepository.DEFAULT_AGENDA_END_TIME)
        start_field.setObjectName("CompactAgendaTime")
        end_field.setObjectName("CompactAgendaTime")
        start_field.setMaximumWidth(150)
        end_field.setMaximumWidth(150)
        remove = button("Excluir", secondary=True)
        remove.setObjectName("CompactAgendaAction")
        remove.setToolTip("Remover este período")
        remove.clicked.connect(lambda: self.remove_period(row))
        remove.setMaximumWidth(86)
        layout.addWidget(number)
        layout.addWidget(start_field, 1)
        layout.addWidget(QLabel("até"))
        layout.addWidget(end_field, 1)
        layout.addStretch()
        layout.addWidget(remove)
        self.rows.append((row, start_field, end_field))
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
        self._refresh_rows()

    def remove_period(self, row: QWidget) -> None:
        if len(self.rows) == 1:
            return
        self.rows = [item for item in self.rows if item[0] is not row]
        row.deleteLater()
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        for index, (row, _start, _end) in enumerate(self.rows, start=1):
            label = row.findChild(QLabel)
            if label:
                label.setText(f"Período {index}")
            buttons = row.findChildren(QPushButton)
            if buttons:
                buttons[-1].setEnabled(len(self.rows) > 1)

    def periods(self) -> list[dict[str, str]]:
        return [
            {"start": start.time().toString("HH:mm:ss"), "end": end.time().toString("HH:mm:ss")}
            for _row, start, end in self.rows
        ]


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
        logo.setFixedHeight(150)
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
        self.email.setAccessibleName("E-mail")
        self.password = QLineEdit()
        self.password.setPlaceholderText("Senha")
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setMinimumHeight(44)
        self.password.setAccessibleName("Senha")
        email_label = QLabel("E-mail")
        email_label.setBuddy(self.email)
        email_label.setStyleSheet("background: transparent; font-weight: 700;")
        password_label = QLabel("Senha")
        password_label.setBuddy(self.password)
        password_label.setStyleSheet("background: transparent; font-weight: 700;")
        self.show_password = QCheckBox("Mostrar senha")
        self.show_password.setAccessibleName("Mostrar senha")
        self.show_password.setCursor(Qt.PointingHandCursor)
        self.show_password.setStyleSheet(
            "QCheckBox { background: transparent; border: none; padding: 0; spacing: 8px; }"
        )
        self.show_password.toggled.connect(
            lambda checked: self.password.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        )
        self.error = QLabel("")
        self.error.setMinimumHeight(22)
        self.error.setStyleSheet(f"background: transparent; color: {COLORS['red']};")
        self.error.setAccessibleName("Mensagem de erro de autenticação")
        self.error.setWordWrap(True)
        self.login_button = button("Entrar")
        self.login_button.setMinimumHeight(44)
        self.login_button.setAccessibleName("Entrar")
        self.login_button.clicked.connect(self.handle_login)
        self.password.returnPressed.connect(self.handle_login)

        panel = QFrame()
        panel.setObjectName("LoginPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(36, 34, 36, 34)
        panel_layout.setSpacing(15)
        panel_layout.addWidget(logo)
        panel_layout.addSpacing(10)
        panel_layout.addWidget(email_label)
        panel_layout.addWidget(self.email)
        panel_layout.addWidget(password_label)
        panel_layout.addWidget(self.password)
        panel_layout.addWidget(self.show_password)
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
        self.login_button.setText("Entrando...")
        QApplication.processEvents()
        try:
            profile = self.repo.login(self.email.text().strip(), self.password.text())
            self.main_window = MainWindow(self.repo, profile)
            self.main_window.show()
            apply_window_icon(self.main_window)
            self.close()
        except Exception as exc:
            self.error.setText(str(exc))
            if not self.email.text().strip():
                self.email.setFocus()
            else:
                self.password.setFocus()
        finally:
            self.login_button.setText("Entrar")
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
            "color: #FFFFFF; padding: 3px 7px; font-size: 13px; font-weight: 700;"
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
            ("Exportar sessões", SessionExportPage(repo, profile)),
            ("Documentos", DocumentsPage(repo, profile)),
        ]
        if can(profile, Permission.VIEW_FINANCIALS):
            self.pages.append(("Financeiro", FinancePage(repo)))
        if can(profile, Permission.VIEW_SETTINGS):
            self.pages.append(("Configurações", SettingsPage(repo)))

        for index, (label, page) in enumerate(self.pages):
            self.stack.addWidget(page)
            nav = QPushButton(label)
            nav.setAccessibleName(f"Abrir {label}")
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
        self.auto_refresh_timer.setInterval(30_000)
        self.auto_refresh_timer.timeout.connect(self.auto_refresh_current_page)
        self.agenda_page.activity_completed.connect(self.restart_auto_refresh_timer)
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
        current_page = self.stack.currentWidget()
        if current_page in (self.agenda_page, self.dashboard_page):
            self.safe_refresh_page(current_page, show_popup=False)

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
        if page is self.agenda_page and self.stack.currentWidget() is not self.agenda_page:
            self.agenda_page.professionals_loaded = False
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
        if label == "Agenda":
            if page.is_busy or page.list.verticalScrollBar().isSliderDown():
                return
            page.refresh(show_popup=False)
            return
        self.safe_refresh_page(page, show_popup=False)

    def restart_auto_refresh_timer(self) -> None:
        self.auto_refresh_timer.start()


class DashboardPage(QWidget):
    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any], navigate=None):
        super().__init__()
        self.repo = repo
        self.profile = profile
        self.navigate = navigate
        self.admin_mode_enabled = profile.get("role") == "reception"
        self.professionals: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self._refresh_in_progress = False
        self._refresh_pending = False
        self._refresh_task_id: int | None = None
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

    def _fetch_dashboard(self) -> dict[str, Any]:
        today = date.today()
        professionals = self.repo.professionals(active_only=True)
        can_view_all = self.can_view_all_agendas()
        professional_id = None
        if not can_view_all:
            professional_id = next(
                (
                    professional.get("id")
                    for professional in professionals
                    if professional.get("profile_id") == self.profile.get("id")
                ),
                None,
            )
        rows = [] if not can_view_all and professional_id is None else self.repo.appointments(today, professional_id)
        return {"professionals": professionals, "rows": rows}

    def refresh(self) -> None:
        if self._refresh_in_progress:
            self._refresh_pending = True
            return
        self._refresh_in_progress = True
        self._refresh_task_id = start_worker(self, self._on_refresh_completed, self._fetch_dashboard)

    @Slot(object, object, object)
    def _on_refresh_completed(self, task_id: int, result: dict[str, Any] | None, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._refresh_task_id:
            return
        self._refresh_task_id = None
        self._refresh_in_progress = False
        if error is not None:
            log_exception("Falha ao atualizar painel inicial", error)
        elif result is not None:
            self.professionals = result["professionals"]
            self.rows = result["rows"]
            self._render_dashboard()
        pending = self._refresh_pending
        self._refresh_pending = False
        if pending:
            self.refresh()

    def _render_dashboard(self) -> None:
        rows = self.rows
        counts = self.dashboard_counts(rows)
        clear_layout(self.grid)
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
        subtitle.setStyleSheet("background: transparent; font-size: 13px;")
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
        self.search.setAccessibleName("Buscar paciente")
        self.search.textChanged.connect(self.refresh)
        self.search.setFixedWidth(360)
        self.active_only = QCheckBox("Somente ativos")
        self.active_only.setAccessibleName("Mostrar somente pacientes ativos")
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
        self.new_btn.setAccessibleName("Cadastrar novo paciente")
        self.new_btn.clicked.connect(self.create_record)
        self.new_btn.setVisible(can_manage_patients)
        self.edit_btn = button("Editar paciente", secondary=True)
        self.edit_btn.setAccessibleName("Editar paciente selecionado")
        self.edit_btn.clicked.connect(lambda: self.edit_record(self.table.currentRow()))
        self.edit_btn.setVisible(can_manage_patients)
        self.edit_btn.setEnabled(False)
        self.delete_btn = button("Excluir paciente", danger=True)
        self.delete_btn.setAccessibleName("Excluir paciente selecionado")
        self.delete_btn.clicked.connect(self.delete_record)
        self.delete_btn.setVisible(can_manage_patients)
        self.delete_btn.setEnabled(False)
        card_header.addWidget(self.card_title)
        card_header.addStretch()
        card_header.addWidget(self.search)
        card_header.addWidget(self.active_only)
        card_header.addWidget(self.edit_btn)
        card_header.addWidget(self.delete_btn)
        card_header.addWidget(self.new_btn)
        self.table = QTableWidget(0, 6)
        self.table.setAccessibleName("Lista de pacientes")
        self.table.setHorizontalHeaderLabels(["Nome", "Documento", "Responsável", "Telefone", "Status", "Motivo"])
        full_row_table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemActivated.connect(lambda item: self.edit_record(item.row()))
        self.table.itemSelectionChanged.connect(self.update_action_state)
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
        self.update_action_state()

    def update_action_state(self) -> None:
        has_selection = self.table.currentRow() >= 0
        self.edit_btn.setEnabled(has_selection)
        self.delete_btn.setEnabled(has_selection)

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
        birth_date = record.get("birth_date", "") if record else ""
        try:
            birth_date = date.fromisoformat(str(birth_date)).strftime("%d/%m/%Y") if birth_date else ""
        except ValueError:
            pass
        self.birth_date = QLineEdit(birth_date)
        self.birth_date.setPlaceholderText("DD/MM/AAAA")
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
    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any]):
        super().__init__()
        self.repo = repo
        self.profile = profile
        self.patients: list[dict[str, Any]] = []
        self.professionals: list[dict[str, Any]] = []
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
        self.professional = QComboBox()
        self.professional.setMinimumWidth(240)
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
        filters.addLayout(self.filter_field("Profissional", self.professional))
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

    def load_professionals(self) -> None:
        self.professional.clear()
        try:
            self.professionals = self.repo.session_export_professionals()
            is_admin = self.profile.get("role") == "admin"
            if is_admin:
                self.professional.addItem("Selecione um profissional", None)
            for professional in self.professionals:
                self.professional.addItem(professional.get("full_name", ""), professional.get("id"))
            self.professional.setEnabled(is_admin)
            if not self.professionals:
                self.professional.addItem("Nenhum profissional vinculado", None)
        except Exception as exc:
            show_error(self, exc)
            self.professionals = []
            self.professional.addItem("Não foi possível carregar", None)
            self.professional.setEnabled(False)

    def refresh(self) -> None:
        if not self.patients:
            self.load_patients()
        if not self.professionals:
            self.load_professionals()

    def load_sessions(self) -> None:
        patient_id = self.patient.currentData()
        if not patient_id:
            show_error(self, AppError("Selecione um paciente antes de carregar as sessões."))
            return
        professional_id = self.professional.currentData()
        if not professional_id:
            show_error(self, AppError("Selecione um profissional antes de carregar as sessões."))
            return
        start = self.start_date.date().toPython()
        end = self.end_date.date().toPython()
        if end < start:
            show_error(self, AppError("A data final deve ser maior ou igual a inicial."))
            return
        try:
            self.rows = self.repo.patient_session_exports(patient_id, start, end, professional_id)
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
            f"Profissional: {self.professional.currentText()}",
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
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Nome", "Especialidade", "Telefone", "Grade diária", "Status"])
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
            periods = professional_agenda_periods(row)
            periods_text = ", ".join(
                f"{str(period.get('start', ''))[:5]}–{str(period.get('end', ''))[:5]}" for period in periods
            )
            slot_minutes = row.get("agenda_slot_minutes") or SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES
            step_minutes = row.get("agenda_step_minutes") or SupabaseRepository.DEFAULT_AGENDA_STEP_MINUTES
            values = [
                row.get("full_name", ""),
                row.get("specialty", ""),
                row.get("phone", ""),
                f"{periods_text} | {slot_minutes} min, a cada {step_minutes}",
                "Ativo" if row.get("is_active") else "Inativo",
            ]
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
        self.agenda_periods = AgendaPeriodsEditor(professional_agenda_periods(record or {}))
        self.agenda_timing = AgendaTimingEditor(
            record.get("agenda_slot_minutes") if record else None,
            record.get("agenda_step_minutes") if record else None,
        )
        self.agenda_slot_minutes = self.agenda_timing.duration
        self.agenda_step_minutes = self.agenda_timing.step
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
        layout.addRow("Períodos da grade automática", self.agenda_periods)
        layout.addRow("Duração do atendimento", self.agenda_timing)
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
            "agenda_periods": self.agenda_periods.periods(),
            "agenda_slot_minutes": self.agenda_slot_minutes.currentData(),
            "agenda_step_minutes": self.agenda_step_minutes.currentData(),
            "profile_id": self.profile.currentData(),
            "is_active": self.active.isChecked(),
        }


class AgendaPage(QWidget):
    activity_completed = Signal()

    def __init__(self, repo: SupabaseRepository, profile: dict[str, Any]):
        super().__init__()
        self.repo = repo
        self.profile = profile
        self.admin_mode_enabled = profile.get("role") == "reception"
        self.rows: list[dict[str, Any]] = []
        self.agenda_rows: list[dict[str, Any]] = []
        self.professionals: list[dict[str, Any]] = []
        self._refresh_in_progress = False
        self._refresh_pending = False
        self._refresh_pending_popup = False
        self._refresh_task_id: int | None = None
        self._write_in_progress = False
        self._write_task_id: int | None = None
        self._allowed_to_manage = can(profile, Permission.MANAGE_AGENDA)
        self._pending_scroll_state = (0, False)
        self._rendered_agenda_key: tuple[date, Any] | None = None
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
        self.date_edit.dateChanged.connect(lambda _value: self.refresh())
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
        self.professional_filter.currentIndexChanged.connect(lambda _index: self.refresh())
        self.new_appointment = button("Novo atendimento")
        self.new_appointment.setMinimumHeight(40)
        self.new_appointment.clicked.connect(self.create_appointment)
        self.new_recurring = button("Nova agenda fixa", secondary=True)
        self.new_recurring.setMinimumHeight(40)
        self.new_recurring.clicked.connect(self.create_recurring)
        self.bulk_edit = button("Alterar atendimentos", secondary=True)
        self.bulk_edit.setMinimumHeight(40)
        self.bulk_edit.clicked.connect(self.edit_appointments_bulk)
        self.new_appointment.setEnabled(self._allowed_to_manage)
        self.new_recurring.setEnabled(self._allowed_to_manage)
        self.bulk_edit.setVisible(self._allowed_to_manage)
        self.day_stat_labels: dict[str, QLabel] = {}
        stats_panel = QFrame()
        style_card(stats_panel, COLORS["bg2"], COLORS["line"])
        stats_layout = QGridLayout(stats_panel)
        stats_layout.setContentsMargins(12, 10, 12, 10)
        stats_layout.setHorizontalSpacing(12)
        stats_layout.setVerticalSpacing(8)
        for index, (key, label) in enumerate(
            [
                ("total", "Com atendimento"),
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
            text.setStyleSheet(f"background: transparent; color: {COLORS['muted']}; font-size: 13px; font-weight: 700;")
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
        self.loading_label = QLabel("")
        self.loading_label.setObjectName("Muted")
        self.loading_label.setStyleSheet(f"background: transparent; color: {COLORS['primary']}; font-weight: 700;")
        self.loading_label.hide()
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
        canvas_layout.addWidget(self.loading_label, 0, Qt.AlignLeft)
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

    def can_view_all_agendas(self) -> bool:
        return self.profile.get("role") == "reception" or (
            self.profile.get("role") == "admin" and self.admin_mode_enabled
        )

    def own_professionals(self) -> list[dict[str, Any]]:
        profile_id = self.profile.get("id")
        return [professional for professional in self.professionals if professional.get("profile_id") == profile_id]

    @property
    def is_busy(self) -> bool:
        return self._refresh_in_progress or self._write_in_progress

    def _set_busy_ui(self, message: str = "") -> None:
        write_busy = self._write_in_progress
        visible_message = message if write_busy else ""
        self.loading_label.setText(visible_message)
        self.loading_label.setVisible(bool(visible_message))
        self.new_appointment.setEnabled(self._allowed_to_manage and not write_busy)
        self.new_recurring.setEnabled(self._allowed_to_manage and not write_busy)
        self.bulk_edit.setEnabled(self._allowed_to_manage and not write_busy)
        self.list.setEnabled(not write_busy)

    def _apply_professionals(self, professionals: list[dict[str, Any]], preferred_id: str | None) -> None:
        if self.professionals != professionals:
            self._rendered_agenda_key = None
        self.professional_filter.blockSignals(True)
        self.professional_filter.clear()
        self.professionals = professionals
        self.professionals_loaded = True
        if self.can_view_all_agendas():
            self.professional_filter.addItem("Todos os profissionais", None)
            visible_professionals = self.professionals
        else:
            visible_professionals = self.own_professionals()
            if not visible_professionals:
                self.professional_filter.addItem("Nenhuma agenda vinculada", None)
        for professional in visible_professionals:
            self.professional_filter.addItem(professional["full_name"], professional["id"])
        preferred_index = self.professional_filter.findData(preferred_id)
        if preferred_index >= 0:
            self.professional_filter.setCurrentIndex(preferred_index)
        self.professional_filter.setEnabled(self.can_view_all_agendas() or len(visible_professionals) > 1)
        self.professional_filter.blockSignals(False)

    def _fetch_agenda(
        self,
        selected: date,
        requested_professional_id: str | None,
        reload_professionals: bool,
        admin_mode_enabled: bool,
    ) -> dict[str, Any]:
        professionals = self.repo.professionals(active_only=True) if reload_professionals else list(self.professionals)
        can_view_all = self.profile.get("role") == "reception" or (
            self.profile.get("role") == "admin" and admin_mode_enabled
        )
        professional_id = requested_professional_id
        if not can_view_all:
            own = [row for row in professionals if row.get("profile_id") == self.profile.get("id")]
            own_ids = {row.get("id") for row in own}
            if professional_id not in own_ids:
                professional_id = own[0].get("id") if own else None
        rows = [] if not can_view_all and professional_id is None else self.repo.appointments(selected, professional_id)
        return {
            "selected": selected,
            "professional_id": professional_id,
            "professionals": professionals if reload_professionals else None,
            "rows": rows,
        }

    def refresh(self, show_popup: bool = True) -> None:
        if self._refresh_in_progress or self._write_in_progress:
            self._refresh_pending = True
            self._refresh_pending_popup = self._refresh_pending_popup or bool(show_popup)
            LOGGER.info("Atualização da agenda marcada como pendente; outra operação está em andamento")
            return
        scroll_bar = self.list.verticalScrollBar()
        previous_scroll_value = scroll_bar.value()
        was_at_bottom = (
            scroll_bar.maximum() > scroll_bar.minimum()
            and previous_scroll_value >= scroll_bar.maximum() - 2
        )
        selected = self.date_edit.date().toPython()
        professional_id = self.professional_filter.currentData()
        self._pending_scroll_state = (previous_scroll_value, was_at_bottom)
        self._refresh_in_progress = True
        self._refresh_pending_popup = bool(show_popup)
        self._set_busy_ui("Atualizando agenda...")
        LOGGER.info("Iniciando atualização da agenda")
        self._refresh_task_id = start_worker(
            self,
            self._on_refresh_completed,
            self._fetch_agenda,
            selected,
            professional_id,
            not self.professionals_loaded,
            self.admin_mode_enabled,
        )

    @Slot(object, object, object)
    def _on_refresh_completed(self, task_id: int, result: dict[str, Any] | None, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._refresh_task_id:
            return
        show_popup = self._refresh_pending_popup
        self._refresh_task_id = None
        self._refresh_in_progress = False
        if error is not None:
            log_exception("Falha ao atualizar agenda", error)
            if show_popup:
                show_error(self, error)
        elif result is not None:
            if result.get("professionals") is not None:
                self._apply_professionals(result["professionals"], result.get("professional_id"))
            current_key = (self.date_edit.date().toPython(), self.professional_filter.currentData())
            result_key = (result["selected"], result.get("professional_id"))
            if current_key == result_key:
                new_rows = result["rows"]
                screen_changed = self._rendered_agenda_key != result_key or self.rows != new_rows
                self.rows = new_rows
                if screen_changed:
                    self._render_rows(result["selected"])
                    self._rendered_agenda_key = result_key
                LOGGER.info("Atualização da agenda concluída")
                self.activity_completed.emit()
            else:
                self._refresh_pending = True
                LOGGER.info("Resultado antigo da agenda descartado; uma nova atualização será executada")
        pending = self._refresh_pending
        pending_popup = self._refresh_pending_popup
        self._refresh_pending = False
        self._refresh_pending_popup = False
        self._set_busy_ui("")
        if pending:
            self.refresh(show_popup=pending_popup)

    def _render_rows(self, selected: date) -> None:
        visible_appointments = [row for row in self.rows if row.get("status") != "Cancelado"]
        confirmed = sum(1 for row in visible_appointments if row.get("status") == "Confirmado")
        completed = sum(1 for row in visible_appointments if row.get("status") == "Atendido")
        pending = sum(1 for row in visible_appointments if row.get("status") != "Atendido")
        professional_text = self.professional_filter.currentText()
        self.canvas_subtitle.setText(f"{selected.strftime('%d/%m/%Y')} | {professional_text}")
        self.summary_badge.setText(
            f"{len(visible_appointments)} com atendimento  |  {confirmed} confirmados  |  "
            f"{completed} atendidos  |  {pending} pendentes"
        )
        self.day_stat_labels["total"].setText(str(len(visible_appointments)))
        self.day_stat_labels["confirmed"].setText(str(confirmed))
        self.day_stat_labels["completed"].setText(str(completed))
        self.day_stat_labels["pending"].setText(str(pending))
        self.agenda_rows = build_agenda_grid_rows(
            self.professionals,
            self.rows,
            self.professional_filter.currentData(),
            include_free_slots=self.professional_filter.currentData() is not None,
        )
        self.reconcile_appointment_list()
        QTimer.singleShot(0, self._restore_scroll_position)

    def _restore_scroll_position(self) -> None:
        scroll_bar = self.list.verticalScrollBar()
        previous_scroll_value, was_at_bottom = self._pending_scroll_state
        scroll_bar.setValue(
            scroll_bar.maximum() if was_at_bottom else min(previous_scroll_value, scroll_bar.maximum())
        )

    def reconcile_appointment_list(self) -> None:
        if not self.agenda_rows:
            if self.list.count() == 1 and self.list.item(0).data(Qt.UserRole) is None:
                return
            self.list.clear()
            empty = QListWidgetItem()
            empty.setSizeHint(QSize(10, 150))
            self.list.addItem(empty)
            self.list.setItemWidget(empty, self.empty_agenda_card())
            return

        desired_ids = [row.get(AGENDA_KEY) for row in self.agenda_rows]
        if any(row_id is None for row_id in desired_ids) or len(set(desired_ids)) != len(desired_ids):
            self.rebuild_appointment_list()
            return

        desired_id_set = set(desired_ids)
        existing_items: dict[Any, QListWidgetItem] = {}
        obsolete_rows: list[int] = []
        for index in range(self.list.count()):
            item = self.list.item(index)
            appointment = item.data(Qt.UserRole)
            appointment_id = appointment.get(AGENDA_KEY) if isinstance(appointment, dict) else None
            if appointment_id not in desired_id_set or appointment_id in existing_items:
                obsolete_rows.append(index)
            else:
                existing_items[appointment_id] = item

        for index in reversed(obsolete_rows):
            self.remove_appointment_item(index)

        for target_index, row in enumerate(self.agenda_rows):
            appointment_id = row[AGENDA_KEY]
            item = existing_items.get(appointment_id)
            if item is None:
                item = self.add_appointment_item(row, target_index)
                existing_items[appointment_id] = item
            elif item.data(Qt.UserRole) != row:
                self.update_appointment_item(item, row)

        # Nunca retire um widget de um item para anexá-lo novamente. O QListWidget
        # assume a propriedade do widget e o Qt pode destruí-lo durante essa troca,
        # deixando o wrapper Python apontando para um objeto C++ inválido. Novos
        # atendimentos já são inseridos na posição correta acima; se a ordem dos
        # itens existentes mudou, uma reconstrução limpa é mais segura.
        current_ids = [
            (self.list.item(index).data(Qt.UserRole) or {}).get(AGENDA_KEY)
            for index in range(self.list.count())
        ]
        if current_ids != desired_ids:
            self.rebuild_appointment_list()

    def rebuild_appointment_list(self) -> None:
        self.list.clear()
        for row in self.agenda_rows:
            self.add_appointment_item(row)

    def add_appointment_item(self, row: dict[str, Any], index: int | None = None) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, row)
        item.setSizeHint(QSize(10, 96 if row.get(AGENDA_KIND) == "appointment" else 78))
        if index is None:
            self.list.addItem(item)
        else:
            self.list.insertItem(index, item)
        self.list.setItemWidget(
            item,
            self.appointment_card(row) if row.get(AGENDA_KIND) == "appointment" else self.free_slot_card(row),
        )
        return item

    def update_appointment_item(self, item: QListWidgetItem, row: dict[str, Any]) -> None:
        old_widget = self.list.itemWidget(item)
        self.list.removeItemWidget(item)
        if old_widget is not None:
            old_widget.deleteLater()
        item.setData(Qt.UserRole, row)
        item.setSizeHint(QSize(10, 96 if row.get(AGENDA_KIND) == "appointment" else 78))
        self.list.setItemWidget(
            item,
            self.appointment_card(row) if row.get(AGENDA_KIND) == "appointment" else self.free_slot_card(row),
        )

    def remove_appointment_item(self, index: int) -> None:
        item = self.list.item(index)
        widget = self.list.itemWidget(item)
        self.list.removeItemWidget(item)
        if widget is not None:
            widget.deleteLater()
        self.list.takeItem(index)

    def empty_agenda_card(self) -> QWidget:
        frame = QFrame()
        style_card(frame, COLORS["surface_warm"], COLORS["line"])
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 18, 18, 18)
        title = QLabel("Nenhuma grade disponível")
        title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 17px; font-weight: 800;")
        subtitle = QLabel("Não há profissional disponível para esta visualização.")
        subtitle.setObjectName("Muted")
        subtitle.setStyleSheet("background: transparent;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def free_slot_card(self, row: dict[str, Any]) -> QWidget:
        professional = (row.get("professionals") or {}).get("full_name", "")
        frame = QFrame()
        style_card(frame, "#F4FBF7", "#BBDCC8")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 11, 16, 11)
        layout.setSpacing(16)

        time_label = QLabel(f"{row['start_time'][:5]}\n{row['end_time'][:5]}")
        time_label.setMinimumWidth(66)
        time_label.setAlignment(Qt.AlignCenter)
        time_label.setStyleSheet(
            "background: #E3F5EA; color: #23623C; border: 1px solid #BBDCC8; "
            "border-radius: 8px; padding: 6px; font-size: 16px; font-weight: 800;"
        )
        details = QVBoxLayout()
        free_label = QLabel("Livre")
        free_label.setStyleSheet("background: transparent; color: #23623C; font-size: 16px; font-weight: 800;")
        professional_label = QLabel(professional or "Profissional não informado")
        professional_label.setStyleSheet(f"background: transparent; color: {COLORS['muted']};")
        details.addWidget(free_label)
        details.addWidget(professional_label)
        schedule = button("Agendar", secondary=True)
        schedule.setCursor(Qt.PointingHandCursor)
        schedule.clicked.connect(lambda _checked=False, slot=row: self.create_appointment(slot))
        schedule.setEnabled(self._allowed_to_manage and not self._write_in_progress)
        layout.addWidget(time_label)
        layout.addLayout(details, 1)
        layout.addWidget(schedule)
        return frame

    def appointment_card(self, row: dict[str, Any]) -> QWidget:
        appointments = row.get("_agenda_appointments") or [row]
        patient = (row.get("patients") or {}).get("full_name", "")
        if len(appointments) > 1:
            patient = f"{len(appointments)} atendimentos neste horário"
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

        appointment_start = row.get("start_time", "")
        appointment_end = row.get("end_time", "")
        time_label = QLabel(f"{appointment_start[:5]}\n{appointment_end[:5]}")
        time_label.setMinimumWidth(66)
        time_label.setAlignment(Qt.AlignCenter)
        time_label.setStyleSheet(
            f"background: {COLORS['bg2']}; color: {COLORS['text']}; border: 1px solid {COLORS['line']}; "
            "border-radius: 8px; padding: 8px; font-size: 18px; font-weight: 800;"
        )
        details = QVBoxLayout()
        patient_label = QLabel(f"Com atendimento — {patient or 'Paciente não informado'}")
        patient_label.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 16px; font-weight: 800;")
        professional_label = QLabel(professional or "Profissional não informado")
        professional_label.setStyleSheet(f"background: transparent; color: {COLORS['muted']};")
        details.addWidget(patient_label)
        details.addWidget(professional_label)

        if can(self.profile, Permission.CHANGE_STATUS):
            status = AgendaStatusComboBox()
            status.addItems(STATUSES)
            status.setCurrentText(row["status"])
            status.setCursor(Qt.PointingHandCursor)
            status.setMinimumWidth(164)
            status.status_marker = marker
            status.setStyleSheet(
                f"QComboBox {{ background: {bg}; color: {fg}; border: 1px solid {border_color}; "
                "border-radius: 8px; padding: 7px 28px 7px 10px; font-weight: 800; }}"
                f"QComboBox::drop-down {{ border: none; width: 24px; }}"
                f"QComboBox QAbstractItemView {{ background: {COLORS['surface']}; color: {COLORS['text']}; "
                f"selection-background-color: {COLORS['primary_soft']}; selection-color: {COLORS['primary']}; }}"
            )
            status.currentTextChanged.connect(
                lambda value, appointment=row, combo=status: self.change_status(appointment, combo, value)
            )
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

    def change_status(self, appointment: dict[str, Any], combo: QComboBox, status: str) -> None:
        previous_status = appointment.get("status", "")
        if not status or status == previous_status:
            return
        if self.is_busy:
            combo.blockSignals(True)
            combo.setCurrentText(previous_status)
            combo.blockSignals(False)
            return
        self._write_in_progress = True
        combo.setEnabled(False)
        self._set_busy_ui("Salvando status...")
        self._write_context = {
            "kind": "status",
            "appointment": appointment,
            "combo": combo,
            "status": status,
            "previous_status": previous_status,
        }
        LOGGER.info("Iniciando alteração de status do atendimento")
        self._write_task_id = start_worker(
            self, self._on_agenda_write_completed, self.repo.update_appointment_status, appointment["id"], status
        )

    @Slot(object, object, object)
    def _on_agenda_write_completed(self, task_id: int, result: Any, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._write_task_id:
            return
        context = getattr(self, "_write_context", {})
        self._write_task_id = None
        self._write_in_progress = False
        kind = context.get("kind")
        request_refresh = False
        if kind == "status":
            appointment = context["appointment"]
            combo = context["combo"]
            status = context["status"]
            previous_status = context["previous_status"]
            if error is None:
                appointment["status"] = status
                for source_row in self.rows:
                    if source_row.get("id") == appointment.get("id"):
                        source_row["status"] = status
                        break
                self._render_rows(self.date_edit.date().toPython())
                LOGGER.info("Alteração de status concluída")
                self.activity_completed.emit()
            else:
                combo.blockSignals(True)
                combo.setCurrentText(previous_status)
                combo.blockSignals(False)
                self.apply_status_style(combo, previous_status)
                show_error(self, error)
            combo.setEnabled(True)
        elif kind == "create":
            if error is None:
                LOGGER.info("Criação de atendimento concluída")
                request_refresh = True
            else:
                show_error(self, error)
        elif kind == "recurring":
            if error is None:
                QMessageBox.information(self, "Agenda fixa", f"{result} atendimentos gerados.")
                LOGGER.info("Criação de agenda fixa concluída")
                request_refresh = True
            else:
                show_error(self, error)
        self._write_context = {}
        self._set_busy_ui("")
        if request_refresh:
            self._refresh_pending = False
            self._refresh_pending_popup = False
            self.refresh()
        elif self._refresh_pending and not self._refresh_in_progress:
            pending_popup = self._refresh_pending_popup
            self._refresh_pending = False
            self._refresh_pending_popup = False
            self.refresh(show_popup=pending_popup)

    def apply_status_style(self, combo: QComboBox, status: str) -> None:
        bg, fg = STATUS_COLORS.get(status, ("#FFFFFF", COLORS["text"]))
        border_color = fg if fg != COLORS["text"] else COLORS["primary"]
        marker = getattr(combo, "status_marker", None)
        if marker is not None:
            marker.setStyleSheet(f"background: {border_color}; border-radius: 2px;")
        combo.setStyleSheet(
            f"QComboBox {{ background: {bg}; color: {fg}; border: 1px solid {border_color}; "
            "border-radius: 8px; padding: 7px 28px 7px 10px; font-weight: 800; }}"
            "QComboBox::drop-down { border: none; width: 24px; }"
            f"QComboBox QAbstractItemView {{ background: {COLORS['surface']}; color: {COLORS['text']}; "
            f"selection-background-color: {COLORS['primary_soft']}; selection-color: {COLORS['primary']}; }}"
        )

    def update_day_summary(self) -> None:
        visible_appointments = [row for row in self.rows if row.get("status") != "Cancelado"]
        confirmed = sum(1 for row in visible_appointments if row.get("status") == "Confirmado")
        completed = sum(1 for row in visible_appointments if row.get("status") == "Atendido")
        pending = sum(1 for row in visible_appointments if row.get("status") != "Atendido")
        self.summary_badge.setText(
            f"{len(visible_appointments)} com atendimento  |  {confirmed} confirmados  |  "
            f"{completed} atendidos  |  {pending} pendentes"
        )
        self.day_stat_labels["total"].setText(str(len(visible_appointments)))
        self.day_stat_labels["confirmed"].setText(str(confirmed))
        self.day_stat_labels["completed"].setText(str(completed))
        self.day_stat_labels["pending"].setText(str(pending))

    def selected_row(self) -> dict[str, Any] | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def open_selected(self) -> None:
        row = self.selected_row()
        if not row:
            return
        if row.get(AGENDA_KIND) == "free":
            self.create_appointment(row)
            return
        appointments = row.get("_agenda_appointments") or [row]
        row = appointments[0]
        dialog = AppointmentDetailsDialog(self, self.repo, self.profile, row)
        if dialog.exec():
            self.refresh()

    def create_appointment(self, slot: dict[str, Any] | None = None) -> None:
        if self.is_busy:
            return
        if not isinstance(slot, dict):
            slot = None
        self._write_in_progress = True
        self._set_busy_ui("Carregando formulário...")
        self._write_context = {
            "kind": "prepare_create",
            "selected_date": self.date_edit.date().toPython(),
            "professional_id": slot.get("professional_id") if slot else self.professional_filter.currentData(),
            "start_time": slot.get("start_time") if slot else None,
            "end_time": slot.get("end_time") if slot else None,
        }
        self._write_task_id = start_worker(
            self,
            self._on_create_form_loaded,
            lambda: {
                "patients": self.repo.patients(active_only=True),
                "professionals": self.repo.professionals(active_only=True),
            },
        )

    @Slot(object, object, object)
    def _on_create_form_loaded(self, task_id: int, result: dict[str, Any] | None, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._write_task_id:
            return
        selected_date = self._write_context.get("selected_date", self.date_edit.date().toPython())
        professional_id = self._write_context.get("professional_id")
        start_time = self._write_context.get("start_time")
        end_time = self._write_context.get("end_time")
        self._write_task_id = None
        self._write_in_progress = False
        self._write_context = {}
        self._set_busy_ui("")
        if error is not None:
            show_error(self, error)
            return
        dialog = AppointmentDialog(
            self,
            self.repo,
            selected_date,
            patients=result["patients"],
            professionals=result["professionals"],
            selected_professional_id=professional_id,
            selected_start_time=start_time,
            selected_end_time=end_time,
        )
        if not dialog.exec():
            return
        values = dialog.values()
        self._write_in_progress = True
        self._write_context = {"kind": "create"}
        self._set_busy_ui("Salvando atendimento...")
        LOGGER.info("Iniciando criação de atendimento")
        self._write_task_id = start_worker(
            self, self._on_agenda_write_completed, self.repo.save_appointment, values
        )

    def create_recurring(self) -> None:
        if self.is_busy:
            return
        self._write_in_progress = True
        self._set_busy_ui("Carregando formulário...")
        self._write_context = {
            "kind": "prepare_recurring",
            "professional_id": self.professional_filter.currentData(),
        }
        self._write_task_id = start_worker(
            self,
            self._on_recurring_form_loaded,
            lambda: {
                "patients": self.repo.patients(active_only=True),
                "professionals": self.repo.professionals(active_only=True),
            },
        )

    @Slot(object, object, object)
    def _on_recurring_form_loaded(self, task_id: int, result: dict[str, Any] | None, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._write_task_id:
            return
        professional_id = self._write_context.get("professional_id")
        self._write_task_id = None
        self._write_in_progress = False
        self._write_context = {}
        self._set_busy_ui("")
        if error is not None:
            show_error(self, error)
            return
        dialog = RecurringDialog(
            self,
            self.repo,
            patients=result["patients"],
            professionals=result["professionals"],
            selected_professional_id=professional_id,
        )
        if not dialog.exec():
            return
        values = dialog.values()
        self._write_in_progress = True
        self._write_context = {"kind": "recurring"}
        self._set_busy_ui("Gerando atendimentos...")
        self._write_task_id = start_worker(
            self, self._on_agenda_write_completed, self.repo.save_recurring_schedule, values
        )

    def edit_appointments_bulk(self) -> None:
        dialog = BulkAppointmentEditDialog(self, self.repo)
        dialog.exec()

    @Slot(object, object, object)
    def _on_bulk_write_notification(self, _task_id: int, _result: Any, error: Exception | None) -> None:
        if error is None:
            self.refresh()


class AppointmentDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        repo: SupabaseRepository,
        selected_date: date,
        *,
        patients: list[dict[str, Any]] | None = None,
        professionals: list[dict[str, Any]] | None = None,
        selected_professional_id: str | None = None,
        selected_start_time: str | None = None,
        selected_end_time: str | None = None,
    ):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Atendimento")
        self.patients = patients if patients is not None else repo.patients(active_only=True)
        self.professionals = professionals if professionals is not None else repo.professionals(active_only=True)
        layout = QFormLayout(self)
        self.patient = QComboBox()
        for item in self.patients:
            self.patient.addItem(item["full_name"], item["id"])
        self.professional = QComboBox()
        for item in self.professionals:
            self.professional.addItem(item["full_name"], item["id"])
        if selected_professional_id:
            selected_index = self.professional.findData(selected_professional_id)
            if selected_index >= 0:
                self.professional.setCurrentIndex(selected_index)
        self.date_edit = QDateEdit(QDate(selected_date.year, selected_date.month, selected_date.day))
        self.date_edit.setCalendarPopup(True)
        professional = self.current_professional()
        default_start = selected_start_time or (professional or {}).get("agenda_start_time")
        self.start = agenda_time_field(default_start, SupabaseRepository.DEFAULT_AGENDA_START_TIME)
        self.end = time_field(9, 0)
        selected_end = QTime.fromString((selected_end_time or "")[:5], "HH:mm")
        if selected_end.isValid():
            self.end.setTime(selected_end)
        else:
            self.sync_end_from_duration()
        self.professional.currentIndexChanged.connect(self.apply_professional_defaults)
        self.start.timeChanged.connect(self.sync_end_from_duration)
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

    def current_professional(self) -> dict[str, Any] | None:
        professional_id = self.professional.currentData()
        return next((row for row in self.professionals if row.get("id") == professional_id), None)

    def professional_duration(self) -> int:
        professional = self.current_professional() or {}
        try:
            return int(professional.get("agenda_slot_minutes") or SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES)
        except (TypeError, ValueError):
            return SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES

    def sync_end_from_duration(self, _value: Any = None) -> None:
        self.end.setTime(self.start.time().addSecs(self.professional_duration() * 60))

    def apply_professional_defaults(self, _index: int = -1) -> None:
        professional = self.current_professional() or {}
        start_minutes = agenda_time_to_minutes(
            professional.get("agenda_start_time"),
            SupabaseRepository.DEFAULT_AGENDA_START_TIME,
        )
        self.start.setTime(QTime(start_minutes // 60, start_minutes % 60))
        self.sync_end_from_duration()

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
    def __init__(
        self,
        parent: QWidget,
        repo: SupabaseRepository,
        *,
        patients: list[dict[str, Any]] | None = None,
        professionals: list[dict[str, Any]] | None = None,
        selected_professional_id: str | None = None,
    ):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Agenda fixa")
        self.patients = patients if patients is not None else repo.patients(active_only=True)
        self.professionals = professionals if professionals is not None else repo.professionals(active_only=True)
        layout = QFormLayout(self)
        self.patient = QComboBox()
        for item in self.patients:
            self.patient.addItem(item["full_name"], item["id"])
        self.professional = QComboBox()
        for item in self.professionals:
            self.professional.addItem(item["full_name"], item["id"])
        if selected_professional_id:
            selected_index = self.professional.findData(selected_professional_id)
            if selected_index >= 0:
                self.professional.setCurrentIndex(selected_index)
        self.weekday = QComboBox()
        for idx, label in enumerate(["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]):
            self.weekday.addItem(label, idx)
        professional = self.current_professional()
        self.start = agenda_time_field(
            (professional or {}).get("agenda_start_time"),
            SupabaseRepository.DEFAULT_AGENDA_START_TIME,
        )
        self.end = time_field(9, 0)
        self.sync_end_from_duration()
        self.professional.currentIndexChanged.connect(self.apply_professional_defaults)
        self.start.timeChanged.connect(self.sync_end_from_duration)
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

    def current_professional(self) -> dict[str, Any] | None:
        professional_id = self.professional.currentData()
        return next((row for row in self.professionals if row.get("id") == professional_id), None)

    def professional_duration(self) -> int:
        professional = self.current_professional() or {}
        try:
            return int(professional.get("agenda_slot_minutes") or SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES)
        except (TypeError, ValueError):
            return SupabaseRepository.DEFAULT_AGENDA_SLOT_MINUTES

    def sync_end_from_duration(self, _value: Any = None) -> None:
        self.end.setTime(self.start.time().addSecs(self.professional_duration() * 60))

    def apply_professional_defaults(self, _index: int = -1) -> None:
        professional = self.current_professional() or {}
        start_minutes = agenda_time_to_minutes(
            professional.get("agenda_start_time"),
            SupabaseRepository.DEFAULT_AGENDA_START_TIME,
        )
        self.start.setTime(QTime(start_minutes // 60, start_minutes % 60))
        self.sync_end_from_duration()

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
        self.professionals: list[dict[str, Any]] = []
        self.patients: list[dict[str, Any]] = []
        self._load_in_progress = False
        self._load_pending = False
        self._load_task_id: int | None = None
        self._active_load_key: tuple[Any, ...] | None = None
        self._write_in_progress = False
        self._write_task_id: int | None = None
        self.setWindowTitle("Alterar atendimentos em lote")
        self.setMinimumSize(980, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)

        title = QLabel("Selecionar atendimentos")
        title.setObjectName("PageTitle")
        hint = QLabel("Filtre, selecione uma ou mais linhas e escolha as alterações que deseja aplicar.")
        hint.setObjectName("Muted")
        self.loading_label = QLabel("")
        self.loading_label.setObjectName("Muted")
        self.loading_label.setStyleSheet(f"color: {COLORS['primary']}; font-weight: 700;")
        self.loading_label.hide()
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.loading_label)

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
        self.patient = QComboBox()
        self.patient.addItem("Todos os pacientes", None)
        self.search_button = button("Buscar", secondary=True)
        self.search_button.clicked.connect(self.load_rows)
        for column, (label, widget) in enumerate(
            [("Data inicial", self.start_date), ("Data final", self.end_date), ("Dia da semana", self.weekday_filter),
             ("Profissional", self.professional), ("Paciente", self.patient)]
        ):
            filters.addWidget(QLabel(label), 0, column)
            filters.addWidget(widget, 1, column)
        filters.addWidget(self.search_button, 1, 5)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Selecionar", "Data", "Horário", "Paciente", "Profissional", "Status", "Valor"])
        table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionMode(QAbstractItemView.MultiSelection)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.StrongFocus)
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
        self.select_all_button = button("Selecionar todos", secondary=True)
        self.select_all_button.clicked.connect(self.select_all)
        self.clear_selection_button = button("Limpar seleção", secondary=True)
        self.clear_selection_button.clicked.connect(self.clear_selection)
        selection_bar.addWidget(self.result_label)
        selection_bar.addStretch()
        selection_bar.addWidget(self.select_all_button)
        selection_bar.addWidget(self.clear_selection_button)
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
        self.target_patient = QComboBox()
        self.target_patient.addItem("Não alterar", None)
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
        self.delete_button = button("Excluir selecionados", danger=True)
        self.delete_button.clicked.connect(self.delete_selected)
        cancel = button("Cancelar", secondary=True)
        cancel.clicked.connect(self.reject)
        self.apply_button = button("Aplicar alterações")
        self.apply_button.clicked.connect(self.apply_changes)
        actions.addStretch()
        actions.addWidget(self.delete_button)
        actions.addWidget(cancel)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        QTimer.singleShot(0, self.load_initial_data)

    def delete_selected(self) -> None:
        if self._write_in_progress or self._load_in_progress:
            return
        selected = self.selected_appointments()
        if not selected:
            QMessageBox.warning(self, "Nenhuma seleção", "Selecione pelo menos um atendimento na tabela.")
            return
        answer = QMessageBox.question(
            self,
            "Excluir atendimentos",
            f"Excluir definitivamente {len(selected)} atendimento(s) selecionado(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        appointment_ids = [appointment["id"] for appointment in selected if appointment.get("id")]
        self._write_in_progress = True
        self._write_kind = "delete"
        self._deleted_ids = set(appointment_ids)
        self._set_busy_ui("Excluindo atendimentos...")
        LOGGER.info("Iniciando exclusão em lote de %s atendimento(s)", len(appointment_ids))
        self._write_task_id = start_worker(
            self, self._on_bulk_write_completed, self.repo.delete_appointments_bulk, appointment_ids
        )
        self._notify_parent_when_completed(self._write_task_id)

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

    @property
    def is_busy(self) -> bool:
        return self._load_in_progress or self._write_in_progress

    def _set_busy_ui(self, message: str = "") -> None:
        busy = self.is_busy
        self.loading_label.setText(message)
        self.loading_label.setVisible(bool(message))
        self.search_button.setEnabled(not busy)
        self.delete_button.setEnabled(not busy)
        self.apply_button.setEnabled(not busy)
        self.table.setEnabled(not busy)
        self.select_all_button.setEnabled(not busy)
        self.clear_selection_button.setEnabled(not busy)
        for control in (
            self.start_date,
            self.end_date,
            self.weekday_filter,
            self.professional,
            self.patient,
        ):
            control.setEnabled(not busy)

    def _notify_parent_when_completed(self, task_id: int) -> None:
        parent = self.parentWidget()
        worker = getattr(self, "_background_workers", {}).get(task_id)
        if worker is not None and isinstance(parent, AgendaPage):
            worker.signals.completed.connect(parent._on_bulk_write_notification)

    def _fetch_bulk_initial_data(self, start: date, end: date) -> dict[str, Any]:
        return {
            "professionals": self.repo.professionals(active_only=False),
            "patients": self.repo.patients(active_only=False),
            "rows": self.repo.financial_appointments(start, end),
        }

    def load_initial_data(self) -> None:
        if self._load_in_progress:
            return
        self._load_in_progress = True
        self._active_load_key = (
            self.start_date.date().toPython(), self.end_date.date().toPython(), None, None, None
        )
        self._set_busy_ui("Carregando atendimentos...")
        self._load_task_id = start_worker(
            self,
            self._on_initial_data_loaded,
            self._fetch_bulk_initial_data,
            self.start_date.date().toPython(),
            self.end_date.date().toPython(),
        )

    @Slot(object, object, object)
    def _on_initial_data_loaded(self, task_id: int, result: dict[str, Any] | None, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._load_task_id:
            return
        self._load_task_id = None
        self._active_load_key = None
        self._load_in_progress = False
        self._set_busy_ui("")
        if error is not None:
            show_error(self, error)
            return
        self.professionals = result["professionals"]
        self.patients = result["patients"]
        for combo in (self.professional, self.target_professional):
            for item in self.professionals:
                combo.addItem(item["full_name"], item["id"])
        for combo in (self.patient, self.target_patient):
            for item in self.patients:
                combo.addItem(item["full_name"], item["id"])
        self._render_bulk_rows(result["rows"])

    def _fetch_bulk_rows(
        self,
        start: date,
        end: date,
        patient_id: str | None,
        professional_id: str | None,
        weekday: int | None,
    ) -> list[dict[str, Any]]:
        rows = self.repo.financial_appointments(
            start, end, patient_id=patient_id, professional_id=professional_id
        )
        if weekday is not None:
            rows = [row for row in rows if date.fromisoformat(row["appointment_date"]).weekday() == weekday]
        return rows

    def load_rows(self) -> None:
        if self.end_date.date() < self.start_date.date():
            QMessageBox.warning(self, "Período inválido", "A data final deve ser maior ou igual à data inicial.")
            return
        request_key = (
            self.start_date.date().toPython(),
            self.end_date.date().toPython(),
            self.patient.currentData(),
            self.professional.currentData(),
            self.weekday_filter.currentData(),
        )
        if self._load_in_progress or self._write_in_progress:
            if self._load_in_progress and request_key == self._active_load_key:
                return
            self._load_pending = True
            return
        self._load_in_progress = True
        self._active_load_key = request_key
        self._set_busy_ui("Atualizando resultados...")
        self._load_task_id = start_worker(
            self,
            self._on_bulk_rows_loaded,
            self._fetch_bulk_rows,
            self.start_date.date().toPython(),
            self.end_date.date().toPython(),
            self.patient.currentData(),
            self.professional.currentData(),
            self.weekday_filter.currentData(),
        )

    @Slot(object, object, object)
    def _on_bulk_rows_loaded(self, task_id: int, result: list[dict[str, Any]] | None, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._load_task_id:
            return
        self._load_task_id = None
        self._active_load_key = None
        self._load_in_progress = False
        self._set_busy_ui("")
        if error is not None:
            show_error(self, error)
        elif result is not None:
            self._render_bulk_rows(result)
        if self._load_pending:
            self._load_pending = False
            self.load_rows()

    def _render_bulk_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
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
            check.setEnabled(appointment_is_selectable(row))
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
        if self._write_in_progress or self._load_in_progress:
            return
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
        self._write_in_progress = True
        self._write_kind = "update"
        self._set_busy_ui("Aplicando alterações...")
        LOGGER.info("Iniciando alteração em lote de %s atendimento(s)", len(selected))
        self._write_task_id = start_worker(
            self,
            self._on_bulk_write_completed,
            self.repo.update_appointments_bulk,
            selected,
            start_time=f"{start_time}:00" if start_time else None,
            end_time=f"{end_time}:00" if end_time else None,
            status=self.status.currentData(),
            consultation_fee=self.fee.text().strip() or None,
            professional_id=self.target_professional.currentData(),
            patient_id=self.target_patient.currentData(),
        )
        self._notify_parent_when_completed(self._write_task_id)

    @Slot(object, object, object)
    def _on_bulk_write_completed(self, task_id: int, result: Any, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._write_task_id:
            return
        kind = getattr(self, "_write_kind", "")
        self._write_task_id = None
        self._write_in_progress = False
        self._set_busy_ui("")
        if error is not None:
            log_exception("Falha em operação de agenda em lote", error)
            show_error(self, error)
            return
        if kind == "delete":
            self._render_bulk_rows([row for row in self.rows if row.get("id") not in self._deleted_ids])
            QMessageBox.information(
                self, "Atendimentos excluídos", f"{result} atendimento(s) excluído(s) com sucesso."
            )
            LOGGER.info("Exclusão em lote concluída")
        elif kind == "update":
            QMessageBox.information(self, "Atendimentos alterados", f"{result} atendimento(s) atualizado(s).")
            LOGGER.info("Alteração em lote concluída")
            self.accept()


class AppointmentEditDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, profile: dict[str, Any], appointment: dict[str, Any]):
        super().__init__(parent)
        self.repo = repo
        self.profile = profile
        self.appointment = appointment
        self._operation_in_progress = False
        self._operation_task_id: int | None = None
        self.setWindowTitle("Editar atendimento")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        appointment_date = date.fromisoformat(appointment["appointment_date"])
        self.appointment_date = QDateEdit(QDate(appointment_date.year, appointment_date.month, appointment_date.day))
        self.appointment_date.setCalendarPopup(True)
        start_parts = [int(value) for value in appointment["start_time"][:5].split(":")]
        end_parts = [int(value) for value in appointment["end_time"][:5].split(":")]
        self.start_time = time_field(*start_parts)
        self.end_time = time_field(*end_parts)
        self.consultation_fee = QLineEdit()
        self.consultation_fee.setPlaceholderText("Ex.: 150,00")
        self.status = QComboBox()
        self.status.addItems(STATUSES)
        self.status.setCurrentText(appointment["status"])
        form = QGridLayout()
        for column, (label, field) in enumerate([
            ("Data", self.appointment_date), ("Início", self.start_time),
            ("Fim", self.end_time), ("Valor da consulta", self.consultation_fee),
        ]):
            form.addWidget(QLabel(label), 0, column)
            form.addWidget(field, 1, column)
        form.addWidget(QLabel("Status"), 2, 0)
        form.addWidget(self.status, 3, 0, 1, 4)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.loading_label = QLabel("Carregando dados financeiros...")
        self.loading_label.setObjectName("Muted")
        layout.addWidget(self.loading_label)
        self.delete_btn = button("Excluir atendimento", danger=True)
        self.delete_btn.clicked.connect(self.delete_appointment)
        cancel = button("Cancelar", secondary=True)
        cancel.clicked.connect(self.reject)
        self.save_btn = button("Salvar alterações")
        self.save_btn.clicked.connect(self.save_changes)
        actions.addWidget(self.delete_btn)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(self.save_btn)
        layout.addLayout(actions)
        self._operation_in_progress = True
        self._set_operation_ui("Carregando dados financeiros...")
        self._operation_task_id = start_worker(
            self, self._on_financial_loaded, self.repo.appointment_financials, appointment["id"]
        )

    def _set_operation_ui(self, message: str = "") -> None:
        self.loading_label.setText(message)
        self.loading_label.setVisible(bool(message))
        self.delete_btn.setEnabled(not self._operation_in_progress)
        self.save_btn.setEnabled(not self._operation_in_progress)

    @Slot(object, object, object)
    def _on_financial_loaded(self, task_id: int, result: dict[str, Any] | None, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._operation_task_id:
            return
        self._operation_task_id = None
        self._operation_in_progress = False
        self._set_operation_ui("")
        if error is not None:
            show_error(self, error)
            return
        financial = result or {}
        if financial.get("consultation_fee") is not None:
            self.consultation_fee.setText(f"{float(financial['consultation_fee']):.2f}".replace(".", ","))

    def save_changes(self) -> None:
        if self._operation_in_progress:
            return
        try:
            self.repo.parse_consultation_fee(self.consultation_fee.text())
            values = {
                "patient_id": self.appointment["patient_id"],
                "professional_id": self.appointment["professional_id"],
                "appointment_date": self.appointment_date.date().toPython().isoformat(),
                "start_time": self.start_time.time().toString("HH:mm:ss"),
                "end_time": self.end_time.time().toString("HH:mm:ss"),
                "status": self.status.currentText(),
            }
        except Exception as exc:
            show_error(self, exc)
            return
        self._operation_in_progress = True
        self._operation_kind = "save"
        self._set_operation_ui("Salvando alterações...")
        self._operation_task_id = start_worker(
            self,
            self._on_edit_operation_completed,
            self._save_changes_in_repository,
            values,
            self.consultation_fee.text(),
        )

    def _save_changes_in_repository(self, values: dict[str, Any], fee: str) -> None:
        self.repo.save_appointment(values, self.appointment["id"])
        existing = self.repo.appointment_financials(self.appointment["id"]) or {}
        self.repo.save_appointment_financials(self.appointment["id"], {
            "consultation_fee": fee,
            "payment_status": existing.get("payment_status", "Pendente"),
            "payment_method": existing.get("payment_method", ""),
            "financial_notes": existing.get("financial_notes", ""),
        })

    def delete_appointment(self) -> None:
        if self._operation_in_progress:
            return
        patient = (self.appointment.get("patients") or {}).get("full_name", "este paciente")
        answer = QMessageBox.question(
            self, "Excluir atendimento",
            f"Deseja excluir o atendimento de {patient}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self._operation_in_progress = True
            self._operation_kind = "delete"
            self._set_operation_ui("Excluindo atendimento...")
            self._operation_task_id = start_worker(
                self,
                self._on_edit_operation_completed,
                self.repo.delete_appointment,
                self.appointment["id"],
            )

    @Slot(object, object, object)
    def _on_edit_operation_completed(self, task_id: int, _result: Any, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._operation_task_id:
            return
        self._operation_task_id = None
        self._operation_in_progress = False
        self._set_operation_ui("")
        if error is not None:
            show_error(self, error)
            return
        self.accept()


class AppointmentDetailsDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, profile: dict[str, Any], appointment: dict[str, Any]):
        super().__init__(parent)
        self.repo = repo
        self.profile = profile
        self.appointment = appointment
        self._note_in_progress = False
        self._note_task_id: int | None = None
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
        is_professional = profile.get("role") == "professional"

        self.note = QTextEdit()
        self.note.setPlaceholderText("Relatório da sessão")
        self.note.setMinimumHeight(460)
        self.note.setEnabled(can(profile, Permission.WRITE_SESSION_NOTE))
        self.loading_label = QLabel("Carregando relatório...")
        self.loading_label.setObjectName("Muted")

        edit_buttons = QHBoxLayout()
        self.edit_button = None
        if profile.get("role") in {"admin", "reception"} and can_manage:
            self.edit_button = button("Editar atendimento", secondary=True)
            self.edit_button.setMinimumWidth(170)
            self.edit_button.clicked.connect(self.open_edit_dialog)
            edit_buttons.addWidget(self.edit_button)
        edit_buttons.addStretch()

        note_buttons = QHBoxLayout()
        self.save_note_button = None
        if can(profile, Permission.WRITE_SESSION_NOTE):
            self.save_note_button = button("Salvar status e relatório" if is_professional else "Salvar relatório")
            self.save_note_button.setMinimumWidth(170)
            self.save_note_button.clicked.connect(self.save_session_report)
            note_buttons.addWidget(self.save_note_button)
        note_buttons.addStretch()

        layout.addWidget(header)
        layout.addWidget(patient_card)
        layout.addLayout(edit_buttons)
        if is_professional and can(profile, Permission.CHANGE_STATUS):
            professional_status_row = QHBoxLayout()
            professional_status_row.addWidget(QLabel("Status"))
            professional_status_row.addWidget(self.status, 1)
            layout.addLayout(professional_status_row)
        layout.addWidget(QLabel("Relatório da sessão"))
        layout.addWidget(self.loading_label)
        layout.addWidget(self.note, 1)
        layout.addLayout(note_buttons)
        self._note_in_progress = True
        self._set_note_ui("Carregando relatório...")
        self._note_task_id = start_worker(
            self, self._on_note_loaded, self.repo.session_note_for, appointment["id"]
        )

    def _set_note_ui(self, message: str = "") -> None:
        self.loading_label.setText(message)
        self.loading_label.setVisible(bool(message))
        if self.edit_button is not None:
            self.edit_button.setEnabled(not self._note_in_progress)
        if self.save_note_button is not None:
            self.save_note_button.setEnabled(not self._note_in_progress)
        self.note.setEnabled(can(self.profile, Permission.WRITE_SESSION_NOTE) and not self._note_in_progress)

    @Slot(object, object, object)
    def _on_note_loaded(self, task_id: int, result: dict[str, Any] | None, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._note_task_id:
            return
        self._note_task_id = None
        self._note_in_progress = False
        self._set_note_ui("")
        if error is not None:
            log_exception("Falha ao carregar relatório da sessão", error)
            return
        self.note.setPlainText(result.get("note", "") if result else "")

    def info_label(self, title: str, value: str) -> QLabel:
        label = QLabel(f"{title}\n{value}")
        label.setWordWrap(True)
        label.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-weight: 600;")
        return label

    def open_edit_dialog(self) -> None:
        if self.profile.get("role") not in {"admin", "reception"} or not can(self.profile, Permission.MANAGE_AGENDA):
            raise AppError("Você não possui permissão para editar o atendimento.")
        dialog = AppointmentEditDialog(self, self.repo, self.profile, self.appointment)
        if dialog.exec():
            self.accept()

    def save_session_report(self) -> None:
        if self._note_in_progress:
            return
        try:
            if not can(self.profile, Permission.WRITE_SESSION_NOTE):
                raise AppError("Você não possui permissão para salvar o relatório da sessão.")
        except Exception as exc:
            show_error(self, exc)
            return
        status = self.status.currentText()
        note = self.note.toPlainText().strip()
        self._note_in_progress = True
        self._set_note_ui("Salvando relatório...")
        self._note_task_id = start_worker(
            self, self._on_note_saved, self._save_session_report_in_repository, status, note
        )

    def _save_session_report_in_repository(self, status: str, note: str) -> None:
        if self.profile.get("role") == "professional" and can(self.profile, Permission.CHANGE_STATUS):
            self.repo.update_appointment_status(self.appointment["id"], status)
        self.repo.save_session_note(self.appointment, note)

    @Slot(object, object, object)
    def _on_note_saved(self, task_id: int, _result: Any, error: Exception | None) -> None:
        forget_worker(self, task_id)
        if task_id != self._note_task_id:
            return
        self._note_task_id = None
        self._note_in_progress = False
        self._set_note_ui("")
        if error is not None:
            show_error(self, error)
            return
        self.accept()

class FinancePage(QWidget):
    def __init__(self, repo: SupabaseRepository):
        super().__init__()
        self.repo = repo
        self.rows: list[dict[str, Any]] = []
        self.appointment_rows: list[dict[str, Any]] = []
        self.patient_payment_item_rows: list[dict[str, Any]] = []
        self.professional_payout_item_rows: list[dict[str, Any]] = []
        self.summary_rows: list[dict[str, Any]] = []
        self.summary_payment_item_rows: list[dict[str, Any]] = []
        self.summary_payout_item_rows: list[dict[str, Any]] = []
        self.receipt_history_rows: list[dict[str, Any]] = []
        self.professionals: list[dict[str, Any]] = []
        self.patients: list[dict[str, Any]] = []
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
        self.use_date_filter.setVisible(False)
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
        _, month_end = current_month_period(today.toPython())
        self.end_date = QDateEdit(QDate(month_end.year, month_end.month, month_end.day))
        self.end_date.setCalendarPopup(True)
        self.end_date.setMinimumWidth(130)
        self.end_date.dateChanged.connect(self.refresh)
        self.status_filter = QComboBox()
        self.status_filter.setMinimumWidth(140)
        self.status_filter.addItem("Todos", "all")
        self.status_filter.addItem("Abertos", "open")
        self.status_filter.addItem("Fechados", "closed")
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
            "Agendas por paciente",
            ["Paciente", "Agendas", "Valor recebido"],
        )
        full_row_table_polish(self.patient_summary_table)
        self.patient_summary_table.cellDoubleClicked.connect(self.open_patient_receipt_from_summary)
        self.patient_summary_table.setToolTip("Dê duplo clique em uma linha para abrir a operação financeira")
        self.patient_summary_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.patient_summary_table.verticalHeader().setDefaultSectionSize(40)
        self.patient_summary_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, self.patient_summary_table.columnCount()):
            self.patient_summary_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        patient_action_hint = QLabel("Duplo clique em um paciente para abrir a operação financeira")
        patient_action_hint.setObjectName("Muted")
        patient_action_hint.setStyleSheet("background: transparent;")
        patient_summary_panel.layout().insertWidget(1, patient_action_hint)
        self.professional_summary_table, professional_summary_panel = self.summary_table_panel(
            "Agendas por funcionário",
            ["Funcionário", "Agendas", "Valor repassado"],
        )
        full_row_table_polish(self.professional_summary_table)
        self.professional_summary_table.cellDoubleClicked.connect(self.open_professional_payout_from_summary)
        self.professional_summary_table.setToolTip("Dê duplo clique em uma linha para abrir a operação financeira")
        self.professional_summary_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.professional_summary_table.verticalHeader().setDefaultSectionSize(40)
        self.professional_summary_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, self.professional_summary_table.columnCount()):
            self.professional_summary_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        professional_action_hint = QLabel("Duplo clique em um funcionário para abrir a operação financeira")
        professional_action_hint.setObjectName("Muted")
        professional_action_hint.setStyleSheet("background: transparent;")
        professional_summary_panel.layout().insertWidget(1, professional_action_hint)
        summary_lists.addWidget(patient_summary_panel, 1)
        summary_lists.addWidget(professional_summary_panel, 1)
        summary_layout.addLayout(summary_lists, 1)

        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)
        history_layout.setContentsMargins(0, 6, 0, 0)
        history_layout.setSpacing(6)
        receipt_history_page = QWidget()
        receipt_history_layout = QVBoxLayout(receipt_history_page)
        receipt_history_layout.setContentsMargins(0, 0, 0, 0)
        self.receipt_history_table, receipt_history_panel = self.summary_table_panel(
            "Operações financeiras",
            [
                "Data", "Paciente", "Profissional", "Valor recebido", "% repasse",
                "Valor repasse", "Situação", "Forma", "Atendimentos",
            ],
        )
        full_row_table_polish(self.receipt_history_table)
        self.receipt_history_table.cellDoubleClicked.connect(lambda _row, _col: self.edit_history_transaction("receipt"))
        receipt_history_actions = QHBoxLayout()
        new_payout = button("Novo repasse")
        edit_receipt = button("Editar operação", secondary=True)
        delete_receipt = button("Excluir operação", secondary=True)
        new_payout.clicked.connect(self.new_financial_operation)
        edit_receipt.clicked.connect(lambda: self.edit_history_transaction("receipt"))
        delete_receipt.clicked.connect(lambda: self.delete_history_transaction("receipt"))
        receipt_history_actions.addStretch()
        receipt_history_actions.addWidget(new_payout)
        receipt_history_actions.addWidget(edit_receipt)
        receipt_history_actions.addWidget(delete_receipt)
        receipt_history_panel.layout().insertLayout(1, receipt_history_actions)
        receipt_history_layout.addWidget(receipt_history_panel, 1)
        history_layout.addWidget(receipt_history_page, 1)

        self.tabs.addTab(summary_tab, "Resumo")
        self.tabs.addTab(history_tab, "Financeiro")

        layout.addWidget(title)
        layout.addWidget(filters_panel)
        layout.addWidget(self.tabs, 1)
        self.lookups_loaded = False
        self.use_date_filter.blockSignals(True)
        self.use_date_filter.setChecked(True)
        self.use_date_filter.blockSignals(False)
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

    def open_patient_receipt_from_summary(self, row: int, _col: int) -> None:
        item = self.patient_summary_table.item(row, 0)
        patient_id = item.data(Qt.UserRole) if item else None
        if not patient_id:
            return
        try:
            dialog = PatientReceiptDialog(
                self,
                self.repo,
                self.money,
                patient_id=patient_id,
                professional_id=self.professional_filter.currentData(),
                start_date=self.start_date.date().toPython(),
                end_date=self.end_date.date().toPython(),
                situation="all",
            )
            if dialog.exec():
                self.repo.save_patient_payment(dialog.values(), dialog.selected_items())
                QMessageBox.information(self, "Recebimento", "Recebimento registrado com sucesso.")
                self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def new_financial_operation(self) -> None:
        try:
            dialog = PatientReceiptDialog(
                self,
                self.repo,
                self.money,
                patient_id=self.patient_filter.currentData(),
                professional_id=self.professional_filter.currentData(),
                start_date=self.start_date.date().toPython(),
                end_date=self.end_date.date().toPython(),
                situation="all",
            )
            if dialog.exec():
                self.repo.save_patient_payment(dialog.values(), dialog.selected_items())
                QMessageBox.information(self, "Financeiro", "Operação financeira registrada com sucesso.")
                self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def open_professional_payout_from_summary(self, row: int, _col: int) -> None:
        item = self.professional_summary_table.item(row, 0)
        professional_id = item.data(Qt.UserRole) if item else None
        if not professional_id:
            return
        try:
            dialog = PatientReceiptDialog(
                self,
                self.repo,
                self.money,
                professional_id=professional_id,
                patient_id=self.patient_filter.currentData(),
                start_date=self.start_date.date().toPython(),
                end_date=self.end_date.date().toPython(),
                situation="all",
            )
            if dialog.exec():
                self.repo.save_patient_payment(dialog.values(), dialog.selected_items())
                QMessageBox.information(self, "Financeiro", "Operação financeira registrada com sucesso.")
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
            rows = self.repo.financial_appointments(
                self.start_date.date().toPython(),
                self.end_date.date().toPython(),
                patient_id=patient_id,
            )
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
            rows = self.repo.financial_appointments(
                self.start_date.date().toPython(),
                self.end_date.date().toPython(),
                professional_id=professional_id,
            )
            payout_items = self.repo.professional_payout_items_for_appointments([row["id"] for row in rows])
        except Exception as exc:
            show_error(self, exc)
            rows = []
            payout_items = []

        repassed_appointments: set[str] = set()
        for item in payout_items:
            appointment_id = item.get("appointment_id")
            if appointment_id and item.get("is_repassed", True):
                repassed_appointments.add(appointment_id)

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
            paid = payout if row.get("id") in repassed_appointments else 0.0
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
        text.setStyleSheet("background: transparent; font-size: 13px; font-weight: 700;")
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
        self.start_date.setEnabled(True)
        self.end_date.setEnabled(True)
        if refresh:
            self.refresh()

    def refresh(self) -> None:
        if not self.lookups_loaded:
            self.load_patients()
            self.load_professionals()
            self.lookups_loaded = True
        try:
            start = self.start_date.date().toPython()
            end = self.end_date.date().toPython()
            patient_id = self.patient_filter.currentData()
            professional_id = self.professional_filter.currentData()
            self.rows = self.repo.patient_payment_balance_appointments(
                patient_id=patient_id,
                professional_id=professional_id,
                start_date=start,
                end_date=end,
                balance_filter=self.status_filter.currentData() or "open",
            )
            appointment_ids = [row.get("id") for row in self.rows if row.get("id")]
            self.patient_payment_item_rows = self.repo.patient_payment_items_for_appointments(appointment_ids)
            self.professional_payout_item_rows = self.repo.professional_payout_items_for_appointments(appointment_ids)
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
            except Exception as history_exc:
                log_exception("Erro ao carregar histórico financeiro", history_exc)
                self.receipt_history_rows = []
            self.appointment_rows = self.filter_appointment_rows_by_situation(
                appointment_rows,
                appointment_payment_items,
                self.status_filter.currentData() or "open",
            )
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
            self.appointment_rows = []
            self.patient_payment_item_rows = []
            self.professional_payout_item_rows = []
            self.summary_rows = []
            self.summary_payment_item_rows = []
            self.summary_payout_item_rows = []
            self.receipt_history_rows = []
        self.fill_summary()
        self.fill_financial_history()

    def fill_financial_history(self) -> None:
        self.receipt_history_table.setRowCount(len(self.receipt_history_rows))
        for row_index, row in enumerate(self.receipt_history_rows):
            items = row.get("patient_payment_items") or []
            professional_name = (row.get("professionals") or {}).get("full_name")
            if not professional_name and items:
                legacy_professional_id = (items[0].get("appointments") or {}).get("professional_id")
                professional_name = next(
                    (
                        professional.get("full_name")
                        for professional in self.professionals
                        if professional.get("id") == legacy_professional_id
                    ),
                    None,
                )
            raw_date = str(row.get("payment_date") or "")
            try:
                date_text = date.fromisoformat(raw_date[:10]).strftime("%d/%m/%Y")
            except ValueError:
                date_text = raw_date
            values = [
                date_text,
                (row.get("patients") or {}).get("full_name", "Paciente não informado"),
                professional_name or "Profissional não informado",
                self.money(row.get("amount")),
                f"{float(row.get('payout_percentage') or 0):g}%",
                self.money(row.get("payout_amount")),
                "Repassado" if row.get("is_repassed") else "Não repassado",
                row.get("payment_method") or "Não informado",
                str(len(items)),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter if column not in {1, 2} else Qt.AlignLeft | Qt.AlignVCenter)
                if column == 0:
                    item.setData(Qt.UserRole, row.get("id"))
                self.receipt_history_table.setItem(row_index, column, item)
        self.receipt_history_table.resizeColumnsToContents()
        self.receipt_history_table.horizontalHeader().setStretchLastSection(True)

    def selected_history_transaction(self, transaction_type: str) -> dict[str, Any] | None:
        table = self.receipt_history_table if transaction_type == "receipt" else self.payout_history_table
        rows = self.receipt_history_rows if transaction_type == "receipt" else self.payout_history_rows
        row_index = table.currentRow()
        return rows[row_index] if 0 <= row_index < len(rows) else None

    def edit_history_transaction(self, transaction_type: str) -> None:
        transaction = self.selected_history_transaction(transaction_type)
        if not transaction:
            QMessageBox.information(self, "Histórico", "Selecione uma movimentação para editar.")
            return
        item_key = "patient_payment_items" if transaction_type == "receipt" else "professional_payout_items"
        linked_dates = []
        for item in transaction.get(item_key) or []:
            raw_date = (item.get("appointments") or {}).get("appointment_date")
            try:
                linked_dates.append(date.fromisoformat(str(raw_date)[:10]))
            except ValueError:
                pass
        start = min([self.start_date.date().toPython(), *linked_dates])
        end = max([self.end_date.date().toPython(), *linked_dates])
        if transaction_type == "receipt":
            receipt_professional_id = transaction.get("professional_id")
            if not receipt_professional_id and transaction.get(item_key):
                receipt_professional_id = (
                    (transaction[item_key][0].get("appointments") or {}).get("professional_id")
                )
            dialog = PatientReceiptDialog(
                self, self.repo, self.money,
                patient_id=transaction.get("patient_id"), professional_id=receipt_professional_id,
                start_date=start, end_date=end,
                situation="all", transaction=transaction,
            )
        else:
            dialog = ProfessionalPayoutDialog(
                self, self.repo, self.money,
                professional_id=transaction.get("professional_id"), start_date=start, end_date=end,
                situation="all", transaction=transaction,
            )
        if not dialog.exec():
            return
        try:
            if transaction_type == "receipt":
                self.repo.update_patient_payment(transaction["id"], dialog.values(), dialog.selected_items())
            else:
                self.repo.update_professional_payout(transaction["id"], dialog.values(), dialog.selected_items())
            QMessageBox.information(self, "Histórico", "Movimentação atualizada com sucesso.")
            self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def delete_history_transaction(self, transaction_type: str) -> None:
        transaction = self.selected_history_transaction(transaction_type)
        if not transaction:
            QMessageBox.information(self, "Histórico", "Selecione uma movimentação para excluir.")
            return
        label = "recebimento" if transaction_type == "receipt" else "repasse"
        answer = QMessageBox.question(
            self,
            "Confirmar exclusão",
            f"Deseja excluir este {label}? Os atendimentos vinculados voltarão a ficar em aberto.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            if transaction_type == "receipt":
                self.repo.delete_patient_payment(transaction["id"])
            else:
                self.repo.delete_professional_payout(transaction["id"])
            QMessageBox.information(self, "Histórico", "Movimentação excluída com sucesso.")
            self.refresh()
        except Exception as exc:
            show_error(self, exc)

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
        paid_appointments: set[str] = set()
        for item in payment_items:
            appointment_id = item.get("appointment_id")
            if appointment_id and item.get("is_paid", True):
                paid_appointments.add(appointment_id)

        filtered: list[dict[str, Any]] = []
        for row in rows:
            fee = self.fee_for(row)
            paid = fee if row.get("id") in paid_appointments else 0.0
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
        rows_by_id = {row.get("id"): row for row in self.rows if row.get("id")}
        totals: dict[str, float] = {}
        for row in self.patient_payment_item_rows:
            appointment_id = row.get("appointment_id")
            appointment = rows_by_id.get(appointment_id)
            if not appointment or not row.get("is_paid", True):
                continue
            totals[appointment_id] = self.fee_for(appointment)
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
            if row.get("is_paid", True):
                totals[patient_id] = totals.get(patient_id, 0.0) + self.fee_for(appointment)
        return totals

    def payout_totals_by_appointment(self) -> dict[str, float]:
        rows_by_id = {row.get("id"): row for row in self.rows if row.get("id")}
        totals: dict[str, float] = {}
        for row in self.professional_payout_item_rows:
            appointment_id = row.get("appointment_id")
            appointment = rows_by_id.get(appointment_id)
            if not appointment or not row.get("is_repassed", True):
                continue
            totals[appointment_id] = self.fee_for(appointment) * 0.70
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
            if row.get("is_repassed", True):
                totals[professional_id] = totals.get(professional_id, 0.0) + self.fee_for(appointment) * 0.70
        return totals

    def payment_totals_by_payment_date(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.receipt_history_rows:
            patient_id = row.get("patient_id")
            if not patient_id:
                continue
            totals[patient_id] = totals.get(patient_id, 0.0) + float(row.get("amount") or 0)
        return totals

    def payout_totals_by_payout_date(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.payout_history_rows:
            professional_id = row.get("professional_id")
            if not professional_id:
                continue
            totals[professional_id] = totals.get(professional_id, 0.0) + float(row.get("amount") or 0)
        return totals

    def fill_summary(self) -> None:
        clear_layout(self.summary_grid)
        received_cash = round(sum(float(row.get("amount") or 0) for row in self.receipt_history_rows), 2)
        receipt_payout_cash = sum(
            float(row.get("payout_amount") or 0)
            for row in self.receipt_history_rows
            if row.get("is_repassed")
        )
        paid_cash = round(receipt_payout_cash, 2)
        clinic_cash = round(received_cash - paid_cash, 2)
        cards = [
            ("Valor recebido", self.money(received_cash), COLORS["green"]),
            ("Repassado para funcionários", self.money(paid_cash), COLORS["blue"]),
            ("Parte da clínica", self.money(clinic_cash), COLORS["primary"]),
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
            self.summary_grid.addWidget(frame, 0, col)
        self.fill_group_tables()

    def fill_group_tables(self) -> None:
        patient_rows = self.group_by_patient()
        professional_rows = self.group_by_professional()
        self.fill_plain_table(
            self.patient_summary_table,
            patient_rows,
            ["name", "count", "received"],
            money_keys={"received"},
        )
        self.fill_plain_table(
            self.professional_summary_table,
            professional_rows,
            ["name", "count", "paid"],
            money_keys={"paid"},
        )

    def group_by_patient(self) -> list[dict[str, Any]]:
        selected_patient_id = self.patient_filter.currentData()
        selected_professional_id = self.professional_filter.currentData()
        professional_patient_ids = {
            row.get("patient_id")
            for row in self.appointment_rows
            if row.get("patient_id")
        } if selected_professional_id else None
        grouped: dict[str, dict[str, Any]] = {
            patient["id"]: {
                "id": patient["id"], "name": patient.get("full_name", "Paciente não informado"),
                "count": 0, "total": 0.0, "received": 0.0, "open": 0.0,
            }
            for patient in self.patients
            if patient.get("id")
            and (not selected_patient_id or patient.get("id") == selected_patient_id)
            and (professional_patient_ids is None or patient.get("id") in professional_patient_ids)
        }
        payments = self.payment_totals_by_payment_date()
        for row in self.appointment_rows:
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
            item["name"] = self.patient_name_for(row)
            item["count"] += 1
            item["total"] += fee
        for patient_id, item in grouped.items():
            item["received"] = payments.get(patient_id, 0.0)
            item["open"] = max(item["total"] - item["received"], 0)
        return sorted(grouped.values(), key=lambda item: item["name"].casefold())

    def group_by_professional(self) -> list[dict[str, Any]]:
        selected_professional_id = self.professional_filter.currentData()
        selected_patient_id = self.patient_filter.currentData()
        patient_professional_ids = {
            row.get("professional_id")
            for row in self.appointment_rows
            if row.get("professional_id")
        } if selected_patient_id else None
        grouped: dict[str, dict[str, Any]] = {
            professional["id"]: {
                "id": professional["id"], "name": professional.get("full_name", "Funcionário não informado"),
                "count": 0, "gross": 0.0, "percent": "70%", "total_payout": 0.0,
                "paid": 0.0, "open": 0.0,
            }
            for professional in self.professionals
            if professional.get("id") and (
                not selected_professional_id or professional.get("id") == selected_professional_id
            )
            and (patient_professional_ids is None or professional.get("id") in patient_professional_ids)
        }
        repassed_by_professional: dict[str, float] = {}
        for operation in self.receipt_history_rows:
            if not operation.get("is_repassed"):
                continue
            professional_id = operation.get("professional_id")
            if not professional_id:
                items = operation.get("patient_payment_items") or []
                if items:
                    professional_id = (items[0].get("appointments") or {}).get("professional_id")
            if professional_id:
                repassed_by_professional[professional_id] = round(
                    repassed_by_professional.get(professional_id, 0.0)
                    + float(operation.get("payout_amount") or 0),
                    2,
                )
        for row in self.appointment_rows:
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
            item["name"] = self.professional_name_for(row)
            item["count"] += 1
            item["gross"] += fee
            item["total_payout"] = round(item["total_payout"] + self.repo.professional_share(fee), 2)
        for professional_id, item in grouped.items():
            item["paid"] = repassed_by_professional.get(professional_id, 0.0)
        return sorted(grouped.values(), key=lambda item: item["name"].casefold())

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

    def payment_colors(self, status: str) -> tuple[str, str]:
        return {
            "Pago": (COLORS["green_card"], COLORS["green"]),
            "Pendente": (COLORS["yellow_card"], COLORS["text"]),
            "Cortesia": (COLORS["blue_soft"], COLORS["blue"]),
            "Cancelado": (COLORS["gray"], COLORS["muted"]),
            "Nao informado": (COLORS["primary_soft"], COLORS["primary_dark"]),
        }.get(status, (COLORS["bg2"], COLORS["text"]))


class FinancialHistoryEditDialog(QDialog):
    PAYMENT_METHODS = ["PIX", "Dinheiro", "Crédito", "Débito", "Unimed", "Outro", "Nao informado"]

    def __init__(self, parent: QWidget, transaction: dict[str, Any], transaction_type: str, money_formatter):
        super().__init__(parent)
        self.transaction_type = transaction_type
        is_receipt = transaction_type == "receipt"
        self.setWindowTitle("Editar recebimento" if is_receipt else "Editar repasse")
        self.setMinimumWidth(520)
        layout = QFormLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        raw_date = transaction.get("payment_date" if is_receipt else "payout_date")
        try:
            parsed_date = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            parsed_date = date.today()
        self.transaction_date = QDateEdit(QDate(parsed_date.year, parsed_date.month, parsed_date.day))
        self.transaction_date.setCalendarPopup(True)
        self.transaction_date.setDisplayFormat("dd/MM/yyyy")
        self.amount = QLineEdit(money_formatter(transaction.get("amount")))
        self.payment_method = QComboBox()
        self.payment_method.addItems(self.PAYMENT_METHODS)
        current_method = self.payment_method.findText(str(transaction.get("payment_method") or "Nao informado"))
        if current_method >= 0:
            self.payment_method.setCurrentIndex(current_method)
        layout.addRow("Data", self.transaction_date)
        layout.addRow("Valor recebido" if is_receipt else "Valor repassado", self.amount)
        layout.addRow("Forma de pagamento", self.payment_method)
        actions = QHBoxLayout()
        cancel = button("Cancelar", secondary=True)
        save = button("Salvar alterações")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addRow(actions)

    def values(self) -> dict[str, Any]:
        return {
            "date": self.transaction_date.date().toPython().isoformat(),
            "amount": self.amount.text().strip(),
            "payment_method": self.payment_method.currentText(),
        }


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


class LegacyPatientReceiptDialog(QDialog):
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
        self.table.setFocusPolicy(Qt.StrongFocus)
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


class LegacyProfessionalPayoutDialog(QDialog):
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
        self.table.setFocusPolicy(Qt.StrongFocus)
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


class FinancialTransactionDialog(QDialog):
    PAYMENT_METHODS = ["PIX", "Dinheiro", "Crédito", "Débito", "Unimed", "Outro"]

    def __init__(
        self,
        parent: QWidget,
        repo: SupabaseRepository,
        money_formatter,
        transaction_type: str,
        *,
        patient_id: str | None = None,
        professional_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        situation: str = "all",
        transaction: dict[str, Any] | None = None,
    ):
        super().__init__(parent)
        self.repo = repo
        self.money = money_formatter
        self.transaction_type = transaction_type
        self.rows: list[dict[str, Any]] = []
        self.row_checks: list[QCheckBox] = []
        self.updating_checks = False
        self.selected_total_value = 0.0
        self.selected_gross_total_value = 0.0
        self.amount_overridden = False
        self.initial_patient_id = patient_id
        self.initial_professional_id = professional_id
        self.situation = situation
        self.edit_transaction = transaction
        item_key = "patient_payment_items" if transaction_type == "receipt" else "professional_payout_items"
        self.initial_selected_ids = {
            item.get("appointment_id") for item in (transaction or {}).get(item_key, []) if item.get("appointment_id")
        }
        is_receipt = transaction_type == "receipt"
        self.setWindowTitle(
            ("Editar operação financeira" if is_receipt else "Editar repasse")
            if transaction else ("Registrar operação financeira" if is_receipt else "Registrar repasse")
        )
        self.setMinimumSize(1300, 760)
        self.resize(1300, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(10)

        title = QLabel("Operação financeira" if is_receipt else "Repasse a profissional")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Selecione livremente os atendimentos vinculados a esta operação. "
            "Atendimentos cancelados permanecem disponíveis e são apenas destacados."
        )
        subtitle.setObjectName("Muted")
        root.addWidget(title)
        root.addWidget(subtitle)

        filters = page_card()
        filter_layout = QGridLayout(filters)
        filter_layout.setContentsMargins(14, 10, 14, 10)
        filter_layout.setHorizontalSpacing(10)
        filter_layout.setVerticalSpacing(4)
        self.subject = QComboBox()
        self.subject.setMinimumWidth(260)
        self.counterpart = QComboBox()
        self.counterpart.setMinimumWidth(230)
        period_start, period_end = current_month_period()
        period_start = start_date or period_start
        period_end = end_date or period_end
        self.period_start = QDateEdit(QDate(period_start.year, period_start.month, period_start.day))
        self.period_end = QDateEdit(QDate(period_end.year, period_end.month, period_end.day))
        for field in (self.period_start, self.period_end):
            field.setCalendarPopup(True)
            field.setDisplayFormat("dd/MM/yyyy")
        search = button("Buscar", secondary=True)
        search.clicked.connect(self.load_appointments)
        filter_fields = [
            ("Paciente" if is_receipt else "Profissional", self.subject),
            ("Profissional" if is_receipt else "Paciente", self.counterpart),
            ("Data inicial", self.period_start),
            ("Data final", self.period_end),
        ]
        for column, (label, widget) in enumerate(filter_fields):
            field_label = QLabel(label)
            field_label.setStyleSheet("background: transparent;")
            filter_layout.addWidget(field_label, 0, column)
            filter_layout.addWidget(widget, 1, column)
        filter_layout.addWidget(search, 1, len(filter_fields))
        filter_layout.setColumnStretch(0, 2)
        root.addWidget(filters)

        table_panel = page_card()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(14, 10, 14, 10)
        table_layout.setSpacing(8)
        table_header = QHBoxLayout()
        table_title = QLabel("Atendimentos do período")
        table_title.setStyleSheet(f"background: transparent; color: {COLORS['text']}; font-size: 16px; font-weight: 800;")
        self.result_label = QLabel("0 atendimento(s)")
        self.result_label.setObjectName("Muted")
        table_header.addWidget(table_title)
        table_header.addStretch()
        table_header.addWidget(self.result_label)
        headers = (
            ["Receber", "Data", "Horário", "Paciente", "Profissional", "Status", "Valor original", "Forma registrada", "Já registrado"]
            if is_receipt
            else ["Repassar", "Data", "Horário", "Profissional", "Paciente", "Status", "Valor original", "Repasse ref.", "Já repassado"]
        )
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setItemDelegate(FinancialRowDelegate(self.table))
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setMinimumHeight(44 * 6 + 38)
        self.table.setFocusPolicy(Qt.StrongFocus)
        check_icon = resource_path("check_white.svg").as_posix()
        self.table.setStyleSheet(
            self.table.styleSheet()
            + f"""
            QTableWidget::item {{ border: none; border-radius: 0; padding: 0 6px; }}
            QCheckBox::indicator {{ width: 21px; height: 21px; border-radius: 5px; border: 2px solid {COLORS['primary']}; background: #FFFFFF; }}
            QCheckBox::indicator:checked {{ background: {COLORS['primary']}; border: 2px solid {COLORS['primary']}; image: url("{check_icon}"); }}
            """
        )
        table_layout.addLayout(table_header)
        table_layout.addWidget(self.table, 1)
        root.addWidget(table_panel, 1)

        footer = page_card()
        footer_layout = QGridLayout(footer)
        footer_layout.setContentsMargins(14, 10, 14, 10)
        footer_layout.setHorizontalSpacing(10)
        footer_layout.setVerticalSpacing(5)
        transaction_date_value = (transaction or {}).get("payment_date" if is_receipt else "payout_date")
        try:
            parsed_transaction_date = date.fromisoformat(str(transaction_date_value)[:10])
        except ValueError:
            parsed_transaction_date = date.today()
        self.transaction_date = QDateEdit(QDate(parsed_transaction_date.year, parsed_transaction_date.month, parsed_transaction_date.day))
        self.transaction_date.setCalendarPopup(True)
        self.transaction_date.setDisplayFormat("dd/MM/yyyy")
        self.transaction_date.setFixedWidth(155)
        self.reference_total = QLineEdit("0,00")
        self.reference_total.setReadOnly(True)
        self.reference_total.setFixedWidth(120 if is_receipt else 180)
        self.reference_total.setCursor(Qt.ArrowCursor)
        self.reference_total.setToolTip("Calculado automaticamente pelos atendimentos selecionados")
        self.reference_total.setStyleSheet(
            f"background: #F4F1ED; color: {COLORS['muted']}; border: 1px dashed {COLORS['line']};"
        )
        self.professional_share_total = None
        if not is_receipt:
            self.professional_share_total = QLineEdit("0,00")
            self.professional_share_total.setReadOnly(True)
            self.professional_share_total.setFixedWidth(220)
            self.professional_share_total.setCursor(Qt.ArrowCursor)
            self.professional_share_total.setToolTip("70% do valor total dos atendimentos selecionados")
            self.professional_share_total.setStyleSheet(
                f"background: #F4F1ED; color: {COLORS['muted']}; border: 1px dashed {COLORS['line']};"
            )
        self.amount = QLineEdit("0,00")
        self.amount.setFixedWidth(140 if is_receipt else 200)
        editable_style = f"background: #FFFFFF; color: {COLORS['text']};"
        self.amount.setStyleSheet(editable_style)
        self.amount.setPlaceholderText("Valor efetivamente recebido" if is_receipt else "Valor efetivamente repassado")
        self.amount.textEdited.connect(self.mark_amount_overridden)

        self.payment_method = QComboBox()
        self.payment_method.setFixedWidth(130 if is_receipt else 200)
        self.payment_method.addItems(self.PAYMENT_METHODS)
        if transaction:
            self.amount.setText(self.money(transaction.get("amount")).replace("R$ ", ""))
            self.amount_overridden = True
            method_index = self.payment_method.findText(str(transaction.get("payment_method") or ""))
            if method_index >= 0:
                self.payment_method.setCurrentIndex(method_index)

        self.payout_percentage = None
        self.receipt_payout_amount = None
        self.receipt_is_repassed = None
        if is_receipt:
            self.payout_percentage = QLineEdit()
            self.payout_percentage.setValidator(QIntValidator(0, 100, self.payout_percentage))
            self.payout_percentage.setAlignment(Qt.AlignCenter)
            self.payout_percentage.setFixedWidth(90)
            self.payout_percentage.setMinimumHeight(48)
            self.payout_percentage.setText(str(int((transaction or {}).get("payout_percentage") or 70)))
            self.receipt_payout_amount = QLineEdit("0,00")
            self.receipt_payout_amount.setReadOnly(True)
            self.receipt_payout_amount.setFixedWidth(105)
            self.receipt_payout_amount.setStyleSheet(
                f"background: #F4F1ED; color: {COLORS['muted']}; border: 1px dashed {COLORS['line']};"
            )
            self.receipt_is_repassed = QComboBox()
            self.receipt_is_repassed.addItem("Não repassado", False)
            self.receipt_is_repassed.addItem("Repassado", True)
            self.receipt_is_repassed.setFixedWidth(145)
            self.receipt_is_repassed.setMinimumHeight(48)
            self.receipt_is_repassed.setCursor(Qt.PointingHandCursor)
            self.receipt_is_repassed.setCurrentIndex(1 if (transaction or {}).get("is_repassed", False) else 0)
            self.payout_percentage.textChanged.connect(self.update_receipt_payout_amount)
            self.amount.textChanged.connect(self.update_receipt_payout_amount)

        if is_receipt:
            field_rows = [[
                ("Data do recebimento", self.transaction_date),
                ("Valor total", self.reference_total),
                ("Valor cobrado / recebido", self.amount),
                ("Forma de pagamento", self.payment_method),
                ("% repasse", self.payout_percentage),
                ("Valor do repasse", self.receipt_payout_amount),
                ("Situação do repasse", self.receipt_is_repassed),
            ]]
        else:
            field_rows = [[
                ("Data do repasse", self.transaction_date),
                ("Valor total", self.reference_total),
                ("Parte do funcionário 70%", self.professional_share_total),
                ("Valor repassado", self.amount),
                ("Forma de pagamento", self.payment_method),
            ]]
        footer_columns = 8 if is_receipt else 5
        for block, field_row in enumerate(field_rows):
            label_row = block * 2
            for column, (label, widget) in enumerate(field_row):
                field_label = QLabel(label)
                field_label.setStyleSheet("background: transparent;")
                footer_layout.addWidget(field_label, label_row, column)
                footer_layout.addWidget(widget, label_row + 1, column)
        footer_layout.setColumnStretch(footer_columns - 1, 1)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        cancel = button("Cancelar", secondary=True)
        cancel.clicked.connect(self.reject)
        save = button(
            "Salvar alterações" if transaction else "Salvar operação" if is_receipt else "Salvar repasse"
        )
        save.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save)
        if is_receipt:
            footer_layout.addLayout(actions, 1, 7)
        else:
            footer_layout.addLayout(actions, 2, 0, 1, footer_columns)
        root.addWidget(footer)

        self.load_subjects()
        if self.edit_transaction:
            self.subject.setEnabled(False)
        self.subject.currentIndexChanged.connect(self.load_appointments)
        self.counterpart.currentIndexChanged.connect(self.load_appointments)
        self.load_appointments()
        self.update_receipt_payout_amount()

    def update_receipt_payout_amount(self) -> None:
        if self.transaction_type != "receipt" or self.receipt_payout_amount is None:
            return
        percentage = int(self.payout_percentage.text() or 0) if self.payout_percentage is not None else 0
        payout = round(self.input_amount(self.amount) * percentage / 100, 2)
        self.receipt_payout_amount.setText(self.money(payout).replace("R$ ", ""))

    def load_subjects(self) -> None:
        self.subject.clear()
        self.counterpart.clear()
        try:
            patients = self.repo.patients(active_only=False)
            professionals = self.repo.professionals(active_only=False)
        except Exception:
            patients = []
            professionals = []
        primary_rows = patients if self.transaction_type == "receipt" else professionals
        counterpart_rows = professionals if self.transaction_type == "receipt" else patients
        self.subject.addItem("Todos os pacientes" if self.transaction_type == "receipt" else "Todos os profissionais", None)
        self.counterpart.addItem("Todos os profissionais" if self.transaction_type == "receipt" else "Todos os pacientes", None)
        for row in primary_rows:
            self.subject.addItem(row.get("full_name", ""), row.get("id"))
        for row in counterpart_rows:
            self.counterpart.addItem(row.get("full_name", ""), row.get("id"))
        primary_id = self.initial_patient_id if self.transaction_type == "receipt" else self.initial_professional_id
        counterpart_id = self.initial_professional_id if self.transaction_type == "receipt" else self.initial_patient_id
        primary_index = self.subject.findData(primary_id)
        counterpart_index = self.counterpart.findData(counterpart_id)
        if primary_index >= 0:
            self.subject.setCurrentIndex(primary_index)
        if counterpart_index >= 0:
            self.counterpart.setCurrentIndex(counterpart_index)

    def load_appointments(self) -> None:
        subject_id = self.subject.currentData()
        counterpart_id = self.counterpart.currentData()
        start = self.period_start.date().toPython()
        end = self.period_end.date().toPython()
        if start > end:
            QMessageBox.warning(self, "Período inválido", "A data final deve ser maior ou igual à data inicial.")
            return
        try:
            if self.transaction_type == "receipt":
                self.rows = self.repo.patient_receipt_appointments(
                    subject_id, start, end, None, counterpart_id
                )
            else:
                self.rows = self.repo.professional_payout_appointments(
                    subject_id, start, end, None, counterpart_id
                )
            if self.situation == "open":
                flag = "_is_paid" if self.transaction_type == "receipt" else "_is_repassed"
                self.rows = [row for row in self.rows if not row.get(flag)]
            elif self.situation == "closed":
                flag = "_is_paid" if self.transaction_type == "receipt" else "_is_repassed"
                self.rows = [row for row in self.rows if row.get(flag)]
        except Exception as exc:
            show_error(self, exc)
            self.rows = []
        self.fill_table()

    def fill_table(self) -> None:
        self.updating_checks = True
        self.row_checks = []
        self.table.clearContents()
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            financial = self.repo.financial_row(row)
            cancelled = row.get("status") == "Cancelado"
            check = QCheckBox()
            check.setCursor(Qt.PointingHandCursor)
            check.setEnabled(appointment_is_selectable(row))
            check.setChecked(row.get("id") in self.initial_selected_ids if self.edit_transaction else True)
            check.stateChanged.connect(
                lambda _state=0, index=row_index: self.update_selected_total(index)
            )
            holder = QWidget()
            holder.setStyleSheet(f"background: {'#FFF1F2' if cancelled else COLORS['surface']};")
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignCenter)
            holder_layout.addWidget(check)
            self.table.setCellWidget(row_index, 0, holder)
            self.row_checks.append(check)

            appointment_date = row.get("appointment_date", "")
            try:
                appointment_date = date.fromisoformat(str(appointment_date)[:10]).strftime("%d/%m/%Y")
            except ValueError:
                pass
            if self.transaction_type == "receipt":
                values = [
                    appointment_date,
                    f"{row.get('start_time', '')[:5]} às {row.get('end_time', '')[:5]}",
                    (row.get("patients") or {}).get("full_name", ""),
                    (row.get("professionals") or {}).get("full_name", ""),
                    row.get("status", "Não informado"),
                    self.money(financial.get("consultation_fee")),
                    financial.get("payment_method") or "—",
                    "Pago" if row.get("_is_paid") else "Em aberto",
                ]
            else:
                values = [
                    appointment_date,
                    f"{row.get('start_time', '')[:5]} às {row.get('end_time', '')[:5]}",
                    (row.get("professionals") or {}).get("full_name", ""),
                    (row.get("patients") or {}).get("full_name", ""),
                    row.get("status", "Não informado"),
                    self.money(row.get("_gross_amount")),
                    self.money(row.get("_payout_amount")),
                    "Repassado" if row.get("_is_repassed") else "Em aberto",
                ]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter)
                item.setData(FINANCIAL_ROW_SELECTED_ROLE, True)
                if cancelled and column == 5:
                    item.setForeground(QColor(COLORS["red"]))
                self.table.setItem(row_index, column, item)
            self.table.setRowHeight(row_index, 44)
        widths = [75, 105, 115, 180, 180, 125, 125, 125, 120]
        for column in range(self.table.columnCount()):
            self.table.setColumnWidth(column, widths[column])
        self.updating_checks = False
        self.result_label.setText(f"{len(self.rows)} atendimento(s)")
        self.update_selected_total()

    def selected_indexes(self) -> list[int]:
        return [index for index, check in enumerate(self.row_checks) if check.isChecked()]

    def row_reference_amount(self, row_index: int) -> float:
        row = self.rows[row_index]
        if self.transaction_type == "receipt":
            return round(float(self.repo.financial_row(row).get("consultation_fee") or 0), 2)
        return round(float(row.get("_payout_amount") or 0), 2)

    def row_gross_amount(self, row_index: int) -> float:
        row = self.rows[row_index]
        if self.transaction_type == "receipt":
            return self.row_reference_amount(row_index)
        return round(float(row.get("_gross_amount") or 0), 2)

    def selected_total(self) -> float:
        return round(self.selected_total_value, 2)

    def update_selected_total(self, changed_row: int | None = None) -> None:
        if self.updating_checks:
            return
        if changed_row is None:
            self.selected_total_value = round(
                sum(
                    self.row_reference_amount(index)
                    for index, check in enumerate(self.row_checks)
                    if check.isChecked()
                ),
                2,
            )
            self.selected_gross_total_value = round(
                sum(
                    self.row_gross_amount(index)
                    for index, check in enumerate(self.row_checks)
                    if check.isChecked()
                ),
                2,
            )
            rows_to_update = range(len(self.row_checks))
        else:
            check = self.row_checks[changed_row]
            delta = self.row_reference_amount(changed_row)
            gross_delta = self.row_gross_amount(changed_row)
            self.selected_total_value = round(
                self.selected_total_value + (delta if check.isChecked() else -delta),
                2,
            )
            self.selected_gross_total_value = round(
                self.selected_gross_total_value + (gross_delta if check.isChecked() else -gross_delta),
                2,
            )
            rows_to_update = (changed_row,)
        for row_index in rows_to_update:
            check = self.row_checks[row_index]
            checked = check.isChecked()
            holder = self.table.cellWidget(row_index, 0)
            cancelled = self.rows[row_index].get("status") == "Cancelado"
            background = COLORS["primary_soft"] if checked else "#FFF1F2" if cancelled else COLORS["surface"]
            if holder:
                holder.setStyleSheet(f"background: {background};")
            for column in range(1, self.table.columnCount()):
                item = self.table.item(row_index, column)
                if item:
                    item.setData(FINANCIAL_ROW_SELECTED_ROLE, checked)
        total = self.selected_total_value
        displayed_total = self.selected_gross_total_value if self.transaction_type == "payout" else total
        self.reference_total.setText(self.money(displayed_total).replace("R$ ", ""))
        if self.professional_share_total is not None:
            self.professional_share_total.setText(self.money(total).replace("R$ ", ""))
        self.update_suggested_amount()

    def input_amount(self, field: QLineEdit) -> float:
        text = field.text().strip().replace("R$", "").replace(" ", "")
        if not text:
            return 0.0
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        try:
            return max(float(text), 0.0)
        except ValueError:
            return 0.0

    def mark_amount_overridden(self) -> None:
        self.amount_overridden = True

    def update_suggested_amount(self) -> None:
        if self.amount_overridden:
            return
        suggestion = self.selected_total()
        self.amount.setText(f"{suggestion:.2f}".replace(".", ","))

    def selected_items(self) -> list[dict[str, Any]]:
        return [
            {
                "appointment_id": self.rows[index]["id"],
                "patient_id": self.rows[index].get("patient_id"),
                "professional_id": self.rows[index].get("professional_id"),
            }
            for index in self.selected_indexes()
        ]

    def values(self) -> dict[str, Any]:
        values = {
            "payment_method": self.payment_method.currentText() or "Nao informado",
            "amount": self.amount.text().strip(),
            "discount": "0,00",
            "surcharge": "0,00",
            "notes": "",
        }
        if self.transaction_type == "receipt":
            values.update({
                "patient_id": self.subject.currentData(),
                "professional_id": self.counterpart.currentData(),
                "payment_date": self.transaction_date.date().toPython().isoformat(),
                "payout_percentage": int(self.payout_percentage.text() or 0),
                "payout_amount": self.receipt_payout_amount.text(),
                "is_repassed": bool(self.receipt_is_repassed.currentData()),
            })
        else:
            values.update({"professional_id": self.subject.currentData(), "payout_date": self.transaction_date.date().toPython().isoformat()})
        return values

    def accept(self) -> None:
        if self.transaction_type == "receipt":
            patient_id = self.subject.currentData()
            professional_id = self.counterpart.currentData()
            if not patient_id or not professional_id:
                QMessageBox.warning(self, "Recebimento", "Selecione um paciente e um profissional.")
                return
            selected = self.selected_items()
            if any(item.get("patient_id") != patient_id or item.get("professional_id") != professional_id for item in selected):
                QMessageBox.warning(
                    self, "Recebimento",
                    "Todos os atendimentos devem pertencer ao paciente e ao profissional selecionados.",
                )
                return
        super().accept()


class PatientReceiptDialog(FinancialTransactionDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, money_formatter, **filters):
        super().__init__(parent, repo, money_formatter, "receipt", **filters)


class ProfessionalPayoutDialog(FinancialTransactionDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, money_formatter, **filters):
        super().__init__(parent, repo, money_formatter, "payout", **filters)


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
        self.search.setAccessibleName("Buscar documento")
        self.search.setMinimumWidth(360)
        self.search.textChanged.connect(lambda _text="": self.refresh())
        self.professional_filter = QComboBox()
        self.professional_filter.setAccessibleName("Filtrar documentos por profissional")
        self.professional_filter.setMinimumWidth(260)
        self.professional_filter.currentIndexChanged.connect(lambda _index=0: self.refresh())
        upload_btn = button("Enviar documento")
        upload_btn.setAccessibleName("Enviar novo documento")
        upload_btn.clicked.connect(self.upload_document)
        download_btn = button("Baixar", secondary=True)
        download_btn.setAccessibleName("Baixar documento selecionado")
        download_btn.clicked.connect(self.download_selected)
        delete_btn = button("Excluir documento", danger=True)
        delete_btn.setAccessibleName("Excluir documento selecionado")
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
        self.table.setAccessibleName("Lista de documentos")
        self.table.setHorizontalHeaderLabels(["Documento", "Profissional", "Tipo", "Tamanho", "Enviado em", "Descrição"])
        full_row_table_polish(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemActivated.connect(lambda _item: self.download_selected())
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
        self.setMinimumSize(680, 480)
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
        attendance_tab = QScrollArea()
        attendance_tab.setWidgetResizable(True)
        attendance_tab.setFrameShape(QFrame.NoFrame)
        attendance_tab.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        attendance_content = QWidget()
        attendance_layout = QVBoxLayout(attendance_content)
        attendance_layout.setContentsMargins(12, 12, 12, 12)
        attendance_layout.setSpacing(12)
        attendance_tab.setWidget(attendance_content)
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
        self.agenda_periods = AgendaPeriodsEditor()
        self.agenda_timing = AgendaTimingEditor()
        self.agenda_slot_minutes = self.agenda_timing.duration
        self.agenda_step_minutes = self.agenda_timing.step
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
        schedule_card = card()
        schedule_layout = QGridLayout(schedule_card)
        schedule_layout.setContentsMargins(16, 14, 16, 16)
        schedule_layout.setHorizontalSpacing(18)
        schedule_layout.setVerticalSpacing(10)
        schedule_title = QLabel("Configuração da agenda")
        schedule_title.setObjectName("SectionTitle")
        schedule_hint = QLabel(
            "Estes períodos geram sugestões de horários livres. Atendimentos manuais podem ser criados em qualquer horário."
        )
        schedule_hint.setObjectName("Muted")
        schedule_hint.setWordWrap(True)
        schedule_layout.addWidget(schedule_title, 0, 0, 1, 2)
        schedule_layout.addWidget(schedule_hint, 1, 0, 1, 2)
        self.add_field(schedule_layout, "Períodos da grade automática", self.agenda_periods, 2, 0, 2)
        self.add_field(schedule_layout, "Duração do atendimento", self.agenda_timing, 3, 0, 2)

        history_card = card()
        history_layout = QVBoxLayout(history_card)
        history_layout.setContentsMargins(16, 14, 16, 16)
        history_layout.setSpacing(10)
        history_title = QLabel("Histórico de atendimentos")
        history_title.setObjectName("SectionTitle")
        self.history_table.setMinimumHeight(210)
        history_layout.addWidget(history_title)
        history_layout.addWidget(self.history_table)
        attendance_layout.addWidget(schedule_card)
        attendance_layout.addWidget(history_card)
        attendance_layout.addStretch()

        tabs.addTab(personal_tab, "Dados pessoais")
        tabs.addTab(address_tab, "Endereço")
        tabs.addTab(attendance_tab, "Agenda")
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(save)
        root.addWidget(tabs, 1)
        root.addLayout(actions)
        self.sync_attendance_fields()

    def add_field(
        self, layout: QGridLayout, label: str, widget: QWidget, row: int, col: int, col_span: int = 1
    ) -> None:
        box = QVBoxLayout()
        box.setSpacing(6)
        box.addWidget(QLabel(label))
        box.addWidget(widget)
        layout.addLayout(box, row, col, 1, col_span)

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
        self.agenda_periods.setEnabled(enabled)
        self.agenda_timing.setEnabled(enabled)
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
            "agenda_periods": self.agenda_periods.periods(),
            "agenda_slot_minutes": self.agenda_slot_minutes.currentData(),
            "agenda_step_minutes": self.agenda_step_minutes.currentData(),
        }


class ProfilePermissionsDialog(QDialog):
    def __init__(self, parent: QWidget, repo: SupabaseRepository, profile: dict[str, Any]):
        super().__init__(parent)
        self.repo = repo
        self.profile = profile
        try:
            self.professional = repo.professional_for_profile(profile["id"]) or {}
        except Exception:
            self.professional = {}
        self.setWindowTitle("Funcionário do sistema")
        self.setMinimumSize(680, 480)
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
        attendance_tab = QScrollArea()
        attendance_tab.setWidgetResizable(True)
        attendance_tab.setFrameShape(QFrame.NoFrame)
        attendance_tab.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        attendance_content = QWidget()
        attendance_layout = QVBoxLayout(attendance_content)
        attendance_layout.setContentsMargins(12, 12, 12, 12)
        attendance_layout.setSpacing(12)
        attendance_tab.setWidget(attendance_content)
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
        self.agenda_periods = AgendaPeriodsEditor(professional_agenda_periods(self.professional))
        self.agenda_timing = AgendaTimingEditor(
            self.professional.get("agenda_slot_minutes"),
            self.professional.get("agenda_step_minutes"),
        )
        self.agenda_slot_minutes = self.agenda_timing.duration
        self.agenda_step_minutes = self.agenda_timing.step
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
        schedule_card = card()
        schedule_layout = QGridLayout(schedule_card)
        schedule_layout.setContentsMargins(16, 14, 16, 16)
        schedule_layout.setHorizontalSpacing(18)
        schedule_layout.setVerticalSpacing(10)
        schedule_title = QLabel("Configuração da agenda")
        schedule_title.setObjectName("SectionTitle")
        schedule_hint = QLabel(
            "Estes períodos geram sugestões de horários livres. Atendimentos manuais podem ser criados em qualquer horário."
        )
        schedule_hint.setObjectName("Muted")
        schedule_hint.setWordWrap(True)
        schedule_layout.addWidget(schedule_title, 0, 0, 1, 2)
        schedule_layout.addWidget(schedule_hint, 1, 0, 1, 2)
        self.add_field(schedule_layout, "Períodos da grade automática", self.agenda_periods, 2, 0, 2)
        self.add_field(schedule_layout, "Duração do atendimento", self.agenda_timing, 3, 0, 2)

        history_card = card()
        history_layout = QVBoxLayout(history_card)
        history_layout.setContentsMargins(16, 14, 16, 16)
        history_layout.setSpacing(10)
        history_title = QLabel("Histórico de atendimentos")
        history_title.setObjectName("SectionTitle")
        self.history_table.setMinimumHeight(240)
        history_layout.addWidget(history_title)
        history_layout.addWidget(self.history_table)
        attendance_layout.addWidget(schedule_card)
        attendance_layout.addWidget(history_card)
        attendance_layout.addStretch()

        tabs.addTab(personal_tab, "Dados pessoais")
        tabs.addTab(address_tab, "Endereço")
        tabs.addTab(attendance_tab, "Agenda")
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(save)
        root.addWidget(tabs, 1)
        root.addLayout(actions)
        self.sync_attendance_fields()

    def add_field(
        self, layout: QGridLayout, label: str, widget: QWidget, row: int, col: int, col_span: int = 1
    ) -> None:
        box = QVBoxLayout()
        box.setSpacing(6)
        box.addWidget(QLabel(label))
        box.addWidget(widget)
        layout.addLayout(box, row, col, 1, col_span)

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
        self.agenda_periods.setEnabled(enabled)
        self.agenda_timing.setEnabled(enabled)
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
            "agenda_periods": self.agenda_periods.periods(),
            "agenda_slot_minutes": self.agenda_slot_minutes.currentData(),
            "agenda_step_minutes": self.agenda_step_minutes.currentData(),
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
