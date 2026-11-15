"""Lo que comparten los tests que validan una Ficha: la persona, el archivo y la edición.

Vive acá y no en `conftest.py` porque no es infraestructura de la suite sino vocabulario del
caso: un documento de la FIE, una persona de la SEU y lo que su pantalla manda al guardar.
"""

from __future__ import annotations

from aplicacion import entrar
from capa_modelos_falsa import TRANSCRIPCION_FIJA
from historia_viva.api.sesion import COOKIE_DE_SESION

MARIA = "11111111-1111-4111-8111-111111111111"
JORGE = "22222222-2222-4222-8222-222222222222"
DIRECTORA_DE_CARRERA = "33333333-3333-4333-8333-333333333333"
ADMINISTRADOR_TECNICO = "44444444-4444-4444-8444-444444444444"
"""Tomás, de la semilla: el único rol con OPERAR_SISTEMA (DEC-124)."""
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"

CONTEXTO = (
    "Es la sesión en la que se aprobó el primer plan de estudios de Ingeniería en Sistemas."
)
"""Lo que solo sabe la persona. Viaja aparte del cuerpo y no es un campo del frontmatter."""

TRANSCRIPCION_CORREGIDA = TRANSCRIPCION_FIJA.replace(
    "Facultad.", "Facultad de Ingeniería del Ejército."
)
"""La transcripción después de que una persona arregló lo que el modelo escribió mal (US 14).
Difiere de `TRANSCRIPCION_FIJA` en un punto exacto, que es lo que vuelve distinguible "lo que
guardó la persona" de "lo que propuso el modelo"."""

CON_CONTEXTO = {"contexto": CONTEXTO}
"""El Contexto ya agregado: el trabajo que no se puede perder por un campo duro que falta."""

EDICION_COMPLETA = {
    "version_base": 1,
    "tipo": "acta",
    "tipo_actividad": "Evento Académico",
    "fecha_inicio": "1975-05-03",
    "precision_fecha": "dia",
    "origen": "Archivo de la Secretaría de Extensión Universitaria",
    "propietario": "Secretaría de Extensión Universitaria",
    "resumen": "Acta de la sesión del Consejo Académico del 3 de mayo de 1975.",
    "cuerpo": TRANSCRIPCION_FIJA,
    "contexto": "",
}
"""Lo que manda la pantalla con los nueve obligatorios puestos y una Fecha exacta (DEC-126)."""


def abrir(cliente, pieza_id):
    """Lo que hace la pantalla partida al abrirse: arranca el reloj de la medición (US 22)."""
    respuesta = cliente.post(f"/api/piezas/{pieza_id}/apertura")
    assert respuesta.status_code == 201
    return respuesta.json()


def mediciones(cliente):
    """Las filas de `GET /api/mediciones`, de la más reciente a la más antigua.

    Las lee el Administrador técnico (DEC-93) y la sesión vuelve después a quien estaba.
    """
    cookie = COOKIE_DE_SESION[cliente.app.state.modo_de_ingreso]
    anterior = cliente.cookies.get(cookie)
    respuesta = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/mediciones")
    if anterior:
        cliente.cookies.set(cookie, anterior)
    else:
        cliente.cookies.delete(cookie)
    assert respuesta.status_code == 200
    return respuesta.json()["mediciones"]


def edicion_sin(*campos: str) -> dict[str, object]:
    """`EDICION_COMPLETA` menos los campos que la persona no conocía.

    Es exactamente lo que manda la pantalla cuando alguien aprieta cualquiera de los dos
    botones con un dato duro que no tiene a mano: los campos vacíos no viajan.
    """
    return {clave: valor for clave, valor in EDICION_COMPLETA.items() if clave not in campos}
