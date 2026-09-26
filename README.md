# Peru Fuel Data Pipeline

Pipeline en Python para consolidar y mantener precios de combustibles publicados por OSINERGMIN.

## Objetivo

Separar el proceso en dos etapas:

1. **Backfill histórico local**: descargar y procesar 2023, 2024, 2025 y 2026 una sola vez desde una PC con buena conexión.
2. **Actualización incremental**: consultar la fuente de precios recientes y registrar únicamente cambios nuevos.

La fecha de negocio se conserva como `FECHA_PRECIO`. La fecha en que el pipeline detecta el registro se guarda aparte como `FECHA_CAPTURA`.

## Fuente oficial

Página de documentos SCOP de OSINERGMIN:

https://www.osinergmin.gob.pe/empresas/hidrocarburos/scop/documentos-scop

La página publica archivos anuales de registro de precios de Combustibles Líquidos (CL) y una sección de “Registros de últimos precios”.

El código **no asume silenciosamente** que el ZIP anual es una fuente incremental eficiente. Para `update`, primero intenta encontrar una fuente de últimos precios. Si OSINERGMIN la sirve mediante JavaScript o un endpoint no visible en el HTML, se puede proporcionar mediante `--source-url` o la variable de entorno `FUEL_LATEST_URL`.

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Descubrir fuentes

```bash
python main.py --mode discover
```

## Carga histórica inicial

```bash
python main.py --mode backfill --years 2023 2024 2025 2026
```

Genera:

- `data/local/historico_precios.csv`: histórico completo para uso local.
- `data/history/YYYY-MM.csv`: particiones mensuales.
- `data/estado_actual.csv`: último precio conocido por establecimiento + producto.

## Actualización incremental

```bash
python main.py --mode update
```

También puedes indicar la fuente exacta:

```bash
python main.py --mode update --source-url "URL_PUBLICA"
```

O configurar:

```text
FUEL_LATEST_URL=URL_PUBLICA
```

## GitHub Actions

Cuatro cortes diarios, hora Lima:

- 06:00
- 10:00
- 14:00
- 17:00

Equivalentes UTC: 11:00, 15:00, 19:00 y 22:00.

El workflow también permite ejecución manual.

> Antes de depender de la automatización, ejecutar el backfill y una prueba de `update` localmente. Si “Registros de últimos precios” usa JavaScript, inspeccionar Network en el navegador y guardar la URL como Repository Variable `FUEL_LATEST_URL`.

## Datos

Columnas canónicas cuando existen en la fuente:

- `ID_ESTABLECIMIENTO`
- `RAZON_SOCIAL`
- `NOMBRE_COMERCIAL`
- `DIRECCION`
- `DISTRITO`
- `PROVINCIA`
- `DEPARTAMENTO`
- `LATITUD`
- `LONGITUD`
- `PRODUCTO`
- `PRECIO`
- `UNIDAD`
- `FECHA_PRECIO`
- `FECHA_CAPTURA`
- `ESTABLECIMIENTO_KEY`
- `DIAS_SIN_ACTUALIZAR`

El parser conserva además columnas originales normalizadas para no perder información útil.

## Estado

Base funcional inicial. Falta validar con una ejecución real la forma exacta en que OSINERGMIN expone actualmente “Registros de últimos precios”. El pipeline falla explícitamente si no encuentra esa fuente, en vez de descargar todo 2026 cuatro veces al día sin avisar.
