"""axe-core sobre el HTML estático de cada pantalla, sin violaciones (RNF-18, DEC-146 punto 21).

Corre en Chrome sin ventana: cada pantalla se copia con sus estilos y sin su script (lo que
arma el script con datos del servidor se revisa a mano) y axe deja el resultado en el DOM que
Chrome vuelca. `axe/axe.min.js` es axe-core 4.10.3 (MPL-2.0), copiado tal cual de npm.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from historia_viva.api.app import DIRECTORIO_WEB

PANTALLAS = sorted(DIRECTORIO_WEB.glob("*.html"))
AXE = Path(__file__).resolve().parent / "axe" / "axe.min.js"

CORRER_AXE = """
axe.run(document, { runOnly: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"] })
  .then((r) => {
    const salida = document.createElement("pre");
    salida.id = "resultado-axe";
    salida.textContent = JSON.stringify(
      r.violations.map((v) => ({ regla: v.id, nodos: v.nodes.map((n) => n.target) })));
    document.body.append(salida);
  });
"""


def _chrome() -> str | None:
    candidatos = [
        os.environ.get("HV_CHROME", ""),
        *(shutil.which(n) or "" for n in ("chrome", "google-chrome", "chromium", "msedge")),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((c for c in candidatos if c and Path(c).is_file()), None)


@pytest.mark.rnf("RNF-18")
@pytest.mark.skipif(_chrome() is None, reason="sin Chrome no corre axe-core")
@pytest.mark.xdist_group("chrome")  # un Chrome por vez: varios en paralelo vencen el tiempo
@pytest.mark.parametrize("pantalla", PANTALLAS, ids=[p.name for p in PANTALLAS])
def test_axe_sin_violaciones_en_el_html_estatico(pantalla, tmp_path):
    shutil.copytree(DIRECTORIO_WEB / "estaticos", tmp_path / "estaticos")
    shutil.copy(AXE, tmp_path / "axe.min.js")
    (tmp_path / "correr.js").write_text(CORRER_AXE, encoding="utf-8")
    estatico = re.sub(r'<script src="/estaticos/[^"]+"></script>', "", pantalla.read_text("utf-8"))
    estatico = estatico.replace('"/estaticos/', '"estaticos/').replace(
        "</body>", '<script src="axe.min.js"></script><script src="correr.js"></script></body>'
    )
    copia = tmp_path / pantalla.name
    copia.write_text(estatico, encoding="utf-8")

    volcado = subprocess.run(
        [
            str(_chrome()), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={tmp_path / 'perfil'}", "--allow-file-access-from-files",
            "--virtual-time-budget=20000", "--dump-dom", copia.as_uri(),
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=300, check=True,
    ).stdout
    resultado = re.search(r'<pre id="resultado-axe">(.*?)</pre>', volcado, re.S)

    assert resultado, "axe no terminó"
    assert json.loads(html.unescape(resultado[1])) == []
