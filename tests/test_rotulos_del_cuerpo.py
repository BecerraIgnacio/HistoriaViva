"""El rótulo del cuerpo de la Ficha por clase de Pieza (ticket 135, RF-11).

Lo que sale literal del archivo (PDF, Word, planilla, `.txt`) se rotula «Del archivo»; la marca 🤖 de
propuesta queda para lo que escribe un modelo. `rotulos.js` corre en Chrome sin ventana, como axe.
"""

from __future__ import annotations

import html
import json
import re
import subprocess

import pytest
from test_accesibilidad import _chrome

from historia_viva.api.app import DIRECTORIO_WEB
from historia_viva.dominio.originales import CSV, DOCX, XLSX

CASOS = {
    "pdf": ("application/pdf", "archivo"),
    "word": (DOCX, "archivo"),
    "xlsx": (XLSX, "archivo"),
    "csv": (CSV, "archivo"),
    "txt": ("text/plain; charset=utf-8", "archivo"),
    "foto": ("image/jpeg", "archivo"),
    "video": ("video/mp4", "archivo"),
    "aporte": ("text/plain", "aporte"),
    # 155: la foto descripta en Afirmaciones; su cuerpo es solo lo que leyó el OCR.
    "foto_con_afirmaciones": ("image/jpeg", "archivo", True),
    "foto_firmada": ("image/jpeg", "archivo", True, True),
}


@pytest.fixture(scope="module")
def rotulos(tmp_path_factory) -> dict[str, dict]:
    carpeta = tmp_path_factory.mktemp("rotulos")
    (carpeta / "rotulos.js").write_bytes((DIRECTORIO_WEB / "estaticos" / "rotulos.js").read_bytes())
    (carpeta / "correr.js").write_text(
        f"const pre = document.createElement('pre'); pre.id = 'resultado';"
        f"pre.textContent = JSON.stringify(Object.fromEntries(Object.entries({json.dumps(CASOS)})"
        f".map(([k, args]) => [k, rotuloDelCuerpo(...args)])));"
        f"document.body.append(pre);",
        encoding="utf-8",
    )
    pagina = carpeta / "rotulos.html"
    pagina.write_text(
        '<!doctype html><meta charset="utf-8"><body>'
        '<script src="rotulos.js"></script><script src="correr.js"></script></body>',
        encoding="utf-8",
    )
    volcado = subprocess.run(
        [
            str(_chrome()), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={carpeta / 'perfil'}", "--allow-file-access-from-files",
            "--dump-dom", pagina.as_uri(),
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=300, check=True,
    ).stdout
    resultado = re.search(r'<pre id="resultado">(.*?)</pre>', volcado, re.S)
    assert resultado, volcado
    return json.loads(html.unescape(resultado[1]))


@pytest.mark.rf("RF-11")
@pytest.mark.skipif(_chrome() is None, reason="sin Chrome no corre el script de la pantalla")
@pytest.mark.xdist_group("chrome")
@pytest.mark.parametrize("clase", ["pdf", "word", "xlsx", "csv", "txt"])
def test_lo_extraido_del_archivo_se_rotula_del_archivo_sin_marca_de_propuesta(rotulos, clase):
    rotulo = rotulos[clase]
    assert rotulo["rotulo"] == "Texto del archivo"
    assert rotulo["sinModelo"] == "Del archivo"
    assert not rotulo["propuesto"]
    assert rotulo["ayuda"].startswith(rotulos["txt"]["ayuda"])
    assert "🤖" not in rotulo["rotulo"] + rotulo["ayuda"]
    # Solo el PDF puede traer páginas escaneadas: la ayuda lo aclara, y en el resto es la del TXT.
    assert ("(OCR), no de un modelo de lenguaje" in rotulo["ayuda"]) == (clase == "pdf")


@pytest.mark.rf("RF-11")
@pytest.mark.skipif(_chrome() is None, reason="sin Chrome no corre el script de la pantalla")
@pytest.mark.xdist_group("chrome")
def test_la_descripcion_de_una_foto_conserva_la_marca_y_el_video_y_el_aporte_no_la_tienen(rotulos):
    assert rotulos["foto"]["rotulo"].startswith("🤖")
    assert rotulos["foto"]["propuesto"]
    assert rotulos["foto"]["sinModelo"] == ""
    assert not rotulos["video"]["propuesto"]
    assert rotulos["aporte"] == {**rotulos["aporte"], "propuesto": False, "sinModelo": "Del Agente"}


@pytest.mark.rf("RF-11")
@pytest.mark.skipif(_chrome() is None, reason="sin Chrome no corre el script de la pantalla")
@pytest.mark.xdist_group("chrome")
def test_el_cuerpo_de_una_foto_con_afirmaciones_se_rotula_lectura_automatica(rotulos):
    # ADR-0007: lo propuesto por el modelo son las Afirmaciones; el cuerpo es el OCR.
    rotulo = rotulos["foto_con_afirmaciones"]
    assert (rotulo["rotulo"], rotulo["sinModelo"], rotulo["propuesto"]) == (
        "Texto visible en la imagen", "Lectura automática", False
    )
    assert "🤖" not in rotulo["rotulo"] + rotulo["ayuda"]
    assert "(OCR), no un modelo de lenguaje" in rotulo["ayuda"]
    # Firmada, el cuerpo trae también las aceptadas por una persona: el rótulo lo dice.
    firmada = rotulos["foto_firmada"]
    assert firmada["rotulo"] == "Afirmaciones aceptadas y texto visible en la imagen"
    assert "las que aceptó una persona" in firmada["ayuda"]
    assert not firmada["propuesto"]


@pytest.mark.rf("RF-11")
@pytest.mark.parametrize("pantalla", sorted(DIRECTORIO_WEB.glob("*.html")), ids=lambda p: p.name)
def test_ninguna_pantalla_dice_prototipo_en_el_titulo_ni_en_el_encabezado(pantalla):
    texto = pantalla.read_text(encoding="utf-8")
    encabezados = re.findall(r"<(title|h1)[^>]*>(.*?)</\1>", texto, re.S)
    assert encabezados
    assert [e for _, e in encabezados if "Prototipo" in e] == []
