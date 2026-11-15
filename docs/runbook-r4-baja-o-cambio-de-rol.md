# Runbook R4: baja de una persona o cambio de rol

Comandos de PowerShell, en la raíz del repositorio, con la pila arriba (runbook R1). `dc` y `sql` como en el
runbook R1:

```powershell
function dc { docker compose --env-file D:\hv\.env @args }
function sql { dc exec -T postgres psql -U historia -d historia_viva -c @args }
```

«Runbook R4» es de operación; no es la rebanada R4 de construcción (R-37).

## Cuándo

Una persona deja la SEU o la Carrera (baja), cambia de función (cambio de rol) o vuelve
(reactivación). El alta es el runbook R3.

## Pasos

1. **Baja** (DEC-113):

   ```powershell
   dc run --rm web alta-persona --email <usuario>@fie.undef.edu.ar --inactiva
   ```

   No borra: conserva lo que firmó y su historial. **La baja revoca sus sesiones** en la misma
   transacción: el pedido siguiente de esa persona da 401 y no puede volver a entrar.
2. **Cambio de rol:**

   ```powershell
   dc run --rm web alta-persona --email <usuario>@fie.undef.edu.ar --rol coordinador
   ```

   (`rgc`, `coordinador`, `director_carrera` o `admin_tecnico`). La sesión lee el rol de la base
   en cada pedido: rige en el pedido siguiente, sin cerrar la sesión.
3. **Reactivar** a alguien dado de baja: `dc run --rm web alta-persona --email <email> --activa`.

Lo mismo se hace desde Administración → Personas (`http://localhost/admin.html`) con
`GESTIONAR_PERSONAS`. Por pantalla, `admin_tecnico` lo toca solo el técnico durante el
interinato; después, solo este comando (R-02, runbook R3). Nadie se da de baja ni se cambia el rol
a sí mismo desde la pantalla. Mientras dure el interinato (sin RGC activa), cada acción, también la
del comando, queda auditada con la marca `interino`.

## Verificación

- El comando imprime «Listo.» y sale con 0.
- `sql "SELECT email, rol, activo FROM acervo.usuario WHERE email = '<email>';"`.
- La auditoría (`persona.baja` con las sesiones revocadas, `persona.rol` con antes y después,
  `persona.reactivacion`):

  ```powershell
  sql "SELECT cuando, accion, actor_etiqueta, antes, despues FROM acervo.auditoria WHERE accion IN ('persona.baja', 'persona.rol', 'persona.reactivacion') ORDER BY cuando DESC LIMIT 5;"
  ```

  En la baja, `despues` = `{"activo": false, "sesiones_revocadas": N}`.
- Sesiones: `sql "SELECT count(*) FROM acervo.sesion s JOIN acervo.usuario u USING (usuario_id) WHERE u.email = '<email>' AND s.revocada_el IS NULL;"`
  da 0 después de la baja. Si la persona tenía la pantalla abierta, al recargar vuelve a la
  Entrada.
- Cambio de rol: con la sesión de esa persona, `/api/yo` trae el rol y los permisos nuevos (un
  técnico pasado a `director_carrera` recibe 403 en Administración).

## Si falla

- **`recurso-no-encontrado: No hay una persona con ese email o identificador.`** (sale con 1): el
  email no está o está mal escrito; `sql "SELECT email, rol, activo FROM acervo.usuario;"`.
- **`ultimo-titular-de-permiso: Es la última persona activa con …`** (sale con 1): es la única con
  `GESTIONAR_PERSONAS` u `OPERAR_SISTEMA` (R-03). Primero el alta de quien la reemplaza
  (runbook R3), después la baja.
- **«Falta qué hacer: --rol, --inactiva o --activa.»** (sale con 2).
- **Una baja o reactivación repetida** dice «Listo.» y no hace nada (no audita): ya estaba así.
- **La persona dada de baja sigue entrando:** no debería; revisar el email exacto y la consulta
  de sesiones. Si tiene un token de Agente a su cargo, revocarlo aparte (runbook R6).

## Registro de ejecución

Lo ejecuta en la PC alguien que no lo escribió. Lo que falló se corrige en el texto de arriba.

| Fecha | Quién | Commit | Desvíos | Corregido en el texto |
|---|---|---|---|---|
| 2026-10-20 | Implementador del ticket 115 (ensayo sin Docker) | `c6a31d8` | `alta-persona` en proceso sobre una base de pgserver: cambio a `director_carrera`, baja (`sesiones_revocadas`), baja repetida (sin auditoría), reactivación, email inexistente, la última RGC (`ultimo-titular-de-permiso`) y sin acción. Que el pedido siguiente dé 401 lo prueba `test_identidad::test_alta_persona_inactiva_corta_el_acceso_y_reactivar_lo_devuelve`. No cuenta como ejecución en la PC | Se sumaron la baja repetida y «Falta qué hacer» a Si falla |
| | | | | |
