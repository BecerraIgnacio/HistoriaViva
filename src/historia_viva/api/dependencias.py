"""Las piezas que la aplicación arma al construirse y los endpoints reciben inyectadas.

Viven acá y no en el router que las usó primero porque las comparten varios: el Módulo de
Fichas lo leen la carga, el detalle, el original y la Ficha.
"""

from __future__ import annotations

from fastapi import Request

from historia_viva.dominio.errores import PiezaNoEncontrada
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.permisos import ve_piezas_no_validadas
from historia_viva.dominio.tipos import DetallePieza
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Principal
from historia_viva.ingesta import Ingesta
from historia_viva.instrumentacion import RegistroMediciones
from historia_viva.originales import AlmacenDeOriginales
from historia_viva.procesamiento.cola import Cola
from historia_viva.validacion import Validacion

__all__ = [
    "almacen_de_originales",
    "cola_de_trabajos",
    "ingesta_de_piezas",
    "modulo_de_fichas",
    "registro_de_mediciones",
    "validacion_de_fichas",
    "visible_para",
]


def visible_para(principal: Principal, detalle: DetallePieza) -> DetallePieza:
    """`detalle`, o 404 si quien pide solo tiene CONSULTAR y la Pieza no está Validada (R-05):
    no se entera de que existe. La comparten el detalle, el original y la Ficha."""
    if detalle.fila.estado is not Estado.VALIDADA and not ve_piezas_no_validadas(principal.rol):
        raise PiezaNoEncontrada(detalle.fila.pieza_id)
    return detalle


def modulo_de_fichas(request: Request) -> ModuloFichasPostgres:
    fichas: ModuloFichasPostgres = request.app.state.fichas
    return fichas


def registro_de_mediciones(request: Request) -> RegistroMediciones:
    registro: RegistroMediciones = request.app.state.mediciones
    return registro


def validacion_de_fichas(request: Request) -> Validacion:
    validacion: Validacion = request.app.state.validacion
    return validacion


def almacen_de_originales(request: Request) -> AlmacenDeOriginales:
    originales: AlmacenDeOriginales = request.app.state.originales
    return originales


def cola_de_trabajos(request: Request) -> Cola:
    cola: Cola = request.app.state.cola
    return cola


def ingesta_de_piezas(request: Request) -> Ingesta:
    ingesta: Ingesta = request.app.state.ingesta
    return ingesta
