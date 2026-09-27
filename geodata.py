from __future__ import annotations

from src.geodata import run_geodata


def main() -> None:
    result = run_geodata()
    report = result.report

    print("\n=== GEOCODIFICACION DE ESTABLECIMIENTOS ===")
    for layer in report["gis_layers"]:
        print(
            f"Layer {layer['id']} - {layer['name']}: "
            f"{layer['records']:,} registros"
        )
    print(f"Total establecimientos: {report['establishments_total']:,}")
    print(f"Con coordenadas: {report['establishments_with_coordinates']:,}")
    print(f"Sin coordenadas: {report['establishments_without_coordinates']:,}")
    print(f"Cobertura: {report['coverage_percent']:.2f} %")
    print(f"Ambiguos: {report['ambiguous_matches']:,}")
    print(f"Registros GIS sin correspondencia: {report['gis_without_match']:,}")

    if result.changed_files:
        print("Archivos actualizados:")
        for path in result.changed_files:
            print(f"  - {path}")
    else:
        print("Sin cambios en los artefactos GIS.")


if __name__ == "__main__":
    main()
