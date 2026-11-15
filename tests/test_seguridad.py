"""Las pantallas por dentro (spec, módulo `web`): lo que trae el Acervo o el servidor se pinta
como texto y toda mutación lleva `X-HV: 1` (DEC-37, DEC-45).

Se leen los HTML servidos y su script de `/estaticos` tal como están (039: nada en línea).
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient

from aplicacion import ORIGEN
from historia_viva.api.app import DIRECTORIO_WEB
from historia_viva.composicion import main

MVP = Path(__file__).resolve().parents[1]

PANTALLAS = sorted(DIRECTORIO_WEB.glob("*.html"))
# Las arma el servidor con todo el texto escapado (118: `api/ayuda.py`) y no cargan ningún script:
# no tienen `.js` propio que revisar, y para ellas se exige que no haya `<script>`.
SIN_SCRIPT = {"ayuda.html"}
CON_SCRIPT = [p for p in PANTALLAS if p.name not in SIN_SCRIPT]


@pytest.mark.rnf("RNF-21")
@pytest.mark.parametrize("pantalla", PANTALLAS, ids=[p.name for p in PANTALLAS])
def test_seg_sin_innerhtml(pantalla):
    html = pantalla.read_text(encoding="utf-8")
    if pantalla.name in SIN_SCRIPT:
        assert not re.findall(r"<script\b", html, re.IGNORECASE)
        return
    # Con los scripts que comparte con otras pantallas (084: `piezas.js`).
    scripts = re.findall(r'<script src="/estaticos/([^"]+)"', html)
    assert f"{pantalla.stem}.js" in scripts
    codigo = html + "".join(
        (DIRECTORIO_WEB / "estaticos" / script).read_text(encoding="utf-8") for script in scripts
    )

    # Un nombre de archivo o una transcripción con `<img onerror>` se mostraría como HTML.
    assert not re.findall(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", codigo)
    mutaciones = list(re.finditer(r'method:\s*"(POST|PUT|PATCH|DELETE)"', codigo))
    for mutacion in mutaciones:
        llamada = max(
            codigo.rfind("fetch(", 0, mutacion.start()), codigo.rfind("pedir(", 0, mutacion.start())
        )
        if codigo.startswith("pedir(", llamada):
            # `pedir` pone la cabecera en todo pedido: está en su cuerpo, antes de que cierre.
            definicion = codigo.index("async function pedir(")
            assert '"X-HV": "1"' in codigo[definicion : codigo.index("\n}\n", definicion)]
            continue
        pedido = codigo[llamada : codigo.index(");", mutacion.end())]
        assert '"X-HV": "1"' in pedido or (
            "CSRF" in pedido and 'const CSRF = { "X-HV": "1" };' in codigo
        ), pedido
    assert mutaciones or pantalla.name not in ("index.html", "validar.html", "admin.html")
    # La carga múltiple sube con XMLHttpRequest para ver el avance (083).
    if re.search(r'\.open\("(POST|PUT|PATCH|DELETE)"', codigo):
        assert "xhr.setRequestHeader(cabecera, valor)" in codigo
        assert 'const CSRF = { "X-HV": "1" };' in codigo


CSP = (
    "default-src 'self'; img-src 'self'; media-src 'self'; frame-src 'self'; "
    "frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
)
"""La política de la tabla de encabezados del spec (DEC-109): sin `'unsafe-inline'`."""


@pytest.mark.rnf("RNF-21")
@pytest.mark.confirma("la app confirma la subida de la Pieza desde su pool")
def test_seg_cabeceras_de_respuesta(cliente, pieza_pendiente):
    respuestas = {
        ruta: cliente.get(ruta)
        for ruta in (
            "/",
            "/validar.html",
            "/admin.html",
            "/conversacion.html",
            "/lote.html",
            "/informes.html",
            "/lotes.html",
            "/efemerides.html",
            "/api/yo",
            "/api/no-existe",
            f"/api/piezas/{pieza_pendiente}/original",
        )
    }
    cliente.cookies.clear()
    respuestas["/api/yo sin sesión"] = cliente.get("/api/yo")

    for ruta, respuesta in respuestas.items():
        cabeceras = respuesta.headers
        assert cabeceras["content-security-policy"] == CSP, ruta
        assert cabeceras["x-content-type-options"] == "nosniff", ruta
        assert cabeceras["referrer-policy"] == "no-referrer", ruta
        assert cabeceras["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
        assert cabeceras["cross-origin-opener-policy"] == "same-origin", ruta
        assert cabeceras["cross-origin-resource-policy"] == "same-origin", ruta
        assert "server" not in cabeceras, ruta
        # En la PC, por HTTP: HSTS solo en el servidor de la universidad con HTTPS.
        assert "strict-transport-security" not in cabeceras, ruta
    assert respuestas["/api/yo sin sesión"].status_code == 401
    # El original lo guarda el navegador (spec, `Cache-Control` de `…/original`); el resto, no.
    original = respuestas.pop(f"/api/piezas/{pieza_pendiente}/original")
    assert original.headers["cache-control"] == "private, max-age=86400"
    assert {r: c.headers.get("cache-control") for r, c in respuestas.items()} == dict.fromkeys(
        respuestas, "no-store"
    )


@pytest.mark.rnf("RNF-21")
@pytest.mark.parametrize("pantalla", PANTALLAS, ids=[p.name for p in PANTALLAS])
def test_seg_sin_scripts_ni_estilos_en_linea(pantalla):
    html = pantalla.read_text(encoding="utf-8")

    # Con la CSP sin `'unsafe-inline'` el navegador no correría nada de esto (DEC-109).
    assert not re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", html)
    assert not re.findall(r"<style\b", html)
    assert not re.findall(r"\son[a-z]+\s*=|\sstyle\s*=|javascript:", html, re.IGNORECASE)
    recursos = re.findall(r'<(?:script|link)\b[^>]*\s(?:src|href)="([^"]+)"', html)
    assert recursos
    for recurso in recursos:
        assert recurso.startswith("/estaticos/"), recurso
        assert (DIRECTORIO_WEB / recurso.lstrip("/")).is_file(), recurso


@pytest.mark.rnf("RNF-21")
def test_seg_error_no_manejado_sin_traza_ni_sql_y_con_cabeceras(cliente):
    app = cliente.app

    @app.get("/api/falla-de-prueba")
    def fallar() -> None:
        raise psycopg.errors.UndefinedTable('no existe "secreta"\nSELECT clave FROM secreta')

    respuesta = TestClient(app, raise_server_exceptions=False).get("/api/falla-de-prueba")

    assert respuesta.status_code == 500
    assert respuesta.headers["content-type"] == "application/problem+json"
    assert respuesta.json()["type"] == "/errores/error-interno"
    assert not re.findall(r"secreta|SELECT|Traceback|psycopg", respuesta.text)
    assert respuesta.headers["content-security-policy"] == CSP
    assert respuesta.headers["cache-control"] == "no-store"


@pytest.mark.rnf("RNF-21")
def test_seg_web_sin_cabecera_server(url_app, tmp_path, monkeypatch):
    arrancada: dict = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **opciones: arrancada.update(app=app, **opciones)
    )
    for variable, valor in {
        "HV_BD_URL": url_app,
        "HV_ORIGINALES": str(tmp_path),
        "HV_MOTOR_URL": "http://127.0.0.1:1",
        "HV_AUTH": "dev",
        "HV_ENTORNO": "desarrollo",
        "HV_ORIGEN": ORIGEN,
    }.items():
        monkeypatch.setenv(variable, valor)
    assert main(["web"]) == 0
    arrancada["app"].state.bd.cerrar()

    # uvicorn pone `server: uvicorn` por su cuenta; Caddy quita el suyo (`Caddyfile`).
    assert uvicorn.Config(**arrancada).server_header is False


@pytest.mark.rnf("RNF-21")
@pytest.mark.parametrize("pantalla", CON_SCRIPT, ids=[p.name for p in CON_SCRIPT])
def test_seg_salida_modelo_texto(pantalla):
    """Un enlace o una imagen en la salida de un modelo no se vuelve activo: todo `href` y
    `src` que arman las pantallas es una ruta propia, con los ids escapados. La salida se pinta
    con `textContent` (`test_seg_sin_innerhtml`) y la CSP no deja traer imágenes de afuera
    (`img-src 'self'`, `test_seg_cabeceras_de_respuesta`)."""
    codigo = (DIRECTORIO_WEB / "estaticos" / f"{pantalla.stem}.js").read_text(encoding="utf-8")
    definiciones = dict(re.findall(r"const (\w+) = (`[^`]*`|\"[^\"]*\");", codigo))

    asignaciones = re.findall(
        r"\.(?:href|src)\s*=\s*([^;]+);|setAttribute\(\"(?:href|src)\",([^;]+);", codigo
    )
    assert asignaciones
    for valor in ("".join(partes).strip() for partes in asignaciones):
        valor = definiciones.get(valor, valor)
        if valor == "URL.createObjectURL(await respuesta.blob())":
            continue  # 070: el archivo que bajó `fetch` de una ruta propia, no texto de un modelo
        literales = re.findall(r"`[^`]*`|\"[^\"]*\"", valor)
        assert literales, valor
        for literal in literales:
            assert literal[1] == "/" and literal[2:3] != "/", literal
            assert all(
                interpolado.startswith("encodeURIComponent(")
                for interpolado in re.findall(r"\$\{([^}]*)\}", literal)
            ), literal


@pytest.mark.rnf("RNF-21")
def test_seg_cabeceras():
    """El proxy local (spec, tabla de Caddy): tope de cuerpo por ruta con condiciones que no se
    superponen, tiempo máximo de lectura, sin `Server` y sin HSTS en la PC."""
    caddyfile = (MVP / "Caddyfile").read_text(encoding="utf-8")
    activo = "\n".join(linea.split("#")[0] for linea in caddyfile.splitlines())

    # Dos `request_body` que coinciden se componen y gana el menor: la subida de video no
    # podría pasar de 45 MB.
    video = re.search(r"@(\w+) path /api/piezas/video\n", activo)
    resto = re.search(r"@(\w+) not path /api/piezas/video\n", activo)
    assert video and resto
    assert re.search(rf"request_body @{video[1]} \{{\s*max_size 2050MiB\s*\}}", activo)
    assert re.search(rf"request_body @{resto[1]} \{{\s*max_size 45MB\s*\}}", activo)
    assert len(re.findall(r"request_body", activo)) == 2
    # Sin tope de tiempo, una subida lenta ocupa un hilo de la app (007).
    assert re.search(r"servers \{\s*timeouts \{\s*read_body \d+m\s*\}\s*\}", activo)
    assert re.search(r"\n\s*header -Server\n", activo)
    assert "Strict-Transport-Security" not in activo
    assert "Strict-Transport-Security max-age=31536000" in caddyfile
    assert re.search(r"\n\s*header -Via\n", activo)
    # Un solo sitio, y el flujo de la Conversación y los 206 sin búfer.
    assert len(re.findall(r"^[^\s{}][^\n]*\{$", activo, re.M)) == 1
    assert re.search(r"reverse_proxy web:8000 \{\s*flush_interval -1\s*\}", activo)


@pytest.mark.rnf("RNF-14")
def test_seg_corpus_real_fuera_del_repositorio():
    """El conjunto de referencia del banco (`HV_REFERENCIA`), sus corridas y el corpus semilla
    real quedan fuera de git (DEC-97, DEC-120). `gitleaks` corre aparte, en CI."""
    import subprocess

    raiz = MVP.parent

    def git(*argumentos: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *argumentos], cwd=raiz, capture_output=True, text=True, check=False
        )

    ignoradas = [
        "mvp/referencia/piezas.csv", "referencia/originales/acta.pdf",
        "mvp/referencia/corridas/m2.json", "corpus-semilla/foto.jpg",
    ]
    assert git("check-ignore", *ignoradas).stdout.split() == ignoradas
    rastreados = git("ls-files").stdout.splitlines()
    assert rastreados, "sin git no se puede revisar"
    assert [
        ruta for ruta in rastreados
        if re.search(r"(^|/)(referencia|corpus-semilla)/|(^|/)(piezas|consultas)\.csv$", ruta)
    ] == []
