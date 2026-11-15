"""La prueba de carga de `/v1/fragmentos` (165) contra una API simulada en un hilo: sin base ni
pila. Cada tercer pedido de fragmentos es un 429, como cuando se agota el cupo del token."""

from __future__ import annotations

import csv
import itertools
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from typing import ClassVar

import pytest

from historia_viva.banco.carga import CONSULTAS, correr, informe, main

pytestmark = pytest.mark.rnf("RNF-10")

TOKEN = "hv_0123abcd_simulado"


class _ApiSimulada(BaseHTTPRequestHandler):
    vistos: ClassVar[list[tuple[str, str | None, str | None]]] = []
    contador = itertools.count(1)

    def do_GET(self) -> None:  # noqa: N802 — el nombre lo pone `http.server`
        partes = urlsplit(self.path)
        q = parse_qs(partes.query).get("q", [None])[0]
        self.vistos.append((partes.path, self.headers.get("Authorization"), q))
        if q == "cuerpo cortado":  # un 500 que anuncia 100 bytes y manda 3
            self.send_response(500)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"{}{")
            self.close_connection = True
        elif partes.path == "/api/salud":
            self._responder(200, b'{"estado": "ok"}')
        elif self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._responder(401, b"{}")
        elif next(self.contador) % 3 == 0:
            self._responder(429, b"{}", {"Retry-After": "1"})
        else:
            self._responder(200, b'{"total": 0, "fragmentos": []}')

    def _responder(self, estado: int, cuerpo: bytes, cabeceras: dict[str, str] | None = None):
        self.send_response(estado)
        for nombre, valor in {"Content-Length": str(len(cuerpo)), **(cabeceras or {})}.items():
            self.send_header(nombre, valor)
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture
def api() -> Iterator[str]:
    _ApiSimulada.vistos = []
    _ApiSimulada.contador = itertools.count(1)
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), _ApiSimulada)
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    yield f"http://127.0.0.1:{servidor.server_address[1]}"
    servidor.shutdown()
    servidor.server_close()


def test_varios_usuarios_con_el_token_esperan_el_retry_after_y_terminan_a_tiempo(api):
    inicio = time.monotonic()
    pedidos = correr(api, [TOKEN], usuarios=3, duracion_s=0.6, salud=True)
    transcurrido = time.monotonic() - inicio

    fragmentos = [p for p in pedidos if p.ruta == "/v1/fragmentos"]
    # El 429 pide 1 s: cada usuario espera hasta el final de la ventana y no insiste.
    assert 3 <= len(fragmentos) <= 9
    assert {p.estado for p in fragmentos} == {200, 429}
    assert {p.estado for p in pedidos if p.ruta == "/api/salud"} == {200}
    assert transcurrido < 3
    assert {(ruta, auth) for ruta, auth, _ in _ApiSimulada.vistos} == {
        ("/v1/fragmentos", f"Bearer {TOKEN}"), ("/api/salud", None)
    }
    assert {q for _, _, q in _ApiSimulada.vistos if q} <= set(CONSULTAS)


def test_sin_respuesta_cuenta_aparte_y_no_inunda():
    pedidos = correr("http://127.0.0.1:1", [TOKEN], usuarios=2, duracion_s=0.3)

    assert pedidos and {p.estado for p in pedidos} == {None}
    assert len(pedidos) <= 4  # tras cada falla espera 1 s, recortado al final de la ventana


def test_el_informe_da_codigos_percentiles_de_los_200_y_pedidos_por_segundo():
    from historia_viva.banco.carga import Pedido

    pedidos = [Pedido("/v1/fragmentos", 200, ms) for ms in (100, 200, 300, 400)]
    pedidos += [Pedido("/v1/fragmentos", 429, 1), Pedido("/v1/fragmentos", None, 60_000)]

    texto = informe(pedidos, segundos=2, usuarios=5, url="http://web:8000", tokens=1)

    assert "5 usuario(s), 1 token(s), 2.0 s contra http://web:8000" in texto
    assert "| `/v1/fragmentos` | 6 | 200: 4, 429: 1, sin respuesta: 1 | 200 | 400 | 400 | 3,0 |" in texto


def test_main_lee_el_token_del_entorno_las_consultas_del_csv_y_escribe_el_csv(
    api, tmp_path, monkeypatch, capsys
):
    consultas = tmp_path / "consultas.csv"
    consultas.write_text("consulta_id,texto\nc1,convenio con la empresa\nc2,ab\n", "utf-8")
    salida = tmp_path / "carga.csv"
    monkeypatch.setenv("HV_CARGA_TOKEN", f" {TOKEN} ")

    codigo = main(["--url", api, "--usuarios", "2", "--duracion", "0.3",
                   "--consultas", str(consultas), "--csv", str(salida)])

    assert codigo == 0
    assert "| `/v1/fragmentos` |" in capsys.readouterr().out
    assert {q for _, _, q in _ApiSimulada.vistos} == {"convenio con la empresa"}
    with salida.open(encoding="utf-8-sig", newline="") as entrada:
        filas = list(csv.DictReader(entrada))
    assert filas and {f["ruta"] for f in filas} == {"/v1/fragmentos"}


def test_main_sin_token_no_arranca(monkeypatch, capsys):
    monkeypatch.delenv("HV_CARGA_TOKEN", raising=False)

    with pytest.raises(SystemExit) as salida:
        main(["--url", "http://127.0.0.1:1"])

    assert salida.value.code == 2
    assert "HV_CARGA_TOKEN" in capsys.readouterr().err


def test_un_error_con_el_cuerpo_cortado_cuenta_como_sin_respuesta_y_no_corta_la_corrida(api):
    pedidos = correr(api, [TOKEN], usuarios=1, duracion_s=0.3, consultas=["cuerpo cortado"])

    assert pedidos and {p.estado for p in pedidos} == {None}
