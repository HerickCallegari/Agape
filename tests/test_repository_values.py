import sys
import types
import unittest


if "supabase" not in sys.modules:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.Client = object
    supabase_stub.create_client = lambda *_args, **_kwargs: None
    sys.modules["supabase"] = supabase_stub

if "httpx" not in sys.modules:
    sys.modules["httpx"] = types.ModuleType("httpx")

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv_stub

from agape_app.repository import AppError, SupabaseRepository


class RepositoryValueTests(unittest.TestCase):
    def setUp(self):
        self.repo = object.__new__(SupabaseRepository)

    def test_consultation_fee_plain_integer_text(self):
        self.assertEqual(self.repo.parse_consultation_fee("50"), 50.0)

    def test_consultation_fee_numeric_is_not_multiplied(self):
        self.assertEqual(self.repo.parse_consultation_fee(50.0), 50.0)

    def test_consultation_fee_brazilian_decimal(self):
        self.assertEqual(self.repo.parse_consultation_fee("R$ 1.250,50"), 1250.50)

    def test_consultation_fee_dot_decimal(self):
        self.assertEqual(self.repo.parse_consultation_fee("50.75"), 50.75)

    def test_blank_consultation_fee_means_no_value(self):
        self.assertIsNone(self.repo.parse_consultation_fee(""))
        self.assertIsNone(self.repo.parse_consultation_fee(None))

    def test_negative_consultation_fee_is_rejected(self):
        with self.assertRaises(AppError):
            self.repo.parse_consultation_fee("-1")

    def test_payment_money_formats(self):
        self.assertEqual(self.repo.parse_money("50"), 50.0)
        self.assertEqual(self.repo.parse_money(50.0), 50.0)
        self.assertEqual(self.repo.parse_money("1.250,50"), 1250.50)
        self.assertEqual(self.repo.parse_money("50.75"), 50.75)

    def test_cancelled_and_justified_absence_are_not_chargeable(self):
        for status in ("Cancelado", "Falta com aviso"):
            row = {"status": status, "appointment_financials": {"consultation_fee": 100}}
            self.assertFalse(self.repo.appointment_is_chargeable(row))

    def test_other_appointment_statuses_are_chargeable(self):
        for status in ("Agendado", "Confirmado", "Atendido", "Falta sem aviso"):
            row = {"status": status, "appointment_financials": {"consultation_fee": 100}}
            self.assertTrue(self.repo.appointment_is_chargeable(row))

    def test_financial_courtesy_and_cancellation_are_not_chargeable(self):
        for financial_status in ("Cortesia", "Cancelado"):
            row = {
                "status": "Atendido",
                "appointment_financials": {"consultation_fee": 100, "payment_status": financial_status},
            }
            self.assertFalse(self.repo.appointment_is_chargeable(row))

    def test_professional_share_is_rounded_to_cents(self):
        self.assertEqual(self.repo.professional_share(99.99), 69.99)


class FakeResult:
    data = []


class FakeQuery:
    def __init__(self, updates):
        self.updates = updates
        self.payload = None

    def select(self, *_args): return self
    def eq(self, *_args): return self
    def neq(self, *_args): return self
    def lt(self, *_args): return self
    def gt(self, *_args): return self
    def update(self, payload):
        self.payload = payload
        return self
    def execute(self):
        if self.payload is not None:
            self.updates.append(self.payload)
        return FakeResult()


class FakeClient:
    def __init__(self):
        self.updates = []

    def table(self, _name):
        return FakeQuery(self.updates)


class BulkUpdateTests(unittest.TestCase):
    def setUp(self):
        self.repo = object.__new__(SupabaseRepository)
        self.repo.client = FakeClient()
        self.saved_financials = []
        self.repo.appointment_financials = lambda _appointment_id: None
        self.repo.save_appointment_financials = (
            lambda appointment_id, values: self.saved_financials.append((appointment_id, values))
        )
        self.row = {
            "id": "appointment-1", "professional_id": "professional-1", "patient_id": "patient-1",
            "appointment_date": "2026-08-03", "start_time": "08:00:00",
            "end_time": "09:00:00", "status": "Agendado", "patients": {"full_name": "Paciente"},
        }

    def test_value_only_does_not_write_schedule_or_status(self):
        self.repo.update_appointments_bulk([self.row], consultation_fee="50")
        self.assertEqual(self.repo.client.updates, [])
        self.assertEqual(self.saved_financials[0][1]["consultation_fee"], 50.0)

    def test_status_only_writes_only_status(self):
        self.repo.update_appointments_bulk([self.row], status="Confirmado")
        self.assertEqual(self.repo.client.updates, [{"status": "Confirmado"}])
        self.assertEqual(self.saved_financials, [])

    def test_time_only_writes_only_start_and_end(self):
        self.repo.update_appointments_bulk([self.row], start_time="10:00:00", end_time="11:00:00")
        self.assertEqual(
            self.repo.client.updates,
            [{"start_time": "10:00:00", "end_time": "11:00:00"}],
        )
        self.assertEqual(self.saved_financials, [])

    def test_professional_and_patient_can_be_reassigned(self):
        self.repo.update_appointments_bulk(
            [self.row], professional_id="professional-2", patient_id="patient-2"
        )
        self.assertEqual(
            self.repo.client.updates,
            [{"professional_id": "professional-2", "patient_id": "patient-2"}],
        )
        self.assertEqual(self.saved_financials, [])

    def test_no_fields_is_rejected(self):
        with self.assertRaises(AppError):
            self.repo.update_appointments_bulk([self.row])

    def test_incomplete_or_reversed_time_is_rejected(self):
        with self.assertRaises(AppError):
            self.repo.update_appointments_bulk([self.row], start_time="10:00:00")
        with self.assertRaises(AppError):
            self.repo.update_appointments_bulk(
                [self.row], start_time="11:00:00", end_time="10:00:00"
            )


class AppointmentFinancialTests(unittest.TestCase):
    def setUp(self):
        self.repo = object.__new__(SupabaseRepository)
        self.repo.client = FakeClient()

    def test_null_text_fields_are_saved_as_empty_strings(self):
        self.repo.appointment_financials = lambda _appointment_id: {
            "id": "financial-1",
            "payment_method": None,
            "financial_notes": None,
        }

        self.repo.save_appointment_financials(
            "appointment-1",
            {
                "consultation_fee": "140,00",
                "payment_method": None,
                "financial_notes": None,
            },
        )

        payload = self.repo.client.updates[-1]
        self.assertEqual(payload["payment_method"], "")
        self.assertEqual(payload["financial_notes"], "")


if __name__ == "__main__":
    unittest.main()
