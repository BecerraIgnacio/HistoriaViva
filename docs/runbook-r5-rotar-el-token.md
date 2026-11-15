# Runbook R5: rotar el token del Agente 1

Comandos de PowerShell, en la raíz del repositorio, con la pila arriba (runbook R1). `dc` y `sql` como en el
runbook R1:

```powershell
function dc { docker compose --env-file D:\hv\.env @args }
function sql { dc exec -T postgres psql -U historia -d historia_viva -c @args }
```

«Runbook R5» es de operación; no es la rebanada R5 de construcción (R-37).

## Cuándo

El token del Agente 1 vence (180 días desde la emisión), cambia quién lo tiene o se sospecha que
se copió sin que haya un incidente en curso. Si hay un incidente (el token se usa mal ahora), no
se rota: runbook R7 y después runbook R6.

## Pasos

1. **Anotar el prefijo del token vigente** (`hv_` y 8 hex), en Administración → Tokens
   (`http://localhost/admin.html`) o:

   ```powershell
   sql "SELECT a.nombre, t.token_prefijo, t.creado_el, t.vence_el, t.revocado_el FROM acervo.token_agente t JOIN acervo.agente_externo a USING (agente_id) ORDER BY t.creado_el DESC;"
   ```

2. **Emitir uno nuevo** para el mismo Agente, **sin `--alcances`** (conserva los que ya tiene):

   ```powershell
   dc run --rm web emitir-token "Agente 1"
   ```

   Imprime el token una sola vez y, al final, «Prefijo hv_…, vence el AAAA-MM-DD.». El viejo sigue
   vigente hasta el paso 5. Emitir no enciende `/v1` (DEC-136).
3. **Entregarlo en persona o por el gestor de contraseñas**, nunca por chat ni correo. Se borra la
   pantalla (`Clear-Host`) después de copiarlo.
4. **Confirmar que funciona** desde la PC, sin escribir el token en la línea de comandos (quedaría
   en `ConsoleHost_history.txt`):

   ```powershell
   $h = "Authorization: Bearer " + (Read-Host "token nuevo")
   curl.exe -s -o NUL -w "%{http_code}`n" -H $h "http://localhost/v1/fragmentos?q=convenio"
   ```

   Con `/v1` apagada (lo normal fuera de la sesión presencial) da **`503`**: el token pasó la
   autenticación, que va antes del freno. Un token que no sirve da **`401`**. Con `/v1`
   encendida, `200` (o `503` si el índice no tiene fragmentos del modelo vigente, runbook R18).
5. **Revocar el viejo** con el prefijo del paso 1 (runbook R6):

   ```powershell
   dc run --rm web revocar-token hv_0123abcd
   Remove-Variable h
   ```

Desde la pantalla de tokens, «Rotar» hace los pasos 2 y 5 juntos: emite el nuevo y programa la
revocación de los anteriores con un solape (24 h propuestas, 7 días como máximo). Se usa cuando
quien tiene el token viejo necesita tiempo para cambiarlo.

## Verificación

- La consulta del paso 1: el nuevo sin `revocado_el` y el viejo con fecha.
- El viejo da `401` y el nuevo `503` (o `200` con `/v1` encendida), con el `curl.exe` del paso 4.
- La auditoría, solo con prefijos, nunca el token:

  ```powershell
  sql "SELECT cuando, accion, actor_etiqueta, despues FROM acervo.auditoria WHERE accion IN ('agente.emision', 'agente.revocacion') ORDER BY cuando DESC LIMIT 4;"
  ```

## Si falla

- **««Agente 1» ya existe: sus alcances no se cambian por comando.»** (sale con 2): se pasó
  `--alcances`; repetir sin.
- **««Agente 1» es nuevo: decí sus --alcances.»** (sale con 2): el nombre no coincide con el del
  Agente (mayúsculas, espacios); copiarlo de la consulta del paso 1.
- **El nuevo da `401`:** se pegó mal; repetir el paso 4. Si sigue, revocar ese nuevo (runbook R6)
  y volver al paso 2. El viejo no se revoca hasta que el nuevo funcione.
- **No se pudo revocar el viejo:** runbook R6, Si falla.

## Registro de ejecución

Lo ejecuta en la PC alguien que no lo escribió. Lo que falló se corrige en el texto de arriba.

| Fecha | Quién | Commit | Desvíos | Corregido en el texto |
|---|---|---|---|---|
| 2026-10-20 | Implementador del ticket 115 (ensayo sin Docker) | `c6a31d8` | En proceso contra una base de pgserver y `web` en un puerto local: emitir sin `--alcances` para un Agente nuevo (sale con 2), alta con alcances, segundo token sin `--alcances`, con `--alcances` para uno existente (sale con 2); `/v1` apagada: sin token `401`, el nuevo `503`, uno inventado `401`; revocado el viejo, `401` y el nuevo sigue en `503`. Con `q=x` y `/v1` encendida da `400` (consulta corta): por eso `q=convenio`. No cuenta como ejecución en la PC | Se cambió `q=x` por `q=convenio` en el paso 4 |
| | | | | |
