from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import HISTORY_DIR, INCREMENTAL_DIR, TIMEZONE

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
    """Normalize OSINERGMIN IDs from Excel/CSV."""
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

    DIAS_SIN_ACTUALIZAR is intentionally calculated in Power BI instead of
    being persisted, so days without price changes do not rewrite the file.
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
    incremental_dir: Path = INCREMENTAL_DIR,
) -> pd.DataFrame:
    """Return events not already stored in current state, history, or staging."""
    snapshot = add_establishment_key(snapshot)
    snapshot = snapshot.copy()
    snapshot["_SIG"] = _event_signature(snapshot)
    snapshot = snapshot.drop_duplicates("_SIG", keep="last")

    existing_sig: set[str] = set()

    if current is not None and not current.empty:
        existing_sig.update(
            _event_signature(add_establishment_key(current)).dropna().tolist()
        )

    for path in _history_paths_for_snapshot(
        snapshot,
        history_dir=history_dir,
        incremental_dir=incremental_dir,
    ):
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
    incremental_dir: Path,
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
                incremental_dir / f"{period}.csv",
            ]
        )
    return paths


def history_partition_paths(history_dir: Path = HISTORY_DIR) -> list[Path]:
    """Return closed history partitions.

    The normal state is Parquet-only. CSV support remains for legacy files.
    """
    if not history_dir.exists():
        return []

    parquet_paths = list(history_dir.glob("*.parquet"))
    legacy_csv = [
        path
        for path in history_dir.glob("*.csv")
        if not path.name.endswith("-incremental.csv")
    ]
    return sorted(parquet_paths + legacy_csv)


def read_history_partition(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, low_memory=False)


def _prepare_for_parquet(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()

    if "FECHA_PRECIO" in frame.columns:
        frame["FECHA_PRECIO"] = pd.to_datetime(
            frame["FECHA_PRECIO"],
            errors="coerce",
        )
    if "PRECIO" in frame.columns:
        frame["PRECIO"] = pd.to_numeric(frame["PRECIO"], errors="coerce")

    for column in frame.columns:
        if pd.api.types.is_object_dtype(frame[column]) or pd.api.types.is_string_dtype(
            frame[column]
        ):
            frame[column] = frame[column].astype("string")

    return frame


def _dedupe_events(frame: pd.DataFrame) -> pd.DataFrame:
    frame = add_establishment_key(frame)
    frame["_SIG"] = _event_signature(frame)
    return (
        frame.drop_duplicates("_SIG", keep="last")
        .drop(columns="_SIG")
        .reset_index(drop=True)
    )


def append_monthly_history(
    changes: pd.DataFrame,
    history_dir: Path = HISTORY_DIR,
) -> list[Path]:
    """Write historical backfill as monthly Parquet."""
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
            combined = pd.concat([existing, part], ignore_index=True, sort=False)
        elif legacy_csv.exists():
            existing = read_history_partition(legacy_csv)
            combined = pd.concat([existing, part], ignore_index=True, sort=False)
        else:
            combined = part

        combined = _prepare_for_parquet(_dedupe_events(combined))
        combined.to_parquet(
            path,
            index=False,
            compression=HISTORY_COMPRESSION,
        )
        written.append(path)

    return written


def append_incremental_history(
    changes: pd.DataFrame,
    incremental_dir: Path = INCREMENTAL_DIR,
) -> list[Path]:
    """Append new events to the hot staging layer.

    CSV is intentional here: Git stores append-only text efficiently. These
    files are temporary and are compacted into monthly Parquet after a month
    closes, so the permanent history remains Parquet-only.
    """
    if changes.empty:
        return []

    incremental_dir.mkdir(parents=True, exist_ok=True)
    work = add_establishment_key(changes)

    date = pd.to_datetime(work.get("FECHA_PRECIO"), errors="coerce")
    capture = pd.to_datetime(work.get("FECHA_CAPTURA"), errors="coerce")
    partition = date.dt.strftime("%Y-%m")
    partition = partition.fillna(capture.dt.strftime("%Y-%m")).fillna("sin-fecha")
    work["_PARTITION"] = partition

    written: list[Path] = []

    for period, part in work.groupby("_PARTITION", dropna=False):
        path = incremental_dir / f"{period}.csv"
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


def compact_closed_months(
    history_dir: Path = HISTORY_DIR,
    incremental_dir: Path = INCREMENTAL_DIR,
    current_period: str | None = None,
) -> list[Path]:
    """Merge closed-month staging CSVs into their permanent Parquet files.

    Any staging month strictly older than current_period is compacted. The
    staging CSV is deleted only after the Parquet write succeeds.
    """
    history_dir.mkdir(parents=True, exist_ok=True)
    incremental_dir.mkdir(parents=True, exist_ok=True)

    if current_period is None:
        current_period = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m")

    staged: list[tuple[str, Path]] = []
    for path in incremental_dir.glob("????-??.csv"):
        period = path.stem
        if period < current_period:
            staged.append((period, path))

    # Backward compatibility for the first staging file created before the
    # dedicated data/incremental directory existed.
    for path in history_dir.glob("????-??-incremental.csv"):
        period = path.name[:7]
        if period < current_period:
            staged.append((period, path))

    compacted: list[Path] = []

    for period, delta_path in sorted(staged):
        delta = pd.read_csv(delta_path, low_memory=False)
        target = history_dir / f"{period}.parquet"

        if target.exists():
            base = pd.read_parquet(target)
            combined = pd.concat([base, delta], ignore_index=True, sort=False)
        else:
            combined = delta

        combined = _prepare_for_parquet(_dedupe_events(combined))

        # Write to a temporary sibling first, then atomically replace.
        temp_target = target.with_suffix(".parquet.tmp")
        combined.to_parquet(
            temp_target,
            index=False,
            compression=HISTORY_COMPRESSION,
        )
        temp_target.replace(target)
        delta_path.unlink()
        compacted.append(target)

    return compacted
