# Runbook R17: borrar el corpus semilla real y dejar constancia

Comandos de PowerShell, en la raíz del repositorio. `dc` abrevia `docker compose --env-file D:\hv\.env`. El
corpus semilla real (30 a 50 Piezas, con el aval de C49) vive en la PC solo durante el PPS: al
terminar se borra de todos lados y queda una constancia firmada (DEC-120, historia 227). Las
Conversaciones no se borran durante el MVP y se borran acá, junto con el corpus (R-19).

## Cuándo

Al terminar el PPS, después de congelar los resultados y la entrega. Una sola vez; antes, una vez
como ensayo con el corpus de prueba (se ejecuta igual y la constancia dice «ensayo»).

## Dónde vive el corpus real

| # | Dónde | Qué tiene | Se borra en el paso |
|---|---|---|---|
| 1 | Volumen `hvmvp_pgdata` | Piezas, Fichas y versiones, índice, Conversaciones (R-19), efemérides, Relatos, informes, auditoría | 3 |
| 2 | Volúmenes `hvrestauracion_*` | La copia de la restauración probada (runbook R9) | 3 |
| 3 | `HV_ORIGINALES` (bind mount) | Los originales | 4 |
| 4 | `HV_REFERENCIA` | Conjunto de referencia del banco (`piezas.csv`, `consultas.csv`, `evaluaciones.csv` de M4) y corridas | 4 |
| 5 | `HV_VOLCADOS` | Volcados (`.dump`, `.globales.sql`, `.referencia.json`) y `registro.jsonl` | 4 |
| 6 | Carpeta de la restauración probada (`--destino`, p. ej. `E:\hv-restauracion`) | Snapshot restaurado e informes | 4 |
| 7 | CSV y planillas de mediciones fuera de `HV_REFERENCIA` | Lo que se copió para analizar | 4 |
| 8 | Informes exportados `.docx` / `.xlsx` | En Descargas y donde se hayan guardado | 4 |
| 9 | Capturas y grabaciones con Piezas reales | Carpeta de capturas, Descargas, el repositorio | 4 |
| 10 | Caché del navegador de la PC | Originales abiertos en la pantalla partida | 4 |
| 11 | Snapshots de restic | `RESTIC_REPOSITORY` (disco externo) y su copia, si la hubo | 5 |
| 12 | USB de los originales y de restic | Copias | 5 |
| 13 | Google Drive | Repositorio de restic o réplica de originales, si se usó (124, 126) | 5 |
| 14 | Espacio libre de los discos y disco virtual de WSL | Bloques de lo borrado | 6 |

## Pasos

1. **Exportar a la entrega lo que no lleva contenido:** los resultados de M1 a M4 (los JSON,
   CSV y tablas del banco; las corridas se revisan para que no citen texto de Piezas) y la
   bitácora. Copiarlos fuera de las carpetas de la tabla.
2. **La lista para la constancia**, antes de borrar nada (sin contenido: solo hashes y conteos):

   ```powershell
   New-Item -ItemType Directory -Force D:\constancia
   dc exec -T postgres psql -X -A -t -U historia -d historia_viva -c "SELECT huella_original FROM acervo.pieza WHERE huella_original IS NOT NULL ORDER BY 1" |
     Set-Content -Encoding ascii D:\constancia\sha256.txt
   dc exec -T postgres psql -X -U historia -d historia_viva -c "SELECT estado, count(*) FROM acervo.pieza GROUP BY estado ORDER BY 1" |
     Set-Content -Encoding utf8 D:\constancia\piezas-por-estado.txt
   (Get-Content D:\constancia\sha256.txt | Measure-Object -Line).Lines
   ```

   La constancia y la lista de SHA-256 no son corpus: se guardan fuera de los discos que se
   borran (la entrega).
3. **Base y volúmenes** (la excepción al runbook R1). Primero el proyecto de la restauración: su
   `ollama-embeddings` monta `hvmvp_modelos` y, arriba, el `down -v` de la PC falla con
   «volume is in use».

   ```powershell
   docker compose --env-file D:\hv\.env -f compose.yml -f compose.restauracion.yml --project-name historiaviva-restauracion down -v
   dc down -v
   docker volume ls --filter name=hvmvp --filter name=hvrestauracion
   ```

4. **Carpetas y archivos** (filas 3 a 10). Con las rutas del `.env`:

   ```powershell
   Remove-Item -Recurse -Force D:\hv\originales, D:\hv\referencia, D:\hv\volcados, E:\hv-restauracion
   ```

   Después, a mano: las planillas y CSV copiados, los `.docx`/`.xlsx` exportados
   (`Get-ChildItem $env:USERPROFILE\Downloads -Include *.docx, *.xlsx -Recurse`), las capturas y
   grabaciones con Piezas reales (en el repositorio, `git log --stat -- docs` para ver si alguna
   entró), y el caché del navegador (Configuración → Privacidad → Borrar datos de navegación →
   Desde siempre).
5. **Backups:** todos los snapshots y el repositorio entero.

   ```powershell
   # Las dos variables, con los valores de D:\hv\.env.
   $env:RESTIC_REPOSITORY = "E:\restic"; $env:RESTIC_PASSWORD_FILE = "E:\restic.clave"
   $ids = restic snapshots --json | ConvertFrom-Json | ForEach-Object { $_.id }
   $ids.Count                                   # va a la constancia
   restic forget --prune $ids
   Remove-Item -Recurse -Force $env:RESTIC_REPOSITORY
   ```

   Si hubo Drive: borrar la carpeta del repositorio y la de la réplica de originales, y vaciar la
   papelera de Drive.

   **El USB**, con formato completo (no rápido). Primero identificarlo: la letra no se copia de
   este texto.

   ```powershell
   Get-Disk | Where-Object BusType -eq USB | Get-Partition | Get-Volume |
     Format-Table DriveLetter, FileSystemLabel, @{n='GB';e={[int]($_.Size/1GB)}}
   $usb = Read-Host "Letra del USB de la tabla de arriba (etiqueta y tamaño confirmados)"
   $v = Get-Volume -DriveLetter $usb
   if ((Get-Partition -DriveLetter $usb | Get-Disk).BusType -ne 'USB') { throw "$usb no es un USB" }
   "Se formatea $usb ($($v.FileSystemLabel), $([int]($v.Size/1GB)) GB)"
   Format-Volume -DriveLetter $usb -Full -Confirm
   ```

   `-Confirm` pregunta antes de borrar; si la letra, la etiqueta o el tamaño no son los del USB,
   se contesta que no.
6. **Espacio libre:** compactar el disco virtual de WSL (runbook R11, paso 5) y sobrescribir el
   espacio libre de cada unidad donde estuvo algo: `cipher /w:D:\` y `cipher /w:C:\` (tarda
   horas). La protección de fondo es BitLocker en todas las unidades (`manage-bde -status`).
7. **Constancia**, firmada por el alumno, al Director y a la SEU.

## Verificación

Cómo se verificó (va en la constancia):

```powershell
docker volume ls --filter name=hvmvp --filter name=hvrestauracion          # vacío
Test-Path D:\hv\originales, D:\hv\referencia, D:\hv\volcados, E:\restic      # todos False
$lista = [System.Collections.Generic.HashSet[string]](Get-Content D:\constancia\sha256.txt)
$hallados = Get-ChildItem C:\Users, D:\, E:\ -Recurse -File -ErrorAction SilentlyContinue |
  Get-FileHash -Algorithm SHA256 | Where-Object { $lista.Contains($_.Hash.ToLower()) }
$hallados.Count                                                            # 0
```

La búsqueda por hash recorre las unidades enteras: ninguno de los SHA-256 de la lista aparece en
ningún archivo, con cualquier nombre. No cubre el contenido convertido (una Ficha copiada en un
documento): eso lo cubren los pasos 1 y 4.

**Constancia de borrado del corpus semilla real (DEC-120)**

| Campo | Valor |
|---|---|
| Fecha del borrado | |
| Ensayo o borrado final | |
| Qué se borró | ___ Piezas (por estado: ___), sus Fichas, el índice, las Conversaciones, efemérides, Relatos e informes; lista de SHA-256 adjunta (`sha256.txt`, ___ líneas), sin contenido |
| Dónde estaba | PC (`hvmvp_pgdata`, `hvrestauracion_*`, `HV_ORIGINALES`, `HV_REFERENCIA`, `HV_VOLCADOS`, carpeta de restauración, Descargas, capturas, caché del navegador); disco externo de restic; USB; Drive: sí / no |
| Cómo se verificó | `docker volume ls` vacío; `Test-Path` en False; búsqueda por SHA-256 en C:, D: y E: con 0 coincidencias; `restic` sin repositorio; USB formateado; `cipher /w` en C: y D:; BitLocker activo |
| Desvíos | |
| Firma del alumno | |

Se entrega al Director y a la SEU.

## Si falla

- **Un volumen no se borra (`volume is in use`):** `dc ps -a` y `docker ps -a --filter volume=<nombre>`;
  bajar ese contenedor y repetir el paso 3.
- **La búsqueda por hash encuentra algo:** borrar ese archivo, anotar dónde estaba (va en
  «Desvíos») y repetir la verificación.
- **No hay forma de borrar una copia** (un USB perdido, un Drive sin acceso): se anota en la
  constancia como desvío y se avisa al Director y a la SEU.
