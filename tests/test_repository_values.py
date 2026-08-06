import sys
import types
import unittest
from datetime import date
from pathlib import Path


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
from agape_app.financial_rules import appointment_is_selectable, current_month_period, default_selected_indexes, selected_reference_total, suggested_transaction_amount


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
    def gte(self, *_args): return self
    def lte(self, *_args): return self
    def order(self, *_args, **_kwargs): return self
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
        self.rpc_calls = []

    def table(self, _name):
        return FakeQuery(self.updates)

    def rpc(self, name, payload):
        self.rpc_calls.append((name, payload))
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


class FinancialTransactionRuleTests(unittest.TestCase):
    def test_current_month_period_handles_leap_year(self):
        self.assertEqual(
            current_month_period(date(2028, 2, 15)),
            (date(2028, 2, 1), date(2028, 2, 29)),
        )

    def test_reference_total_does_not_depend_on_appointment_status(self):
        rows = [
            {"status": "Atendido", "reference": 100},
            {"status": "Cancelado", "reference": 150},
        ]
        self.assertEqual(selected_reference_total(rows, [0, 1], "reference"), 250.0)
        self.assertTrue(appointment_is_selectable(rows[1]))

    def test_reference_total_accepts_rows_from_multiple_months(self):
        rows = [
            {"appointment_date": "2026-07-10", "reference": 80},
            {"appointment_date": "2026-09-10", "reference": 120},
        ]
        self.assertEqual(selected_reference_total(rows, [0, 1], "reference"), 200.0)

    def test_search_results_start_with_every_row_selected(self):
        rows = [{"id": "one"}, {"id": "two"}, {"id": "three"}]
        self.assertEqual(default_selected_indexes(rows), [0, 1, 2])

    def test_suggested_amount_applies_discount_and_surcharge(self):
        self.assertEqual(suggested_transaction_amount(300, 25, 10), 285.0)


class AtomicFinancialTransactionTests(unittest.TestCase):
    def setUp(self):
        self.repo = object.__new__(SupabaseRepository)
        self.repo.profile = {"id": "profile-1"}
        self.repo.client = FakeClient()

    def test_patient_payment_is_sent_as_one_atomic_rpc_with_history_fields(self):
        rows = [
            {"id": "appointment-july", "appointment_financials": {"consultation_fee": 100}},
            {"id": "appointment-august", "appointment_financials": {"consultation_fee": 150}},
        ]
        self.repo.patient_payment_balance_appointments = lambda **_kwargs: rows

        self.repo.save_patient_payment(
            {
                "patient_id": "patient-1", "payment_date": "2026-08-06",
                "amount": "230,00", "discount": "30,00", "surcharge": "10,00",
                "payment_method": "PIX", "notes": "Meses diferentes",
            },
            [{"appointment_id": "appointment-july"}, {"appointment_id": "appointment-august"}],
        )

        self.assertEqual(len(self.repo.client.rpc_calls), 1)
        name, payload = self.repo.client.rpc_calls[0]
        self.assertEqual(name, "register_patient_payment")
        self.assertEqual(payload["p_reference_amount"], 250.0)
        self.assertEqual(payload["p_discount_amount"], 30.0)
        self.assertEqual(payload["p_surcharge_amount"], 10.0)
        self.assertEqual(payload["p_appointment_ids"], ["appointment-july", "appointment-august"])
        self.assertNotIn("p_item_amounts", payload)

    def test_patient_and_professional_filters_can_be_combined(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return []

        self.repo.patient_payment_balance_appointments = capture
        self.repo.patient_receipt_appointments(
            "patient-1", date(2026, 8, 1), date(2026, 8, 31), None, "professional-1"
        )
        self.assertEqual(captured["patient_id"], "patient-1")
        self.assertEqual(captured["professional_id"], "professional-1")

    def test_cancelled_appointment_can_be_included_in_payout_rpc(self):
        rows = [{"id": "cancelled", "status": "Cancelado", "_payout_amount": 70.0}]
        self.repo.professional_payout_appointments = lambda *_args, **_kwargs: rows

        self.repo.save_professional_payout(
            {
                "professional_id": "professional-1", "payout_date": "2026-08-06",
                "amount": "70,00", "discount": "0", "surcharge": "0",
                "payment_method": "PIX", "notes": "",
            },
            [{"appointment_id": "cancelled"}],
        )

        self.assertEqual(len(self.repo.client.rpc_calls), 1)
        self.assertEqual(self.repo.client.rpc_calls[0][0], "register_professional_payout")
        self.assertEqual(self.repo.client.rpc_calls[0][1]["p_appointment_ids"], ["cancelled"])

    def test_new_payment_items_are_inserted_without_individual_amount(self):
        migration = (
            Path(__file__).resolve().parents[1]
            / "supabase_migration_20260806_financial_item_status_flags.sql"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "patient_payment_items (patient_payment_id, appointment_id, is_paid)",
            migration,
        )
        self.assertIn(
            "professional_payout_items (professional_payout_id, appointment_id, is_repassed)",
            migration,
        )

    def test_financial_transactions_are_updated_through_rpc(self):
        self.repo.update_patient_payment("payment-1", {
            "date": "2026-08-06", "amount": "950,00", "payment_method": "PIX"
        })
        self.repo.update_professional_payout("payout-1", {
            "date": "2026-08-06", "amount": "700,00", "payment_method": "Dinheiro"
        })
        self.assertEqual(self.repo.client.rpc_calls[0][0], "update_patient_payment")
        self.assertEqual(self.repo.client.rpc_calls[0][1]["p_amount"], 950.0)
        self.assertEqual(self.repo.client.rpc_calls[1][0], "update_professional_payout")
        self.assertEqual(self.repo.client.rpc_calls[1][1]["p_amount"], 700.0)

    def test_financial_transactions_are_deleted_through_rpc(self):
        self.repo.delete_patient_payment("payment-1")
        self.repo.delete_professional_payout("payout-1")
        self.assertEqual(
            self.repo.client.rpc_calls,
            [
                ("delete_patient_payment", {"p_payment_id": "payment-1"}),
                ("delete_professional_payout", {"p_payout_id": "payout-1"}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
