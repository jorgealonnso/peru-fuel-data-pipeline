import pandas as pd

from src.incremental import (
    add_establishment_key,
    append_incremental_history,
    build_current,
    detect_changes,
    detect_new_history_events,
)
from src.parser import canonicalize


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


def test_real_osinergmin_columns_are_canonicalized():
    raw = pd.DataFrame(
        [
            {
                "NRO_REGISTRO": "100010-056-261225",
                "RUC": "20494106893",
                "RAZON": "ECOX S.A.C.",
                "DEPARTAMENTO": "SAN MARTIN",
                "PROVINCIA": "TOCACHE",
                "DISTRITO": "TOCACHE",
                "DIRECCION": "JR. MARISCAL RAMON CASTILLA 671",
                "FCHA_REGISTRO": "2026-09-23 11:59:31",
                "COD_PRODUCTO": 126,
                "PRODUCTO": "GASOHOL REGULAR",
                "PRECIO_VENTA": 23.5,
                "UNIDAD": "Galones",
                "CODIGO_OSINERG": 100010,
            }
        ]
    )

    frame = canonicalize(raw)

    assert frame.loc[0, "ID_ESTABLECIMIENTO"] == 100010
    assert frame.loc[0, "RAZON_SOCIAL"] == "ECOX S.A.C."
    assert float(frame.loc[0, "PRECIO"]) == 23.5
    assert str(frame.loc[0, "FECHA_PRECIO"]) == "2026-09-23 11:59:31"
    assert "FECHA_CAPTURA" in frame.columns


def test_duplicate_snapshot_events_are_removed():
    snapshot = pd.DataFrame(
        [
            _row(15.5, "2026-09-26 10:30:00"),
            _row(15.5, "2026-09-26 10:30:00"),
        ]
    )

    changes = detect_changes(snapshot, None)

    assert len(changes) == 1


def test_historical_register_and_osinerg_code_share_key():
    frame = add_establishment_key(
        pd.DataFrame(
            [
                {"ID_ESTABLECIMIENTO": "100010-056-261225", "PRODUCTO": "GASOHOL REGULAR"},
                {"ID_ESTABLECIMIENTO": "100010", "PRODUCTO": "GASOHOL REGULAR"},
            ]
        )
    )

    assert frame["ESTABLECIMIENTO_KEY"].nunique() == 1
    assert frame.loc[0, "ESTABLECIMIENTO_KEY"] == "ID:100010"


def test_update_checks_monthly_history_for_existing_events(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    pd.DataFrame(
        [
            {
                "ID_ESTABLECIMIENTO": "100010-056-261225",
                "PRODUCTO": "GASOHOL REGULAR",
                "PRECIO": 23.5,
                "FECHA_PRECIO": "2026-09-23 11:59:31",
                "FECHA_CAPTURA": "2026-09-26T14:00:00-05:00",
            }
        ]
    ).to_parquet(history_dir / "2026-09.parquet", index=False)

    snapshot = pd.DataFrame(
        [
            {
                "ID_ESTABLECIMIENTO": "100010",
                "PRODUCTO": "GASOHOL REGULAR",
                "PRECIO": 23.5,
                "FECHA_PRECIO": "2026-09-23 11:59:31",
                "FECHA_CAPTURA": "2026-09-26T15:00:00-05:00",
            }
        ]
    )

    assert detect_new_history_events(snapshot, None, history_dir).empty


def test_excel_decimal_ids_share_the_same_key():
    frame = add_establishment_key(
        pd.DataFrame(
            [
                {"ID_ESTABLECIMIENTO": "100010.0", "PRODUCTO": "GASOHOL REGULAR"},
                {"ID_ESTABLECIMIENTO": 100010, "PRODUCTO": "GASOHOL REGULAR"},
                {"ID_ESTABLECIMIENTO": "100010-056-261225", "PRODUCTO": "GASOHOL REGULAR"},
            ]
        )
    )

    assert frame["ESTABLECIMIENTO_KEY"].nunique() == 1
    assert frame.loc[0, "ESTABLECIMIENTO_KEY"] == "ID:100010"


def test_scheduled_updates_use_append_only_csv(tmp_path):
    history_dir = tmp_path / "history"

    first = pd.DataFrame([_row(15.5, "2026-09-26 10:30:00")])
    second = pd.DataFrame([_row(15.7, "2026-09-26 13:20:00")])

    first_paths = append_incremental_history(first, history_dir)
    second_paths = append_incremental_history(second, history_dir)

    expected = history_dir / "2026-09-incremental.csv"
    assert first_paths == [expected]
    assert second_paths == [expected]
    assert not (history_dir / "2026-09.parquet").exists()

    stored = pd.read_csv(expected)
    assert len(stored) == 2
    assert stored["PRECIO"].tolist() == [15.5, 15.7]


def test_current_state_does_not_persist_relative_age():
    current = build_current(pd.DataFrame([_row(15.5, "2026-09-26 10:30:00")]))

    assert "DIAS_SIN_ACTUALIZAR" not in current.columns
