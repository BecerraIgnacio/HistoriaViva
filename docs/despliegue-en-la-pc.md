# Despliegue en la PC y monitoreo mínimo

La PC del alumno es el servidor del MVP (DEC-119): la SEU trabaja en forma presencial en ella y
nada se publica a la red ni a internet. Este documento dice cómo queda instalada, cómo se ve en un
minuto si está sana, cómo se mide el techo de 8 GB (RNF-03) y la lista de chequeo de la higiene
de la PC, que se ejecuta una vez y queda firmada al final.

Todos los comandos son de PowerShell y se corren en la raíz del repositorio. `D:\hv\.env` es el `.env` de la PC,
fuera del OneDrive (lo exigen el respaldo y el arranque, DEC-144).

## 1. Instalación, una sola vez

1. **Docker Desktop** → *Settings* → *General* → *Start Docker Desktop when you sign in*.
   En *Resources* → *Advanced*, la *Disk image location* fuera del OneDrive (la de fábrica,
   `%LOCALAPPDATA%\Docker\wsl`, sirve): ahí vive `hvmvp_pgdata`.
2. **WSL** (`%USERPROFILE%\.wslconfig`, el reparto de 32 GB del spec §2.1):

   ```ini
   [wsl2]
   memory=20GB
   processors=10
   swap=8GB
   autoMemoryReclaim=gradual
   ```

   Después, `wsl --shutdown` y volver a abrir Docker Desktop.
3. **Imágenes:** `docker compose --env-file D:\hv\.env build` (el arranque no construye).
4. **Tarea de arranque** (al iniciar sesión espera a Docker y hace `docker compose up -d`):

   ```powershell
   uv run python -m historia_viva.respaldo.arranque --entorno D:\hv\.env programar
   ```

   Corre en la sesión de quien opera, sin ventana y sin elevar. Se niega a correr si el `.env`
   o `HV_ORIGINALES` caen dentro del OneDrive.
5. **Tarea de respaldo:** `uv run python -m historia_viva.respaldo --entorno D:\hv\.env programar`
   (ticket 040).

Solo Caddy publica un puerto, y solo en `127.0.0.1:80`. Ningún contenedor monta el socket de
Docker. Lo cubren `test_compose_sin_puertos_publicados` y
`test_cada_servicio_rota_sus_logs_y_ninguno_monta_el_socket_de_docker`.

## 2. ¿Está sano? En un minuto

| Qué | Cómo | Sano |
|---|---|---|
| Salud | En el navegador de la PC, con sesión de Administrador técnico: `http://localhost/api/salud` | `"estado": "ok"`; en `trabajadores`, `procesamiento`, `indexacion` y `efemeride` con `vivo: true` |
| Contenedores | `docker compose --env-file D:\hv\.env ps` | Todos `running`; `postgres`, `web`, `ollama` y `ollama-embeddings` además `healthy` |
| Cola | La consulta de abajo | Sin fallidos nuevos. La antigüedad no alarma: 200 fotos son 8 h de cola normal |
| Disco | `Get-PSDrive D` y `docker system df` | Más de 20 GB libres en la unidad de `HV_ORIGINALES`; cada subida exige el tamaño + 2 GiB |
| Respaldo | `uv run python -m historia_viva.respaldo --entorno D:\hv\.env estado` | Sale con 0: último snapshot de menos de 26 h y la última corrida bien |

Sin sesión, `/api/salud` responde solo `{"estado": ...}` (DEC-102). Un trabajador figura caído
cuando su último latido tiene más de 60 s (latido cada 15 s, ticket 035).

**Consulta de la cola** (encolados, en curso, fallidos y el encolado más viejo, por tipo; un
fallido cuenta si es el último trabajo de ese tipo para la Pieza, como `reprocesar --fallidos`):

```powershell
docker compose --env-file D:\hv\.env exec -T postgres psql -U historia -d historia_viva -c "
SELECT tipo,
       count(*) FILTER (WHERE estado = 'encolado') AS encolados,
       count(*) FILTER (WHERE estado = 'en_curso') AS en_curso,
       count(*) FILTER (WHERE estado = 'fallido' AND ultimo) AS fallidos,
       min(encolado_el) FILTER (WHERE estado = 'encolado') AS mas_viejo_el
FROM (SELECT tipo, estado, encolado_el,
             row_number() OVER (PARTITION BY pieza_id, tipo ORDER BY encolado_el DESC) = 1 AS ultimo
      FROM acervo.trabajo_procesamiento) AS t
GROUP BY tipo ORDER BY tipo;"
```

Es la de `Cola.resumen()` (la que muestra `/api/salud` en `cola`), separada por tipo.

**Logs:** `docker compose --env-file D:\hv\.env logs --tail 100 web` (o el servicio que sea).
Cada servicio rota en 5 archivos de 20 MB (driver `local`). La app escribe una línea JSON por
evento (`ts`, `nivel`, `origen`, `evento` y los campos de la lista blanca: `solicitud_id`,
`trabajo_id`, `pieza_id`, `capacidad`, `modelo`, `duracion_ms`, `principal_id`). De un pedido
queda la ruta sin la consulta y de una excepción la clase y la traza, sin el mensaje (RNF-19).

## 3. Prueba: el trabajador de indexación caído se ve en menos de 60 s

1. Anotar la hora y `docker compose --env-file D:\hv\.env kill trabajador-indexacion`.
2. Recargar `/api/salud` cada 10 s: antes de 60 s, `trabajadores.indexacion.vivo` pasa a
   `false` y `estado` a `degradado` (también sin sesión).
3. `docker compose --env-file D:\hv\.env up -d trabajador-indexacion` y en 15 s vuelve a `ok`.

`kill` cuenta como parada manual: `restart: unless-stopped` no lo levanta solo hasta el paso 3.

## 4. Escaneo desde otra máquina de la red

Con la PC en la red de siempre y la pila arriba, desde otra máquina de la misma red:

```bash
nmap -Pn -p- --open <IP de la PC>
```

Esperado: ningún puerto de Historia Viva (80, 8000, 5432, 11434). Si aparecen puertos propios de
Windows (135, 139, 445), se anotan: son de la PC corporativa y de su firewall, no del sistema.
Sin `nmap`, desde otra PC con Windows: `Test-NetConnection <IP> -Port 80` da
`TcpTestSucceeded : False`. En la PC misma, `Get-NetTCPConnection -State Listen -LocalPort 80`
muestra solo `127.0.0.1`.

## 5. Techo de 8 GB y 4 vCPU (RNF-03, historia 237)

Una vez antes de la sesión. `compose.techo.yml` deja `ollama` en 4,5 GB y 4 CPU con un solo
modelo cargado, `ollama-embeddings` en 1 GB y recorta la base, `web` y los trabajadores; los topes
suman menos de 8 GiB.

1. Copiar `%USERPROFILE%\.wslconfig` a `.wslconfig.32gb` y dejarlo así:

   ```ini
   [wsl2]
   memory=8GB
   processors=4
   swap=0
   autoMemoryReclaim=gradual
   ```

   Sin swap: con swap la pila «entraría» paginando y la medición no diría nada.
2. `wsl --shutdown`, abrir Docker Desktop y
   `docker compose --env-file D:\hv\.env -f compose.yml -f compose.techo.yml up -d`.
3. En otra ventana, el pico de la pila (suma de los contenedores, una muestra cada 5 s):

   ```powershell
   $unidad = @{ B = 1; KiB = 1KB; MiB = 1MB; GiB = 1GB }; $pico = 0
   while ($true) {
     $total = (docker stats --no-stream --format "{{.MemUsage}}" | ForEach-Object {
       if (($_ -split ' / ')[0] -match '([\d.]+)(\w+)') { [double]$Matches[1] * $unidad[$Matches[2]] }
     } | Measure-Object -Sum).Sum
     if ($total -gt $pico) { $pico = $total; "{0:N2} GiB a las {1:HH:mm:ss}" -f ($pico / 1GB), (Get-Date) }
     Start-Sleep 5
   }
   ```

4. Cargar un Lote de 20 fotos y esperar a que las 20 queden Pendientes (o fallidas). Durante la
   tanda, una búsqueda desde la pantalla.
5. Anotar en la tabla. ¿Murió alguno por memoria?
   `docker inspect --format "{{.Name}} {{.State.OOMKilled}}" $(docker compose --env-file D:\hv\.env ps -aq)`.
6. Volver: `.wslconfig.32gb` a `.wslconfig`, `wsl --shutdown`, Docker Desktop y
   `docker compose --env-file D:\hv\.env up -d`.

| Fecha | Quién | Commit | Pico de la pila (GiB) | ¿Entra en 8 GB? | OOM (servicio) | Tanda: 20 fotos en (min) | Notas |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

«Entra» es que las 20 terminan sin ningún OOM con el pico por debajo de 8 GiB.

## 6. Retención de los backups

`respaldar` poda una vez por semana, después de un snapshot bueno: `restic forget --prune` con
`--keep-within 2d --keep-daily 14 --keep-weekly 8 --keep-monthly 3` sobre los snapshots
`historia-viva`, y borra de `HV_VOLCADOS` los volcados de más de 7 días (ya están en los
snapshots). La poda queda en `registro.jsonl`; si falla, `estado` sale con 1. Una retención corta
hace que un dato suprimido (runbook R14) no sobreviva meses en los backups.

## 7. Lista de chequeo de la higiene de la PC

Se ejecuta una vez, con la PC como va a quedar para la sesión, y se firma.

| # | Qué | Cómo se verifica | Esperado | ✓ |
|---|---|---|---|---|
| 1 | No suspende enchufada | `powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE` (si no: `powercfg /change standby-timeout-ac 0`) | Corriente alterna: `0x00000000` | |
| 2 | No hiberna enchufada | `powercfg /query SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE` (si no: `powercfg /change hibernate-timeout-ac 0`) | Corriente alterna: `0x00000000` | |
| 3 | Cerrar la tapa enchufada no suspende | `powercfg /query SCHEME_CURRENT SUB_BUTTONS LIDACTION` | Corriente alterna: `0x00000000` (no hacer nada) | |
| 4 | Plan de energía | `powercfg /getactivescheme` | Equilibrado o Alto rendimiento, no Ahorro de energía | |
| 5 | Docker Desktop inicia con Windows | `(Get-Content "$env:APPDATA\Docker\settings-store.json" \| ConvertFrom-Json).AutoStart` | `True` | |
| 6 | WSL con los valores del spec | `Get-Content "$env:USERPROFILE\.wslconfig"` | `memory=20GB`, `processors=10`, `swap=8GB`, `autoMemoryReclaim=gradual` | |
| 7 | Tarea de arranque | `schtasks /Query /TN "Historia Viva\Arranque"`; cerrar sesión, volver a entrar y a los 3 min `docker compose --env-file D:\hv\.env ps` | La tarea existe; la pila está arriba sin tocar nada | |
| 8 | Tarea de respaldo | `schtasks /Query /TN "Historia Viva\Respaldo"` | Existe | |
| 9 | Horas activas de Windows Update cubren 8 a 22 | `Get-ItemProperty HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings \| Select ActiveHoursStart, ActiveHoursEnd` | Empieza a las 8 o antes y termina a las 22 o después | |
| 10 | Sin reinicio automático mientras se usa | *Configuración* → *Windows Update*: actualizaciones pausadas la semana de una sesión; si IT lo administra, anotar su política | Pausadas o política anotada | |
| 11 | Datos fuera del OneDrive | `uv run python -m historia_viva.respaldo --entorno D:\hv\.env estado` | No sale con 2 (el `.env`, los originales, los volcados y el repositorio están fuera) | |
| 12 | `pgdata` fuera del OneDrive | Docker Desktop → *Resources* → *Advanced* → *Disk image location* | Fuera del OneDrive | |
| 13 | Disco de la PC y USB cifrados | En una consola de administrador: `manage-bde -status` | `Protección activada` en C:, en la unidad de los datos y en el USB de restic | |
| 14 | Nada publicado en la red | Sección 4 | Ningún puerto de Historia Viva | |

Ejecutada el ____ por ____________________. Desvíos: ____________________.
