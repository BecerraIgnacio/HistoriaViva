# Runbook R3: alta de persona y primer Administrador técnico

Comandos de PowerShell, en la raíz del repositorio, con la pila arriba (runbook R1). `dc` y `sql` como en el
runbook R1:

```powershell
function dc { docker compose --env-file D:\hv\.env @args }
function sql { dc exec -T postgres psql -U historia -d historia_viva -c @args }
```

«Runbook R3» es de operación; no es la rebanada R3 de construcción (R-37).

## Cuándo

- **Instalación nueva:** la base nace sin personas (DEC-52). La primera persona es el primer
  Administrador técnico.
- **La SEU nombra a alguien** (la RGC y el Coordinador, C47) o entra una persona nueva con uno de
  los cuatro roles: `rgc`, `coordinador`, `director_carrera`, `admin_tecnico` (DEC-124).

## Pasos

1. **Primer Administrador técnico** (una sola vez, sobre la base vacía):

   ```powershell
   dc run --rm web alta-persona --email <usuario>@fie.undef.edu.ar --nombre "<Nombre y apellido>" --rol admin_tecnico
   ```

   **El primer `admin_tecnico` se crea solo por este comando** (R-02): ninguna pantalla ni ruta
   HTTP lo crea. Mientras no haya RGC, ese técnico es interino: también gestiona personas y retira
   Piezas, y todo lo que hace queda auditado con la marca `interino` (R-01).
2. **Las demás personas**, por comando o desde Administración → Personas
   (`http://localhost/admin.html`, quien tiene `GESTIONAR_PERSONAS`: la RGC, o el técnico durante
   el interinato):

   ```powershell
   dc run --rm web alta-persona --email <usuario>@fie.undef.edu.ar --nombre "<Nombre y apellido>" --rol rgc
   ```

   (`--rol coordinador`, `director_carrera` o `admin_tecnico`). El email tiene que ser de un
   dominio de `HV_DOMINIOS_PERMITIDOS` (vacía: `fie.undef.edu.ar`); se guarda en minúsculas.
3. **El alta de la primera RGC termina el interinato** (R-01), en la misma transacción: desde el
   pedido siguiente el técnico queda con `CONSULTAR` y `OPERAR_SISTEMA` y pierde la pestaña
   Personas. Después de eso, `admin_tecnico` se otorga o se quita **solo con `alta-persona`**: por
   HTTP nadie lo toca (la RGC recibe 403 y su selector no lo ofrece).
4. **Primer ingreso:** con OIDC, la persona entra con la cuenta de Google de ese email y el primer
   ingreso vincula su cuenta; con `HV_AUTH=dev`, entra en la PC desde la Entrada con el email.
   Hasta entonces figura «Habilitada, todavía no ingresó».

## Verificación

- El comando imprime «Habilitada, todavía no ingresó.» y sale con 0.
- `sql "SELECT email, rol, activo, ultimo_acceso_el FROM acervo.usuario ORDER BY creado_el;"`: la
  persona con su rol, `activo = t` y `ultimo_acceso_el` vacío hasta que entra.
- La auditoría del alta (`actor_tipo = cli` por comando):

  ```powershell
  sql "SELECT cuando, actor_tipo, actor_etiqueta, despues FROM acervo.auditoria WHERE accion = 'persona.alta' ORDER BY cuando DESC LIMIT 5;"
  ```

  Mientras dura el interinato, `despues` lleva `"interino": true`. El alta de la primera RGC
  lleva además `"interinato_terminado": true`.
- Fin del interinato: con la sesión del técnico, `http://localhost/api/yo` → `permisos` sin
  `GESTIONAR_PERSONAS` ni `RETIRAR_PIEZAS`.

## Si falla

- **`persona-existente: Ya hay una persona con ese email.`** (sale con 1): ya está. Si está de
  baja, se reactiva (runbook R4); si cambia el rol, runbook R4.
- **`dominio-no-permitido: …`** (sale con 1): el email no es de la FIE. Si la SEU autoriza otro
  dominio, se suma a `HV_DOMINIOS_PERMITIDOS` en `D:\hv\.env` (separados por comas) y `dc up -d web`.
- **«Un alta lleva --nombre y --rol.»** (sale con 2): falta `--rol`.
- **Por HTTP, 403 al dar `admin_tecnico`:** es la regla R-02 después del interinato; se hace por
  comando (paso 2).
- **La persona no puede entrar:** el email del alta tiene que ser exactamente el de su cuenta de
  Google. Si se equivocó, se da de baja el equivocado (runbook R4) y se hace un alta nueva.

## Registro de ejecución

Lo ejecuta en la PC alguien que no lo escribió. Lo que falló se corrige en el texto de arriba.

| Fecha | Quién | Commit | Desvíos | Corregido en el texto |
|---|---|---|---|---|
| 2026-10-20 | Implementador del ticket 115 (ensayo sin Docker) | `c6a31d8` | `alta-persona` en proceso (`python -m historia_viva.composicion`, lo mismo que corre `dc run --rm web`) sobre una base vacía: primer técnico, alta repetida (`persona-existente`), dominio ajeno, alta sin `--rol`, Coordinador y primera RGC con `interinato_terminado: true`. El actor queda `cli` con el usuario del sistema; en el contenedor será `app`, no quien opera (anotarlo en la bitácora). No cuenta como ejecución en la PC | Se sumó «Un alta lleva --nombre y --rol.» a Si falla |
| | | | | |
