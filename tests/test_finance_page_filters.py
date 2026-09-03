import unittest

from agape_app.app import FinancePage


class FakeComboBox:
    def __init__(self, value):
        self.value = value

    def currentData(self):
        return self.value


class FakeFinancePage:
    group_by_patient = FinancePage.group_by_patient
    group_by_professional = FinancePage.group_by_professional

    def __init__(self, professional_id=None, patient_id=None):
        self.patient_filter = FakeComboBox(patient_id)
        self.professional_filter = FakeComboBox(professional_id)
        self.patients = [
            {"id": "patient-1", "full_name": "Ana"},
            {"id": "patient-2", "full_name": "Bruno"},
            {"id": "patient-3", "full_name": "Carla"},
        ]
        self.professionals = [
            {"id": "professional-1", "full_name": "Daniel"},
            {"id": "professional-2", "full_name": "Eliane"},
            {"id": "professional-3", "full_name": "Fernanda"},
        ]
        self.receipt_history_rows = []
        self.appointment_rows = [
            {
                "id": "appointment-1",
                "patient_id": "patient-2",
                "professional_id": "professional-2",
                "patients": {"full_name": "Bruno"},
                "professionals": {"full_name": "Eliane"},
                "appointment_financials": {"consultation_fee": 100},
            }
        ]
        self.repo = self

    def payment_totals_by_payment_date(self):
        return {}

    def fee_for(self, row):
        return float((row.get("appointment_financials") or {}).get("consultation_fee") or 0)

    def patient_name_for(self, row):
        return (row.get("patients") or {}).get("full_name", "Paciente não informado")

    def professional_name_for(self, row):
        return (row.get("professionals") or {}).get("full_name", "Funcionário não informado")

    def professional_share(self, value):
        return float(value) * 0.70


class FinancePatientFilterTests(unittest.TestCase):
    def test_selected_professional_lists_only_patients_with_matching_appointments(self):
        page = FakeFinancePage(professional_id="professional-1")

        rows = page.group_by_patient()

        self.assertEqual([row["id"] for row in rows], ["patient-2"])
        self.assertEqual(rows[0]["count"], 1)

    def test_all_professionals_keeps_all_patients_visible(self):
        page = FakeFinancePage()

        rows = page.group_by_patient()

        self.assertEqual([row["id"] for row in rows], ["patient-1", "patient-2", "patient-3"])

    def test_selected_patient_lists_only_professionals_with_matching_appointments(self):
        page = FakeFinancePage(patient_id="patient-2")

        rows = page.group_by_professional()

        self.assertEqual([row["id"] for row in rows], ["professional-2"])
        self.assertEqual(rows[0]["count"], 1)

    def test_all_patients_keeps_all_professionals_visible(self):
        page = FakeFinancePage()

        rows = page.group_by_professional()

        self.assertEqual(
            [row["id"] for row in rows],
            ["professional-1", "professional-2", "professional-3"],
        )


if __name__ == "__main__":
    unittest.main()
