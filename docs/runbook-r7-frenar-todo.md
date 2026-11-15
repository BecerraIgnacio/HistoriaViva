# Runbook R7: frenar todo

El control 4 de ADR-0004 (DEC-88, DEC-136). Comandos de PowerShell, en la raíz del repositorio, con la pila arriba
(runbook R1). `dc` y `sql` como en el runbook R1:

```powershell
function dc { docker compose --env-file D:\hv\.env @args }
function sql { dc exec -T postgres psql -U historia -d historia_viva -c @args }
```

«Runbook R7» es de operación; no es la rebanada R7 de construcción (R-37).

## Cuándo

- Un incidente: el Agente 1 o un token hace pedidos que no debería, una Pieza con inyección se
  procesa en bucle, el motor se comporta raro, o hay que dejar la PC sin que nada corra.
- Antes de revocar un token por incidente (runbook R6) y si falla el runbook R18.

Para frenar solo la cola (medir con el banco, liberar CPU) alcanza
`interruptor detener procesamiento_habilitado`; para cortar solo `/v1`,
`interruptor detener api_agentes_habilitada`. «Frenar todo» acciona los dos.

## Pasos

1. **Frenar todo**, con motivo (5 caracteres o más):

   ```powershell
   dc run --rm web interruptor frenar-todo --motivo "<qué pasó>"
   ```

   O Administración → Interruptor → «Frenar todo…» (`http://localhost/admin.html`). Queda un
   evento por freno con quién y el motivo. Desde el pedido siguiente, sin caché:
   - **la cola se detiene**: ningún trabajador toma un trabajo nuevo de procesamiento ni de
     indexación; lo que se sube se acepta y queda encolado («esperando al motor (procesamiento
     detenido)»);
   - **la indexación se detiene**: es un tipo de la misma cola, y `reindexar` se niega;
   - **la generación de efemérides se detiene**: el planificador no genera, ni la programada ni
     «Generar ahora» (el pedido queda marcado y se atiende al reanudar);
   - **`/v1` responde `503`** a todo token válido.

   El trabajo que ya estaba en curso termina su plan: el freno no lo corta.
2. **Corte duro** (si hay que cortar también lo que está en curso, o el freno no alcanza):
   detener los trabajadores.

   ```powershell
   dc stop trabajador-procesamiento trabajador-indexacion trabajador-efemerides
   ```

   Cada uno espera hasta 30 s al trabajo en curso; lo que no termine queda `en_curso` y se retoma
   solo al vencer su arriendo (runbook R12). Para cortar el trabajo ya mismo:
   `dc kill trabajador-procesamiento` (o el que sea). No se paran `web`, `ollama` ni
   `ollama-embeddings`: la búsqueda y la Conversación los necesitan.
3. **La búsqueda y la Conversación siguen**, también con el corte duro: el freno no se consulta
   en la búsqueda web ni en la Conversación (LIM-24), y validar y correr el `banco` siguen.
4. **Volver**, cuando el incidente está resuelto y anotado:

   ```powershell
   dc up -d trabajador-procesamiento trabajador-indexacion trabajador-efemerides   # si hubo corte duro
   dc run --rm web interruptor reanudar procesamiento_habilitado --motivo "<por qué se vuelve>"
   ```

   Lo encolado se completa sin repetir trabajo hecho. `/v1` **no** se vuelve a encender: solo el
   Administrador técnico la enciende a mano, cuando corresponde (runbook R18).

## Verificación

Cada punto del paso 1 se prueba así:

| Qué | Cómo | Esperado |
|---|---|---|
| Los dos frenos | `dc run --rm web interruptor estado` | `procesamiento_habilitado: detenido desde …` y `api_agentes_habilitada: detenido desde …`, con el motivo |
| La cola | Subir una Pieza del corpus de prueba y correr la consulta de la cola de `despliegue-en-la-pc.md` §2 dos veces, con 2 min entre una y otra | `encolados` no baja; `en_curso` en 0 cuando termina lo que estaba |
| La indexación | `dc run --rm web reindexar` | Sale con 1: «El procesamiento está detenido: reanudalo con `interruptor reanudar procesamiento_habilitado` y volvé a correr el comando.» |
| Las efemérides | Administración → Programación de efemérides → «Generar ahora» | Franja «El procesamiento está detenido: no se generan efemérides hasta que se reanude.»; no aparece ninguna Propuesta nueva en Efemérides |
| `/v1` | `curl.exe -s -o NUL -w "%{http_code}`n" -H $h "http://localhost/v1/fragmentos?q=convenio"` con un token vigente (`$h` como en el runbook R5) | `503` |
| La salud | `http://localhost/api/salud` con sesión de Administrador técnico | `procesamiento: "pausado"`, `api_agentes: "pausada"`; el freno no degrada `estado` |
| El corte duro | `dc ps` y la salud después de 60 s | Los tres trabajadores sin `running`; `trabajadores.<tipo>.vivo: false` y `estado: "degradado"` (esperado) |
| La búsqueda y la Conversación | Una pregunta en la Conversación sobre una Pieza Validada | Las Fichas con sus Citas y la respuesta, con el freno y con el corte duro |
| Al volver | La consulta de la cola y la salud | `encolados` baja hasta 0; `estado: "ok"`; `api_agentes` sigue `pausada` |

La auditoría del freno es la tabla de eventos (sin duplicar en `auditoria`):
`sql "SELECT en, clave, habilitado, actor_tipo, actor_etiqueta, motivo FROM acervo.evento_interruptor ORDER BY en DESC LIMIT 4;"`.

## Si falla

- **«Para detener hay que dar un motivo de 5 caracteres o más.»**: repetir con un motivo.
- **La base no responde** (el comando no puede escribir el evento): los trabajadores fallan
  cerrados (sin leer el freno no toman) y `/v1` da `503`; igual, corte duro (paso 2). Si el
  incidente es de `/v1` y hay que cortarla sin base: `dc stop caddy` (corta también la web).
- **Un trabajo sigue en curso después del corte duro:** runbook R12.
- **La Conversación no responde con el freno:** no es el freno; ver el motor (runbook R10) y la
  salud.
- **Al reanudar no avanza la cola:** `interruptor estado` (¿quedó detenido?) y la salud
  (¿trabajadores vivos? ¿motor?).

## Registro de ejecución

Lo ejecuta en la PC alguien que no lo escribió. Lo que falló se corrige en el texto de arriba.

| Fecha | Quién | Commit | Desvíos | Corregido en el texto |
|---|---|---|---|---|
| 2026-10-20 | Implementador del ticket 115 (ensayo sin Docker) | `c6a31d8` | En proceso: `interruptor frenar-todo` sin motivo válido (se niega) y con motivo; `interruptor estado`; `reindexar` se niega; `/v1` con token vigente → `503`; `reanudar procesamiento_habilitado` deja `/v1` detenida. Ensayo automático `test_busqueda::test_runbook_r7_frenar_todo_deja_la_indexacion_encolada_y_la_busqueda_responde` (el trabajador de indexación no toma, la búsqueda responde, al reanudar se indexa). Lo demás lo cubren `test_interruptor::test_seg_interruptor_detenido_la_cola_no_avanza_y_lo_encolado_se_completa_al_reanudar`, `test_efemerides::test_con_el_procesamiento_frenado_o_el_motor_caido_el_planificador_no_hace_nada`, `test_conversacion::test_con_el_procesamiento_frenado_la_conversacion_sigue_respondiendo`, `test_procesamiento::test_frenar_a_mitad_del_plan_deja_terminar_el_trabajo_en_curso_y_no_toma_otro` y `test_composicion::test_docker_stop_le_da_al_trabajador_tiempo_de_terminar_el_trabajo_en_curso`. El corte duro con Docker no se ejecutó. No cuenta como ejecución en la PC | — |
| | | | | |
