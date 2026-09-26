from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import HISTORY_DIR, TIMEZONE

CURRENT_KEY = ["ESTABLECIMIENTO_KEY", "PRODUCTO"]
EVENT_KEY = ["ESTABLECIMIENTO_KEY", "PRODUCTO", "FECHA_PRECIO", "PRECIO"]
HISTORY_COMPRESSION = "zstd"


def _clean_key_value(value: object) -> str:
    if pd.isna(value):
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.upper().split())


def add_establishment_key(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for col in ("ID_ESTABLECIMIENTO", "RAZON_SOCIAL", "NOMBRE_COMERCIAL", "DIRECCION", "DISTRITO"):
        if col not in frame.columns:
            frame[col] = pd.NA

    def make_key(row: pd.Series) -> str:
        official_id = _clean_key_value(row["ID_ESTABLECIMIENTO"])
        if official_id:
            register_match = official_id.split("-", 1)[0]
            if register_match.isdigit():
                official_id = register_match
            return f"ID:{official_id}"
        raw = "|".join(
            _clean_key_value(row[col])
            for col in ("RAZON_SOCIAL", "NOMBRE_COMERCIAL", "DIRECCION", "DISTRITO")
        )
        return f"HASH:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:20]}"

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
    price_text = pd.to_numeric(temp["PRECIO"], errors="coerce").round(6).astype("string")
    return (
        temp["ESTABLECIMIENTO_KEY"].astype("string").fillna("")
        + "|"
        + temp["PRODUCTO"].astype("string").fillna("").str.upper()
        + "|"
        + date_text
        + "|"
        + price_text.fillna("")
    )


def build_current(history: pd.DataFrame) -> pd.DataFrame:
    history = add_establishment_key(history)
    history = history.copy()

    if "FECHA_PRECIO" not in history.columns:
        history["FECHA_PRECIO"] = pd.NaT
    history["_FECHA_SORT"] = pd.to_datetime(history["FECHA_PRECIO"], errors="coerce")

    if "FECHA_CAPTURA" not in history.columns:
        history["FECHA_CAPTURA"] = pd.NA
    history["_CAPTURA_SORT"] = pd.to_datetime(history["FECHA_CAPTURA"], errors="coerce")

    history = history.sort_values(["_FECHA_SORT", "_CAPTURA_SORT"], na_position="first")
    current = history.drop_duplicates(CURRENT_KEY, keep="last").copy()

    now = pd.Timestamp(datetime.now(ZoneInfo(TIMEZONE)))
    price_date = pd.to_datetime(current["FECHA_PRECIO"], errors="coerce")
    if getattr(price_date.dt, "tz", None) is None:
        price_date = price_date.dt.tz_localize(TIMEZONE, nonexistent="NaT", ambiguous="NaT")
    else:
        price_date = price_date.dt.tz_convert(TIMEZONE)

    current["DIAS_SIN_ACTUALIZAR"] = (now.normalize() - price_date.dt.normalize()).dt.days
    return current.drop(columns=["_FECHA_SORT", "_CAPTURA_SORT"], errors="ignore").reset_index(drop=True)


def build_current_from_history(history_dir: Path = HISTORY_DIR) -> pd.DataFrame:
    current = pd.DataFrame()
    for path in history_partition_paths(history_dir):
        part = read_history_partition(path)
        if part.empty:
            continue
        current = build_current(part if current.empty else pd.concat([current, part], ignore_index=True, sort=False))
    return current


def detect_changes(snapshot: pd.DataFrame, current: pd.DataFrame | None) -> pd.DataFrame:
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
    snapshot = add_establishment_key(snapshot)
    snapshot = snapshot.copy()
    snapshot["_SIG"] = _event_signature(snapshot)
    snapshot = snapshot.drop_duplicates("_SIG", keep="last")

    existing_sig: set[str] = set()
    if current is not None and not current.empty:
        existing_sig.update(_event_signature(add_establishment_key(current)).dropna().tolist())

    for path in _history_paths_for_snapshot(snapshot, history_dir):
        if not path.exists():
            continue
        existing = read_history_partition(path)
        if existing.empty:
            continue
        existing_sig.update(_event_signature(add_establishment_key(existing)).dropna().tolist())

    changes = snapshot.loc[~snapshot["_SIG"].isin(existing_sig)].copy()
    return changes.drop(columns="_SIG").reset_index(drop=True)


def _history_paths_for_snapshot(snapshot: pd.DataFrame, history_dir: Path) -> list[Path]:
    date = pd.to_datetime(snapshot.get("FECHA_PRECIO"), errors="coerce")
    capture = pd.to_datetime(snapshot.get("FECHA_CAPTURA"), errors="coerce")
    partition = date.dt.strftime("%Y-%m")
    partition = partition.fillna(capture.dt.strftime("%Y-%m")).fillna("sin-fecha")
    paths: list[Path] = []
    for period in sorted(partition.dropna().unique()):
        paths.append(history_dir / f"{period}.parquet")
        paths.append(history_dir / f"{period}.csv")
    return paths


def history_partition_paths(history_dir: Path = HISTORY_DIR) -> list[Path]:
    if not history_dir.exists():
        return []
    paths = list(history_dir.glob("*.parquet"))
    if not paths:
        paths = list(history_dir.glob("*.csv"))
    return sorted(paths)


def read_history_partition(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, low_memory=False)


def _prepare_for_parquet(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in frame.columns:
        if pd.api.types.is_object_dtype(frame[column]) or pd.api.types.is_string_dtype(frame[column]):
            frame[column] = frame[column].astype("string")
    return frame


def append_monthly_history(changes: pd.DataFrame, history_dir: Path = HISTORY_DIR) -> list[Path]:
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
            combined = part if existing.empty else pd.concat([existing, part], ignore_index=True, sort=False)
        elif legacy_csv.exists():
            existing = read_history_partition(legacy_csv)
            combined = part if existing.empty else pd.concat([existing, part], ignore_index=True, sort=False)
        else:
            combined = part

        combined = add_establishment_key(combined)
        combined["_SIG"] = _event_signature(combined)
        combined = combined.drop_duplicates("_SIG", keep="last").drop(columns="_SIG")

        combined = _prepare_for_parquet(combined)
        combined.to_parquet(path, index=False, compression=HISTORY_COMPRESSION)
        written.append(path)

    return written
