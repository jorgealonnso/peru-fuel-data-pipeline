from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

import pandas as pd

from .config import HISTORY_DIR

CURRENT_KEY = ["ESTABLECIMIENTO_KEY", "PRODUCTO"]
EVENT_KEY = ["ESTABLECIMIENTO_KEY", "PRODUCTO", "FECHA_PRECIO", "PRECIO"]
HISTORY_COMPRESSION = "zstd"

INCREMENTAL_COLUMNS = [
    "ID_ESTABLECIMIENTO",
    "RAZON_SOCIAL",
    "NOMBRE_COMERCIAL",
    "DIRECCION",
    "DISTRITO",
    "PROVINCIA",
    "DEPARTAMENTO",
    "LATITUD",
    "LONGITUD",
    "PRODUCTO",
    "PRECIO",
    "UNIDAD",
    "FECHA_PRECIO",
    "FECHA_CAPTURA",
    "ESTABLECIMIENTO_KEY",
    "URL_FUENTE",
]


def _clean_key_value(value: object) -> str:
    if pd.isna(value):
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.upper().split())


def _normalize_official_id(value: object) -> str:
    """Normalize OSINERGMIN IDs from Excel/CSV.

    Excel may materialize integer codes as 100010.0, while historical
    registration fields may contain suffixes such as 100010-056-261225.
    Both must map to the same establishment key.
    """
    text = _clean_key_value(value)
    if not text:
        return ""

    first_component = text.split("-", 1)[0].strip()

    decimal_integer = re.fullmatch(r"(\d+)\.0+", first_component)
    if decimal_integer:
        return decimal_integer.group(1)

    if first_component.isdigit():
        return first_component

    return text


def add_establishment_key(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for col in (
        "ID_ESTABLECIMIENTO",
        "RAZON_SOCIAL",
        "NOMBRE_COMERCIAL",
        "DIRECCION",
        "DISTRITO",
    ):
        if col not in frame.columns:
            frame[col] = pd.NA

    def make_key(row: pd.Series) -> str:
        official_id = _normalize_official_id(row["ID_ESTABLECIMIENTO"])
        if official_id:
            return f"ID:{official_id}"

        raw = "|".join(
            _clean_key_value(row[col])
            for col in ("RAZON_SOCIAL", "NOMBRE_COMERCIAL", "DIRECCION", "DISTRITO")
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
        return f"HASH:{digest}"

    frame["ESTABLECIMIENTO_KEY"] = frame.apply(make_key, axis=1)

    if "PRODUCTO" not in frame.columns:
        frame["PRODUCTO"] = pd.NA
    frame["PRODUCTO"] = frame["PRODUCTO"].astype("string").str.strip()

    return frame


def _event_signature(frame: pd.DataFrame) -> pd.Series:
    temp = frame.copy()
    for col in EVENT_KEY:
        if col not in temp.columns:
            temp[col] = pd.NA

    date = pd.to_datetime(temp["FECHA_PRECIO"], errors="coerce")
    date_text = date.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")
    price_text = (
        pd.to_numeric(temp["PRECIO"], errors="coerce")
        .round(6)
        .astype("string")
        .fillna("")
    )

    return (
        temp["ESTABLECIMIENTO_KEY"].astype("string").fillna("")
        + "|"
        + temp["PRODUCTO"].astype("string").fillna("").str.upper()
        + "|"
        + date_text
        + "|"
        + price_text
    )


def build_current(history: pd.DataFrame) -> pd.DataFrame:
    """Build one latest row per establishment/product.

    DIAS_SIN_ACTUALIZAR is intentionally not persisted. Persisting a
    relative age field would change thousands of CSV rows every day even
    when no fuel price changed. Power BI should calculate that metric from
    FECHA_PRECIO and the refresh date.
    """
    history = add_establishment_key(history)
    history = history.copy()

    if "FECHA_PRECIO" not in history.columns:
        history["FECHA_PRECIO"] = pd.NaT
    history["_FECHA_SORT"] = pd.to_datetime(history["FECHA_PRECIO"], errors="coerce")

    if "FECHA_CAPTURA" not in history.columns:
        history["FECHA_CAPTURA"] = pd.NA
    history["_CAPTURA_SORT"] = pd.to_datetime(history["FECHA_CAPTURA"], errors="coerce")

    history = history.sort_values(
        ["_FECHA_SORT", "_CAPTURA_SORT"],
        na_position="first",
        kind="stable",
    )

    current = history.drop_duplicates(CURRENT_KEY, keep="last").copy()
    current = current.drop(
        columns=["_FECHA_SORT", "_CAPTURA_SORT", "DIAS_SIN_ACTUALIZAR"],
        errors="ignore",
    )

    current = current.sort_values(CURRENT_KEY, kind="stable", na_position="last")
    return current.reset_index(drop=True)


def build_current_from_history(history_dir: Path = HISTORY_DIR) -> pd.DataFrame:
    current = pd.DataFrame()
    for path in history_partition_paths(history_dir):
        part = read_history_partition(path)
        if part.empty:
            continue

        current = build_current(
            part
            if current.empty
            else pd.concat([current, part], ignore_index=True, sort=False)
        )

    return current


def detect_changes(
    snapshot: pd.DataFrame,
    current: pd.DataFrame | None,
) -> pd.DataFrame:
    snapshot = add_establishment_key(snapshot)
    snapshot = snapshot.copy()
    snapshot["_SIG"] = _event_signature(snapshot)
    snapshot = snapshot.drop_duplicates("_SIG", keep="last")

    if current is None or current.empty:
        return snapshot.drop(columns="_SIG").reset_index(drop=True)

    current = add_establishment_key(current)
    current_sig = set(_event_signature(current).dropna().tolist())
    changes = snapshot.loc[~snapshot["_SIG"].isin(current_sig)].copy()
    return changes.drop(columns="_SIG").reset_index(drop=True)


def detect_new_history_events(
    snapshot: pd.DataFrame,
    current: pd.DataFrame | None,
    history_dir: Path = HISTORY_DIR,
) -> pd.DataFrame:
    """Return events not already stored in current state or history."""
    snapshot = add_establishment_key(snapshot)
    snapshot = snapshot.copy()
    snapshot["_SIG"] = _event_signature(snapshot)
    snapshot = snapshot.drop_duplicates("_SIG", keep="last")

    existing_sig: set[str] = set()

    if current is not None and not current.empty:
        existing_sig.update(
            _event_signature(add_establishment_key(current)).dropna().tolist()
        )

    for path in _history_paths_for_snapshot(snapshot, history_dir):
        if not path.exists():
            continue
        existing = read_history_partition(path)
        if existing.empty:
            continue
        existing_sig.update(
            _event_signature(add_establishment_key(existing)).dropna().tolist()
        )

    changes = snapshot.loc[~snapshot["_SIG"].isin(existing_sig)].copy()
    return changes.drop(columns="_SIG").reset_index(drop=True)


def _history_paths_for_snapshot(
    snapshot: pd.DataFrame,
    history_dir: Path,
) -> list[Path]:
    date = pd.to_datetime(snapshot.get("FECHA_PRECIO"), errors="coerce")
    capture = pd.to_datetime(snapshot.get("FECHA_CAPTURA"), errors="coerce")

    partition = date.dt.strftime("%Y-%m")
    partition = partition.fillna(capture.dt.strftime("%Y-%m")).fillna("sin-fecha")

    paths: list[Path] = []
    for period in sorted(partition.dropna().unique()):
        paths.extend(
            [
                history_dir / f"{period}.parquet",
                history_dir / f"{period}.csv",
                history_dir / f"{period}-incremental.csv",
            ]
        )

    return paths


def history_partition_paths(history_dir: Path = HISTORY_DIR) -> list[Path]:
    """Return historical Parquet plus append-only incremental CSV files."""
    if not history_dir.exists():
        return []

    parquet_paths = list(history_dir.glob("*.parquet"))
    csv_paths = list(history_dir.glob("*.csv"))
    return sorted(parquet_paths + csv_paths)


def read_history_partition(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, low_memory=False)


def _prepare_for_parquet(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in frame.columns:
        if pd.api.types.is_object_dtype(frame[column]) or pd.api.types.is_string_dtype(
            frame[column]
        ):
            frame[column] = frame[column].astype("string")
    return frame


def append_monthly_history(
    changes: pd.DataFrame,
    history_dir: Path = HISTORY_DIR,
) -> list[Path]:
    """Write historical backfill as monthly Parquet.

    Scheduled updates should use append_incremental_history instead, so Git
    does not receive a rewritten multi-megabyte binary file four times a day.
    """
    if changes.empty:
        return []

    history_dir.mkdir(parents=True, exist_ok=True)
    work = changes.copy()

    date = pd.to_datetime(work.get("FECHA_PRECIO"), errors="coerce")
    capture = pd.to_datetime(work.get("FECHA_CAPTURA"), errors="coerce")

    partition = date.dt.strftime("%Y-%m")
    partition = partition.fillna(capture.dt.strftime("%Y-%m")).fillna("sin-fecha")
    work["_PARTITION"] = partition

    written: list[Path] = []

    for period, part in work.groupby("_PARTITION", dropna=False):
        path = history_dir / f"{period}.parquet"
        legacy_csv = history_dir / f"{period}.csv"
        part = part.drop(columns="_PARTITION")

        if path.exists():
            existing = read_history_partition(path)
            combined = (
                part
                if existing.empty
                else pd.concat([existing, part], ignore_index=True, sort=False)
            )
        elif legacy_csv.exists():
            existing = read_history_partition(legacy_csv)
            combined = (
                part
                if existing.empty
                else pd.concat([existing, part], ignore_index=True, sort=False)
            )
        else:
            combined = part

        combined = add_establishment_key(combined)
        combined["_SIG"] = _event_signature(combined)
        combined = (
            combined.drop_duplicates("_SIG", keep="last")
            .drop(columns="_SIG")
            .reset_index(drop=True)
        )

        combined = _prepare_for_parquet(combined)
        combined.to_parquet(
            path,
            index=False,
            compression=HISTORY_COMPRESSION,
        )
        written.append(path)

    return written


def append_incremental_history(
    changes: pd.DataFrame,
    history_dir: Path = HISTORY_DIR,
) -> list[Path]:
    """Append scheduled updates to small text files.

    Large monthly Parquet files remain immutable. This keeps Git history
    compact and lets each scheduled run add only the new price events.
    """
    if changes.empty:
        return []

    history_dir.mkdir(parents=True, exist_ok=True)
    work = add_establishment_key(changes)

    date = pd.to_datetime(work.get("FECHA_PRECIO"), errors="coerce")
    capture = pd.to_datetime(work.get("FECHA_CAPTURA"), errors="coerce")

    partition = date.dt.strftime("%Y-%m")
    partition = partition.fillna(capture.dt.strftime("%Y-%m")).fillna("sin-fecha")
    work["_PARTITION"] = partition

    written: list[Path] = []

    for period, part in work.groupby("_PARTITION", dropna=False):
        path = history_dir / f"{period}-incremental.csv"
        part = part.drop(columns="_PARTITION").copy()

        columns = [column for column in INCREMENTAL_COLUMNS if column in part.columns]
        part = part[columns]

        part.to_csv(
            path,
            mode="a",
            header=not path.exists() or path.stat().st_size == 0,
            index=False,
            encoding="utf-8-sig",
        )
        written.append(path)

    return written
