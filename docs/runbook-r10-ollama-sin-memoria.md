# Runbook R10: Ollama sin memoria

Comandos de PowerShell, en la raíz del repositorio. `dc` abrevia `docker compose --env-file D:\hv\.env`.

## Cuándo

- `/api/salud` dice `degradado` con el motor caído o un modelo faltante, o la Conversación y la
  descripción de fotos dejan de responder.
- `ollama` (o `ollama-embeddings`) se reinició solo o figura `OOMKilled`:

  ```powershell
  docker inspect --format "{{.Name}} {{.State.OOMKilled}} {{.RestartCount}}" $(dc ps -aq ollama ollama-embeddings)
  ```

Mientras el motor no responde, el cortacircuito no deja tomar trabajos (DEC-86): la cola espera
`encolada` y nada pasa a fallido por eso. Solo puede quedar fallido el trabajo que el motor dejó
a mitad de una respuesta. La búsqueda sin texto y las pantallas siguen.

## Pasos

1. **Reiniciar el motor:** `dc restart ollama` (o `ollama-embeddings`, si fue ese) y esperar
   `healthy` en `dc ps`. Si vuelve a morir con la próxima foto, seguir.
2. **Un solo modelo cargado:** en `D:\hv\.env`, `OLLAMA_MAX_LOADED_MODELS=1` y `dc up -d ollama`.
   Cuesta recargar el modelo al pasar de visión a resumen; no cuesta memoria.
3. **Menos hilos:** en la pantalla de modelos (Administrador técnico; ticket 110), bajar
   `num_thread` de la fila de la capacidad que corría (`describir_imagen` o `resumir`). La
   edición queda validada y auditada (DEC-87); lo ya encolado conserva su configuración. Sin esa
   pantalla, este paso no se hace: no se edita `configuracion_capacidad` a mano.
4. **Cerrar cargas de Windows:** navegador con muchas pestañas, Teams, máquinas virtuales.
   `Get-Process | Sort-Object WS -Descending | Select-Object -First 10 Name, @{n='MB';e={[int]($_.WS/1MB)}}`.
5. **Retomar lo que quedó fallido:**

   ```powershell
   dc run --rm web reprocesar --fallidos
   ```

   Encola de nuevo el último trabajo fallido de cada Pieza. Las que ya llegaron a 6 reprocesos se
   saltean y se listan (runbook R12).

## Verificación

- `dc ps`: `ollama` y `ollama-embeddings` `healthy`; el `OOMKilled` de arriba en `false`.
- `/api/salud`: `"estado": "ok"`, sin modelos faltantes.
- La consulta de la cola de `docs/despliegue-en-la-pc.md` §2: los `encolados` bajan y
  `fallidos` no sube.
- Una foto de prueba llega a Pendiente.

## Si falla

- **Muere igual con un modelo y menos hilos:** la PC no da para ese modelo. Runbook R8 con uno
  más chico, y anotar el pico con `compose.techo.yml` (`docs/despliegue-en-la-pc.md` §5).
- **El modelo figura faltante después del reinicio:** `dc run --rm modelos`.
- **`reprocesar` saltea Piezas por el tope:** se revisa por qué fallan seis veces antes de
  `--forzar` (runbook R12).
- **Docker entero sin memoria** (`docker info` no responde): `wsl --shutdown`, abrir Docker
  Desktop y `dc up -d`; revisar `memory=` en `%USERPROFILE%\.wslconfig`.
