# Runbook R13: reconstruir el índice

Comandos de PowerShell, en la raíz del repositorio. `dc` abrevia `docker compose --env-file D:\hv\.env`. El
índice (`acervo.data_fragmento`) es derivado de las Fichas Validadas: se puede borrar y
reconstruir sin perder nada del Acervo (DEC-143). Reconstruirlo entero tarda ≈ 40 min.

## Cuándo

- La búsqueda o la Conversación responden 503 «índice no disponible», o muestran el aviso
  `indice_incompleto` por más tiempo que el de una tanda de indexación.
- `/api/salud` → `indice` con `fallidas` o `de_otro_modelo` que no bajan.
- Después de cambiar el modelo de `embeber` (runbook R8), o de una restauración sin el índice
  (runbook R9).

## Pasos

1. **Las 3 consultas de referencia antes de tocar nada**, si la búsqueda todavía responde:

   ```powershell
   New-Item -ItemType Directory -Force D:\hv\consultas
   dc run --rm web consultas-de-referencia | Set-Content -Encoding utf8 D:\hv\consultas\antes.json
   ```

   Son tres búsquedas fijas (dos con texto y una por período, `busqueda.CONSULTAS_DE_REFERENCIA`);
   la última línea es el JSON con las Piezas de cada una. Si la búsqueda no responde, sirve el
   `.referencia.json` del último volcado (`D:\hv\volcados`), con lo que había entonces.
2. **El procesamiento tiene que estar andando:** `dc run --rm web interruptor estado`. Con
   `procesamiento_habilitado` en falso, `reindexar` se niega (sale con 1).
3. **Reconstruir:**

   ```powershell
   dc run --rm web reindexar
   ```

   Encola una indexación por Pieza Validada no retirada (o reusa la que ya tiene) y queda
   auditado (`indice.reconstruccion`). La corre `trabajador-indexacion`, que tiene que estar vivo
   (`/api/salud` → `trabajadores.indexacion.vivo`). Mientras tanto, la búsqueda y la Conversación
   responden con lo ya reindexado y el aviso `indice_incompleto` (DEC-02).
4. **Esperar a que termine:** `/api/salud` → `indice.pendientes` en 0 e `incompleto` en `false`;
   la consulta de la cola (`docs/despliegue-en-la-pc.md` §2) sin `indexacion` encolada ni en
   curso.

## Verificación

Las 3 consultas de referencia dan lo mismo que antes:

```powershell
dc run --rm -v D:\hv\consultas:/consultas:ro web consultas-de-referencia --comparar /consultas/antes.json
```

Sale con 0 y dice «Las 3 consultas de referencia dan lo esperado.» Si no, lista cada consulta
con las Piezas esperadas y las obtenidas. Con el `.referencia.json` de un volcado, montar
`D:\hv\volcados` y pasar ese archivo a `--comparar`: solo da lo mismo si desde el volcado no se
validó ni se retiró nada.

## Si falla

- **`reindexar` sale con 1 «procesamiento detenido»:** reanudarlo con motivo
  (`dc run --rm web interruptor reanudar procesamiento_habilitado --motivo "..."`) y repetir.
- **Quedan indexaciones `fallidas`:** `dc run --rm web reprocesar --fallidos --tipo indexacion`;
  si fallan por el modelo, `dc run --rm modelos` o runbook R10.
- **`de_otro_modelo` no baja:** la fila de `embeber` cambió y quedaron fragmentos del modelo
  anterior; `reindexar` otra vez con el modelo nuevo bajado.
- **Las consultas dan otras Piezas con el índice completo:** si entre el paso 1 y la verificación
  se validó, editó o retiró algo, es esperable (anotar cuáles). Si no, comparar el modelo de
  `embeber` con el de antes (runbook R8) y avisar: el índice no es el que era.
- **`consultas-de-referencia` sale con 1 «El índice no tiene fragmentos…»:** el índice sigue
  vacío; volver al paso 4.
