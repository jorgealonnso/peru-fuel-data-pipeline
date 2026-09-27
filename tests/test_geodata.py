from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.geodata import (
    ArcGISClient,
    build_establishment_dimension,
    build_report,
    features_to_frame,
    match_establishments,
    normalize_address,
    normalize_official_code,
    normalize_ruc,
    valid_wgs84_coordinates,
    write_geodata_outputs,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        if params.get("returnIdsOnly") == "true":
            return FakeResponse({"objectIds": [1, 2, 3]})
        object_ids = [int(value) for value in params["objectIds"].split(",")]
        return FakeResponse(
            {
                "features": [
                    {
                        "attributes": {"OBJECTID": object_id},
                        "geometry": {"x": -77.0, "y": -12.0},
                    }
                    for object_id in object_ids
                ]
            }
        )


def _metadata(layer_id=35, name="Grifo y EESS"):
    return {
        "id": layer_id,
        "name": name,
        "objectIdField": "OBJECTID",
        "maxRecordCount": 2,
        "fields": [
            {"name": "OBJECTID"},
            {"name": "N"},
            {"name": "RUC"},
            {"name": "ACTIVIDAD"},
            {"name": "DIRECCION"},
            {"name": "DEPARTAMENTO"},
            {"name": "PROVINCIA"},
            {"name": "DISTRITO"},
        ],
    }


def _current_row(key="ID:100010"):
    return {
        "ESTABLECIMIENTO_KEY": key,
        "ID_ESTABLECIMIENTO": "100010",
        "CODIGO_OSINERG": "100010.0",
        "NRO_REGISTRO": "100010-056-261225",
        "RUC": "20494106893",
        "RAZON_SOCIAL": "ECOX S.A.C.",
        "ACTIVIDAD": "ESTACION DE SERVICIOS / GRIFOS",
        "DIRECCION": "Jr. Mariscal Ramon Castilla Nro. 671",
        "DEPARTAMENTO": "SAN MARTIN",
        "PROVINCIA": "TOCACHE",
        "DISTRITO": "TOCACHE",
        "FECHA_PRECIO": "2026-09-26 12:00:00",
        "FECHA_CAPTURA": "2026-09-26T14:00:00-05:00",
    }


def _gis_feature(object_id=1, code="100010-056-261225", x=-76.5, y=-8.2):
    return {
        "attributes": {
            "OBJECTID": object_id,
            "N": code,
            "RUC": "20494106893",
            "ACTIVIDAD": "ESTACION DE SERVICIOS / GRIFOS",
            "DIRECCION": "JR MARISCAL RAMON CASTILLA 671",
            "DEPARTAMENTO": "SAN MARTIN",
            "PROVINCIA": "TOCACHE",
            "DISTRITO": "TOCACHE",
        },
        "geometry": {"x": x, "y": y},
    }


def test_arcgis_downloads_all_object_ids_in_batches():
    session = FakeSession()
    client = ArcGISClient("https://example.test/FeatureServer", session, page_size=2)

    features = client.fetch_layer_features(35, _metadata())

    assert [feature["attributes"]["OBJECTID"] for feature in features] == [1, 2, 3]
    batch_calls = [params for _, params in session.calls if "objectIds" in params]
    assert [params["objectIds"] for params in batch_calls] == ["1,2", "3"]
    assert all(params["outSR"] == "4326" for params in batch_calls)


def test_geometry_x_is_longitude_and_y_is_latitude():
    frame = features_to_frame([_gis_feature(x=-76.5, y=-8.2)], _metadata(), "2026-09-26T00:00:00+00:00")

    assert frame.loc[0, "LONGITUD"] == -76.5
    assert frame.loc[0, "LATITUD"] == -8.2


def test_coordinate_validation_rejects_swapped_or_non_peruvian_values():
    assert valid_wgs84_coordinates(-77.0, -12.0)
    assert not valid_wgs84_coordinates(-12.0, -77.0)
    assert not valid_wgs84_coordinates(500.0, -12.0)


def test_official_code_normalization():
    assert normalize_official_code("0000018-ICA") == "0000018"
    assert normalize_official_code("100010.0") == "100010"
    assert normalize_official_code("100010-056-261225") == "100010"


def test_official_codes_with_different_zero_padding_do_not_collide():
    assert normalize_official_code("0000018-ICA") != normalize_official_code(
        "0018-EGLP-15-2007"
    )


def test_legacy_register_prefix_is_not_treated_as_official_code():
    row = _current_row()
    row["CODIGO_OSINERG"] = pd.NA
    row["ID_ESTABLECIMIENTO"] = "0002-GRIF-25-2003"
    row["NRO_REGISTRO"] = pd.NA
    row["REGISTRO_DE_HIDROCARBUROS"] = "0002-GRIF-25-2003"

    dimension = build_establishment_dimension(pd.DataFrame([row]))

    assert dimension.loc[0, "_N_CODE"] == ""
    assert dimension.loc[0, "_N_REGISTER"] == "0002GRIF252003"


def test_ruc_normalization():
    assert normalize_ruc("20 494 106 893") == "20494106893"
    assert normalize_ruc("20494106893.0") == "20494106893"
    assert normalize_ruc("123") == ""


def test_address_normalization():
    assert normalize_address("  Av. Perú Nº  123, Lima ") == "AV PERU N 123 LIMA"


def test_exact_official_code_match():
    establishments = build_establishment_dimension(pd.DataFrame([_current_row()]))
    gis = features_to_frame([_gis_feature()], _metadata(), "2026-09-26T00:00:00+00:00")

    result, diagnostics = match_establishments(establishments, gis, "2026-09-26T00:00:00+00:00")

    assert result.loc[0, "GEO_MATCHED"]
    assert result.loc[0, "GEO_MATCH_METHOD"] == "CODIGO_OSINERGMIN"
    assert result.loc[0, "GEO_MATCH_SCORE"] == 1.0
    assert diagnostics.ambiguous == 0


def test_ambiguous_official_code_is_not_forced():
    source = _current_row()
    source["NRO_REGISTRO"] = pd.NA
    establishments = build_establishment_dimension(pd.DataFrame([source]))
    second = _gis_feature(object_id=2, x=-75.0, y=-9.0)
    second["attributes"]["N"] = "100010-999-010101"
    gis = features_to_frame([_gis_feature(), second], _metadata(), "2026-09-26T00:00:00+00:00")

    result, diagnostics = match_establishments(establishments, gis, "2026-09-26T00:00:00+00:00")

    assert not result.loc[0, "GEO_MATCHED"]
    assert result.loc[0, "GEO_MATCH_METHOD"] == "UNMATCHED"
    assert diagnostics.ambiguous == 1


def test_unmatched_establishment_remains_null():
    establishments = build_establishment_dimension(pd.DataFrame([_current_row()]))
    gis = features_to_frame([_gis_feature(code="999999-050-010101")], _metadata(), "2026-09-26T00:00:00+00:00")

    result, _ = match_establishments(establishments, gis, "2026-09-26T00:00:00+00:00")

    assert not result.loc[0, "GEO_MATCHED"]
    assert pd.isna(result.loc[0, "LATITUD"])
    assert pd.isna(result.loc[0, "LONGITUD"])


def test_dimension_has_one_row_per_establishment_key():
    older = _current_row()
    newer = _current_row()
    older["FECHA_PRECIO"] = "2026-09-25 10:00:00"
    newer["FECHA_PRECIO"] = "2026-09-26 10:00:00"
    newer["DIRECCION"] = "DIRECCION NUEVA"

    dimension = build_establishment_dimension(pd.DataFrame([older, newer]))

    assert len(dimension) == 1
    assert dimension.loc[0, "DIRECCION"] == "DIRECCION NUEVA"


def test_invalid_coordinate_cannot_be_matched():
    establishments = build_establishment_dimension(pd.DataFrame([_current_row()]))
    gis = features_to_frame([_gis_feature(x=-8.2, y=-76.5)], _metadata(), "2026-09-26T00:00:00+00:00")

    result, _ = match_establishments(establishments, gis, "2026-09-26T00:00:00+00:00")

    assert not result.loc[0, "GEO_MATCHED"]


def test_execution_without_gis_leaves_everything_unmatched():
    establishments = build_establishment_dimension(pd.DataFrame([_current_row()]))

    result, diagnostics = match_establishments(establishments, pd.DataFrame(), "2026-09-26T00:00:00+00:00")

    assert len(result) == 1
    assert not result.loc[0, "GEO_MATCHED"]
    assert diagnostics.used_gis_rows == set()


def test_output_writes_are_idempotent(tmp_path: Path):
    establishments = build_establishment_dimension(pd.DataFrame([_current_row()]))
    gis = features_to_frame([_gis_feature()], _metadata(), "2026-09-26T00:00:00+00:00")
    dimension, diagnostics = match_establishments(establishments, gis, "2026-09-26T00:00:00+00:00")
    report = build_report(
        dimension,
        diagnostics,
        [],
        [{"id": 35, "name": "Grifo y EESS", "records": 1}],
        1,
        0,
        "abc",
        "2026-09-26T00:00:00+00:00",
    )

    first = write_geodata_outputs(dimension, report, set(), tmp_path)
    second = write_geodata_outputs(dimension, report, set(), tmp_path)

    assert len(first) == 3
    assert second == []
