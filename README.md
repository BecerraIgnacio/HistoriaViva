# Historia Viva

Archivo histórico institucional de la Facultad de Ingeniería del Ejército: recibe documentos del
Acervo de la Facultad, los enriquece con el contexto que aportan las personas, y los deja buscables
en lenguaje natural. Este repositorio es el MVP.

## Contenido

| Carpeta | Qué hay |
| --- | --- |
| `src/historia_viva/` | API FastAPI, web, trabajadores, capa de modelos y migraciones |
| `tests/` | La suite |
| `db/` | Arranque de Postgres con pgvector |
| `docs/` | Manual, guía de una página, despliegue y runbooks R1–R18 (la web los sirve en `/ayuda`), y el contrato OpenAPI `/v1` con el Agente 1 |
| `compose*.yml`, `Dockerfile`, `Caddyfile*` | Despliegue con Docker |

## Arranque

Requiere Docker Desktop. Los comandos se corren en la raíz del repositorio.

1. Copiar `.env.example` a un `.env` fuera del OneDrive y completarlo (las claves se generan con
   `openssl rand -hex 24`).
2. `docker compose --env-file <ruta>\.env build`
3. `docker compose --env-file <ruta>\.env up -d`. El servicio `modelos` baja los modelos de Ollama
   la primera vez.
4. `http://localhost/api/salud` responde `{"estado":"ok"}`. La web queda en `http://localhost`.

Instalación completa: [docs/despliegue-en-la-pc.md](docs/despliegue-en-la-pc.md). Arrancar y parar:
[docs/runbook-r1-arrancar-y-parar.md](docs/runbook-r1-arrancar-y-parar.md). Uso:
[docs/manual.md](docs/manual.md).

## Tests

```powershell
docker compose -f compose.pruebas.yml up -d
$env:HV_BD_URL_PRUEBAS = "postgresql://historia:historia@127.0.0.1:55433/historia_viva"
uv sync
uv run pytest -n auto
```

Los tests marcados `real` (modelos y servicios reales) no corren por defecto: `uv run pytest -m real`.
