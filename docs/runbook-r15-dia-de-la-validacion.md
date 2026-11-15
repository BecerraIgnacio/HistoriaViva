# Runbook R15: día de la validación

Comandos de PowerShell, en la raíz del repositorio. `dc` abrevia `docker compose --env-file D:\hv\.env`. Toda
interacción de la SEU es presencial, en la PC (DEC-119). Se ejecuta completo una vez como
**ensayo general** (con las personas de prueba) y otra el día de la sesión (con las de la SEU).

## Cuándo

El día de una sesión con la SEU (la Validación nº 2 y su ensayo), desde una hora antes.

## Pasos

1. **Volcado antes de la sesión** (RPO ≈ 0 alrededor de las sesiones, spec §2.3):

   ```powershell
   uv run python -m historia_viva.respaldo --entorno D:\hv\.env respaldar --motivo antes-de-sesion
   ```

2. **La PC:** cargador enchufado; plan de energía Equilibrado o Alto rendimiento
   (`powercfg /getactivescheme`); Windows Update en pausa; cerrar navegadores con muchas
   pestañas, Teams, máquinas virtuales y todo lo pesado.
3. **La pila:** `dc up -d` y esperar `healthy` en `dc ps` (`postgres`, `web`, `ollama`,
   `ollama-embeddings`).
4. **Salud:** en el navegador de la PC, `http://localhost/api/salud` con sesión de Administrador
   técnico: `"estado": "ok"`, los tres trabajadores `vivo: true`, sin modelos faltantes e
   `indice.incompleto` en `false`.
5. **Precalentar los modelos:**
   - La búsqueda (el modelo de `embeber` queda residente): `dc run --rm web consultas-de-referencia`
     sale con 0.
   - La generación: una pregunta en la Conversación sobre el corpus de prueba, a menos de 5 min
     de empezar (`ollama` descarga el modelo a los 5 min sin uso, `OLLAMA_KEEP_ALIVE`).
6. **Ingreso de las dos personas**, cada una en la PC y con su cuenta: en el ensayo, dos de las
   personas de prueba (con `HV_AUTH=dev`, `/auth/dev` con `mrossi@fie.undef.edu.ar` y
   `jperalta@fie.undef.edu.ar`); el día de la sesión, las dos de la SEU dadas de alta por el
   runbook R3. Cada una entra, ve su pantalla de inicio y sale.
7. **A mano:** la guía de una página impresa (`docs/guia-de-una-pagina.html`) y el plan B:
   el proyector con las capturas locales del recorrido, hechas con el corpus de prueba (nunca
   con Piezas reales, runbook R17).
8. **Durante la sesión:** se sigue el guion; si algo se corta, `/api/salud` y el runbook que
   corresponda (R10 motor, R11 disco, R12 trabajo colgado, R7 frenar todo).
9. **Volcado al terminar:**

   ```powershell
   uv run python -m historia_viva.respaldo --entorno D:\hv\.env respaldar --motivo despues-de-sesion
   ```

## Verificación

- `uv run python -m historia_viva.respaldo --entorno D:\hv\.env estado` sale con 0 y su última
  corrida es la del paso 9.
- `/api/salud` dio `ok` antes de empezar y las dos personas entraron.
- Planilla, una fila por ejecución:

| Fecha | Quién | Ensayo o sesión | Volcado antes | Salud ok | Precalentado (s) | Ingreso 1 | Ingreso 2 | Volcado después | Desvíos |
|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | |

## Si falla

- **El volcado del paso 1 falla:** `uv run python -m historia_viva.respaldo --entorno D:\hv\.env estado`
  dice qué; sin volcado previo, la sesión no carga material nuevo.
- **La salud no da `ok`:** el campo que falla manda: motor o modelo (runbook R10), trabajador
  caído (`dc up -d <servicio>`), disco (runbook R11), índice incompleto (runbook R13).
- **Una persona no puede entrar:** con `dev`, que su email esté dado de alta y activo (runbook R3);
  con Google, que use la cuenta de la FIE. Mientras tanto, la otra persona sigue.
- **La PC o la pila no levantan a tiempo:** plan B, el proyector con las capturas locales; la
  sesión se reprograma para lo que necesite el sistema en vivo.
