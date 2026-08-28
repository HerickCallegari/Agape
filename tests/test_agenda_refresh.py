import os
import threading
import time
import unittest
from datetime import date
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from agape_app.app import (
    AgendaPage,
    AppointmentDialog,
    AppointmentEditDialog,
    BulkAppointmentEditDialog,
    friendly_error_message,
)
from agape_app.repository import SupabaseRepository


class FakeAgendaRepository:
    def __init__(self):
        self.rows = []
        self.appointment_calls = 0
        self.patient_calls = 0
        self.gate = None
        self.save_gate = None
        self.error = None

    def professionals(self, active_only=True):
        return [{"id": "professional-1", "full_name": "Profissional", "profile_id": "profile-1"}]

    def appointments(self, selected_date, professional_id=None):
        self.appointment_calls += 1
        if self.gate is not None:
            self.gate.wait(2)
        if self.error is not None:
            raise self.error
        return list(self.rows)

    def patients(self, active_only=True):
        self.patient_calls += 1
        if self.gate is not None:
            self.gate.wait(2)
        return [{"id": "patient-1", "full_name": "Paciente"}]

    def financial_appointments(self, *args, **kwargs):
        self.financial_calls = getattr(self, "financial_calls", 0) + 1
        if self.gate is not None:
            self.gate.wait(2)
        if self.error is not None:
            raise self.error
        return list(self.rows)

    def financial_row(self, row):
        return row.get("appointment_financials") or {}

    def delete_appointments_bulk(self, appointment_ids):
        self.delete_calls = getattr(self, "delete_calls", 0) + 1
        if self.gate is not None:
            self.gate.wait(2)
        if self.error is not None:
            raise self.error
        self.rows = [row for row in self.rows if row.get("id") not in set(appointment_ids)]
        return len(appointment_ids)

    def save_appointment(self, values, record_id=None):
        self.save_calls = getattr(self, "save_calls", 0) + 1
        if self.save_gate is not None:
            self.save_gate.wait(2)
        if self.error is not None:
            raise self.error

    def appointment_financials(self, appointment_id):
        if self.gate is not None:
            self.gate.wait(2)
        return None

    def parse_consultation_fee(self, value):
        return 0

    def delete_appointment(self, appointment_id):
        self.individual_delete_calls = getattr(self, "individual_delete_calls", 0) + 1
        if self.gate is not None:
            self.gate.wait(2)
        if self.error is not None:
            raise self.error


def appointment(identifier: str, start_time: str) -> dict:
    return {
        "id": identifier,
        "patient_id": f"patient-{identifier}",
        "professional_id": "professional-1",
        "appointment_date": date.today().isoformat(),
        "start_time": start_time,
        "end_time": "12:00:00",
        "status": "Agendado",
        "patients": {"full_name": f"Paciente {identifier}"},
        "professionals": {"full_name": "Profissional", "profile_id": "profile-1"},
    }


class AgendaRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.repo = FakeAgendaRepository()
        self.page = AgendaPage(
            self.repo,
            {"id": "profile-1", "role": "admin", "permissions": ["manage_agenda", "change_status"]},
        )
        self.page.admin_mode_enabled = True

    def tearDown(self):
        self.page.deleteLater()
        self.app.processEvents()

    def wait_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_new_appointment_is_inserted_without_reusing_owned_widget(self):
        self.repo.rows = [appointment("first", "08:00:00"), appointment("last", "10:00:00")]
        self.page.refresh()
        self.assertTrue(self.wait_until(lambda: not self.page.is_busy))
        original_widgets = {
            self.page.list.item(index).data(256)["id"]: self.page.list.itemWidget(self.page.list.item(index))
            for index in range(self.page.list.count())
        }

        self.repo.rows.insert(1, appointment("middle", "09:00:00"))
        self.page.refresh()
        self.assertTrue(self.wait_until(lambda: not self.page.is_busy))

        ids = [self.page.list.item(index).data(256)["id"] for index in range(self.page.list.count())]
        self.assertEqual(ids, ["first", "middle", "last"])
        self.assertIs(self.page.list.itemWidget(self.page.list.item(0)), original_widgets["first"])
        self.assertIs(self.page.list.itemWidget(self.page.list.item(2)), original_widgets["last"])

    def test_first_appointment_replaces_empty_card(self):
        self.page.refresh(show_popup=False)
        self.assertTrue(self.wait_until(lambda: not self.page.is_busy))
        self.assertEqual(self.page.list.count(), 1)
        self.assertIsNone(self.page.list.item(0).data(256))

        self.repo.rows = [appointment("first", "08:00:00")]
        self.page.refresh(show_popup=False)
        self.assertTrue(self.wait_until(lambda: not self.page.is_busy))
        self.assertEqual(self.page.list.item(0).data(256)["id"], "first")

    def test_refresh_is_single_flight_and_runs_one_pending_request(self):
        self.repo.gate = threading.Event()
        self.page.refresh(show_popup=False)
        self.assertTrue(self.wait_until(lambda: self.repo.appointment_calls == 1))

        self.page.refresh(show_popup=False)
        self.assertEqual(self.repo.appointment_calls, 1)
        self.assertTrue(self.page._refresh_pending)

        self.repo.gate.set()
        self.assertTrue(self.wait_until(lambda: self.repo.appointment_calls == 2 and not self.page.is_busy))
        self.assertFalse(self.page._refresh_pending)

    def test_slow_refresh_keeps_qt_event_loop_responsive(self):
        self.repo.gate = threading.Event()
        timer_fired = []
        from PySide6.QtCore import QTimer

        self.page.refresh(show_popup=False)
        QTimer.singleShot(20, lambda: timer_fired.append(True))
        self.assertTrue(self.wait_until(lambda: bool(timer_fired), timeout=1))
        self.assertTrue(self.page.is_busy)
        self.repo.gate.set()
        self.assertTrue(self.wait_until(lambda: not self.page.is_busy))

    def test_timeout_returns_refresh_to_idle(self):
        self.repo.error = httpx.ConnectTimeout("slow server")
        self.page.refresh(show_popup=False)
        self.assertTrue(self.wait_until(lambda: not self.page.is_busy))
        self.assertIsNone(self.page._refresh_task_id)
        self.assertFalse(self.page.loading_label.isVisible())
        self.assertIn("demorou para responder", friendly_error_message(self.repo.error))

    def test_double_create_request_starts_only_one_patient_load(self):
        self.repo.gate = threading.Event()
        with patch.object(AppointmentDialog, "exec", return_value=0):
            self.page.create_appointment()
            self.page.create_appointment()
            self.assertTrue(self.wait_until(lambda: self.repo.patient_calls == 1))
            self.assertEqual(self.repo.patient_calls, 1)
            self.repo.gate.set()
            self.assertTrue(self.wait_until(lambda: not self.page.is_busy))

    def test_double_create_during_save_sends_only_one_insert(self):
        self.repo.save_gate = threading.Event()
        with (
            patch.object(AppointmentDialog, "exec", return_value=1),
            patch.object(AppointmentDialog, "values", return_value={
                "patient_id": "patient-1",
                "professional_id": "professional-1",
                "appointment_date": date.today().isoformat(),
                "start_time": "08:00:00",
                "end_time": "09:00:00",
                "status": "Agendado",
                "consultation_fee": "",
            }),
        ):
            self.page.create_appointment()
            self.assertTrue(self.wait_until(lambda: getattr(self.repo, "save_calls", 0) == 1))
            self.page.create_appointment()
            self.assertEqual(self.repo.save_calls, 1)
            self.repo.save_gate.set()
            self.assertTrue(self.wait_until(lambda: not self.page.is_busy))

    def test_completed_refreshes_release_worker_references(self):
        for _index in range(20):
            self.page.refresh(show_popup=False)
            self.assertTrue(self.wait_until(lambda: not self.page.is_busy))
        self.assertEqual(getattr(self.page, "_background_workers", {}), {})


class BulkAppointmentAsyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def setUp(self):
        self.repo = FakeAgendaRepository()
        self.repo.rows = [appointment("one", "08:00:00")]
        self.parent = QWidget()
        self.dialog = BulkAppointmentEditDialog(self.parent, self.repo)
        self.assertTrue(self.wait_until(lambda: not self.dialog.is_busy and self.dialog.table.rowCount() == 1))

    def tearDown(self):
        self.dialog.deleteLater()
        self.parent.deleteLater()
        self.app.processEvents()

    def test_double_delete_dispatches_only_one_request_and_updates_locally(self):
        self.dialog.appointment_checks[0].setChecked(True)
        self.repo.gate = threading.Event()
        with (
            patch.object(QMessageBox, "question", return_value=QMessageBox.Yes),
            patch.object(QMessageBox, "information"),
        ):
            self.dialog.delete_selected()
            self.dialog.delete_selected()
            self.assertTrue(self.wait_until(lambda: getattr(self.repo, "delete_calls", 0) == 1))
            self.assertEqual(self.repo.delete_calls, 1)
            self.repo.gate.set()
            self.assertTrue(self.wait_until(lambda: not self.dialog.is_busy))
        self.assertEqual(self.dialog.table.rowCount(), 0)
        self.assertTrue(self.dialog.delete_button.isEnabled())

    def test_empty_bulk_selection_does_not_send_delete(self):
        with patch.object(QMessageBox, "warning") as warning:
            self.dialog.delete_selected()
        warning.assert_called_once()
        self.assertEqual(getattr(self.repo, "delete_calls", 0), 0)

    def test_duplicate_search_while_loading_is_ignored(self):
        self.repo.gate = threading.Event()
        calls_before = self.repo.financial_calls
        self.dialog.load_rows()
        self.dialog.load_rows()
        self.assertTrue(self.wait_until(lambda: self.repo.financial_calls == calls_before + 1))
        self.repo.gate.set()
        self.assertTrue(self.wait_until(lambda: not self.dialog.is_busy))
        self.assertEqual(self.repo.financial_calls, calls_before + 1)

    def test_timeout_restores_bulk_dialog_controls(self):
        self.dialog.appointment_checks[0].setChecked(True)
        self.repo.error = httpx.ReadTimeout("slow read")
        with (
            patch.object(QMessageBox, "question", return_value=QMessageBox.Yes),
            patch("agape_app.app.show_error") as error_popup,
        ):
            self.dialog.delete_selected()
            self.assertTrue(self.wait_until(lambda: not self.dialog.is_busy))
        error_popup.assert_called_once()
        self.assertTrue(self.dialog.delete_button.isEnabled())
        self.assertEqual(self.dialog.table.rowCount(), 1)

    def test_closing_dialog_during_slow_load_does_not_invoke_deleted_widgets(self):
        slow_repo = FakeAgendaRepository()
        slow_repo.gate = threading.Event()
        dialog = BulkAppointmentEditDialog(self.parent, slow_repo)
        self.app.processEvents()
        dialog.close()
        dialog.deleteLater()
        self.app.processEvents()
        slow_repo.gate.set()
        self.assertTrue(self.wait_until(lambda: not QThreadPool.globalInstance().activeThreadCount()))


class IndividualAppointmentAsyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_double_individual_delete_sends_only_one_request(self):
        repo = FakeAgendaRepository()
        parent = QWidget()
        dialog = AppointmentEditDialog(
            parent,
            repo,
            {"id": "profile-1", "role": "admin"},
            appointment("one", "08:00:00"),
        )
        self.assertTrue(self.wait_until(lambda: not dialog._operation_in_progress))
        repo.gate = threading.Event()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            dialog.delete_appointment()
            dialog.delete_appointment()
            self.assertTrue(self.wait_until(lambda: getattr(repo, "individual_delete_calls", 0) == 1))
            self.assertEqual(repo.individual_delete_calls, 1)
            repo.gate.set()
            self.assertTrue(self.wait_until(lambda: not dialog._operation_in_progress))
        dialog.deleteLater()
        parent.deleteLater()
        self.app.processEvents()


class RepositoryTimeoutTests(unittest.TestCase):
    def test_postgrest_timeout_is_explicit_and_bounded(self):
        timeout = SupabaseRepository.POSTGREST_TIMEOUT
        self.assertEqual(timeout.connect, 8.0)
        self.assertEqual(timeout.read, 20.0)
        self.assertEqual(timeout.write, 20.0)
        self.assertEqual(timeout.pool, 5.0)


if __name__ == "__main__":
    unittest.main()
