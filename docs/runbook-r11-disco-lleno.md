# Runbook R11: disco lleno

Comandos de PowerShell, en la raíz del repositorio. `dc` abrevia `docker compose --env-file D:\hv\.env`.

## Cuándo

- Una subida responde 507: la unidad de `HV_ORIGINALES` no tiene el tamaño del archivo más
  2 GiB libres (DEC-112).
- `postgres` se cae o no escribe (`No space left on device` en `dc logs postgres`), o Docker
  Desktop avisa que el disco virtual está lleno.
- La revisión de antes de cada sesión (`docs/despliegue-en-la-pc.md` §2) da menos de 20 GB
  libres.

## Pasos

1. **Medir dónde está el espacio:**

   ```powershell
   Get-PSDrive C, D | Select-Object Name, @{n='LibreGB';e={[int]($_.Free/1GB)}}
   docker system df
   ```

2. **Limpiar Docker sin tocar volúmenes:** `docker system prune` (contenedores parados, redes,
   imágenes colgadas y caché de construcción). **Nunca `--volumes`**: ahí viven la base
   (`hvmvp_pgdata`) y los modelos. Si quedó el proyecto de la restauración probada, bajarlo con su
   `down -v` (runbook R9, paso 4): sus volúmenes son `hvrestauracion_*`.
3. **Volcados viejos:** la poda semanal borra de `HV_VOLCADOS` los de más de 7 días (ya están en
   los snapshots). Para adelantarla, con `uv run python -m historia_viva.respaldo --entorno D:\hv\.env estado`
   en 0:

   ```powershell
   Get-ChildItem D:\hv\volcados -File |
     Where-Object { $_.Name -match '^\d{8}T\d{6}Z-' -and $_.LastWriteTime -lt (Get-Date).AddDays(-7) } |
     Remove-Item
   ```
4. **Mover al USB lo que no es del Acervo:** videos y descargas propios que esperan subirse o ya
   se subieron. Los archivos de `HV_ORIGINALES` y `HV_VOLCADOS` **no se mueven**: un original
   movido es un faltante para `verificar-originales`.
5. **Compactar el disco virtual de WSL** (Docker no devuelve solo el espacio que liberó):

   ```powershell
   dc stop
   wsl --shutdown
   # Consola de administrador. La ruta según la versión de Docker Desktop:
   #   %LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx  o  %LOCALAPPDATA%\Docker\wsl\data\ext4.vhdx
   diskpart
   #   select vdisk file="C:\Users\<usuario>\AppData\Local\Docker\wsl\disk\docker_data.vhdx"
   #   attach vdisk readonly
   #   compact vdisk
   #   detach vdisk
   #   exit
   ```

   Abrir Docker Desktop y `dc up -d` (runbook R1).
6. **Ampliar:** si nada alcanza, mover la *Disk image location* de Docker Desktop a una unidad
   con más espacio (*Settings* → *Resources* → *Advanced*, fuera del OneDrive) o liberar la
   unidad de `HV_ORIGINALES`.

## Verificación

- `Get-PSDrive`: más de 20 GB libres en la unidad de `HV_ORIGINALES` y en la del disco virtual.
- `docker system df` muestra lo recuperado; `dc ps`, todo `running`.
- `/api/salud` en `"estado": "ok"` y una subida de prueba pasa (sin 507).
- `dc exec -T web python -m historia_viva.respaldo.restauracion verificar-originales` sale con 0.

## Si falla

- **`postgres` no arranca después de llenarse:** no borrar nada de su volumen. Liberar espacio
  y `dc up -d postgres`; si sigue sin arrancar, runbook R9.
- **`compact vdisk` no achica:** correr antes `docker system prune` y repetir con Docker
  cerrado; si Docker Desktop tiene *Resource Saver*, salir del todo (ícono de la bandeja → *Quit*).
- **El 507 sigue con espacio libre:** el chequeo es sobre la unidad del bind mount de
  `HV_ORIGINALES`, no sobre C:. Revisar a qué unidad apunta en `D:\hv\.env`.
