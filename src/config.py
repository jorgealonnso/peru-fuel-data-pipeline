from __future__ import annotations

import os
from pathlib import Path

SCOP_DOCUMENTS_URL = (
    "https://www.osinergmin.gob.pe/empresas/hidrocarburos/scop/documentos-scop"
)

HISTORICAL_URL_TEMPLATE = (
    "https://www.osinergmin.gob.pe/seccion/centro_documental/hidrocarburos/"
    "SCOP/SCOP-DOCS/{year}/Registro-precios/"
    "CL-Registro-precios-DMA-V-CCA-CCE-{year}.zip"
)

TARGET_YEARS = (2023, 2024, 2025, 2026)
TIMEZONE = "America/Lima"

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
LOCAL_DIR = DATA_DIR / "local"
HISTORY_DIR = DATA_DIR / "history"
INCREMENTAL_DIR = DATA_DIR / "incremental"
CURRENT_FILE = DATA_DIR / "estado_actual.csv"
LOCAL_HISTORY_FILE = LOCAL_DIR / "historico_precios_manifest.json"

LATEST_URL_ENV = "FUEL_LATEST_URL"
REQUEST_TIMEOUT = (15, 240)
USER_AGENT = (
    "Mozilla/5.0 (compatible; PeruFuelDataPipeline/1.0; "
    "+https://github.com/jorgealonnso/peru-fuel-data-pipeline)"
)


def configured_latest_url() -> str | None:
    value = os.getenv(LATEST_URL_ENV, "").strip()
    return value or None
