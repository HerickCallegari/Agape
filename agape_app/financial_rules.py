from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Any


def current_month_period(today: date | None = None) -> tuple[date, date]:
    current = today or date.today()
    last_day = monthrange(current.year, current.month)[1]
    return date(current.year, current.month, 1), date(current.year, current.month, last_day)


def selected_reference_total(rows: list[dict[str, Any]], selected_indexes: list[int], field: str) -> float:
    return round(sum(float(rows[index].get(field) or 0) for index in selected_indexes), 2)


def suggested_transaction_amount(reference_total: float, discounts: float, surcharges: float) -> float:
    return round(max(float(reference_total) + float(surcharges) - float(discounts), 0), 2)


def appointment_is_selectable(_row: dict[str, Any]) -> bool:
    return True


def default_selected_indexes(rows: list[dict[str, Any]]) -> list[int]:
    """Every search result starts selected; the user may uncheck any row."""
    return list(range(len(rows)))
