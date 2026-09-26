import pandas as pd

from src.incremental import build_current, detect_changes


def _row(price: float, date: str) -> dict:
    return {
        "ID_ESTABLECIMIENTO": "001",
        "NOMBRE_COMERCIAL": "GRIFO DEMO",
        "PRODUCTO": "DIESEL B5 S-50",
        "PRECIO": price,
        "FECHA_PRECIO": date,
        "FECHA_CAPTURA": "2026-09-26T14:00:00-05:00",
    }


def test_same_event_is_not_reinserted():
    current = build_current(pd.DataFrame([_row(15.5, "2026-09-26 10:30:00")]))
    snapshot = pd.DataFrame([_row(15.5, "2026-09-26 10:30:00")])
    assert detect_changes(snapshot, current).empty


def test_new_price_is_detected():
    current = build_current(pd.DataFrame([_row(15.5, "2026-09-26 10:30:00")]))
    snapshot = pd.DataFrame([_row(15.7, "2026-09-26 13:20:00")])
    changes = detect_changes(snapshot, current)
    assert len(changes) == 1
    assert float(changes.iloc[0]["PRECIO"]) == 15.7


def test_build_current_keeps_latest_price():
    history = pd.DataFrame([
        _row(15.5, "2026-09-26 10:30:00"),
        _row(15.7, "2026-09-26 13:20:00"),
    ])
    current = build_current(history)
    assert len(current) == 1
    assert float(current.iloc[0]["PRECIO"]) == 15.7
