from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pandas as pd

from src.config import CURRENT_FILE, HISTORY_DIR, LOCAL_DIR, LOCAL_HISTORY_FILE, TARGET_YEARS
from src.downloader import discover_historical_urls, discover_latest_candidates, download_to_temp, fetch_scop_html
from src.incremental import add_establishment_key, append_monthly_history, build_current, detect_changes
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
    frames: list[pd.DataFrame] = []

    for year in years:
        url = urls[year]
        print(f"\n[{year}] Descargando: {url}")
        path = download_to_temp(url)
        try:
            frame = parse_download(path)
            frame["ANIO_FUENTE"] = year
            frame["URL_FUENTE"] = url
            print(f"[{year}] Filas procesadas: {len(frame):,}")
            frames.append(frame)
        finally:
            _cleanup_download(path)

    history = add_establishment_key(pd.concat(frames, ignore_index=True, sort=False))

    sig_cols = [c for c in ("ESTABLECIMIENTO_KEY", "PRODUCTO", "FECHA_PRECIO", "PRECIO") if c in history.columns]
    before = len(history)
    if len(sig_cols) == 4:
        history = history.drop_duplicates(sig_cols, keep="last")
    removed = before - len(history)

    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    history.to_csv(LOCAL_HISTORY_FILE, index=False, encoding="utf-8-sig")
    append_monthly_history(history, HISTORY_DIR)

    current = build_current(history)
    _save_current(current)

    _print_quality_summary(history)
    print(f"Duplicados exactos eliminados: {removed:,}")
    print(f"\nHistórico local: {LOCAL_HISTORY_FILE}")
    print(f"Estado actual: {CURRENT_FILE}")


def _choose_update_url(cli_url: str | None) -> str:
    if cli_url:
        return cli_url

    from src.config import configured_latest_url

    env_url = configured_latest_url()
    if env_url:
        return env_url

    html = fetch_scop_html()
    candidates = discover_latest_candidates(html=html)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise RuntimeError("Hay múltiples candidatos. Ejecuta --mode discover y elige uno con --source-url.")

    raise RuntimeError(
        "No se pudo descubrir automáticamente la fuente de 'Registros de últimos precios'. "
        "No descargaré todo 2026 en cada corte. Inspecciona Network y usa --source-url "
        "o configura FUEL_LATEST_URL."
    )


def run_update(source_url: str | None) -> None:
    url = _choose_update_url(source_url)
    print(f"Fuente incremental: {url}")
    path = download_to_temp(url)
    try:
        snapshot = parse_download(path)
        snapshot["URL_FUENTE"] = url
    finally:
        _cleanup_download(path)

    current = _load_current()
    changes = detect_changes(snapshot, current)

    if changes.empty:
        print("Sin cambios nuevos.")
        if current is not None and not current.empty:
            _save_current(build_current(current))
        return

    written = append_monthly_history(changes)

    combined = changes if current is None or current.empty else pd.concat([current, changes], ignore_index=True, sort=False)
    _save_current(build_current(combined))

    print(f"Cambios nuevos: {len(changes):,}")
    for path in written:
        print(f"Actualizado: {path}")
    print(f"Actualizado: {CURRENT_FILE}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Peru fuel data pipeline")
    parser.add_argument("--mode", choices=("discover", "backfill", "update"), required=True)
    parser.add_argument("--years", nargs="+", type=int, default=list(TARGET_YEARS))
    parser.add_argument("--source-url", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "discover":
        run_discover()
    elif args.mode == "backfill":
        run_backfill(args.years)
    else:
        run_update(args.source_url)


if __name__ == "__main__":
    main()
