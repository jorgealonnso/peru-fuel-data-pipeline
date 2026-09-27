# Peru Fuel Data Pipeline

Pipeline en Python para consolidar y mantener precios de combustibles publicados por OSINERGMIN.

## Objetivo

El proyecto separa el proceso en dos etapas:

1. **Backfill histórico local**: descargar y procesar 2023, 2024, 2025 y 2026 una sola vez desde una PC con buena conexión.
2. **Actualización incremental automática**: consultar la fuente de precios recientes y registrar únicamente cambios nuevos.

La fecha de negocio se conserva como `FECHA_PRECIO`. La fecha en que el pipeline detecta el registro se guarda aparte como `FECHA_CAPTURA`.

## Fuente oficial

Página de documentos SCOP de OSINERGMIN:

https://www.osinergmin.gob.pe/empresas/hidrocarburos/scop/documentos-scop

El pipeline utiliza los archivos oficiales para el histórico y mantiene separada la lógica de actualización reciente.

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

En macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Descubrir fuentes

```bash
python main.py --mode discover
```

Este modo muestra las fuentes históricas detectadas y los candidatos disponibles para precios recientes.

## Carga histórica inicial

```bash
python main.py --mode backfill --years 2023 2024 2025 2026
```

La carga inicial genera:

- `data/history/YYYY-MM.parquet`: histórico mensual comprimido con ZSTD.
- `data/estado_actual.csv`: último precio conocido por establecimiento + producto.
- `data/local/historico_precios_manifest.json`: manifiesto local con resumen y control del backfill.

Los ZIP/XLSX descargados se procesan temporalmente y no se conservan.

## Actualización incremental

```bash
python main.py --mode update
```

También se puede indicar explícitamente una o varias fuentes:

```bash
python main.py --mode update --source-url "URL_1,URL_2"
```

O configurar la variable:

```text
FUEL_LATEST_URL=URL_1,URL_2
```

Cada ejecución:

- descarga únicamente la fuente de precios recientes;
- normaliza los registros;
- compara cada evento contra `estado_actual` y el histórico;
- evita duplicados por establecimiento + producto + fecha de precio + precio;
- agrega únicamente eventos nuevos.

## Estrategia de almacenamiento

El histórico inicial queda en **Parquet**, porque es compacto y eficiente para grandes volúmenes.

Las ejecuciones programadas **no reescriben los Parquet mensuales**. El repositorio separa dos capas:

```text
data/history/YYYY-MM.parquet      # histórico permanente / meses consolidados
data/incremental/YYYY-MM.csv      # staging temporal del mes activo
```

El CSV incremental es intencional y temporal: Git versiona muy bien cambios append-only en texto, mientras que reescribir un Parquet binario varias veces al día haría crecer el historial del repositorio innecesariamente.

El día 1 de cada mes GitHub Actions ejecuta automáticamente:

```bash
python main.py --mode compact
```

Ese proceso fusiona el staging del mes cerrado con su Parquet, elimina duplicados, reemplaza el Parquet mensual y borra el CSV temporal. Por ejemplo:

```text
data/history/2026-09.parquet
        +
data/incremental/2026-09.csv
        ↓
data/history/2026-09.parquet
```

Después de la compactación, `data/history/` vuelve a contener únicamente Parquet para ese mes. Octubre continúa en `data/incremental/2026-10.csv` hasta su propio cierre.

`data/estado_actual.csv` mantiene una sola fila por establecimiento + producto y se ordena de forma estable para minimizar los diffs de Git.

## GitHub Actions

El workflow `.github/workflows/update_fuel.yml` está configurado para cuatro cortes diarios en hora Lima:

- 06:00
- 10:00
- 14:00
- 17:00

Equivalentes UTC:

- 11:00
- 15:00
- 19:00
- 22:00

También permite ejecución manual desde GitHub Actions.

El workflow `.github/workflows/compact_history.yml` corre el **día 1 de cada mes a las 07:00 hora Lima**, después del corte de las 06:00, y compacta automáticamente todos los meses cerrados que tengan staging pendiente. Ambos workflows comparten el mismo grupo de concurrencia para evitar que actualización y compactación se ejecuten al mismo tiempo.

## Fechas

Dos campos deben mantenerse separados:

- `FECHA_PRECIO`: fecha/hora real informada por OSINERGMIN.
- `FECHA_CAPTURA`: fecha/hora en la que este pipeline detectó el registro.

Ejemplo:

```text
FECHA_PRECIO  = 2026-09-26 12:37:00
FECHA_CAPTURA = 2026-09-26 14:00:00-05:00
```

## Identificación del establecimiento

El pipeline prioriza el código oficial de OSINERGMIN.

También normaliza casos en los que Excel representa un código entero como `100010.0` y registros históricos compuestos como `100010-056-261225`, de modo que ambos se relacionen con la misma llave:

```text
ID:100010
```

Si no existe un código oficial utilizable, se genera una llave hash a partir de datos del establecimiento.

## Estado actual y Power BI

El archivo `data/estado_actual.csv` conserva `FECHA_PRECIO`.

`DIAS_SIN_ACTUALIZAR` **no se persiste físicamente** porque es un valor relativo que cambiaría para miles de filas todos los días aunque ningún precio haya cambiado. Debe calcularse en Power BI durante el refresh.

Ejemplo DAX:

```text
Dias sin actualizar =
DATEDIFF(
    'estado_actual'[FECHA_PRECIO],
    TODAY(),
    DAY
)
```

## Datos canónicos

Cuando la fuente los proporciona, el pipeline conserva:

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
- `URL_FUENTE`

## Estado del proyecto

El backfill 2023-2026 ya fue generado y versionado en particiones mensuales Parquet.

La actualización incremental ya fue validada ejecutándose directamente en GitHub Actions contra las fuentes de últimos precios de OSINERGMIN. Una segunda ejecución consecutiva no generó duplicados.

La compactación mensual también queda automatizada mediante GitHub Actions, por lo que no requiere una PC encendida ni intervención manual.

## Geodatos de establecimientos

La geocodificación se ejecuta de forma independiente del pipeline frecuente de
precios:

```bash
python geodata.py
```

El proceso inspecciona las capas puntuales del servicio oficial ArcGIS REST de
OSINERGMIN, compara sus actividades con las presentes en `estado_actual.csv` y
descarga únicamente las capas compatibles. La extracción solicita geometría en
WGS84 (`EPSG:4326`) y pagina por lotes de `OBJECTID`, por lo que no depende del
límite de registros de una respuesta.

La asignación de coordenadas es conservadora y prioriza:

1. código oficial OSINERGMIN;
2. registro oficial;
3. RUC + departamento + provincia + distrito + dirección normalizada;
4. RUC + dirección normalizada;
5. coincidencia aproximada validada y no ambigua de nombre y ubicación.

Una coincidencia ambigua se deja sin coordenadas. El proceso genera:

```text
data/bi/dim_establecimiento.parquet
data/bi/geocoding_report.json
data/bi/geocoding_unmatched.csv
```

`dim_establecimiento.parquet` contiene una fila por `ESTABLECIMIENTO_KEY` y es
la fuente geográfica recomendada para Power BI. `estado_actual.csv` continúa
siendo la fuente del precio vigente y las particiones mensuales continúan
siendo la fuente del histórico.

En el modelo de Power BI se deben conservar estas relaciones de filtro simple:

```text
DimEstablecimiento[ESTABLECIMIENTO_KEY] 1 -> * EstadoActual[ESTABLECIMIENTO_KEY]
DimEstablecimiento[ESTABLECIMIENTO_KEY] 1 -> * HistoricoPrecio[ESTABLECIMIENTO_KEY]
```

`LATITUD` y `LONGITUD` son números decimales y deben categorizarse como
`Latitude` y `Longitude`, respectivamente.

El workflow `.github/workflows/update_geodata.yml` permite ejecución manual y
se ejecuta mensualmente, el día 5 a las 08:30 hora Lima. Si la respuesta GIS no
cambió, conserva la marca de actualización y no reescribe los artefactos, por
lo que no crea commits innecesarios.
