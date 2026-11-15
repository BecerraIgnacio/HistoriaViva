# Runbook R18: encendido manual de `/v1` para la sesión presencial con el Agente 1

Procedimiento de la sesión presencial con Juan (historia 226; DEC-119, DEC-136, R-32): `/v1`, y
nada más, en la IP de la LAN, con un token temporal, y todo se baja al terminar. Usa el override
`compose.sesion-presencial.yml` (ticket 131). Comandos de PowerShell, en la raíz del repositorio, con la pila arriba
(`docker compose up -d`, ver `despliegue-en-la-pc.md`). `dc` abrevia
`docker compose --env-file D:\hv\.env`:

```powershell
function dc { docker compose --env-file D:\hv\.env @args }
```

## Cuándo

Solo durante la sesión presencial con Juan, con su máquina y la PC en la misma red (el hotspot).
Fuera de ella, `/v1` queda apagada: la migración la deja así (`api_agentes_habilitada = false`) y
ningún comando la enciende salvo `interruptor reanudar` (DEC-136); el 8000 no se publica.

**Qué corpus lee el Agente 1 (C49, R-32).** El Agente 1 lee el corpus real solo si C49 lo autoriza
en forma explícita y por escrito. Si no lo autoriza, la sesión usa el corpus de prueba. Mientras
C49 siga abierta no hay corpus real en la PC (sin el aval no se carga material real), así que la
sesión corre sobre el corpus de prueba de la instalación. Si C49 avala el corpus semilla pero no
la lectura del Agente 1, la sesión no se hace sobre esta instalación: queda pendiente decidir dónde
se levanta el corpus de prueba (anotado en el ticket 114).

**Antes de empezar:** el corpus que se va a usar tiene que tener Piezas Validadas e indexadas, y
al menos una Pieza Pendiente para probar el 404. Con el índice vacío, `/v1/fragmentos` da `503`.

## Pasos

1. **Emitir el token temporal**, con un Agente propio de la sesión (la fecha en el nombre) y los
   alcances mínimos, los de lectura:

   ```powershell
   dc run --rm web emitir-token "Agente 1 sesión 2026-11-05" --alcances fragmentos:leer piezas:leer
   ```

   Si la sesión va a probar Aportes o efemérides, se suma ese alcance (`aportes:escribir`,
   `efemerides:leer`) y nada más. El token sale una sola vez: se le da a Juan en mano o en un
   papel, nunca por chat ni correo. Anotar el **prefijo** (`hv_` y 8 hex) que imprime al final:
   con él se revoca. Queda `agente.emision` en la auditoría. `emitir-token` **no** enciende `/v1`:

   ```powershell
   dc run --rm web interruptor estado     # api_agentes_habilitada: detenido
   ```

   Lo temporal lo ponen el nombre del Agente, que no se reusa, y la revocación del paso 6: si no
   se revoca, el token vence a los 180 días.
2. **Encender `/v1` a mano**, con motivo:

   ```powershell
   dc run --rm web interruptor reanudar api_agentes_habilitada --motivo "Sesión presencial con Juan"
   ```

   O desde P8 → Interruptor.
3. **Publicar solo `/v1` en la LAN** con el override. La IP es la «Dirección IPv4» del adaptador
   Wi-Fi en `ipconfig`:

   ```powershell
   $env:HV_IP_LAN = "192.168.43.10"
   dc -f compose.yml -f compose.sesion-presencial.yml up -d caddy
   dc -f compose.yml -f compose.sesion-presencial.yml ps caddy   # 127.0.0.1:80 y <IP>:8000
   ```

   Sin `HV_IP_LAN`, el override no levanta (no publica en todas las interfaces). A Juan se le da
   `AGENTE1_HV_BASE_URL=http://<IP>:8000`, sin `/v1`, y el token como `AGENTE1_HV_TOKEN`.
4. **Durante la sesión**, cada pedido de Juan deja su `X-Solicitud-Id` en la respuesta, en el log
   de `web` y en la auditoría de sus Aportes: un problema se busca por ese id
   (`dc logs web | Select-String req_…`).
5. **Al terminar, apagar `/v1`** y verificar el `503` con el token **todavía vigente** (con el
   token revocado da `401`, porque `/v1` autentica antes de mirar el freno):

   El token no se escribe en la línea de comandos: quedaría en pantalla y en el historial de
   PowerShell (`ConsoleHost_history.txt`), vigente 180 días si la revocación fallara. Se lee con
   `Read-Host` a una variable:

   ```powershell
   dc run --rm web interruptor detener api_agentes_habilitada --motivo "Fin de la sesión con Juan"
   $h = "Authorization: Bearer " + (Read-Host "token")   # pegar el token; no queda en el historial
   curl.exe -i -H $h "http://localhost/v1/fragmentos?q=x"   # 503
   ```

6. **Revocar el token** (rige en el pedido siguiente):

   ```powershell
   dc run --rm web revocar-token hv_0123abcd
   curl.exe -i -H $h "http://localhost/v1/fragmentos?q=x"   # 401
   ```

7. **Bajar el override:**

   ```powershell
   dc up -d caddy                 # Caddy vuelve a escuchar solo en 127.0.0.1:80
   Remove-Item Env:HV_IP_LAN
   Remove-Variable h
   dc ps caddy                    # solo 127.0.0.1:80
   dc run --rm web interruptor estado   # api_agentes_habilitada: detenido
   ```

## Verificación

Desde la máquina de Juan (o cualquier otra de la LAN), contra `http://<IP de la PC>:8000`:

| Momento | Pedido | Esperado |
| --- | --- | --- |
| Después del paso 1 | `interruptor estado` en la PC | `api_agentes_habilitada: detenido` |
| Después del paso 3 | `GET /v1/fragmentos?q=convenio` sin token | `401 token_invalido` |
| Después del paso 3 | el mismo, con `Authorization: Bearer <token>` | `200` |
| Después del paso 3 | `GET /v1/piezas/pza_<uuid de una Pendiente>` con token | `404 pieza_no_disponible` |
| Después del paso 3 | `GET /`, `/api/salud`, `/api/yo`, `/auth/dev` | `404` (no llegan a `web`) |
| Después del paso 3 | `Test-NetConnection <IP> -Port 80` | `TcpTestSucceeded : False` |
| Después del paso 5 | `GET /v1/fragmentos` con token, desde la PC | `503 servicio_no_disponible` |
| Después del paso 6 | el mismo, con token | `401 token_invalido` |
| Después del paso 7 | cualquier pedido a `<IP>:8000` | no conecta |

En `curl.exe`: `curl.exe -i "http://<IP>:8000/v1/fragmentos?q=convenio"` y, con token,
`-H "Authorization: Bearer <token>"`.

Si se olvida el paso 1 o el 2, el pedido con token da `401` o `503`: no hay estado intermedio que
deje `/v1` abierta. El ensayo automático de estos pasos, con los mismos comandos y Caddy real con
`Caddyfile.sesion-presencial` en la IP de la LAN de la máquina que corre los tests, es
`test_contrato_v1::test_el_ensayo_del_runbook_r18_llega_desde_la_ip_de_la_lan_solo_a_v1` (se
saltea sin Caddy). El recorrido completo del Agente 1 (Aportes, efemérides, Relatos) es
`test_el_agente_1_recorre_el_sistema_real_con_el_procedimiento_del_runbook`.

## Si falla

- `emitir-token` dice que el Agente ya existe: otro nombre (la fecha de hoy) o revocar el token
  anterior y emitir uno nuevo para el mismo Agente, sin `--alcances` (reusa los que el Agente ya
  tenía: los mínimos, si se dio de alta hoy con el paso 3).
- Con token, `503` después del paso 2: `dc run --rm web interruptor estado`. Si sigue detenida,
  repetir el paso 2. Si está habilitada, el índice no tiene fragmentos del modelo vigente: indexar
  el corpus de la sesión antes de seguir (`dc logs web | Select-String IndiceNoDisponible`).
- Desde la otra máquina no conecta al 8000 pero desde la PC sí: la red figura como Pública y el
  firewall de Windows bloquea el puerto. Pasarla a Privada o habilitar Docker Desktop en el
  firewall; no abrir otros puertos.
- `dc -f … up -d caddy` falla con «Falta HV_IP_LAN»: falta el `$env:HV_IP_LAN` del paso 3.
- Después del paso 7, `dc ps caddy` muestra todavía `<IP>:8000`: repetir `dc up -d caddy` sin
  `-f`. Un reinicio de la PC no vuelve a publicar el 8000 (Caddy queda abajo con `restart: "no"`
  hasta `dc up -d caddy`).
- No se pudo revocar: el token sigue vigente hasta los 180 días. Detener `/v1` (paso 5) alcanza
  para cortar el acceso mientras tanto. Después, runbook R7.
