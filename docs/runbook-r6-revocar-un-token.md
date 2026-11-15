# Runbook R6: revocar un token

Comandos de PowerShell, en la raíz del repositorio, con la pila arriba (runbook R1). `dc` y `sql` como en el
runbook R1:

```powershell
function dc { docker compose --env-file D:\hv\.env @args }
function sql { dc exec -T postgres psql -U historia -d historia_viva -c @args }
```

«Runbook R6» es de operación; no es la rebanada R6 de construcción (R-37).

## Cuándo

- Termina la sesión presencial con el Agente 1 (runbook R18, paso 6).
- Se rotó el token (runbook R5, paso 5).
- **Por incidente** (el token se filtró o el Agente hace pedidos que no debería): **antes** de
  revocar, runbook R7 («Frenar todo»), que corta `/v1` para todos los tokens y frena la cola.
  Después se vuelve acá.

## Pasos

1. **El prefijo** (`hv_` y 8 hex) del token a revocar, de Administración → Tokens
   (`http://localhost/admin.html`) o de la consulta:

   ```powershell
   sql "SELECT a.nombre, t.token_prefijo, t.creado_el, t.vence_el, t.ultimo_uso_el, t.revocado_el FROM acervo.token_agente t JOIN acervo.agente_externo a USING (agente_id) WHERE t.revocado_el IS NULL ORDER BY t.creado_el DESC;"
   ```

2. **Revocar:**

   ```powershell
   dc run --rm web revocar-token hv_0123abcd
   ```

   O desde la pantalla de tokens, «Revocar». Rige en el pedido siguiente: no hay caché.
3. **Si es por incidente:** se anota en la bitácora el prefijo, `ultimo_uso_el` y la hora, y se
   buscan sus pedidos en la auditoría y en el log de `web` (los Aportes llevan el `solicitud_id`).
   `/v1` queda apagada hasta que el Administrador técnico decida encenderla a mano
   (`interruptor reanudar api_agentes_habilitada --motivo …`, DEC-136).

## Verificación

- El comando imprime «Token hv_0123abcd revocado: el pedido siguiente da 401.».
- Con el token revocado, desde la PC (sin escribir el token en la línea de comandos):

  ```powershell
  $h = "Authorization: Bearer " + (Read-Host "token revocado")
  curl.exe -s -o NUL -w "%{http_code}`n" -H $h "http://localhost/v1/fragmentos?q=convenio"   # 401
  Remove-Variable h
  ```

  Da `401` con `/v1` encendida o apagada: la autenticación va antes del freno.
- La consulta del paso 1 ya no lo lista y queda `agente.revocacion` en la auditoría:
  `sql "SELECT cuando, actor_etiqueta, despues FROM acervo.auditoria WHERE accion = 'agente.revocacion' ORDER BY cuando DESC LIMIT 3;"`.

## Si falla

- **«Eso no es un prefijo de token (`hv_` y 8 hex): no se revocó nada.»** (sale con 2): se pegó el
  token entero o un prefijo incompleto. Usar los 11 caracteres del prefijo. Si se pegó el token
  entero en la consola, `Clear-Host` y borrar esa línea del historial
  (`(Get-PSReadLineOption).HistorySavePath`).
- **«No hay un token sin revocar …»** (sale con 1): ya estaba revocado o el prefijo no existe
  (el mensaje muestra el id interno del token, no el prefijo). Verificar con la consulta del paso 1.
- **No se puede revocar** (la base no responde): `dc run --rm web interruptor detener api_agentes_habilitada --motivo "<por qué>"`
  corta `/v1` para todos mientras tanto; si tampoco responde, `dc stop web caddy` (runbook R7, corte
  duro de `/v1`).

## Registro de ejecución

Lo ejecuta en la PC alguien que no lo escribió. Lo que falló se corrige en el texto de arriba.

| Fecha | Quién | Commit | Desvíos | Corregido en el texto |
|---|---|---|---|---|
| 2026-10-20 | Implementador del ticket 115 (ensayo sin Docker) | `c6a31d8` | En proceso contra una base de pgserver y `web` en un puerto local: revocar → `401` en el pedido siguiente; repetido sale con 1 («No hay un token sin revocar <uuid>», con el id interno y no el prefijo); el token entero sale con 2 sin revocar nada. No cuenta como ejecución en la PC | Se aclaró en Si falla que el mensaje muestra el id interno |
| | | | | |
