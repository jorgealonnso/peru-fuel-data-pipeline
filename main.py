from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd

from src.config import CURRENT_FILE, HISTORY_DIR, LOCAL_DIR, LOCAL_HISTORY_FILE, TARGET_YEARS
from src.downloader import discover_historical_urls, discover_latest_candidates, download_to_temp, fetch_scop_html
from src.incremental import (
    add_establishment_key,
    append_incremental_history,
    append_monthly_history,
    build_current,
    build_current_from_history,
    compact_closed_months,
    detect_new_history_events,
    history_partition_paths,
    read_history_partition,
)
from src.parser import parse_download


def _cleanup_download(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


def _save_current(frame: pd.DataFrame) -> None:
    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(CURRENT_FILE, index=False, encoding="utf-8-sig")


def _load_current() -> pd.DataFrame | None:
    if not CURRENT_FILE.exists():
        return None
    return pd.read_csv(CURRENT_FILE, low_memory=False)


def _print_quality_summary(history: pd.DataFrame) -> None:
    years = pd.to_datetime(history.get("FECHA_PRECIO"), errors="coerce").dt.year
    print("\n=== RESUMEN DE CALIDAD ===")
    for year in TARGET_YEARS:
        print(f"Registros {year}: {int((years == year).sum()):,}")

    unique_est = history["ESTABLECIMIENTO_KEY"].nunique(dropna=True) if "ESTABLECIMIENTO_KEY" in history.columns else 0
    products = history["PRODUCTO"].nunique(dropna=True) if "PRODUCTO" in history.columns else 0
    invalid_date = int(pd.to_datetime(history["FECHA_PRECIO"], errors="coerce").isna().sum()) if "FECHA_PRECIO" in history.columns else len(history)

    print(f"Establecimientos únicos: {unique_est:,}")
    print(f"Productos únicos: {products:,}")
    print(f"Registros históricos totales: {len(history):,}")
    print(f"Registros con fecha inválida: {invalid_date:,}")


def _summarize_history_files() -> dict[str, object]:
    summary: dict[str, object] = {
        "rows_by_year": {str(year): 0 for year in TARGET_YEARS},
        "rows_total": 0,
        "invalid_dates": 0,
        "unique_establishments": 0,
        "products": [],
        "partitions": [],
    }
    establishments: set[str] = set()
    products: set[str] = set()

    for path in history_partition_paths(HISTORY_DIR):
        frame = read_history_partition(path)
        dates = pd.to_datetime(frame.get("FECHA_PRECIO"), errors="coerce")
        summary["rows_total"] = int(summary["rows_total"]) + len(frame)
        summary["invalid_dates"] = int(summary["invalid_dates"]) + int(dates.isna().sum())
        for year, count in dates.dt.year.value_counts(dropna=True).items():
            key = str(int(year))
            rows_by_year = summary["rows_by_year"]
            assert isinstance(rows_by_year, dict)
            rows_by_year[key] = int(rows_by_year.get(key, 0)) + int(count)
        if "ESTABLECIMIENTO_KEY" in frame.columns:
            establishments.update(frame["ESTABLECIMIENTO_KEY"].dropna().astype(str).unique())
        if "PRODUCTO" in frame.columns:
            products.update(frame["PRODUCTO"].dropna().astype(str).unique())
        partitions = summary["partitions"]
        assert isinstance(partitions, list)
        partitions.append({"file": path.name, "rows": len(frame), "bytes": path.stat().st_size})

    summary["unique_establishments"] = len(establishments)
    summary["products"] = sorted(products)
    return summary


def _print_quality_summary_from_files(summary: dict[str, object]) -> None:
    print("\n=== RESUMEN DE CALIDAD ===")
    rows_by_year = summary["rows_by_year"]
    assert isinstance(rows_by_year, dict)
    for year in TARGET_YEARS:
        print(f"Registros {year}: {int(rows_by_year.get(str(year), 0)):,}")
    print(f"Establecimientos únicos: {int(summary['unique_establishments']):,}")
    products = summary["products"]
    assert isinstance(products, list)
    print(f"Productos únicos: {len(products):,}")
    print(f"Registros históricos totales: {int(summary['rows_total']):,}")
    print(f"Registros con fecha inválida: {int(summary['invalid_dates']):,}")


def run_discover() -> None:
    print("Consultando página oficial SCOP...")
    html = fetch_scop_html()
    historical = discover_historical_urls(list(TARGET_YEARS), html=html)
    latest = discover_latest_candidates(html=html)

    print("\nHistóricos CL DMA/V/CCA/CCE:")
    for year, url in historical.items():
        print(f"  {year}: {url}")

    print("\nCandidatos de últimos precios:")
    if latest:
        for url in latest:
            print(f"  - {url}")
    else:
        print("  No se detectó un archivo descargable en el HTML.")
        print("  Puede usar JavaScript. Inspecciona Network y usa --source-url.")


def run_backfill(years: list[int]) -> None:
    html = fetch_scop_html()
    urls = discover_historical_urls(years, html=html)
    total_removed = 0
    processed: list[dict[str, object]] = []

    for year in years:
        url = urls[year]
        print(f"\n[{year}] Descargando: {url}", flush=True)
        path = download_to_temp(url)
        try:
            print(f"[{year}] Procesando: {path.name}", flush=True)
            frame = parse_download(path)
            frame["ANIO_FUENTE"] = year
            frame["URL_FUENTE"] = url
            print(f"[{year}] Filas procesadas: {len(frame):,}", flush=True)
            print(f"[{year}] Consolidando claves y deduplicando eventos...", flush=True)
            frame = add_establishment_key(frame)
            before = len(frame)
            frame = frame.drop_duplicates(["ESTABLECIMIENTO_KEY", "PRODUCTO", "FECHA_PRECIO", "PRECIO"], keep="last")
            removed = before - len(frame)
            total_removed += removed
            print(f"[{year}] Duplicados exactos eliminados: {removed:,}", flush=True)
            print(f"[{year}] Escribiendo particiones Parquet en: {HISTORY_DIR}", flush=True)
            written = append_monthly_history(frame, HISTORY_DIR)
            processed.append({"year": year, "url": url, "rows": len(frame), "duplicates_removed": removed})
            print(f"[{year}] Particiones actualizadas: {len(written):,}", flush=True)
        finally:
            _cleanup_download(path)

    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    summary = _summarize_history_files()
    manifest = {
        "format": "monthly_parquet",
        "history_dir": str(HISTORY_DIR),
        "processed": processed,
        "summary": summary,
    }
    print(f"Escribiendo manifiesto local: {LOCAL_HISTORY_FILE}", flush=True)
    LOCAL_HISTORY_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Construyendo estado actual: {CURRENT_FILE}", flush=True)
    current = build_current_from_history(HISTORY_DIR)
    _save_current(current)

    _print_quality_summary_from_files(summary)
    print(f"Duplicados exactos eliminados: {total_removed:,}")
    print(f"\nManifiesto local: {LOCAL_HISTORY_FILE}")
    print(f"Estado actual: {CURRENT_FILE}")


def _choose_update_urls(cli_url: str | None) -> list[str]:
    if cli_url:
        return [url.strip() for url in cli_url.split(",") if url.strip()]

    from src.config import configured_latest_url

    env_url = configured_latest_url()
    if env_url:
        return [url.strip() for url in env_url.split(",") if url.strip()]

    html = fetch_scop_html()
    candidates = discover_latest_candidates(html=html)
    if candidates:
        return candidates

    raise RuntimeError(
        "No se pudo descubrir automáticamente la fuente de 'Registros de últimos precios'. "
        "No descargaré todo 2026 en cada corte. Inspecciona Network y usa --source-url "
        "o configura FUEL_LATEST_URL."
    )


def run_update(source_url: str | None) -> None:
    urls = _choose_update_urls(source_url)
    frames: list[pd.DataFrame] = []
    for url in urls:
        print(f"Fuente incremental: {url}")
        path = download_to_temp(url)
        try:
            frame = parse_download(path)
            frame["URL_FUENTE"] = url
            frames.append(frame)
        finally:
            _cleanup_download(path)

    snapshot = pd.concat(frames, ignore_index=True, sort=False)

    current = _load_current()
    changes = detect_new_history_events(snapshot, current)

    if changes.empty:
        print("Sin cambios nuevos. No se modifica ningún archivo.")
        return

    written = append_incremental_history(changes)

    combined = changes if current is None or current.empty else pd.concat([current, changes], ignore_index=True, sort=False)
    _save_current(build_current(combined))

    print(f"Cambios nuevos: {len(changes):,}")
    for path in written:
        print(f"Actualizado: {path}")
    print(f"Actualizado: {CURRENT_FILE}")


def run_compact() -> None:
    compacted = compact_closed_months()
    if not compacted:
        print("No hay meses cerrados pendientes de compactación.")
        return

    print(f"Meses compactados: {len(compacted):,}")
    for path in compacted:
        print(f"Compactado: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Peru fuel data pipeline")
    parser.add_argument(
        "--mode",
        choices=("discover", "backfill", "update", "compact"),
        required=True,
    )
    parser.add_argument("--years", nargs="+", type=int, default=list(TARGET_YEARS))
    parser.add_argument("--source-url", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "discover":
        run_discover()
    elif args.mode == "backfill":
        run_backfill(args.years)
    elif args.mode == "update":
        run_update(args.source_url)
    else:
        run_compact()


if __name__ == "__main__":
    main()
