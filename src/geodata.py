from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import pandas as pd
import requests

from .config import CURRENT_FILE, DATA_DIR, REQUEST_TIMEOUT, USER_AGENT
from .downloader import build_session

ARCGIS_SERVICE_URL = (
    "https://gisem.osinergmin.gob.pe/serverosih/rest/services/"
    "Hidrocarburos_Liquidos/HIDROCARBUROS_LIQUIDOS/FeatureServer"
)

BI_DIR = DATA_DIR / "bi"
DIM_ESTABLISHMENT_FILE = BI_DIR / "dim_establecimiento.parquet"
GEOCODING_REPORT_FILE = BI_DIR / "geocoding_report.json"
GEOCODING_UNMATCHED_FILE = BI_DIR / "geocoding_unmatched.csv"

PERU_LONGITUDE_RANGE = (-82.5, -68.0)
PERU_LATITUDE_RANGE = (-19.0, 1.0)

BUSINESS_COLUMNS = [
    "ESTABLECIMIENTO_KEY",
    "ID_ESTABLECIMIENTO",
    "RUC",
    "RAZON_SOCIAL",
    "NOMBRE_COMERCIAL",
    "ACTIVIDAD",
    "DIRECCION",
    "DISTRITO",
    "PROVINCIA",
    "DEPARTAMENTO",
]

GEO_COLUMNS = [
    "LATITUD",
    "LONGITUD",
    "GEO_MATCHED",
    "GEO_MATCH_METHOD",
    "GEO_MATCH_SCORE",
    "GEO_SOURCE_LAYER",
    "GEO_OBJECTID",
    "GEO_UPDATED_AT",
]


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _as_text(value: object) -> str:
    if _is_missing(value):
        return ""
    return str(value).strip()


def normalize_text(value: object) -> str:
    text = _as_text(value)
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_address(value: object) -> str:
    text = normalize_text(value)
    replacements = {
        "AVENIDA": "AV",
        "JIRON": "JR",
        "CARRETERA": "CAR",
        "NUMERO": "N",
        "NRO": "N",
        "NO": "N",
    }
    return " ".join(replacements.get(token, token) for token in text.split())


def normalize_location(value: object) -> str:
    text = normalize_text(value)
    aliases = {
        "PROV CONST DEL CALLAO": "CALLAO",
        "PROVINCIA CONSTITUCIONAL DEL CALLAO": "CALLAO",
        "SAN MARTIN": "SAN MARTIN",
    }
    return aliases.get(text, text)


def normalize_business_name(value: object) -> str:
    text = normalize_text(value)
    suffixes = {"SAC", "SA", "SRL", "EIRL", "SAA"}
    tokens = [token for token in text.split() if token not in suffixes]
    return " ".join(tokens)


def normalize_ruc(value: object) -> str:
    text = _as_text(value)
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) == 11 else ""


def normalize_official_code(value: object) -> str:
    text = _as_text(value).upper()
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    first = re.split(r"[-/]", text, maxsplit=1)[0].strip()
    if re.fullmatch(r"\d+\.0+", first):
        first = first.split(".", 1)[0]
    digits = re.sub(r"\D", "", first)
    if not digits:
        return ""
    return digits


def normalize_register(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_text(value))


def valid_wgs84_coordinates(longitude: object, latitude: object) -> bool:
    try:
        x = float(longitude)
        y = float(latitude)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(x)
        and math.isfinite(y)
        and -180 <= x <= 180
        and -90 <= y <= 90
        and PERU_LONGITUDE_RANGE[0] <= x <= PERU_LONGITUDE_RANGE[1]
        and PERU_LATITUDE_RANGE[0] <= y <= PERU_LATITUDE_RANGE[1]
    )


def _chunks(values: Sequence[int], size: int) -> Iterator[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class ArcGISClient:
    def __init__(
        self,
        base_url: str = ARCGIS_SERVICE_URL,
        session: requests.Session | None = None,
        page_size: int = 200,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or build_session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.page_size = page_size

    def _get_json(self, path: str, params: Mapping[str, object]) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/{path.lstrip('/')}",
            params=dict(params),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            preview = response.text[:300] if hasattr(response, "text") else ""
            raise RuntimeError(
                "ArcGIS devolvio una respuesta no JSON "
                f"({response.status_code}): {preview!r}"
            ) from exc
        if "error" in payload:
            raise RuntimeError(f"ArcGIS devolvio un error: {payload['error']}")
        return payload

    def list_layers(self) -> list[dict[str, Any]]:
        return self._get_json("layers", {"f": "json"}).get("layers", [])

    def layer_metadata(self, layer_id: int) -> dict[str, Any]:
        return self._get_json(str(layer_id), {"f": "json"})

    def distinct_values(self, layer_id: int, field: str) -> list[object]:
        payload = self._get_json(
            f"{layer_id}/query",
            {
                "f": "json",
                "where": "1=1",
                "outFields": field,
                "returnGeometry": "false",
                "returnDistinctValues": "true",
                "orderByFields": field,
            },
        )
        return [
            feature.get("attributes", {}).get(field)
            for feature in payload.get("features", [])
        ]

    def fetch_layer_features(
        self,
        layer_id: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        metadata = dict(metadata or self.layer_metadata(layer_id))
        object_id_field = metadata.get("objectIdField")
        if not object_id_field:
            raise RuntimeError(f"Layer {layer_id} no informa objectIdField")

        ids_payload = self._get_json(
            f"{layer_id}/query",
            {
                "f": "json",
                "where": "1=1",
                "returnIdsOnly": "true",
            },
        )
        object_ids = sorted(int(value) for value in ids_payload.get("objectIds") or [])
        if not object_ids:
            return []

        batch_size = min(
            self.page_size,
            int(metadata.get("maxRecordCount") or self.page_size),
        )
        features: list[dict[str, Any]] = []
        for batch in _chunks(object_ids, max(1, batch_size)):
            payload = self._get_json(
                f"{layer_id}/query",
                {
                    "f": "json",
                    "objectIds": ",".join(str(value) for value in batch),
                    "outFields": "*",
                    "returnGeometry": "true",
                    "outSR": "4326",
                    "orderByFields": object_id_field,
                },
            )
            features.extend(payload.get("features", []))

        returned_ids = {
            int(feature.get("attributes", {}).get(object_id_field))
            for feature in features
            if feature.get("attributes", {}).get(object_id_field) is not None
        }
        if returned_ids != set(object_ids):
            missing = sorted(set(object_ids) - returned_ids)
            raise RuntimeError(
                f"Layer {layer_id}: descarga incompleta; faltan {len(missing)} OBJECTID"
            )
        return sorted(
            features,
            key=lambda feature: int(feature["attributes"][object_id_field]),
        )


def _field_lookup(metadata: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(field["name"]).upper(): str(field["name"])
        for field in metadata.get("fields", [])
    }


def _attribute(
    attributes: Mapping[str, object],
    fields: Mapping[str, str],
    *aliases: str,
) -> object:
    for alias in aliases:
        actual = fields.get(alias.upper())
        if actual and not _is_missing(attributes.get(actual)):
            return attributes.get(actual)
    return pd.NA


def features_to_frame(
    features: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any],
    updated_at: str,
) -> pd.DataFrame:
    fields = _field_lookup(metadata)
    object_id_field = str(metadata.get("objectIdField") or "OBJECTID")
    rows: list[dict[str, object]] = []

    for feature in features:
        attributes = feature.get("attributes") or {}
        geometry = feature.get("geometry") or {}
        longitude = geometry.get(
            "x",
            _attribute(attributes, fields, "LONGITUD", "LONGITUD_X", "X"),
        )
        latitude = geometry.get(
            "y",
            _attribute(attributes, fields, "LATITUD", "LATITUD_Y", "Y"),
        )
        coordinates_valid = valid_wgs84_coordinates(longitude, latitude)

        register = _attribute(
            attributes,
            fields,
            "N",
            "NRO_REGISTRO",
            "REGISTRO_DE_HIDROCARBUROS",
            "CODIGO_DGH",
        )
        explicit_code = _attribute(
            attributes,
            fields,
            "COD_OSINERGMIN",
            "CODIGO_OSINERGMIN",
            "CODIGO_OSI",
            "CODIGO_OSINERG",
        )
        code = normalize_official_code(explicit_code) or normalize_official_code(
            register
        )

        row = {
            "GIS_RUC": _attribute(attributes, fields, "RUC"),
            "GIS_RAZON_SOCIAL": _attribute(
                attributes,
                fields,
                "RAZON_SOCIAL",
                "RAZON_SOCI",
                "RAZON_SOC",
                "ADMINISTRA",
            ),
            "GIS_NOMBRE_COMERCIAL": _attribute(
                attributes,
                fields,
                "NOMBRE_COMERCIAL",
                "ESTABLEC_O",
                "UNIDAD_FIS",
            ),
            "GIS_ACTIVIDAD": _attribute(attributes, fields, "ACTIVIDAD"),
            "GIS_DIRECCION": _attribute(attributes, fields, "DIRECCION"),
            "GIS_DEPARTAMENTO": _attribute(
                attributes,
                fields,
                "DEPARTAMENTO",
                "DEPARTAMEN",
                "DPTO",
            ),
            "GIS_PROVINCIA": _attribute(attributes, fields, "PROVINCIA"),
            "GIS_DISTRITO": _attribute(attributes, fields, "DISTRITO"),
            "GIS_REGISTRO": register,
            "GIS_CODIGO_OSINERGMIN": code,
            "LONGITUD": float(longitude) if coordinates_valid else pd.NA,
            "LATITUD": float(latitude) if coordinates_valid else pd.NA,
            "COORDINATE_VALID": coordinates_valid,
            "GEO_SOURCE_LAYER": str(metadata.get("name") or metadata.get("id")),
            "GEO_LAYER_ID": int(metadata["id"]),
            "GEO_OBJECTID": attributes.get(object_id_field),
            "GEO_UPDATED_AT": updated_at,
        }
        rows.append(row)

    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "GIS_RUC",
                "GIS_RAZON_SOCIAL",
                "GIS_NOMBRE_COMERCIAL",
                "GIS_ACTIVIDAD",
                "GIS_DIRECCION",
                "GIS_DEPARTAMENTO",
                "GIS_PROVINCIA",
                "GIS_DISTRITO",
                "GIS_REGISTRO",
                "GIS_CODIGO_OSINERGMIN",
                "LONGITUD",
                "LATITUD",
                "COORDINATE_VALID",
                "GEO_SOURCE_LAYER",
                "GEO_LAYER_ID",
                "GEO_OBJECTID",
                "GEO_UPDATED_AT",
            ]
        )
    return _add_gis_normalized_columns(frame)


def _first_normalized(
    frame: pd.DataFrame,
    columns: Sequence[str],
    normalizer: Any,
) -> pd.Series:
    result = pd.Series("", index=frame.index, dtype="string")
    for column in columns:
        if column not in frame.columns:
            continue
        normalized = frame[column].map(normalizer).astype("string")
        result = result.mask(result.eq("") & normalized.ne(""), normalized)
    return result.fillna("")


def _add_gis_normalized_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["_N_CODE"] = frame["GIS_CODIGO_OSINERGMIN"].map(
        normalize_official_code
    )
    frame["_N_REGISTER"] = frame["GIS_REGISTRO"].map(normalize_register)
    frame["_N_RUC"] = frame["GIS_RUC"].map(normalize_ruc)
    frame["_N_ADDRESS"] = frame["GIS_DIRECCION"].map(normalize_address)
    frame["_N_DEPARTMENT"] = frame["GIS_DEPARTAMENTO"].map(normalize_location)
    frame["_N_PROVINCE"] = frame["GIS_PROVINCIA"].map(normalize_location)
    frame["_N_DISTRICT"] = frame["GIS_DISTRITO"].map(normalize_location)
    frame["_N_ACTIVITY"] = frame["GIS_ACTIVIDAD"].map(normalize_text)
    frame["_N_NAME"] = _first_normalized(
        frame,
        ["GIS_NOMBRE_COMERCIAL", "GIS_RAZON_SOCIAL"],
        normalize_business_name,
    )
    return frame


def build_establishment_dimension(current: pd.DataFrame) -> pd.DataFrame:
    if "ESTABLECIMIENTO_KEY" not in current.columns:
        raise ValueError("estado_actual.csv no contiene ESTABLECIMIENTO_KEY")

    work = current.copy()
    work["_FECHA_SORT"] = pd.to_datetime(
        work.get("FECHA_PRECIO"), errors="coerce"
    )
    work["_CAPTURA_SORT"] = pd.to_datetime(
        work.get("FECHA_CAPTURA"), errors="coerce"
    )
    work = work.sort_values(
        ["_FECHA_SORT", "_CAPTURA_SORT"],
        kind="stable",
        na_position="first",
    )
    work = work.drop_duplicates("ESTABLECIMIENTO_KEY", keep="last").copy()

    output = pd.DataFrame(index=work.index)
    for column in BUSINESS_COLUMNS:
        if column in work.columns:
            output[column] = work[column]

    output["_N_CODE"] = _first_normalized(
        work,
        ["CODIGO_OSINERG", "CODIGO_OSINERGMIN"],
        normalize_official_code,
    )
    output["_N_REGISTER"] = _first_normalized(
        work,
        ["NRO_REGISTRO", "REGISTRO_DE_HIDROCARBUROS"],
        normalize_register,
    )
    output["_N_RUC"] = work.get("RUC", pd.Series(index=work.index)).map(
        normalize_ruc
    )
    output["_N_ADDRESS"] = work.get(
        "DIRECCION", pd.Series(index=work.index)
    ).map(normalize_address)
    output["_N_DEPARTMENT"] = work.get(
        "DEPARTAMENTO", pd.Series(index=work.index)
    ).map(normalize_location)
    output["_N_PROVINCE"] = work.get(
        "PROVINCIA", pd.Series(index=work.index)
    ).map(normalize_location)
    output["_N_DISTRICT"] = work.get(
        "DISTRITO", pd.Series(index=work.index)
    ).map(normalize_location)
    output["_N_ACTIVITY"] = work.get(
        "ACTIVIDAD", pd.Series(index=work.index)
    ).map(normalize_text)
    output["_N_NAME"] = _first_normalized(
        work,
        ["NOMBRE_COMERCIAL", "RAZON_SOCIAL"],
        normalize_business_name,
    )
    return output.reset_index(drop=True)


def _index_values(
    frame: pd.DataFrame,
    columns: Sequence[str],
) -> dict[tuple[str, ...], list[int]]:
    index: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for row_id, row in frame.iterrows():
        key = tuple(_as_text(row[column]) for column in columns)
        if all(key):
            index[key].append(int(row_id))
    return index


def _refine_candidates(
    source: pd.Series,
    gis: pd.DataFrame,
    candidates: list[int],
) -> list[int]:
    refinements = [
        ["_N_REGISTER"],
        ["_N_RUC", "_N_DEPARTMENT", "_N_PROVINCE", "_N_DISTRICT", "_N_ADDRESS"],
        ["_N_DEPARTMENT", "_N_PROVINCE", "_N_DISTRICT", "_N_ADDRESS"],
    ]
    remaining = candidates
    for columns in refinements:
        values = [_as_text(source.get(column)) for column in columns]
        if not all(values):
            continue
        narrowed = [
            row_id
            for row_id in remaining
            if all(_as_text(gis.at[row_id, column]) == value for column, value in zip(columns, values))
        ]
        if narrowed:
            remaining = narrowed
        if len(remaining) == 1:
            break
    return remaining


def _fuzzy_candidate(
    source: pd.Series,
    gis: pd.DataFrame,
    location_index: Mapping[tuple[str, ...], list[int]],
) -> tuple[int, float] | None:
    location = tuple(
        _as_text(source.get(column))
        for column in ("_N_DEPARTMENT", "_N_PROVINCE", "_N_DISTRICT")
    )
    source_name = _as_text(source.get("_N_NAME"))
    source_address = _as_text(source.get("_N_ADDRESS"))
    if not all(location) or not source_name or not source_address:
        return None

    scored: list[tuple[float, int]] = []
    for row_id in location_index.get(location, []):
        gis_name = _as_text(gis.at[row_id, "_N_NAME"])
        gis_address = _as_text(gis.at[row_id, "_N_ADDRESS"])
        if not gis_name or not gis_address:
            continue
        name_score = SequenceMatcher(None, source_name, gis_name).ratio()
        address_score = SequenceMatcher(None, source_address, gis_address).ratio()
        score = 0.65 * name_score + 0.35 * address_score
        scored.append((score, row_id))

    scored.sort(reverse=True)
    if not scored or scored[0][0] < 0.93:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None
    return scored[0][1], round(scored[0][0], 4)


@dataclass
class MatchDiagnostics:
    ambiguous: int
    used_gis_rows: set[int]
    ambiguous_keys: set[str]


def match_establishments(
    establishments: pd.DataFrame,
    gis_records: pd.DataFrame,
    updated_at: str,
) -> tuple[pd.DataFrame, MatchDiagnostics]:
    establishments = establishments.copy().reset_index(drop=True)
    if gis_records.empty:
        gis = _add_gis_normalized_columns(
            pd.DataFrame(
                columns=[
                    "GIS_CODIGO_OSINERGMIN",
                    "GIS_REGISTRO",
                    "GIS_RUC",
                    "GIS_DIRECCION",
                    "GIS_DEPARTAMENTO",
                    "GIS_PROVINCIA",
                    "GIS_DISTRITO",
                    "GIS_ACTIVIDAD",
                    "GIS_NOMBRE_COMERCIAL",
                    "GIS_RAZON_SOCIAL",
                ]
            )
        )
    else:
        gis = gis_records.copy().reset_index(drop=True)
        if "_N_CODE" not in gis.columns:
            gis = _add_gis_normalized_columns(gis)
        if "COORDINATE_VALID" in gis.columns:
            gis = gis.loc[gis["COORDINATE_VALID"].fillna(False)].reset_index(drop=True)

    indexes = {
        "code": _index_values(gis, ["_N_CODE"]),
        "register": _index_values(gis, ["_N_REGISTER"]),
        "ruc_ubigeo_address": _index_values(
            gis,
            ["_N_RUC", "_N_DEPARTMENT", "_N_PROVINCE", "_N_DISTRICT", "_N_ADDRESS"],
        ),
        "ruc_address": _index_values(gis, ["_N_RUC", "_N_ADDRESS"]),
        "location": _index_values(
            gis, ["_N_DEPARTMENT", "_N_PROVINCE", "_N_DISTRICT"]
        ),
    }

    geo_values: list[dict[str, object]] = []
    used: set[int] = set()
    ambiguous_keys: set[str] = set()

    for _, source in establishments.iterrows():
        matched_row: int | None = None
        method = "UNMATCHED"
        score: float | None = None
        is_ambiguous = False

        hierarchy = [
            ("code", (_as_text(source.get("_N_CODE")),), "CODIGO_OSINERGMIN"),
            ("register", (_as_text(source.get("_N_REGISTER")),), "REGISTRO"),
            (
                "ruc_ubigeo_address",
                tuple(
                    _as_text(source.get(column))
                    for column in (
                        "_N_RUC",
                        "_N_DEPARTMENT",
                        "_N_PROVINCE",
                        "_N_DISTRICT",
                        "_N_ADDRESS",
                    )
                ),
                "RUC_UBIGEO_DIRECCION",
            ),
            (
                "ruc_address",
                tuple(
                    _as_text(source.get(column))
                    for column in ("_N_RUC", "_N_ADDRESS")
                ),
                "RUC_DIRECCION",
            ),
        ]

        for index_name, key, candidate_method in hierarchy:
            if not all(key):
                continue
            candidates = list(indexes[index_name].get(key, []))
            if not candidates:
                continue
            if len(candidates) > 1:
                candidates = _refine_candidates(source, gis, candidates)
            if len(candidates) == 1:
                matched_row = candidates[0]
                method = candidate_method
                score = 1.0
            else:
                is_ambiguous = True
            break

        if matched_row is None and not is_ambiguous:
            fuzzy = _fuzzy_candidate(source, gis, indexes["location"])
            if fuzzy:
                matched_row, score = fuzzy
                method = "FUZZY_VALIDATED"

        if matched_row is None:
            if is_ambiguous:
                ambiguous_keys.add(str(source["ESTABLECIMIENTO_KEY"]))
            geo_values.append(
                {
                    "LATITUD": pd.NA,
                    "LONGITUD": pd.NA,
                    "GEO_MATCHED": False,
                    "GEO_MATCH_METHOD": "UNMATCHED",
                    "GEO_MATCH_SCORE": pd.NA,
                    "GEO_SOURCE_LAYER": pd.NA,
                    "GEO_OBJECTID": pd.NA,
                    "GEO_UPDATED_AT": updated_at,
                }
            )
            continue

        used.add(matched_row)
        matched = gis.loc[matched_row]
        geo_values.append(
            {
                "LATITUD": float(matched["LATITUD"]),
                "LONGITUD": float(matched["LONGITUD"]),
                "GEO_MATCHED": True,
                "GEO_MATCH_METHOD": method,
                "GEO_MATCH_SCORE": score,
                "GEO_SOURCE_LAYER": matched["GEO_SOURCE_LAYER"],
                "GEO_OBJECTID": matched["GEO_OBJECTID"],
                "GEO_UPDATED_AT": updated_at,
            }
        )

    geo_frame = pd.DataFrame(geo_values, index=establishments.index)
    business = establishments[
        [column for column in BUSINESS_COLUMNS if column in establishments.columns]
    ].copy()
    result = pd.concat([business, geo_frame], axis=1)
    for column in BUSINESS_COLUMNS:
        if column in result.columns:
            result[column] = result[column].astype("string")
    for column in ("GEO_MATCH_METHOD", "GEO_SOURCE_LAYER", "GEO_UPDATED_AT"):
        result[column] = result[column].astype("string")
    result["LATITUD"] = pd.to_numeric(result["LATITUD"], errors="coerce")
    result["LONGITUD"] = pd.to_numeric(result["LONGITUD"], errors="coerce")
    result["GEO_MATCHED"] = result["GEO_MATCHED"].astype(bool)
    result["GEO_MATCH_SCORE"] = pd.to_numeric(
        result["GEO_MATCH_SCORE"], errors="coerce"
    ).astype("Float64")
    result["GEO_OBJECTID"] = pd.to_numeric(
        result["GEO_OBJECTID"], errors="coerce"
    ).astype("Int64")
    if result["ESTABLECIMIENTO_KEY"].duplicated().any():
        raise RuntimeError("La dimension contiene ESTABLECIMIENTO_KEY duplicado")

    diagnostics = MatchDiagnostics(
        ambiguous=len(ambiguous_keys),
        used_gis_rows=used,
        ambiguous_keys=ambiguous_keys,
    )
    return result, diagnostics


def audit_point_layers(
    client: ArcGISClient,
    source_activities: Iterable[object],
) -> tuple[list[dict[str, object]], list[dict[str, Any]]]:
    source_normalized = {
        normalize_text(value) for value in source_activities if normalize_text(value)
    }
    audit: list[dict[str, object]] = []
    selected: list[dict[str, Any]] = []

    for summary in client.list_layers():
        if summary.get("geometryType") != "esriGeometryPoint":
            continue
        metadata = client.layer_metadata(int(summary["id"]))
        field_names = [str(field["name"]) for field in metadata.get("fields", [])]
        activity_field = next(
            (field for field in field_names if field.upper() == "ACTIVIDAD"),
            None,
        )
        activities = (
            client.distinct_values(int(summary["id"]), activity_field)
            if activity_field
            else []
        )
        activity_map = {
            normalize_text(value): _as_text(value)
            for value in activities
            if normalize_text(value)
        }
        overlap = sorted(source_normalized.intersection(activity_map))
        is_selected = bool(overlap)
        audit.append(
            {
                "id": int(summary["id"]),
                "name": str(summary["name"]),
                "fields": field_names,
                "activity_values": sorted(activity_map.values()),
                "matching_activities": [activity_map[value] for value in overlap],
                "selected": is_selected,
                "selection_reason": (
                    "ACTIVIDAD presente en estado_actual.csv"
                    if is_selected
                    else (
                        "Sin coincidencia de ACTIVIDAD"
                        if activity_field
                        else "Sin campo ACTIVIDAD comparable"
                    )
                ),
            }
        )
        if is_selected:
            selected.append(metadata)

    return audit, selected


def _feature_fingerprint(
    layers: Sequence[tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]],
) -> str:
    canonical = [
        {
            "id": metadata["id"],
            "name": metadata.get("name"),
            "features": list(features),
        }
        for metadata, features in layers
    ]
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _coverage_breakdown(
    frame: pd.DataFrame,
    column: str,
) -> dict[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    if column not in frame.columns:
        return output
    values = frame[column].fillna("<SIN DATO>").astype(str)
    for value in sorted(values.unique()):
        mask = values.eq(value)
        total = int(mask.sum())
        matched = int(frame.loc[mask, "GEO_MATCHED"].sum())
        output[value] = {
            "total": total,
            "matched": matched,
            "unmatched": total - matched,
            "coverage_percent": round(100 * matched / total, 2) if total else 0.0,
        }
    return output


def build_report(
    dimension: pd.DataFrame,
    diagnostics: MatchDiagnostics,
    layer_audit: Sequence[Mapping[str, object]],
    layer_downloads: Sequence[Mapping[str, object]],
    gis_total: int,
    invalid_coordinates: int,
    source_fingerprint: str,
    updated_at: str,
) -> dict[str, object]:
    total = len(dimension)
    matched = int(dimension["GEO_MATCHED"].sum())
    methods = (
        dimension["GEO_MATCH_METHOD"].fillna("UNMATCHED").value_counts().to_dict()
    )
    approximate = int(methods.get("FUZZY_VALIDATED", 0))
    exact = matched - approximate
    return {
        "updated_at": updated_at,
        "source": ARCGIS_SERVICE_URL,
        "source_fingerprint": source_fingerprint,
        "gis_layers": list(layer_downloads),
        "point_layer_audit": list(layer_audit),
        "gis_records_total": gis_total,
        "gis_records_invalid_coordinates": invalid_coordinates,
        "gis_without_match": gis_total - len(diagnostics.used_gis_rows),
        "establishments_total": total,
        "establishments_with_coordinates": matched,
        "establishments_without_coordinates": total - matched,
        "coverage_percent": round(100 * matched / total, 2) if total else 0.0,
        "exact_matches": exact,
        "approximate_matches": approximate,
        "ambiguous_matches": diagnostics.ambiguous,
        "matches_by_method": {str(key): int(value) for key, value in methods.items()},
        "coverage_by_activity": _coverage_breakdown(dimension, "ACTIVIDAD"),
        "coverage_by_department": _coverage_breakdown(dimension, "DEPARTAMENTO"),
    }


def _load_previous_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_text_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return True


def _write_parquet_if_changed(path: Path, frame: pd.DataFrame) -> bool:
    if path.exists():
        existing = pd.read_parquet(path)
        try:
            pd.testing.assert_frame_equal(
                existing.convert_dtypes().reset_index(drop=True),
                frame.convert_dtypes().reset_index(drop=True),
                check_dtype=False,
                check_like=False,
            )
            return False
        except AssertionError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temp, index=False, compression="zstd")
    temp.replace(path)
    return True


@dataclass
class GeodataRunResult:
    report: dict[str, object]
    dimension: pd.DataFrame
    changed_files: list[Path]


def write_geodata_outputs(
    dimension: pd.DataFrame,
    report: dict[str, object],
    ambiguous_keys: set[str],
    output_dir: Path = BI_DIR,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    dimension_path = output_dir / DIM_ESTABLISHMENT_FILE.name
    report_path = output_dir / GEOCODING_REPORT_FILE.name
    unmatched_path = output_dir / GEOCODING_UNMATCHED_FILE.name

    changed: list[Path] = []
    if _write_parquet_if_changed(dimension_path, dimension):
        changed.append(dimension_path)

    report_text = json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    if _write_text_if_changed(report_path, report_text):
        changed.append(report_path)

    unmatched = dimension.loc[~dimension["GEO_MATCHED"]].copy()
    unmatched["GEO_UNMATCHED_REASON"] = unmatched["ESTABLECIMIENTO_KEY"].map(
        lambda key: "AMBIGUOUS" if str(key) in ambiguous_keys else "NO_MATCH"
    )
    unmatched_text = unmatched.to_csv(index=False, lineterminator="\n")
    if _write_text_if_changed(unmatched_path, unmatched_text):
        changed.append(unmatched_path)
    return changed


def run_geodata(
    current_file: Path = CURRENT_FILE,
    output_dir: Path = BI_DIR,
    client: ArcGISClient | None = None,
) -> GeodataRunResult:
    client = client or ArcGISClient()
    current = pd.read_csv(current_file, low_memory=False)
    establishments = build_establishment_dimension(current)

    layer_audit, selected_metadata = audit_point_layers(
        client,
        establishments.get("ACTIVIDAD", pd.Series(dtype="string")),
    )
    if not selected_metadata:
        raise RuntimeError(
            "Ninguna capa puntual GIS comparte actividades con estado_actual.csv"
        )

    downloaded: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for metadata in selected_metadata:
        features = client.fetch_layer_features(int(metadata["id"]), metadata)
        downloaded.append((metadata, features))

    fingerprint = _feature_fingerprint(downloaded)
    previous = _load_previous_report(output_dir / GEOCODING_REPORT_FILE.name)
    if previous.get("source_fingerprint") == fingerprint:
        updated_at = str(previous.get("updated_at"))
    else:
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    gis_frames: list[pd.DataFrame] = []
    layer_downloads: list[dict[str, object]] = []
    invalid_coordinates = 0
    for metadata, features in downloaded:
        frame = features_to_frame(features, metadata, updated_at)
        gis_frames.append(frame)
        valid = int(frame.get("COORDINATE_VALID", pd.Series(dtype=bool)).sum())
        invalid_coordinates += len(frame) - valid
        layer_downloads.append(
            {
                "id": int(metadata["id"]),
                "name": str(metadata["name"]),
                "records": len(frame),
                "valid_coordinates": valid,
                "invalid_coordinates": len(frame) - valid,
            }
        )

    gis = pd.concat(gis_frames, ignore_index=True, sort=False)
    dimension, diagnostics = match_establishments(
        establishments,
        gis,
        updated_at,
    )
    report = build_report(
        dimension=dimension,
        diagnostics=diagnostics,
        layer_audit=layer_audit,
        layer_downloads=layer_downloads,
        gis_total=len(gis),
        invalid_coordinates=invalid_coordinates,
        source_fingerprint=fingerprint,
        updated_at=updated_at,
    )
    changed = write_geodata_outputs(
        dimension,
        report,
        diagnostics.ambiguous_keys,
        output_dir,
    )
    return GeodataRunResult(report=report, dimension=dimension, changed_files=changed)
