"""Lo que corrigió la auditoría de seguridad del 2026-10-20 (`docs/auditorias/2026-10-20-seguridad.md`)."""

from __future__ import annotations

import pytest

from historia_viva.api.ayuda import html_de


@pytest.mark.rnf("RNF-21")
@pytest.mark.parametrize("ruta", ["/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"])
def test_seg_sin_documentacion_de_la_api_publica(cliente, ruta):
    """SEG-01: el mapa de `/api` y `/v1` no se sirve a cualquiera (ASVS 13.4.5)."""
    cliente.cookies.clear()

    assert cliente.get(ruta).status_code == 404
    # La suite sigue leyendo el esquema de la aplicación (`test_retiro`, `test_identidad`).
    assert "/v1/fragmentos" in cliente.app.openapi()["paths"]


@pytest.mark.historia(244)
def test_seg_ayuda_no_enlaza_a_otro_sitio_con_doble_barra():
    """SEG-02: `//otro.sitio/x` no pasa como ruta de la web; queda el texto."""
    html = html_de("[afuera](//evil.example/x) y [adentro](/validar.html)")

    assert "evil.example" not in html
    assert '<a href="/validar.html">adentro</a>' in html
