# Runbook R16: receta del servidor de la universidad

La receta única que levanta Historia Viva en un servidor Linux sin nada previo (spec §2.2,
DEC-119). Es el mismo `compose.yml` y el mismo `.env.example` de la PC, más el override
`compose.servidor.yml` (puertos 80 y 443, TLS y `HV_AUTH=oidc`). Se ensaya primero en una VM
Linux limpia, y ese ensayo cierra la rebanada R9 (ticket 122); después se repite en el servidor
real. Ticket 121.

- **Sistema operativo:** Ubuntu Server LTS (DIR-97). Lo confirma IT en la consulta **C50**, junto
  con la salida a internet y los recursos (CPU, memoria, GPU y disco). Hasta que responda, la
  receta asume Ubuntu Server LTS con salida a internet y documenta la alternativa sin internet.
- **No hay traspaso de datos** (R-34): el servidor arranca vacío. No viajan `pgdata`, los
  originales, el `.env` de la PC, los tokens, las firmas de ensayo (DEC-139), el corpus de prueba
  ni el corpus real. La SEU vuelve a cargar y validar en el servidor; la lista de Carreras la
  entrega la SEU (C45).
- **Requisitos que define la universidad** (C3, C18, C50): 4+ vCPU, 16 GB, 500 GB de disco, IP fija,
  un nombre en el DNS de la FIE, el cliente OIDC «Interno» de IT y una persona responsable de la
  operación, que no es el alumno.

Todos los comandos son de bash, en el servidor, como el usuario de despliegue y dentro de
`/opt/historia-viva/mvp`. `dc` abrevia el compose del servidor con su `.env`:

```bash
dc() { docker compose --env-file /srv/historia-viva/.env -f compose.yml -f compose.servidor.yml "$@"; }
```

## Cuándo

- **En una VM limpia**, antes de tocar el servidor de la universidad. Se siguen los nueve pasos
  tal como están escritos. Cada desvío se corrige en este texto y se repite hasta que pasa sin
  intervención. Se anotan fecha, quién, tiempo, desvíos y el tag de entrega (acta del ticket 122).
  Con la VM, el paso 8 (prueba de humo) es obligatorio.
- **En el servidor de la universidad**, después del PPS, cuando IT lo entrega. Se repiten los
  nueve pasos, **sin el paso 8**: la prueba de humo deja Piezas, personas y firmas, y el paso 9
  exige que no haya.

## Pasos

Cada paso tiene su verificación: no se pasa al siguiente sin ella.

### 1. Docker Engine y el plugin de compose; un usuario sin privilegios

Con el repositorio oficial de Docker para Ubuntu (sin internet, ver «Alternativa sin internet»):

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git openssl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo adduser --disabled-password --gecos "" historiaviva
sudo usermod -aG docker historiaviva
sudo install -d -o historiaviva -g historiaviva /opt/historia-viva /srv/historia-viva
```

El grupo `docker` equivale a root sobre los contenedores: el usuario `historiaviva` no tiene
`sudo` y solo lo usa quien opera.

**Verificación:** como `historiaviva`, `docker compose version` responde 2.24 o más nuevo
(`compose.servidor.yml` usa `!override`) y `docker run --rm hello-world` corre sin `sudo`.

### 2. El clon del tag de entrega

```bash
git clone --branch <tag-de-entrega> --depth 1 <url-del-repositorio> /opt/historia-viva
cd /opt/historia-viva/mvp
```

**Verificación:** `git describe --tags` da el tag del acta de entrega.

### 3. El `.env` con secretos nuevos

El generador copia `.env.example` y pone claves nuevas de `openssl rand` para Postgres, los dos
roles y la cookie previa al login. También deja `HV_DESPLIEGUE=servidor`, `HV_AUTH=oidc`,
`HV_ENTORNO` vacía y `HV_URL_PUBLICA` y `HV_ORIGEN` con `https://` y el nombre. No lee ningún otro
`.env`, y nada del entorno de la PC se copia (`test_seg_la_receta_genera_secretos_nuevos_sin_leer_el_entorno_de_la_pc`).

```bash
bash generar-env-servidor.sh /srv/historia-viva/.env historiaviva.fie.undef.edu.ar
nano /srv/historia-viva/.env
```

A mano se completa:

| Variable | Valor |
|---|---|
| `HV_OIDC_CLIENTE_ID`, `HV_OIDC_SECRETO` | El cliente «Interno» de IT (C18), con la URI de retorno `https://<nombre>/auth/callback` |
| `HV_OIDC_EMISOR` | Vacía (Google). En la VM, para el paso 8: `http://oidc-falso:9000` |
| `HV_TLS` | `/certs/certificado.pem /certs/clave.pem` con el certificado de IT; un correo para Let's Encrypt si el nombre es público y hay internet; `internal` en la VM |
| `HV_CERTIFICADOS` | `/srv/historia-viva/certificados` (con los archivos de IT, o vacío) |
| `HV_ORIGINALES` | `/srv/historia-viva/originales` |
| `HV_VOLCADOS`, `RESTIC_REPOSITORY`, `RESTIC_PASSWORD_COMMAND` | Los del respaldo del servidor (paso 7), en un disco aparte del de la base |
| `HV_DOMINIOS_PERMITIDOS` | Vacía es `fie.undef.edu.ar` |

Las variables que el compose del servidor exige cortan antes de crear un contenedor (`:?`). Las
que pasan, la app las vuelve a revisar al arrancar (`Entorno`):

- con `HV_DESPLIEGUE=servidor`, ningún comando arranca con `HV_ENTORNO` ni con `HV_AUTH=dev`,
  así que `migrar` no siembra las personas de prueba;
- `web` no arranca sin `HV_OIDC_CLIENTE_ID`, `HV_OIDC_SECRETO` y `HV_CLAVE_COOKIE_PREVIA`, ni
  con `HV_URL_PUBLICA` sin `https://` o distinta de `HV_ORIGEN`.

**Verificación:**

```bash
stat -c %a /srv/historia-viva/.env          # 600
dc config --quiet && echo "compose completo"
HV_ENTORNO=desarrollo dc run --rm --no-deps migrar; echo "salió con $?"
```

La última tiene que salir con 2 y decir «En el servidor HV_ENTORNO va vacía». Una variable que
falta hace fallar `dc config` con «Falta …».

### 4. Los directorios de originales, volcados y certificados

```bash
sudo install -d -o 1000 -g 1000 -m 750 /srv/historia-viva/originales
install -d -m 700 /srv/historia-viva/volcados /srv/historia-viva/certificados
```

`web` y el trabajador escriben los originales como el uid 1000 de la imagen. Con certificado de
IT, sus dos archivos van en `certificados/`, legibles por el contenedor de Caddy.

**Verificación:** `ls -A /srv/historia-viva/originales` no muestra nada y
`stat -c '%u %a' /srv/historia-viva/originales` da `1000 750`.

### 5. Base, roles, migraciones y el resto

```bash
dc build
dc up -d postgres          # la primera vez corre el bootstrap de roles (db/bootstrap.sh)
dc run --rm migrar
dc up -d                   # `modelos` baja los modelos de la configuración vigente
```

**Verificación:**

```bash
dc exec -T web python -c "import os, psycopg; print(psycopg.connect(os.environ['HV_BD_URL']).execute('SELECT current_user').fetchone()[0])"
curl -s https://historiaviva.fie.undef.edu.ar/api/salud
dc ps
```

La primera da `hv_app`. La segunda, `{"estado":"ok"}` cuando terminan de bajar los modelos: antes
dice `degradado` y el detalle muestra `modelos_faltantes`. En la VM con `tls internal`, `curl -k`.
En `dc ps`, todos `running`, y `postgres`, `web`, `ollama` y `ollama-embeddings` también
`healthy`. Desde otra máquina, `nmap -p- <ip>` muestra solo 80 y 443.

### 6. El primer Administrador técnico

```bash
dc run --rm web alta-persona --email tecnico@fie.undef.edu.ar --nombre "Nombre Apellido" --rol admin_tecnico
```

Después, las altas de la SEU desde la pantalla de Personas (runbook R3). El alta del primer RGC
termina el interinato del Administrador técnico (R-01).

**Verificación:** el Administrador técnico entra con «Entrar con Google» en
`https://<nombre>/` y `https://<nombre>/api/yo` dice `admin_tecnico`. En el servidor real esto
prueba el cliente OIDC de IT de punta a punta.

### 7. Backups automáticos y restauración probada

Los mismos comandos de la PC (spec §2.3, tickets 040 y 041), con `uv` y `restic` en el servidor.
`respaldo programar` crea la tarea de Windows: en Linux la programa `cron`, con los horarios de la
PC (02:00 todos los días y 08, 12, 16 y 20 de lunes a sábado).

```bash
sudo apt-get install -y restic
curl -LsSf https://astral.sh/uv/install.sh | sh
uv run python -m historia_viva.respaldo --entorno /srv/historia-viva/.env iniciar
uv run python -m historia_viva.respaldo --entorno /srv/historia-viva/.env respaldar
( crontab -l 2>/dev/null; echo "0 2 * * * cd /opt/historia-viva/mvp && \$HOME/.local/bin/uv run python -m historia_viva.respaldo --entorno /srv/historia-viva/.env respaldar"; echo "0 8,12,16,20 * * 1-6 cd /opt/historia-viva/mvp && \$HOME/.local/bin/uv run python -m historia_viva.respaldo --entorno /srv/historia-viva/.env respaldar" ) | crontab -
uv run python -m historia_viva.respaldo.restauracion restaurar --entorno /srv/historia-viva/.env --destino /srv/historia-viva/restauracion
```

**Verificación:** `respaldo estado` sale con 0, y la restauración da `Resultado: OK` con los
criterios de la restauración probada (conteos, las 3 consultas de referencia y la apertura de los
originales). Con el servidor vacío los conteos son 0 y eso también se compara. Al terminar se baja
el proyecto aparte (`historiaviva-restauracion`). Si falla: runbook R9 (restaurar un backup,
ticket 116).

### 8. Prueba de humo, solo en la VM

Con el corpus de prueba: una Pieza sintética que la prueba crea. El proveedor OIDC es el falso de
036, como servicio de prueba en `compose.humo.yml`, fuera de la receta de producción. Ese override
pone `HV_DESPLIEGUE=humo` en `web`: las mismas guardas del servidor, salvo que el emisor puede ser
http. Con `servidor`, que es lo que fija `compose.servidor.yml` y un `.env` no puede cambiar, un
`HV_OIDC_EMISOR` sin https no arranca.

Primero, con el `.env` todavía sin nada de la prueba, el paso 9 tiene que dar «El servidor está
vacío.»: `dc run --rm web estado-vacio`. Después, en el `.env` de la VM:
`HV_OIDC_EMISOR=http://oidc-falso:9000`, `HV_OIDC_CLIENTE_ID=humo`, `HV_OIDC_SECRETO` con
`openssl rand -hex 16`, `HV_TLS=internal` y `HV_HUMO_ADMIN` con el email del paso 6.

```bash
dc -f compose.humo.yml up -d oidc-falso
dc -f compose.humo.yml up -d web
dc -f compose.humo.yml run --rm humo
```

La prueba entra con OIDC y da de alta a la Coordinación y al RGC. El RGC carga un PDF, espera la
Ficha propuesta, la valida con los nueve obligatorios, espera a que aparezca en la búsqueda y
conversa: la respuesta tiene que citar esa Pieza. Después el Administrador técnico pide «Generar
ahora» la efeméride del 3 de mayo y el planificador la redacta. Por último, la Coordinación arma
el Informe de 1975, lo aprueban el RGC y la Coordinación, y se exporta a `.xlsx` y a `.docx`.
Cada paso espera hasta 30 minutos (CPU sin GPU).

Los `test_seg_*` estáticos corren contra el compose de la VM, desde el clon:

```bash
uv sync
uv run pytest -q -p no:cacheprovider tests/test_servidor.py tests/test_seguridad.py -k "seg_compose_del_servidor or seg_sin_el_override or seg_cabeceras_del_servidor or seg_la_receta or sin_innerhtml or sin_scripts or salida_modelo or (test_seg_cabeceras and not respuesta and not servidor) or corpus_real"
```

**Verificación:** la prueba de humo termina con «La prueba de humo pasó.» y sale con 0; si no,
dice en qué paso falló. Los `test_seg_*` pasan. Después, `dc -f compose.humo.yml run --rm web
estado-vacio` dice «El servidor NO está vacío.»: así se ve que el control del paso 9 detecta lo
que dejó la prueba (y el ingreso por el proveedor falso). Al terminar: sacar del `.env` los
valores de la prueba, `dc -f compose.humo.yml rm -sf oidc-falso` y `dc up -d web`.

### 9. El servidor real, vacío

```bash
dc run --rm web estado-vacio
docker ps -a --filter name=oidc-falso --format '{{.Names}}'
dc exec -T web printenv HV_DESPLIEGUE HV_OIDC_EMISOR
```

Las consultas de control, cada una en su módulo, son estas:

- Piezas, en cualquier estado y también las retiradas;
- tokens de Agente, aunque estén vencidos o revocados;
- firmas de ensayo (`metodo_autenticacion = 'dev'`) en Fichas, aprobaciones de Informe,
  efemérides y Relatos;
- el último evento de `api_agentes_habilitada`.

Además informa `Personas`, que no tiene que ser 0: son las del paso 6, ninguna de prueba.

**Verificación:** `estado-vacio` sale con 0 y dice `Piezas: 0`, `Tokens de Agente: 0`,
`Firmas de ensayo: 0`, `API de Agentes: apagada` y «El servidor está vacío.»
(`test_seg_traspaso_vacio`). `docker ps` no muestra ningún `oidc-falso`: el proveedor de la
prueba de humo nunca corrió acá. `printenv` da `servidor` y una línea vacía (el emisor es
Google). Los tokens del Agente 1 se emiten recién después, en el servidor (runbook R18 para
encender `/v1`).

## Alternativa sin internet

Si C50 dice que el servidor no sale a internet, lo que se baja se lleva en un medio (un disco
USB cifrado) desde una máquina con internet y Docker. Esa máquina no es la PC del MVP ni usa su
`.env` ni sus volúmenes de datos.

En la máquina con internet, con el mismo tag clonado y un `.env` cualquiera que pase `dc config`:

```bash
dc build && dc pull --ignore-buildable
docker save -o /media/medio/imagenes.tar $(dc config --images | sort -u)
dc up -d ollama && dc run --rm modelos
OLLAMA=$(dc config --images | grep ollama/ollama | head -1)
docker run --rm -v hvmvp_modelos:/datos:ro -v /media/medio:/medio --entrypoint tar "$OLLAMA" -czf /medio/modelos.tgz -C /datos .
git -C /opt/historia-viva bundle create /media/medio/historia-viva.bundle <tag-de-entrega>
```

`dc run --rm modelos` baja los modelos de la configuración vigente (los que siembran las
migraciones; en el servidor se cambian después por SQL, RF-32). Para Docker Engine, los paquetes
`.deb` de la misma versión (`docker-ce`, `docker-ce-cli`, `containerd.io`,
`docker-compose-plugin`) desde `download.docker.com`.

En el servidor, en lugar de los pasos 1, 2 y el `build` del 5:

```bash
sudo dpkg -i /media/medio/*.deb
git clone --branch <tag-de-entrega> /media/medio/historia-viva.bundle /opt/historia-viva
docker load -i /media/medio/imagenes.tar
docker volume create hvmvp_modelos
OLLAMA=$(dc config --images | grep ollama/ollama | head -1)
docker run --rm -v hvmvp_modelos:/datos -v /media/medio:/medio --entrypoint tar "$OLLAMA" -xzf /medio/modelos.tgz -C /datos
dc up -d --pull never
```

**Verificación:** `dc run --rm modelos` termina sin bajar nada y `/api/salud` con sesión de
Administrador técnico muestra `capa_modelos: ok` y `modelos_faltantes: []`. El compose fija las
imágenes por digest: si `dc up --pull never` no encuentra una `…@sha256:…` cargada con
`docker load`, es un desvío para el acta. Se resuelve con el almacén de imágenes de containerd de
Docker (conserva el digest) o etiquetando la imagen cargada con ese nombre. Sin internet tampoco
hay Let's Encrypt: `HV_TLS` con el certificado de IT. Los paquetes de `restic` y `uv` del paso 7
viajan en el mismo medio.

## Si falla

| Síntoma | Qué hacer |
|---|---|
| `dc config` dice «Falta …» | Completar esa variable en `/srv/historia-viva/.env` (paso 3) |
| `web` o `migrar` sale con 2 («En el servidor …») | Es la guarda: sacar `HV_ENTORNO`, poner `HV_AUTH=oidc`, completar `HV_OIDC_*` o corregir `HV_URL_PUBLICA`/`HV_ORIGEN`. Nunca se saca `HV_DESPLIEGUE` para que arranque |
| `/api/salud` queda en `degradado` | Ver el detalle con sesión de Administrador técnico: modelos bajando (esperar o la alternativa sin internet) o un trabajador caído (`dc logs` del trabajador) |
| El ingreso vuelve con `?error=proveedor-no-disponible` | El servidor no llega a `accounts.google.com` (C50) o el emisor está mal; con `?error=intento-vencido`, revisar la hora del servidor (`timedatectl`) |
| Caddy no saca el certificado | Con un correo en `HV_TLS`, el nombre tiene que ser público y el 80 alcanzable; si no, el certificado de IT |
| La prueba de humo falla en un paso | El mensaje dice cuál; `dc logs web` y `dc logs trabajador-procesamiento`. Se corrige la receta y se repite desde una VM limpia |
| `estado-vacio` dice «NO está vacío» en el servidor real | No se sigue: algo viajó o se corrió la prueba de humo donde no iba. Se vuelve a empezar desde una base vacía (`dc down -v` antes de que la SEU cargue nada) y se anota |

Los incidentes del servidor ya en uso siguen los runbooks de incidentes (116), y el encendido de
`/v1` para el Agente 1, el runbook R18 (114).

## Lo que queda para una persona

En esta máquina no corre Docker. Lo de contenedores se verificó con `docker compose config`, y la
prueba de humo pasó contra la app levantada en proceso, con el proveedor falso como servicio
aparte. Queda por ejecutar, en la VM (ticket 122) y después en el servidor:

- los nueve pasos tal como están, anotando desvíos (paso 1 con la versión de compose);
- que Caddy saque el certificado con `tls internal` y que `curl` llegue por 443 con la redirección
  del 80;
- la prueba de humo con `compose.humo.yml` y los modelos reales (tiempos de CPU);
- los `test_seg_*` estáticos desde el clon en la VM;
- respaldo y restauración en Linux con `cron` (el código se probó en Windows y con dobles);
- la alternativa sin internet, si C50 la pide;
- en el servidor real: el ingreso con el cliente OIDC de IT y `estado-vacio` en 0.
