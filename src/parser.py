from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import TIMEZONE

HEADER_WORDS = {
    "PRECIO",
    "PRODUCTO",
    "COMBUSTIBLE",
    "FECHA",
    "RAZON",
    "ESTABLECIMIENTO",
    "DISTRITO",
    "PROVINCIA",
    "DEPARTAMENTO",
}

ALIASES: dict[str, list[str]] = {
    "ID_ESTABLECIMIENTO": [
        "ID_ESTABLECIMIENTO",
        "CODIGO_ESTABLECIMIENTO",
        "CODIGO_DE_ESTABLECIMIENTO",
        "CODIGO_OSINERGMIN",
        "CODIGO_OSINERG",
        "REGISTRO_DE_HIDROCARBUROS",
        "NRO_REGISTRO",
        "CODIGO",
    ],
    "RAZON_SOCIAL": ["RAZON_SOCIAL", "RAZON", "RAZON_SOCIAL"],
    "NOMBRE_COMERCIAL": [
        "NOMBRE_COMERCIAL",
        "NOMBRE_ESTABLECIMIENTO",
        "ESTABLECIMIENTO",
    ],
    "DIRECCION": ["DIRECCION", "DOMICILIO"],
    "DISTRITO": ["DISTRITO"],
    "PROVINCIA": ["PROVINCIA"],
    "DEPARTAMENTO": ["DEPARTAMENTO", "REGION"],
    "LATITUD": ["LATITUD", "LAT"],
    "LONGITUD": ["LONGITUD", "LON", "LNG"],
    "PRODUCTO": [
        "PRODUCTO",
        "NOMBRE_PRODUCTO",
        "TIPO_COMBUSTIBLE",
        "COMBUSTIBLE",
    ],
    "PRECIO": [
        "PRECIO",
        "PRECIO_VENTA",
        "PRECIO_PUBLICO",
        "PRECIO_COMBUSTIBLE",
    ],
    "UNIDAD": ["UNIDAD", "UNIDAD_MEDIDA"],
    "FECHA_PRECIO": [
        "FECHA_PRECIO",
        "FECHA_REGISTRO",
        "FCHA_REGISTRO",
        "FECHA_DE_REGISTRO",
        "FECHA_HORA",
        "FECHA_ACTUALIZACION",
        "FECHA_MODIFICACION",
        "FECHA",
    ],
    "HORA_PRECIO": ["HORA_PRECIO", "HORA_REGISTRO", "HORA"],
}


def normalize_column(value: object) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.strip().upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    return text.strip("_") or "COLUMNA"


def _dedupe_columns(columns: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    output: list[str] = []
    for col in columns:
        counts[col] = counts.get(col, 0) + 1
        output.append(col if counts[col] == 1 else f"{col}_{counts[col]}")
    return output


def _header_score(row: pd.Series) -> int:
    values = {normalize_column(value) for value in row.dropna().tolist()}
    return sum(any(word in value for word in HEADER_WORDS) for value in values)


def _detect_header_excel(source: object, sheet_name: str | int = 0) -> int:
    engines = ["calamine", "openpyxl"] if _can_use_calamine() else ["openpyxl"]
    last_error: Exception | None = None
    for engine in engines:
        try:
            preview = pd.read_excel(source, sheet_name=sheet_name, header=None, nrows=30, engine=engine)
            break
        except Exception as exc:
            last_error = exc
            if hasattr(source, "seek"):
                source.seek(0)
    else:
        raise RuntimeError(f"No se pudo inspeccionar Excel: {last_error}")

    if hasattr(source, "seek"):
        source.seek(0)
    scores = preview.apply(_header_score, axis=1)
    if scores.empty:
        return 0
    best = int(scores.idxmax())
    return best if int(scores.loc[best]) >= 2 else 0


def read_excel_smart(source: object, sheet_name: str | int = 0) -> pd.DataFrame:
    header = _detect_header_excel(source, sheet_name=sheet_name)
    engines = ["calamine", "openpyxl"] if _can_use_calamine() else ["openpyxl"]
    last_error: Exception | None = None
    for engine in engines:
        try:
            return pd.read_excel(source, sheet_name=sheet_name, header=header, engine=engine)
        except Exception as exc:
            last_error = exc
            if hasattr(source, "seek"):
                source.seek(0)
    raise RuntimeError(f"No se pudo leer Excel: {last_error}")


def _can_use_calamine() -> bool:
    try:
        import python_calamine  # noqa: F401
    except Exception:
        return False
    return True


def read_csv_smart(source: object) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            raw = pd.read_csv(source, encoding=encoding, sep=None, engine="python", header=None, nrows=30)
            scores = raw.apply(_header_score, axis=1)
            header = int(scores.idxmax()) if not scores.empty and scores.max() >= 2 else 0
            if hasattr(source, "seek"):
                source.seek(0)
            return pd.read_csv(source, encoding=encoding, sep=None, engine="python", header=header)
        except Exception as exc:
            last_error = exc
            if hasattr(source, "seek"):
                source.seek(0)
    raise RuntimeError(f"No se pudo leer CSV: {last_error}")


def _looks_like_xlsx_zip(path: Path) -> bool:
    if not zipfile.is_zipfile(path):
        return False
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        return "[Content_Types].xml" in names and any(n.startswith("xl/") for n in names)


def _parse_excel_path(path: Path) -> list[pd.DataFrame]:
    book = _excel_file(path)
    frames: list[pd.DataFrame] = []
    for sheet in book.sheet_names:
        try:
            frame = read_excel_smart(path, sheet_name=sheet)
            if not frame.empty:
                frame["HOJA_FUENTE"] = sheet
                frames.append(frame)
        except Exception:
            continue
    return frames


def _parse_excel_bytes(payload: bytes, name: str) -> list[pd.DataFrame]:
    buffer = io.BytesIO(payload)
    book = _excel_file(buffer)
    frames: list[pd.DataFrame] = []
    for sheet in book.sheet_names:
        try:
            buffer.seek(0)
            frame = read_excel_smart(buffer, sheet_name=sheet)
            if not frame.empty:
                frame["HOJA_FUENTE"] = sheet
                frame["ARCHIVO_FUENTE"] = name
                frames.append(frame)
        except Exception:
            continue
    return frames


def _excel_file(source: object) -> pd.ExcelFile:
    engines = ["calamine", "openpyxl"] if _can_use_calamine() else ["openpyxl"]
    last_error: Exception | None = None
    for engine in engines:
        try:
            return pd.ExcelFile(source, engine=engine)
        except Exception as exc:
            last_error = exc
            if hasattr(source, "seek"):
                source.seek(0)
    raise RuntimeError(f"No se pudo abrir Excel: {last_error}")


def parse_download(path: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []

    if _looks_like_xlsx_zip(path):
        frames.extend(_parse_excel_path(path))
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith("/") or name.startswith("__MACOSX/"):
                    continue
                lower = name.lower()
                payload = archive.read(name)
                if lower.endswith((".xlsx", ".xls")):
                    frames.extend(_parse_excel_bytes(payload, name))
                elif lower.endswith(".csv"):
                    buffer = io.BytesIO(payload)
                    frame = read_csv_smart(buffer)
                    frame["ARCHIVO_FUENTE"] = name
                    frames.append(frame)
    elif path.suffix.lower() in (".xlsx", ".xls"):
        frames.extend(_parse_excel_path(path))
    elif path.suffix.lower() == ".csv":
        frames.append(read_csv_smart(path))
    else:
        try:
            frames.extend(_parse_excel_path(path))
        except Exception:
            try:
                frames.append(read_csv_smart(path))
            except Exception as exc:
                raise RuntimeError(
                    f"Formato no reconocido para {path.name}. Revisar manualmente la respuesta."
                ) from exc

    if not frames:
        raise RuntimeError(f"No se encontraron tablas procesables en {path.name}")

    normalized = [canonicalize(frame) for frame in frames if not frame.empty]
    normalized = [frame for frame in normalized if not frame.empty]
    if not normalized:
        raise RuntimeError("Las tablas encontradas quedaron vacías tras normalización.")

    return pd.concat(normalized, ignore_index=True, sort=False)


def _find_alias(columns: list[str], aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in columns:
            return alias
    for alias in aliases:
        matches = [col for col in columns if col.startswith(alias + "_")]
        if matches:
            return matches[0]
    return None


def _to_price(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype("string")
        .str.strip()
        .str.replace("S/", "", regex=False)
        .str.replace(",", ".", regex=False)
        .str.extract(r"(-?\d+(?:\.\d+)?)", expand=False)
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _to_datetime(series: pd.Series) -> pd.Series:
    iso = pd.to_datetime(series, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    fallback_source = series.astype("string").where(iso.isna())
    fallback = pd.to_datetime(fallback_source, dayfirst=True, errors="coerce")
    return iso.fillna(fallback)


def canonicalize(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = _dedupe_columns([normalize_column(col) for col in frame.columns])
    frame = frame.dropna(axis=0, how="all").dropna(axis=1, how="all")

    for canonical, aliases in ALIASES.items():
        if canonical in frame.columns:
            continue
        source = _find_alias(list(frame.columns), aliases)
        if source:
            frame[canonical] = frame[source]

    if "PRECIO" in frame.columns:
        frame["PRECIO"] = _to_price(frame["PRECIO"])

    if "FECHA_PRECIO" in frame.columns:
        if "HORA_PRECIO" in frame.columns:
            date_text = frame["FECHA_PRECIO"].astype("string").str.strip()
            time_text = frame["HORA_PRECIO"].astype("string").str.strip()
            combined_dt = pd.to_datetime(date_text + " " + time_text, dayfirst=True, errors="coerce")
            date_only_dt = _to_datetime(frame["FECHA_PRECIO"])
            frame["FECHA_PRECIO"] = combined_dt.fillna(date_only_dt)
        else:
            frame["FECHA_PRECIO"] = _to_datetime(frame["FECHA_PRECIO"])

    if "PRECIO" in frame.columns:
        frame = frame.loc[frame["PRECIO"].notna()].copy()

    frame["FECHA_CAPTURA"] = datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds")
    return frame.reset_index(drop=True)
