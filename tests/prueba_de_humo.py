"""La prueba de humo del servidor (runbook R16, paso 8; ticket 121): lo que hace la SEU, por HTTP.

Entra con OIDC, da de alta a las personas de la prueba, carga una Pieza del corpus de prueba, la
valida, conversa con Citas, genera una efeméride, arma un Informe, lo aprueba y lo exporta. Habla
solo HTTP, como un navegador: corre igual contra la app levantada en proceso (`test_servidor`) que
contra el compose de la VM (`compose.humo.yml`), donde el proveedor OIDC es el falso de 036, un
servicio de prueba fuera de la receta de producción. **Nunca contra el servidor real**: deja
Piezas, personas y firmas que el paso 9 exige que no estén.

    python prueba_de_humo.py --url https://historiaviva.fie.undef.edu.ar \\
        --emisor http://oidc-falso:9000 --admin tecnico@fie.undef.edu.ar [--ca root.crt]

`--admin` es el primer Administrador técnico (paso 6). Las personas de la prueba salen de su
dominio. Sale con 0 si pasa y con 1 en el primer paso que falla, diciendo cuál.

Solo importa la biblioteca estándar, `httpx` y `pymupdf`, que trae la imagen de la app: el
contenedor `humo` la corre montando `tests/` sin instalar nada.
"""

from __future__ import annotations

import argparse
import io
import json
import ssl
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx


TEXTO = (
    "Acta de la sesión del Consejo Académico del 3 de mayo de 1975. "
    "Se resolvió crear el laboratorio de ensayos de materiales de la Facultad, "
    "a cargo del Departamento de Ingeniería Civil."
)
"""El corpus de prueba de la prueba de humo: una sola Pieza, sintética (DEC-120)."""

PREGUNTA = "¿Cuándo se resolvió crear el laboratorio de ensayos de materiales?"

FECHA = "1975-05-03"

EDICION = {
    "version_base": 1,
    "tipo": "acta",
    "tipo_actividad": "Evento Académico",
    "fecha_inicio": FECHA,
    "precision_fecha": "dia",
    "origen": "Corpus de prueba de la prueba de humo",
    "propietario": "Secretaría de Extensión Universitaria",
    "resumen": "Acta del Consejo Académico que crea el laboratorio de ensayos de materiales.",
    "cuerpo": TEXTO,
    "contexto": "Pieza sintética de la prueba de humo del servidor (runbook R16).",
}
"""Lo que manda la pantalla partida con los nueve obligatorios (DEC-126): la Ficha queda
Validada."""


class FallaDeHumo(Exception):
    """Un paso de la prueba de humo no dio lo esperado."""


def pdf_del_corpus() -> bytes:
    """Un PDF de una página con texto, como un acta escaneada con OCR."""
    import pymupdf  # la imagen de la app lo trae: es lo que lee los PDF

    documento = pymupdf.open()
    pagina = documento.new_page()
    pagina.insert_textbox(pymupdf.Rect(72, 72, 520, 400), TEXTO, fontsize=12)
    contenido: bytes = documento.tobytes()
    documento.close()
    return contenido


@dataclass
class Navegador:
    """Una persona con su navegador. Las cookies van a mano: las `__Host-` son `Secure` y
    `httpx` no las manda por http, que es como corre la app en proceso. Sigue las redirecciones
    del ingreso entre la app (`url`) y el proveedor (`emisor`)."""

    url: str
    emisor: str
    ca: str | None = None
    """El certificado raíz con el que se verifica el TLS de la app; sin él, los del sistema."""
    espera: float = 30.0
    cookies: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.url = self.url.rstrip("/")
        self.emisor = self.emisor.rstrip("/")
        tiempo = httpx.Timeout(self.espera, connect=10.0)
        verificar = ssl.create_default_context(cafile=self.ca) if self.ca else True
        self._app = httpx.Client(base_url=self.url, verify=verificar, timeout=tiempo)
        self._proveedor = httpx.Client(base_url=self.emisor, timeout=tiempo)

    def cerrar(self) -> None:
        self._app.close()
        self._proveedor.close()

    def ir(self, metodo: str, url: str, **opciones: Any) -> httpx.Response:
        if url.startswith(self.emisor):
            cliente, ruta = self._proveedor, url.removeprefix(self.emisor)
        else:
            cliente, ruta = self._app, url.removeprefix(self.url)
        cabeceras = dict(opciones.pop("headers", {}))
        if self.cookies and cliente is self._app:
            cabeceras["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        respuesta = cliente.request(metodo, ruta, headers=cabeceras, **opciones)
        cliente.cookies.clear()  # las de la persona viven en `self.cookies`
        if cliente is self._app:
            for linea in respuesta.headers.get_list("set-cookie"):
                for nombre, morsel in SimpleCookie(linea).items():
                    if morsel["max-age"] == "0" or not morsel.value:
                        self.cookies.pop(nombre, None)
                    else:
                        self.cookies[nombre] = morsel.value
        return respuesta

    def entrar(self, email: str) -> None:
        """«Entrar con Google» de punta a punta: la app, el proveedor y la vuelta."""
        self.cookies.clear()
        respuesta = self.ir("GET", "/auth/google")
        for _ in range(5):
            if respuesta.status_code != 302:
                break
            destino = respuesta.headers["location"]
            if destino.startswith(self.emisor):
                # El proveedor falso decide quién entra por la query (036).
                destino += "&" + urlencode({"email": email})
            elif "error=" in urlsplit(destino).query:
                raise FallaDeHumo(f"{email} no pudo entrar: {destino}")
            elif not urlsplit(destino).path.startswith("/auth/"):
                break
            respuesta = self.ir("GET", destino)
        if "__Host-hv_sesion" not in self.cookies:
            raise FallaDeHumo(f"{email} no tiene sesión después del ingreso ({respuesta.status_code}).")

    def api(self, metodo: str, ruta: str, **opciones: Any) -> httpx.Response:
        """Un pedido de la pantalla: toda mutación lleva el `Origin` propio y `X-HV` (DEC-37)."""
        cabeceras = {"Origin": self.url, "X-HV": "1", **opciones.pop("headers", {})}
        return self.ir(metodo, ruta, headers=cabeceras, **opciones)


def _esperado(respuesta: httpx.Response, *estados: int) -> dict[str, Any]:
    if respuesta.status_code not in estados:
        raise FallaDeHumo(
            f"{respuesta.request.method} {respuesta.request.url.path}: {respuesta.status_code},"
            f" se esperaba {' o '.join(map(str, estados))}. {respuesta.text[:500]}"
        )
    if respuesta.headers.get("content-type", "").startswith(("application/json", "application/problem")):
        cuerpo: dict[str, Any] = respuesta.json()
        return cuerpo
    return {}


def _hasta[T](que: str, intento: Callable[[], T | None], espera: float, cada: float = 1.0) -> T:
    """Sondea como la pantalla hasta que `intento` devuelve algo."""
    limite = time.monotonic() + espera
    while True:
        valor = intento()
        if valor is not None:
            return valor
        if time.monotonic() > limite:
            raise FallaDeHumo(f"{que}: no pasó en {espera:.0f} s.")
        time.sleep(cada)


def _eventos(texto: str) -> list[tuple[str, Any]]:
    eventos = []
    for bloque in texto.split("\n\n"):
        lineas = [linea for linea in bloque.split("\n") if not linea.startswith(":")]
        if len(lineas) == 2 and lineas[0].startswith("event: "):
            eventos.append((lineas[0].removeprefix("event: "), json.loads(lineas[1][6:])))
    return eventos


def correr(
    *,
    url: str,
    emisor: str,
    admin: str,
    ca: str | None = None,
    espera: float = 1800.0,
    informar: Callable[[str], None] = print,
) -> None:
    """Los pasos de la prueba de humo, en orden. Lanza `FallaDeHumo` en el primero que falla."""
    dominio = admin.rpartition("@")[2]
    coordinador_email = f"humo-coordinador@{dominio}"
    rgc_email = f"humo-rgc@{dominio}"
    personas = {
        email: Navegador(url, emisor, ca=ca, espera=espera)
        for email in (admin, coordinador_email, rgc_email)
    }
    tecnico, coordinador, rgc = personas.values()
    try:
        informar("1. La salud responde «ok».")
        _hasta(
            "La salud en «ok»",
            lambda: True if tecnico.ir("GET", "/api/salud").json() == {"estado": "ok"} else None,
            espera,
            cada=5.0,
        )

        informar("2. El Administrador técnico entra con OIDC y da de alta a la Coordinación y al RGC.")
        tecnico.entrar(admin)
        for email, nombre, rol in (
            # El Coordinador primero: el alta del primer RGC termina el interinato (R-01).
            (coordinador_email, "Coordinación de la prueba de humo", "coordinador"),
            (rgc_email, "RGC de la prueba de humo", "rgc"),
        ):
            alta = tecnico.api(
                "POST", "/api/admin/personas", json={"email": email, "nombre": nombre, "rol": rol}
            )
            # 409: ya estaba, de una corrida anterior en la misma VM.
            _esperado(alta, 201, 409)
        coordinador.entrar(coordinador_email)
        rgc.entrar(rgc_email)

        informar("3. El RGC carga un PDF del corpus de prueba y espera la Ficha propuesta.")
        subida = rgc.api(
            "POST",
            "/api/piezas",
            files={"archivo": ("acta-laboratorio.pdf", pdf_del_corpus(), "application/pdf")},
        )
        pieza_id = str(_esperado(subida, 201)["pieza_id"])

        def propuesta() -> dict[str, Any] | None:
            detalle = _esperado(rgc.api("GET", f"/api/piezas/{pieza_id}"), 200)
            if detalle.get("error_procesamiento"):
                raise FallaDeHumo(f"El procesamiento falló: {detalle['error_procesamiento']}")
            return detalle if detalle.get("estado") == "Pendiente" else None

        _hasta("La Pieza en Pendiente", propuesta, espera)

        informar("4. La valida con los nueve obligatorios.")
        guardado = _esperado(rgc.api("PUT", f"/api/piezas/{pieza_id}/ficha", json=EDICION), 200)
        if guardado.get("estado") != "Validada":
            raise FallaDeHumo(f"La Ficha quedó {guardado.get('estado')}: {guardado}")

        def indexada() -> bool | None:
            busqueda = rgc.api("GET", "/api/busqueda", params={"q": "laboratorio de ensayos"})
            if busqueda.status_code == 503 and "indice-no-disponible" in busqueda.text:
                return None  # el índice vacío, hasta que entre el primer fragmento
            resultados = _esperado(busqueda, 200).get("resultados", [])
            return True if any(r.get("pieza_id") == pieza_id for r in resultados) else None

        _hasta("La Pieza en la búsqueda", indexada, espera)

        informar("5. Conversa y la respuesta trae Citas verificadas de la Pieza.")
        conversacion = _esperado(rgc.api("POST", "/api/conversaciones"), 201)["conversacion_id"]
        turno = rgc.api(
            "POST",
            f"/api/conversaciones/{conversacion}/mensajes",
            json={"texto": PREGUNTA, "filtros": {}},
        )
        _esperado(turno, 200)
        eventos = dict(_eventos(turno.text))
        citas = [c for c in eventos.get("citas", {}).get("citas", []) if c.get("verificada")]
        if "fin" not in eventos or not any(c.get("pieza_id") == pieza_id for c in citas):
            raise FallaDeHumo(f"La respuesta no cita la Pieza: {list(eventos)} {eventos.get('citas')}")

        informar("6. «Generar ahora» la efeméride del 3 de mayo y el planificador la redacta.")
        objetivo = {"tipo": "dia", "fecha": f"{date.today().year}-05-03"}
        pedido = tecnico.api("POST", "/api/admin/efemerides/generacion", json={"objetivo": objetivo})
        # 409: ya hay una de ese día, de una corrida anterior en la misma VM.
        _esperado(pedido, 202, 409)

        def efemeride() -> dict[str, Any] | None:
            lista = _esperado(rgc.api("GET", "/api/efemerides"), 200)["efemerides"]
            return next((e for e in lista if (e["dia"], e["mes"]) == (3, 5)), None)

        _hasta("La efeméride del 3 de mayo", efemeride, espera, cada=5.0)

        informar("7. La Coordinación arma el Informe de 1975; lo aprueban el RGC y la Coordinación.")
        informe = _esperado(
            coordinador.api(
                "POST", "/api/informes", json={"desde": "1975-01-01", "hasta": "1975-12-31"}
            ),
            201,
        )
        informe_id = informe["informe_id"]
        incluidas = [p["pieza_id"] for g in informe.get("grupos", []) for p in g["piezas"]]
        if pieza_id not in incluidas:
            raise FallaDeHumo(f"El Informe no incluye la Pieza: {incluidas}")
        _esperado(rgc.api("POST", f"/api/informes/{informe_id}/aprobaciones"), 201)
        aprobado = _esperado(
            coordinador.api("POST", f"/api/informes/{informe_id}/aprobaciones"), 201
        )
        if aprobado.get("estado") != "Aprobado":
            raise FallaDeHumo(f"El Informe quedó {aprobado.get('estado')}.")

        informar("8. Lo exporta a .xlsx y a .docx.")
        for formato, parte in (("xlsx", "xl/workbook.xml"), ("docx", "word/document.xml")):
            archivo = coordinador.api(
                "GET", f"/api/informes/{informe_id}/exportacion", params={"formato": formato}
            )
            _esperado(archivo, 200)
            if f".{formato}" not in archivo.headers.get("content-disposition", ""):
                raise FallaDeHumo(f"El .{formato} no viene como adjunto.")
            with zipfile.ZipFile(io.BytesIO(archivo.content)) as comprimido:
                if parte not in comprimido.namelist():
                    raise FallaDeHumo(f"El .{formato} no tiene {parte}.")
    finally:
        for navegador in personas.values():
            navegador.cerrar()
    informar("La prueba de humo pasó.")


def main(argv: list[str] | None = None) -> int:
    lector = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    lector.add_argument("--url", required=True, help="HV_URL_PUBLICA del compose de la VM")
    lector.add_argument("--emisor", required=True, help="el del proveedor OIDC falso")
    lector.add_argument("--admin", required=True, help="email del Administrador técnico")
    lector.add_argument("--ca", help="certificado raíz de Caddy (`tls internal`)")
    lector.add_argument("--espera", type=float, default=1800.0, help="segundos por paso")
    leidos = lector.parse_args(argv)
    try:
        correr(
            url=leidos.url,
            emisor=leidos.emisor,
            admin=leidos.admin,
            ca=leidos.ca,
            espera=leidos.espera,
        )
    except FallaDeHumo as falla:
        print(f"FALLÓ: {falla}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
