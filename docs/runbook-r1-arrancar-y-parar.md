# Runbook R1: arrancar y parar

Comandos de PowerShell, en la raíz del repositorio. `dc` abrevia `docker compose --env-file D:\hv\.env` y `sql`,
una consulta de solo lectura a la base:

```powershell
function dc { docker compose --env-file D:\hv\.env @args }
function sql { dc exec -T postgres psql -U historia -d historia_viva -c @args }
```

«Runbook R1» es de operación; no es la rebanada R1 de construcción (R-37).

## Cuándo

- **Arrancar:** al prender la PC lo hace solo la tarea `Historia Viva\Arranque` al iniciar sesión
  (espera a Docker y hace `docker compose up -d`, ver `despliegue-en-la-pc.md` §1). A mano, cuando
  la tarea no corrió o después de haber parado con este runbook.
- **Parar:** antes de apagar o reiniciar la PC, de actualizar Docker Desktop o Windows, o cuando lo
  pide otro runbook. Un trabajador colgado no se arregla parando todo: runbook R12.

## Pasos

**Arrancar**

1. Docker Desktop abierto: `docker info` responde sin «Cannot connect to the Docker daemon».
2. `dc up -d`. No construye: después de un `git pull` es el runbook R2.
3. Esperar a `modelos` (baja los que falten de la configuración vigente; `web` no lo espera):

   ```powershell
   dc ps -a modelos          # Exited (0)
   dc logs modelos           # termina en «Modelos listos.»
   ```

**Parar**

4. Primero los tres trabajadores, para que cada uno termine el trabajo que tiene en curso:

   ```powershell
   dc stop trabajador-procesamiento trabajador-indexacion trabajador-efemerides
   ```

   Cada uno espera hasta 30 s al trabajo en curso (`stop_grace_period` de 40 s). Si no terminó,
   queda `en_curso` y la próxima toma lo retoma sola al vencer su arriendo (runbook R12).
5. `dc stop`: el resto (`web`, `caddy`, los dos Ollama y `postgres`).

**Nunca** `dc down -v`, `docker volume rm`, `docker volume prune` ni `docker system prune
--volumes`: `hvmvp_pgdata` es el Acervo y `hvmvp_modelos`, los modelos. La única excepción con el
corpus real es el runbook R17, con constancia; el runbook R9 (paso 3.2) borra `hvmvp_pgdata` solo
cuando `postgres` no arranca y ya hay copia. Para parar alcanza `dc stop`; `dc down` sin `-v` no
hace falta.

## Verificación

- **Arrancado:** `dc ps`: todos `running`; `postgres`, `web`, `ollama` y `ollama-embeddings`
  además `healthy`. `curl.exe -s http://localhost/api/salud` → `{"estado":"ok"}`. Con sesión de
  Administrador técnico, en el navegador de la PC, `http://localhost/api/salud`: `trabajadores`
  con `procesamiento`, `indexacion` y `efemeride` en `vivo: true` (latido cada 15 s; tarda hasta
  un minuto) y `modelos_faltantes` vacío.
- **Parado:** antes del paso 5, `sql "SELECT count(*) FROM acervo.trabajo_procesamiento WHERE estado = 'en_curso';"`
  da 0 (o los que el paso 4 dejó por tiempo). Después, `dc ps` no lista nada y los volúmenes
  siguen: `docker volume ls --filter name=hvmvp_` muestra `hvmvp_pgdata`, `hvmvp_modelos` y
  `hvmvp_caddy` (y `hvmvp_sin_referencia`, vacío, si el `.env` no trae `HV_REFERENCIA`).

## Si falla

- **`docker info` no responde:** abrir Docker Desktop y esperar; si no levanta, reiniciar la PC.
  La tarea de arranque espera hasta 10 min a Docker y después abandona: arrancar a mano (paso 2).
- **`modelos` sale con 1:** `dc logs modelos`. «El motor `ollama` … no responde»: `dc up -d ollama`
  y `dc run --rm modelos`. Sin internet no baja: los modelos que ya estaban siguen sirviendo.
- **`web` no queda `healthy`:** `dc logs --tail 100 web`. Si `migrar` falló, `dc logs migrar` y
  runbook R2.
- **`estado` en `degradado`:** con sesión, ver qué parte (`capa_modelos`, `trabajadores`, `video`).
  Un trabajador con `vivo: false`: `dc up -d <servicio>`; si se repite, runbook R12. Motor sin
  memoria: runbook R10. Disco: runbook R11.
- **`dc stop` tarda más de 40 s con un trabajador:** Docker lo mata; su trabajo queda `en_curso`
  hasta que vence el arriendo y se retoma solo al arrancar (runbook R12).

## Registro de ejecución

Lo ejecuta en la PC alguien que no lo escribió. Lo que falló se corrige en el texto de arriba.

| Fecha | Quién | Commit | Desvíos | Corregido en el texto |
|---|---|---|---|---|
| 2026-10-20 | Implementador del ticket 115 (ensayo sin Docker) | `c6a31d8` | Sin Docker en la máquina del ensayo: se verificó que cada servicio, volumen y flag existe (`docker compose config`, `compose.yml`) y el `stop_grace_period` (`test_docker_stop_le_da_al_trabajador_tiempo_de_terminar_el_trabajo_en_curso`). No cuenta como ejecución en la PC | — |
| | | | | |
