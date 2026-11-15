"""El manual en `/ayuda` (ticket 118): se sirve sin romper la CSP y sin HTML activo, enlaza los
runbooks, dice lo que el ticket pide, y una revisión automática falla con un enlace roto o con
una captura que no viene del corpus de prueba."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from historia_viva.api import ayuda
from historia_viva.api.app import DIRECTORIO_WEB
from historia_viva.api.cabeceras import CSP, MiddlewareCabeceras

pytestmark = pytest.mark.historia(244)

DOCS = ayuda.DIRECTORIO_DOCS
MANUAL = (DOCS / "manual.md").read_text(encoding="utf-8")
GUIA = DOCS / "guia-de-una-pagina.html"

MARCA_DE_CAPTURA = ("hv-corpus", "prueba")
"""Una captura del manual lleva en el PNG el texto `hv-corpus=prueba`: se tomó sobre el corpus de
prueba (sintético, historia 214). Una captura sin la marca puede mostrar Piezas reales (riesgo 47,
runbook R17) y la revisión la rechaza."""

NO_SON_DE_LA_PANTALLA = {
    "así",
    "esta Ficha está bien y completa",
    "interino",  # la marca de la auditoría, que la franja de Personas nombra entre comillas
    "el sistema está lento",  # la frase de la guía de una página
    "runbook R1",
    "runbook R18",
    "Runbook Rn",
    "rebanada Rn",
    "runbook Rn",
    "Rn",
    "Un día como hoy…",
    "En octubre…",
}
"""Lo que el manual pone entre «» sin ser un texto de la pantalla."""


@pytest.fixture
def web() -> TestClient:
    app = FastAPI()
    app.add_middleware(MiddlewareCabeceras)
    app.include_router(ayuda.router)
    return TestClient(app)


def _capitulos(markdown: str) -> dict[str, str]:
    partes = re.split(r"^## (.+)$", markdown, flags=re.M)
    return dict(zip(partes[1::2], partes[2::2], strict=True))


def _plano(texto: str) -> str:
    return " ".join(texto.split())


def problemas(docs: Path, nombre: str = "manual") -> list[str]:
    """La revisión automática: cada enlace lleva a un documento y un ancla que existen, y cada
    imagen es una captura de `capturas/` con la marca del corpus de prueba."""
    salida = []
    for es_imagen, destino in ayuda.enlaces((docs / f"{nombre}.md").read_text(encoding="utf-8")):
        if es_imagen:
            captura = docs / destino
            if not re.fullmatch(r"capturas/[a-z0-9][a-z0-9-]*\.png", destino) or not captura.is_file():
                salida.append(f"captura que /ayuda no sirve: {destino}")
            else:
                with Image.open(captura) as imagen:
                    marca = getattr(imagen, "text", {}).get(MARCA_DE_CAPTURA[0])
                if marca != MARCA_DE_CAPTURA[1]:
                    salida.append(f"captura sin la marca del corpus de prueba: {destino}")
            continue
        archivo, _, ancla = destino.partition("#")
        objetivo = docs / (archivo or f"{nombre}.md")
        if archivo and not (re.fullmatch(r"[a-z0-9][a-z0-9-]*\.md", archivo) and objetivo.is_file()):
            salida.append(f"enlace roto: {destino}")
        elif ancla and ancla not in ayuda.anclas(objetivo.read_text(encoding="utf-8")):
            salida.append(f"ancla inexistente: {destino}")
    return salida


def test_ayuda_sirve_el_manual_con_la_csp_y_sin_nada_activo(cliente):
    respuesta = cliente.get("/ayuda")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-security-policy"] == CSP
    assert "'unsafe-inline'" not in CSP
    pagina = respuesta.text
    assert "<title>Manual de Historia Viva — Historia Viva</title>" in pagina
    assert '<h1 id="manual-de-historia-viva">Manual de Historia Viva</h1>' in pagina
    assert not re.search(r"<script|<style|\sstyle=|\son[a-z]+=|javascript:", pagina, re.I)
    assert '<link rel="stylesheet" href="/estaticos/ayuda.css">' in pagina
    assert cliente.get("/estaticos/ayuda.css").status_code == 200


def test_el_markdown_se_muestra_sin_html_activo():
    malicioso = (
        '# Hola <script>alert(1)</script>\n\nTexto <img src=x onerror="alert(1)"> y `<b>` '
        "[a](javascript:alert(1)) [b](data:text/html,x) ![c](https://otro.sitio/x.png)\n\n"
        "| <i>c</i> |\n|---|\n| <iframe> |\n"
    )

    pagina = ayuda.html_de(malicioso)

    assert re.findall(r"<(\w+)", pagina) == [
        "h1", "p", "code", "div", "table", "thead", "tr", "th", "tbody", "tr", "td"
    ]
    assert "&lt;script&gt;" in pagina and "&lt;img src=x onerror=" in pagina
    assert "href" not in pagina and "src=" not in pagina.replace("&lt;img src=x", "")


def test_cada_runbook_y_documento_enlazado_se_sirve_y_lo_demas_da_404(web):
    destinos = {d.partition("#")[0] for _, d in ayuda.enlaces(MANUAL)} - {""}
    pagina = web.get("/ayuda").text

    for destino in destinos:
        nombre = destino.removesuffix(".md")
        assert f'href="/ayuda/{nombre}' in pagina, destino
        respuesta = web.get(f"/ayuda/{nombre}")
        assert respuesta.status_code == 200, destino
        assert not re.search(r"<script|\son[a-z]+=", respuesta.text, re.I), destino
    for ajeno in ("inexistente", "..%2Fpyproject", "Manual", "capturas/x.png", "guia-de-una-pagina"):
        assert web.get(f"/ayuda/{ajeno}").status_code == 404, ajeno


def test_cada_href_del_manual_servido_responde_y_cada_ancla_existe(web):
    pagina = web.get("/ayuda").text
    ids = set(re.findall(r' id="([^"]+)"', pagina))

    for href in set(re.findall(r'href="([^"]+)"', pagina)):
        ruta, _, ancla = html.unescape(href).partition("#")
        if ruta.startswith("/ayuda"):
            destino = web.get(ruta)
            assert destino.status_code == 200, href
            assert not ancla or f'id="{ancla}"' in destino.text, href
        elif not ruta:
            assert ancla in ids, href


def test_la_revision_del_manual_no_encuentra_problemas():
    assert problemas(DOCS) == []


def test_la_revision_falla_con_un_enlace_roto_y_con_una_captura_sin_marca(tmp_path):
    (tmp_path / "capturas").mkdir()
    Image.new("RGB", (4, 4)).save(tmp_path / "capturas" / "real.png")
    marca = PngInfo()
    marca.add_text(*MARCA_DE_CAPTURA)
    Image.new("RGB", (4, 4)).save(tmp_path / "capturas" / "de-prueba.png", pnginfo=marca)
    (tmp_path / "runbook-r1-x.md").write_text("# R1\n\n## Pasos\n", encoding="utf-8")
    (tmp_path / "manual.md").write_text(
        "# M\n\n## Uno\n\n[ok](runbook-r1-x.md#pasos) [ok](#uno) ![ok](capturas/de-prueba.png)\n\n"
        "[roto](runbook-r99.md) [ancla](runbook-r1-x.md#nada) [interna](#dos) "
        "![real](capturas/real.png) ![afuera](https://otro.sitio/x.png)\n",
        encoding="utf-8",
    )

    assert problemas(tmp_path) == [
        "enlace roto: runbook-r99.md",
        "ancla inexistente: runbook-r1-x.md#nada",
        "ancla inexistente: #dos",
        "captura sin la marca del corpus de prueba: capturas/real.png",
        "captura que /ayuda no sirve: https://otro.sitio/x.png",
    ]


def test_cada_capitulo_enlaza_un_runbook_y_estan_todos():
    capitulos = _capitulos(MANUAL)

    assert list(capitulos) == [
        "Contenido", "Qué es Historia Viva", "Roles: quién puede qué", "Entrar", "Cargar", "Validar",
        "Conversar con el Acervo", "Efemérides y Relatos", "Informe de acreditación", "Qué no subir",
        "Administrar", "Operación", "Agente externo", "Lo que todavía está apagado", "Accesibilidad",
    ]
    for titulo, texto in capitulos.items():
        if titulo != "Contenido":
            assert re.search(r"\]\(runbook-r\d+-[a-z0-9-]+\.md\)", texto), titulo
    enlazados = set(re.findall(r"\((runbook-r\d+-[a-z0-9-]+\.md)\)", MANUAL))
    assert enlazados == {p.name for p in DOCS.glob("runbook-r*.md")}
    indice = re.findall(r"\(#([a-z0-9-]+)\)", capitulos["Contenido"])
    assert len(indice) == len(capitulos) - 1


def test_cada_rol_tiene_su_capitulo_y_el_administrador_tecnico_no_firma_fichas():
    roles = _capitulos(MANUAL)["Roles: quién puede qué"]
    secciones = dict(zip(*[iter(re.split(r"^### (.+)$", roles, flags=re.M)[1:])] * 2, strict=True))

    assert list(secciones) == ["RGC", "Coordinador de Extensión", "Director de Carrera", "Administrador técnico"]
    for rol in ("RGC", "Coordinador de Extensión", "Director de Carrera"):
        assert "**Puede:**" in secciones[rol] and "**No puede:**" in secciones[rol], rol
    assert "**No firma Fichas.**" in secciones["Administrador técnico"]
    assert "«Tu rol no firma Fichas.»" in secciones["Administrador técnico"]


@pytest.mark.rnf("RNF-18")
def test_accesibilidad_declara_la_alternativa_textual_del_contenido_archivistico():
    texto = _plano(_capitulos(MANUAL)["Accesibilidad"])

    assert "WCAG 2.1 AA" in texto
    assert "Su alternativa textual es la **descripción de la Ficha**" in texto


def test_operacion_remite_a_los_runbooks_y_distingue_runbook_de_rebanada():
    texto = _plano(_capitulos(MANUAL)["Operación"])

    assert "**«Runbook Rn» no es «rebanada Rn».**" in texto
    assert len(re.findall(r"\| \[Runbook R\d+\]\(runbook-r\d+-", texto)) == 18


def test_bloque_supresion_y_drive_figuran_deshabilitados_con_su_consulta():
    apagado = _plano(_capitulos(MANUAL)["Lo que todavía está apagado"])

    assert "**deshabilitadas**" in apagado
    assert "Aprobación en bloque | C29" in apagado
    assert "«Publicable para el Agente 1» | C31" in apagado
    assert "Réplica de los originales en Drive | C14 y C34" in apagado
    assert "el ticket que la activa actualiza este capítulo" in apagado
    # Lo que se ve hoy coincide con la pantalla del Lote (108).
    assert re.search(r"Deshabilitada hasta que la SEU acepte firmar sin abrir cada Pieza \(C29\)",
                     _plano((DIRECTORIO_WEB / "lote.html").read_text(encoding="utf-8")))


def test_siu_sisesca_y_coneau_global_se_conectarian_por_aportes():
    texto = _plano(_capitulos(MANUAL)["Agente externo"])

    assert "**no se conecta** con SIU, SISESCA ni CONEAU Global" in texto
    assert "`POST /v1/aportes`" in texto and "entra Pendiente" in texto


def test_lo_que_el_manual_cita_de_la_pantalla_existe_tal_cual_en_el_codigo():
    paquete = DIRECTORIO_WEB.parent
    pantallas = " ".join(re.sub(r"<[^>]+>", "", p.read_text(encoding="utf-8")) for p in paquete.glob("web/*.html"))
    codigo = " ".join(
        p.read_text(encoding="utf-8") for patron in ("web/estaticos/*.js", "**/*.py") for p in paquete.glob(patron)
    )
    codigo = re.sub(r"[\"'`]\s*\+?\s*\n\s*f?[\"'`]", "", codigo)  # cadenas partidas en renglones
    codigo = _plano(html.unescape(pantallas + " " + codigo))
    # Un número del manual vale por el dato que la pantalla arma (`${tope}`, `{tope}`).
    plantillas = re.sub(r"\$?\{[^{}]*\}", "§", codigo)
    citas = set(re.findall(r"«([^»]+)»", _plano(MANUAL))) - NO_SON_DE_LA_PANTALLA

    def existe(cita: str) -> bool:
        cita = cita.rstrip(".…:")
        return cita in codigo or re.sub(r"\d+", "§", cita) in plantillas

    faltan = [c for c in citas if not all(existe(parte) for parte in c.split(" › "))]
    assert sorted(faltan) == []


def test_la_guia_de_una_pagina_y_el_manual_usan_los_mismos_nombres():
    guia = _plano(html.unescape(re.sub(r"<[^>]+>", "", GUIA.read_text(encoding="utf-8"))))
    manual = _plano(MANUAL)

    assert "/ayuda" in guia
    assert sorted(n for n in set(re.findall(r"«([^»]+)»", guia)) if f"«{n}" not in manual and n not in "+−") == []
