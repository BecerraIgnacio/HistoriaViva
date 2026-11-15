"""El visor de planilla de Validar: una pestaña por hoja, armada desde la v1 (091).

`GET /api/piezas/{id}/planilla` lee las tablas del cuerpo que extrajo el sistema con
`CuerpoDeFicha.parsear_cuerpo` (DEC-94) y la pantalla las pinta con `textContent` (DEC-45).
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from aplicacion import entrar
from espera import esperar_estado
from validacion import DIRECTORA_DE_CARRERA, EDICION_COMPLETA, MARIA

pytestmark = [
    pytest.mark.historia(113),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HOSTIL = b'Nombre;Nota\nAna;=CMD()\nLuis;<img src=x onerror=alert(1)>\n'


def _subir(cliente, nombre: str, contenido: bytes, mime: str) -> str:
    entrar(cliente, MARIA)
    pieza_id: str = cliente.post(
        "/api/piezas", files={"archivo": (nombre, contenido, mime)}
    ).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    return pieza_id


@pytest.mark.rf("RF-11")
def test_un_csv_es_una_hoja_con_su_rotulo_y_las_celdas_tal_cual(cliente):
    pieza_id = _subir(cliente, "socios.csv", HOSTIL, "text/csv")

    respuesta = cliente.get(f"/api/piezas/{pieza_id}/planilla")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "avisos": [],
        "hojas": [
            {
                "nombre": "socios",
                "desde": 1,
                "hasta": 3,
                "total": 3,
                "nota": "Codificación detectada: UTF-8 · Separador: punto y coma",
                "columnas": ["Nombre", "Nota"],
                "filas": [["Ana", "=CMD()"], ["Luis", "<img src=x onerror=alert(1)>"]],
            }
        ],
    }


def _libro() -> bytes:
    """Dos hojas visibles y una oculta, como las deja Excel."""
    libro = Workbook()
    del libro["Sheet"]
    ventas = libro.create_sheet("Ventas")
    for fila in (["Fecha", "Monto"], ["1998-03-01", 100], ["1998-03-02", 200]):
        ventas.append(fila)
    libro.create_sheet("Notas").append(["Sin firmas"])
    oculta = libro.create_sheet("Borrador")
    oculta.append(["no catalogar"])
    oculta.sheet_state = "hidden"
    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()


@pytest.mark.rf("RF-38")
@pytest.mark.historia(111, 188)
def test_las_hojas_salen_de_la_v1_con_sus_avisos_aunque_se_haya_editado(cliente):
    pieza_id = _subir(cliente, "cooperadora.xlsx", _libro(), XLSX)
    url = f"/api/piezas/{pieza_id}/ficha"
    guardado = cliente.put(
        url,
        json=EDICION_COMPLETA
        | {"version_base": cliente.get(url).json()["version"], "cuerpo": "Reescrito", "contexto": "| a | b |"},
    )
    assert guardado.status_code == 200, guardado.text

    planilla = cliente.get(f"/api/piezas/{pieza_id}/planilla").json()

    assert planilla["avisos"] == ["Se omitió 1 hoja oculta."]
    assert [(h["nombre"], h["columnas"], h["filas"]) for h in planilla["hojas"]] == [
        ("Ventas", ["Fecha", "Monto"], [["1998-03-01", "100"], ["1998-03-02", "200"]]),
        ("Notas", ["Sin firmas"], []),
    ]


@pytest.mark.rf("RF-11")
def test_solo_una_planilla_tiene_visor_de_hojas(cliente, pieza_pendiente):
    respuesta = cliente.get(f"/api/piezas/{pieza_pendiente}/planilla")

    assert (respuesta.status_code, respuesta.json()["type"]) == (404, "/errores/pieza-no-encontrada")


@pytest.mark.rf("RF-11")
def test_una_planilla_recibida_todavia_no_tiene_visor(cliente, capa_modelos):
    capa_modelos.demorar()
    entrar(cliente, MARIA)
    pieza_id = cliente.post(
        "/api/piezas", files={"archivo": ("socios.csv", HOSTIL, "text/csv")}
    ).json()["pieza_id"]

    respuesta = cliente.get(f"/api/piezas/{pieza_id}/planilla")
    entrar(cliente, DIRECTORA_DE_CARRERA)
    oculta = cliente.get(f"/api/piezas/{pieza_id}/planilla")

    assert (respuesta.status_code, respuesta.json()["type"]) == (409, "/errores/transicion-invalida")
    # R-05 va primero: quien solo consulta no se entera de que existe.
    assert (oculta.status_code, oculta.json()["type"]) == (404, "/errores/pieza-no-encontrada")


@pytest.mark.rf("RF-53")
def test_quien_solo_consulta_no_ve_la_planilla_de_una_pieza_no_validada(cliente):
    pieza_id = _subir(cliente, "socios.csv", HOSTIL, "text/csv")
    entrar(cliente, DIRECTORA_DE_CARRERA)

    respuesta = cliente.get(f"/api/piezas/{pieza_id}/planilla")

    # R-05: no se entera de que existe.
    assert (respuesta.status_code, respuesta.json()["type"]) == (404, "/errores/pieza-no-encontrada")


@pytest.mark.rf("RF-38")
def test_el_aviso_de_columnas_recortadas_llega_al_visor_y_a_la_hoja(cliente):
    fila = ";".join(f"c{n}" for n in range(41)).encode()
    pieza_id = _subir(cliente, "ancho.csv", fila + b"\n" + fila + b"\n", "text/csv")

    planilla = cliente.get(f"/api/piezas/{pieza_id}/planilla").json()

    assert planilla["avisos"] == ["Hoja «ancho»: se muestran 40 de 41 columnas."]
    [hoja] = planilla["hojas"]
    assert hoja["nota"].endswith(" · Se muestran 40 de 41 columnas.")
    assert len(hoja["columnas"]) == 40
