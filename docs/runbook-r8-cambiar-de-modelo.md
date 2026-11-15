# Runbook R8: cambiar de modelo

Comandos de PowerShell, en la raíz del repositorio, con la pila arriba (runbook R1). `dc` y `sql` como en el
runbook R1. El conjunto de referencia del banco vive fuera de git y del OneDrive, en la carpeta
que dice `HV_REFERENCIA` en `D:\hv\.env` (acá, `HV_REFERENCIA=D:\hv\referencia`). Si recién se
puso, `dc up -d web` para que `web` la monte: de ahí sale la nota del banco en la pantalla.

```powershell
function dc { docker compose --env-file D:\hv\.env @args }
function sql { dc exec -T postgres psql -U historia -d historia_viva -c @args }
```

«Runbook R8» es de operación; no es la rebanada R8 de construcción (R-37).

## Cuándo

Se quiere cambiar el modelo (o un parámetro como `num_ctx` o `num_thread`) de una de las seis
capacidades de `configuracion_capacidad`: `transcribir`, `describir_imagen`, `resumir`,
`embeber`, `responder` y `redactar_efemeride`. **Primero se corre el banco y recién después se
cambia la fila** en la pantalla de modelos (DEC-87, DEC-132): el banco elige el modelo.

## Pasos

1. **Bajar el candidato** en el Ollama de generación (los dos Ollama comparten `hvmvp_modelos`;
   el de embeddings solo lo lee):

   ```powershell
   dc exec ollama ollama pull <modelo>
   ```

   El candidato tiene que estar en `candidatos.csv` (`D:\hv\referencia\candidatos.csv` o, si no
   está, la del paquete) con `capacidad, modelo, origen_chino, parametros`.
2. **Correr el banco antes de tocar la fila**, con la cola frenada para que la medición no
   compita por la CPU:

   ```powershell
   dc run --rm web interruptor detener procesamiento_habilitado --motivo "Banco de <capacidad>"
   dc run --rm banco comparar --capacidad <capacidad> --modelo <vigente> --modelo <candidato>
   dc run --rm web interruptor reanudar procesamiento_habilitado --motivo "Terminó el banco"
   ```

   `banco comparar` cubre `resumir`, `describir_imagen` (3 repeticiones por defecto,
   `--repeticiones N`) y `embeber` (recall@10 con las consultas de ajuste). Escribe
   `comparar-<capacidad>-<AAAAMMDDTHHMMSS>.{json,csv,md}` en `D:\hv\referencia\corridas` e imprime
   la tabla con el ganador y el mejor no chino. El servicio `banco` monta la carpeta de solo
   lectura salvo `corridas`, y `dc up` no lo arranca. **Sin corrida del banco para esa capacidad no se
   cambia la fila**: `responder` espera a su banco (ticket 078) y `redactar_efemeride` a su
   medición; `transcribir` es local y no tiene modelo que cambiar.
3. **Decidir con la tabla:** se cambia solo al ganador; la pantalla de modelos lo muestra debajo
   del modelo, «Banco: ganador X · mejor no chino Y». Si el ganador es de origen chino, se
   anota también el mejor no chino como alternativa.
4. **Cambiar la fila en la pantalla de modelos:** `http://localhost/admin.html` → Modelos →
   Editar en la capacidad → modelo y parámetros → **Verificar** (valida sin guardar) →
   **Guardar**. La edición está validada y auditada (`modelo.cambio`, con antes y después). Si
   cambia el `num_ctx` de un modelo que comparten otras filas, la pantalla les cambia el mismo y
   lo dice.
5. **Lo encolado conserva el modelo viejo:** cada trabajo guarda su configuración al encolarse;
   el cambio aplica a los trabajos nuevos, y la propuesta de cada Pieza registra el modelo con el
   que se hizo.
6. **Si cambió `embeber`** (el modelo o un prefijo), la pantalla avisa «Reconstruí el índice con
   el comando reindexar»: runbook R13. Hasta reindexar, la búsqueda responde solo con lo ya
   reindexado e `indice_incompleto`.

## Verificación

- La pantalla, en la fila: «✓ Listo» y debajo el historial de la capacidad con el antes y el
  después.
- `dc run --rm modelos` termina con «Modelos listos.» sin bajar nada, y `/api/salud` con sesión
  trae `modelos_faltantes` vacío.
- Los trabajos nuevos usan el modelo nuevo y los ya encolados, el viejo:

  ```powershell
  sql "SELECT pieza_id, estado, encolado_el, configuracion_efectiva->'por_capacidad'->'<capacidad>'->>'modelo' AS modelo FROM acervo.trabajo_procesamiento ORDER BY encolado_el DESC LIMIT 5;"
  ```

- La auditoría: `sql "SELECT cuando, actor_etiqueta, objeto_id, antes, despues FROM acervo.auditoria WHERE accion = 'modelo.cambio' ORDER BY cuando DESC LIMIT 3;"`.

## Si falla

- **`ollama pull` no baja** (sin internet): no se cambia nada; se repite con red.
- **`banco comparar` sale con 1:** «No están en candidatos.csv …» (sumar el candidato a
  `D:\hv\referencia\candidatos.csv`) o la referencia no valida
  (`dc run --rm banco cargar-referencia` dice qué fila). Sin `HV_REFERENCIA` en el `.env` dice
  «Falta la variable de entorno HV_REFERENCIA.».
- **La pantalla no muestra «Banco: ganador …»:** falta `HV_REFERENCIA` en el `.env`, `web` no se
  recreó después de ponerla (`dc up -d web`) o la corrida se escribió con `--salida` fuera de
  `corridas`.
- **«No se guardó nada: la configuración no es válida.»** (422): la pantalla lista los errores
  (por ejemplo, `num_ctx` incompatible); se corrigen y se vuelve a Verificar.
- **«Otra persona guardó «…» mientras editabas.»** (409): la pantalla recarga la fila; se revisa
  y se guarda de nuevo.
- **«○ Falta descargar en el motor»** después de guardar: se guardó igual; paso 1 con ese modelo
  o `dc run --rm modelos`.
- **El modelo nuevo es peor en uso:** se vuelve a la fila anterior desde el historial (mismo
  paso 4, con los valores de «antes»); si era `embeber`, `reindexar` otra vez.

## Registro de ejecución

Lo ejecuta en la PC alguien que no lo escribió. Lo que falló se corrige en el texto de arriba.

| Fecha | Quién | Commit | Desvíos | Corregido en el texto |
|---|---|---|---|---|
| 2026-10-20 | Implementador del ticket 115 (ensayo sin Docker ni Ollama) | `c6a31d8` | No se ejecutó: sin Ollama no hay banco. Se verificó que existen `banco comparar --capacidad resumir\|describir_imagen\|embeber --modelo --repeticiones --salida`, `docker compose run -e -v`, el servicio `modelos` y la pantalla (110). `compose.yml` no pasa `HV_REFERENCIA` a ningún servicio: por eso el banco va con `-e` y `-v`, y la nota «Banco: ganador …» de la pantalla de modelos no aparece en la PC (`web` no ve las corridas); se decide con la tabla del paso 2. No cuenta como ejecución en la PC | Se sumaron `-e`/`-v` al paso 2 y la decisión por la tabla |
| | | | | |
