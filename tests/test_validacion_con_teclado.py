"""Validar con el teclado (147, RF-11): A acepta, R rechaza, las flechas mueven y Enter firma.

`validar.js` corre en Chrome sin ventana, como axe, sobre las respuestas reales de la API (capa
falsa): el `fetch` de la página las sirve y anota lo que la pantalla manda. Las letras pasan por el
mismo `decidir` que los botones, así que la confirmación de 145 y las guardas de 155 valen igual.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess

import pytest
from capa_modelos_falsa import imagen_con_afirmaciones
from test_accesibilidad import _chrome
from test_afirmaciones import DOS_PERSONAS, EN_LA_APP, LOGO, _decidir, _firmar, _foto_pendiente
from test_cruce_con_el_ocr import SELLO
from test_imagenes_sin_texto import (
    BARRAS,
    PERSONAS,
    _con_afirmaciones,
    _esperar_imagenes,
    _word,
    _word_pendiente,
)

from historia_viva.api.app import DIRECTORIO_WEB

# El `fetch` de la página: responde lo que la API respondió y anota cada pedido que modifica algo.
# La decisión tarda un poco, para que haya un pedido en vuelo de verdad.
STUB = """
const RESPUESTAS = __RESPUESTAS__;
window.pedidos = [];
window.confirmaciones = [];
window.respuestaAlConfirmar = false;
window.confirm = (texto) => { confirmaciones.push(texto); return respuestaAlConfirmar; };
const responder = (cuerpo, status = 200) => new Response(JSON.stringify(cuerpo), {
  status, headers: { "Content-Type": "application/json" } });
window.fetch = async (url, opciones = {}) => {
  const metodo = opciones.method || "GET";
  if (metodo !== "GET") pedidos.push({ metodo, url, cuerpo: opciones.body ? JSON.parse(opciones.body) : null });
  else if (url.startsWith("/api/piezas?")) pedidos.push({ metodo, url });
  const decision = url.match(/\\/afirmaciones\\/(\\d+)$/);
  if (decision && metodo === "PUT") {
    await new Promise((r) => setTimeout(r, 200));
    const actual = RESPUESTAS.afirmaciones.afirmaciones.find((a) => a.orden === Number(decision[1]));
    return responder({ ...actual, estado: JSON.parse(opciones.body).decision });
  }
  if (url.endsWith("/ficha") && metodo === "PUT") return responder(RESPUESTAS.firma);
  if (url.endsWith("/apertura")) return responder({ medicion_id: null });
  if (url.startsWith("/api/piezas?")) return responder({ piezas: [] });
  const clave = Object.keys(RESPUESTAS.get).find((c) => url === c);
  return clave ? responder(RESPUESTAS.get[clave]) : responder({ detail: "no" }, 404);
};
"""

RECORRIDO = """
(async () => {
  const $ = (id) => document.getElementById(id);
  const pausa = (ms) => new Promise((r) => setTimeout(r, ms));
  const esperar = async (condicion) => { for (let i = 0; i < 400 && !condicion(); i++) await pausa(25); };
  const tecla = (el, key) => el.dispatchEvent(
    new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
  const r = {};
  const decisiones = () => pedidos.filter((p) => p.url.includes("/afirmaciones/"));
  await esperar(() => document.querySelector("li.afirmacion"));
  const item = (orden) => $(`afirmacion-${orden}`);
  r.orden = [...document.querySelectorAll("li.afirmacion")].map((li) => li.id);
  r.tabindex = [...document.querySelectorAll("li.afirmacion")].map((li) => li.tabIndex);
  r.atajos_visibles = !$("atajos-afirmaciones").hidden;

  // Enter con todo sin decidir: no firma, dice qué falta.
  item(1).focus();
  tecla(item(1), "Enter");
  r.enter_sin_decidir = $("error-guardado").hidden ? null : $("error-guardado").textContent;

  // Flechas y j/k.
  tecla(document.activeElement, "ArrowDown");
  r.foco_abajo = document.activeElement.id;
  tecla(document.activeElement, "j");
  r.foco_j = document.activeElement.id;
  tecla(document.activeElement, "j");
  r.foco_tope = document.activeElement.id;
  tecla(document.activeElement, "k");
  r.foco_k = document.activeElement.id;
  tecla(document.activeElement, "ArrowUp");
  r.foco_arriba = document.activeElement.id;

  // La sigla sin respaldo: la tecla pide la misma confirmación que el botón; cancelar no guarda.
  $("aceptar-2").click();
  tecla(item(2), "a");
  r.confirmaciones = [...confirmaciones];
  r.decisiones_tras_cancelar = decisiones().length;

  // Escribiendo en un campo (fuera de la lista, donde está el oyente) las letras no deciden.
  $("transcripcion").focus();
  for (const k of ["a", "r", "j", "Enter"]) tecla($("transcripcion"), k);
  r.decisiones_al_escribir = decisiones().length;
  r.foco_al_escribir = document.activeElement.id;

  // Con un modificador, la tecla es del navegador.
  item(1).focus();
  item(1).dispatchEvent(new KeyboardEvent("keydown", { key: "r", ctrlKey: true, bubbles: true }));
  r.decisiones_con_ctrl = decisiones().length;

  // R sobre la 1; A dos veces sobre la 3 mientras vuela: un solo pedido (155). Sin confirmación.
  tecla(item(1), "R");
  tecla(item(3), "a");
  tecla(item(3), "a");
  r.enter_en_vuelo = (tecla(item(3), "Enter"), $("error-guardado").textContent);
  await esperar(() => decisiones().length === 2 && !document.querySelector("li.afirmacion button:disabled"));
  r.confirmaciones_sin_riesgo = confirmaciones.length - r.confirmaciones.length;
  // La 2 aceptada con la tecla, confirmando.
  respuestaAlConfirmar = true;
  tecla(item(2), "a");
  await esperar(() => decisiones().length === 3 && !document.querySelector("li.afirmacion button:disabled"));
  r.decisiones = decisiones().map((p) => [p.url.split("/").pop(), p.cuerpo.decision, p.cuerpo.confirmada]);
  r.estados = [1, 2, 3].map((o) => item(o).querySelector(".estado-afirmacion").textContent);

  // El rechazo encoló el resumen rehecho (146): Enter no firma mientras llega, y lo dice.
  item(2).focus();
  tecla(item(2), "Enter");
  r.enter_con_resumen = $("error-guardado").textContent;
  await esperar(() => !$("guardar-completo").disabled);
  // Todo decidido y la Ficha completa: Enter firma, y sin otra Pendiente en Mis Piezas lo dice.
  tecla(item(2), "Enter");
  tecla(item(2), "Enter");  // otra vez, con la firma en vuelo
  r.enter_en_la_firma = $("error-guardado").textContent;
  await esperar(() => !$("confirmacion").hidden && /No quedan/.test($("confirmacion-detalle").textContent));
  r.firmas = pedidos.filter((p) => p.url.endsWith("/ficha")).map((p) => p.cuerpo.version_base);
  r.siguiente_pedida = pedidos.filter((p) => p.url.startsWith("/api/piezas?")).map((p) => p.url);
  r.confirmacion = [$("confirmacion-titulo").textContent, $("confirmacion-detalle").textContent];
  // El Enter que firmó, mantenido, repite sobre «Ir a la siguiente», que tiene el foco: no la aprieta.
  r.foco_tras_firmar = document.activeElement.id;
  r.repeticion_ignorada = !$("ir-a-la-siguiente").dispatchEvent(new KeyboardEvent("keydown",
    { key: "Enter", repeat: true, bubbles: true, cancelable: true }));

  const pre = document.createElement("pre");
  pre.id = "resultado";
  pre.textContent = JSON.stringify(r);
  document.body.append(pre);
})();
"""


def _recorrer(tmp_path, pieza_id: str, respuestas: dict, recorrido: str = RECORRIDO) -> dict:
    shutil.copytree(DIRECTORIO_WEB / "estaticos", tmp_path / "estaticos")
    (tmp_path / "stub.js").write_text(
        STUB.replace("__RESPUESTAS__", json.dumps(respuestas)), encoding="utf-8"
    )
    (tmp_path / "recorrido.js").write_text(recorrido, encoding="utf-8")
    pagina = (DIRECTORIO_WEB / "validar.html").read_text("utf-8").replace('"/estaticos/', '"estaticos/')
    pagina = pagina.replace(
        '<script src="estaticos/rotulos.js">', '<script src="stub.js"></script><script src="estaticos/rotulos.js">'
    ).replace("</body>", '<script src="recorrido.js"></script></body>')
    copia = tmp_path / "validar.html"
    copia.write_text(pagina, encoding="utf-8")
    volcado = subprocess.run(
        [
            str(_chrome()), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={tmp_path / 'perfil'}", "--allow-file-access-from-files",
            "--virtual-time-budget=30000", "--dump-dom", f"{copia.as_uri()}?pieza={pieza_id}",
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=300, check=True,
    ).stdout
    resultado = re.search(r'<pre id="resultado">(.*?)</pre>', volcado, re.S)
    assert resultado, "el recorrido no terminó"
    return json.loads(html.unescape(resultado[1]))


@pytest.fixture
def respuestas(cliente):
    """Una foto suelta de María con tres Afirmaciones (1: personas en una foto, 2: la sigla UNDEF
    sin respaldo, 3: respaldada) y los campos ya completos: queda Incompleta por las Afirmaciones."""
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, LOGO, SELLO))
    assert _firmar(cliente, pieza_id).json()["estado"] == "Incompleta"
    pieza = f"/api/piezas/{pieza_id}"
    urls = [
        "/api/yo", "/api/tipos", "/api/tipos-actividad", "/api/carreras", "/api/salud",
        pieza, f"{pieza}/ficha", f"{pieza}/afirmaciones",
    ]
    get = {url: cliente.get(url).json() for url in urls}
    # La firma que la página va a pedir, respondida de verdad por la API.
    for orden, decision in ((1, "rechazada"), (2, "aceptada"), (3, "aceptada")):
        decidida = _decidir(cliente, pieza_id, orden, decision, confirmada=orden == 2)
        assert decidida.status_code == 200
    firma = _firmar(cliente, pieza_id, version_base=2).json()
    assert firma["estado"] == "Validada"
    return pieza_id, {"get": get, "afirmaciones": get[f"{pieza}/afirmaciones"], "firma": firma}


@EN_LA_APP
@pytest.mark.rf("RF-11")
@pytest.mark.rnf("RNF-18")
@pytest.mark.skipif(_chrome() is None, reason="sin Chrome no corre la pantalla")
@pytest.mark.xdist_group("chrome")  # un Chrome por vez, como axe
def test_se_valida_una_foto_solo_con_teclado_y_la_tecla_pide_la_misma_confirmacion(
    respuestas, tmp_path
):
    pieza_id, datos = respuestas
    assert datos["get"][f"/api/piezas/{pieza_id}"]["lote_id"] is None  # suelta: va a Mis Piezas

    r = _recorrer(tmp_path, pieza_id, datos)

    # Un solo lugar de tabulación para los ítems, en el orden por riesgo de 145.
    assert r["orden"] == ["afirmacion-1", "afirmacion-2", "afirmacion-3"]
    assert (r["tabindex"], r["atajos_visibles"]) == ([0, -1, -1], True)
    assert r["enter_sin_decidir"] == "Todavía no se puede firmar. Falta: decidir 3 Afirmaciones."
    assert [r[k] for k in ("foco_abajo", "foco_j", "foco_tope", "foco_k", "foco_arriba")] == [
        "afirmacion-2", "afirmacion-3", "afirmacion-3", "afirmacion-2", "afirmacion-1"
    ]
    # La sigla sin respaldo: la tecla A muestra el mismo diálogo que el botón; cancelar no guarda.
    [con_el_mouse, con_la_tecla] = r["confirmaciones"]
    assert con_la_tecla == con_el_mouse
    assert "nombra una sigla" in con_la_tecla
    assert r["decisiones_tras_cancelar"] == 0
    # Escribiendo en un campo, o con Ctrl, la tecla no es un atajo.
    assert (r["decisiones_al_escribir"], r["foco_al_escribir"]) == (0, "transcripcion")
    assert r["decisiones_con_ctrl"] == 0
    # Con una decisión en vuelo, ni la misma tecla otra vez ni Enter mandan nada (155).
    assert r["enter_en_vuelo"] == "Esperá: todavía se está guardando una decisión."
    assert r["confirmaciones_sin_riesgo"] == 0
    # La 2 se acepta tras el `confirm`, y la pantalla se lo dice al servidor (SEG-21).
    assert r["decisiones"] == [
        ["1", "rechazada", False], ["3", "aceptada", False], ["2", "aceptada", True]
    ]
    assert r["estados"] == ["✗ Rechazada", "✓ Aceptada", "✓ Aceptada"]
    # Enter firma una sola vez y busca la siguiente Pendiente en Mis Piezas: no queda ninguna.
    assert r["enter_con_resumen"].startswith(
        "Todavía no se puede firmar. Se está rehaciendo el resumen sin las Afirmaciones rechazadas"
    )
    assert r["enter_en_la_firma"] == "Esperá: todavía se está guardando la Ficha."
    assert r["firmas"] == [2]
    assert r["siguiente_pedida"] == ["/api/piezas?estado=Pendiente&mias=true"]
    assert r["confirmacion"][0] == "✓ Ficha Validada"
    assert r["confirmacion"][1].endswith("No quedan Piezas Pendientes en Mis Piezas.")
    assert (r["foco_tras_firmar"], r["repeticion_ignorada"]) == ("ir-a-la-siguiente", True)


# --- Las imágenes embebidas de un documento (148) -----------------------------------------------

RECORRIDO_EMBEBIDAS = """
(async () => {
  const $ = (id) => document.getElementById(id);
  const pausa = (ms) => new Promise((r) => setTimeout(r, ms));
  const esperar = async (condicion) => { for (let i = 0; i < 400 && !condicion(); i++) await pausa(25); };
  const tecla = (el, key) => el.dispatchEvent(
    new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
  const decisiones = () => pedidos.filter((p) => p.url.includes("/afirmaciones/"));
  await esperar(() => document.querySelector("li.afirmacion"));
  const items = [...$("campo-afirmaciones").querySelectorAll("li.afirmacion")];
  const r = {
    items: items.map((li) => [li.id, li.closest("#imagenes-embebidas") !== null, li.tabIndex]),
    atajos_visibles: !$("atajos-afirmaciones").hidden,
  };
  items[0].focus();
  tecla(items[0], "ArrowDown");
  r.foco_abajo = document.activeElement.id;
  tecla(document.activeElement, "a");
  tecla(items[0], "r");
  await esperar(() => decisiones().length === 2 && !document.querySelector("li.afirmacion button:disabled"));
  r.decisiones = decisiones().map((p) => [p.url.split("/").pop(), p.cuerpo.decision, p.cuerpo.confirmada]);
  // Desde `file://` la miniatura no carga: queda el aviso del servidor en vez de una imagen rota.
  await esperar(() => document.querySelector(".miniatura-ausente"));
  r.miniatura = document.querySelector(".miniatura-ausente")?.textContent ?? null;
  const pre = document.createElement("pre");
  pre.id = "resultado";
  pre.textContent = JSON.stringify(r);
  document.body.append(pre);
})();
"""


@EN_LA_APP
@pytest.mark.rf("RF-11", "RF-14")
@pytest.mark.skipif(_chrome() is None, reason="sin Chrome no corre la pantalla")
@pytest.mark.xdist_group("chrome")
def test_las_teclas_recorren_y_deciden_las_afirmaciones_de_una_imagen_embebida(cliente, tmp_path):
    pieza_id = _word_pendiente(cliente, _word(_con_afirmaciones(PERSONAS, BARRAS)))
    _esperar_imagenes(cliente, pieza_id, "descrita")
    pieza = f"/api/piezas/{pieza_id}"
    urls = ["/api/yo", "/api/tipos", "/api/tipos-actividad", "/api/carreras", "/api/salud",
            pieza, f"{pieza}/ficha", f"{pieza}/afirmaciones"]
    get = {url: cliente.get(url).json() for url in urls}
    datos = {"get": get, "afirmaciones": get[f"{pieza}/afirmaciones"], "firma": {}}

    r = _recorrer(tmp_path, pieza_id, datos, RECORRIDO_EMBEBIDAS)

    assert r["items"] == [["afirmacion-1", True, 0], ["afirmacion-2", True, -1]]
    assert (r["atajos_visibles"], r["foco_abajo"]) == (True, "afirmacion-2")
    assert r["decisiones"] == [["2", "aceptada", False], ["1", "rechazada", False]]
    assert r["miniatura"] == "No se pudo mostrar la miniatura. La imagen se ve en el original."
