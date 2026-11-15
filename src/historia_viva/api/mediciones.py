"""Instrumentación: arrancar el reloj de una validación y leer lo que midió la sesión.

Son los dos extremos de la user story 22. `POST /api/piezas/{id}/apertura` lo llama la
pantalla partida al abrirse; el `PUT` de la Ficha lo cierra. `GET /api/mediciones` devuelve
las filas, y `GET /api/mediciones.csv` la misma tabla como archivo para el gate (user story 25).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from historia_viva.api.dependencias import modulo_de_fichas, registro_de_mediciones
from historia_viva.api.sesion import exigir
from historia_viva.dominio.errores import TransicionInvalida
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.permisos import Permiso
from historia_viva.exportacion import escribir_csv
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Principal
from historia_viva.instrumentacion import COLUMNAS, Medicion, RegistroMediciones

__all__ = ["momento", "router"]

router = APIRouter(prefix="/api", tags=["Instrumentación"])


def momento(marca: datetime) -> str:
    """En UTC y al segundo: estas filas se exportan y se comparan entre sí (user story 25)."""
    return marca.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _momento_o_nulo(marca: datetime | None) -> str | None:
    return None if marca is None else momento(marca)


def _fila(medicion: Medicion) -> dict[str, Any]:
    return {
        "medicion_id": str(medicion.medicion_id),
        "pieza_id": str(medicion.pieza_id),
        "persona_id": str(medicion.persona_id),
        "nombre_archivo": medicion.nombre_archivo,
        "abierta_el": momento(medicion.abierta_el),
        "guardada_el": _momento_o_nulo(medicion.guardada_el),
        "duracion_s": medicion.duracion_s,
        "estado_final": None if medicion.estado_final is None else medicion.estado_final.value,
        "campos_modificados": medicion.campos_modificados,
        "campos_faltantes": medicion.campos_faltantes,
        "cuerpo_editado": medicion.cuerpo_editado,
        "caracteres_cuerpo_propuesta": medicion.caracteres_cuerpo_propuesta,
        "caracteres_cuerpo_guardado": medicion.caracteres_cuerpo_guardado,
        "clase_original": medicion.clase_original,
        "metodo_autenticacion": medicion.metodo_autenticacion,
        "modo_validacion": medicion.modo_validacion,
    }


@router.post("/piezas/{pieza_id}/apertura", status_code=201)
def registrar_apertura(
    pieza_id: UUID,
    principal: Annotated[Principal, Depends(exigir(Permiso.VALIDAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
    registro: Annotated[RegistroMediciones, Depends(registro_de_mediciones)],
) -> dict[str, str]:
    """Marca que se abrió la Ficha y arranca el reloj en el servidor (user story 22)."""
    # El estado se mira antes de pedir la propuesta: una Pieza todavía `Recibida` no tiene
    # Ficha que abrir, y eso es un 409 —"esperá a que termine el procesamiento"— y no el 404
    # con que el Módulo de Fichas responde a "no hay versión que leer".
    pieza = fichas.obtener_pieza(pieza_id)
    if pieza.fila.estado is Estado.RECIBIDA:
        raise TransicionInvalida(pieza_id, pieza.fila.estado.value, Estado.PENDIENTE.value)
    apertura = registro.abrir(
        pieza=pieza, propuesta=fichas.obtener_propuesta(pieza_id), persona_id=principal.usuario_id
    )
    return {
        "medicion_id": str(apertura.medicion_id),
        "abierta_el": momento(apertura.abierta_el),
    }


@router.get("/mediciones")
def listar_mediciones(
    _: Annotated[Principal, Depends(exigir(Permiso.OPERAR_SISTEMA))],
    registro: Annotated[RegistroMediciones, Depends(registro_de_mediciones)],
) -> dict[str, list[dict[str, Any]]]:
    """Una fila por Ficha trabajada, de la más reciente a la más antigua.

    Solo con OPERAR_SISTEMA, como el CSV: son tiempos de trabajo por persona (DEC-93)."""
    return {"mediciones": [_fila(medicion) for medicion in registro.listar()]}


@router.get("/mediciones.csv", response_class=Response)
def exportar_mediciones(
    _: Annotated[Principal, Depends(exigir(Permiso.OPERAR_SISTEMA))],
    registro: Annotated[RegistroMediciones, Depends(registro_de_mediciones)],
) -> Response:
    """Las mismas filas de `GET /api/mediciones`, como adjunto para la planilla del gate.

    Solo con OPERAR_SISTEMA: son tiempos de trabajo por persona (DEC-93)."""
    return Response(
        content=escribir_csv(
            COLUMNAS,
            ([fila[columna] for columna in COLUMNAS] for fila in map(_fila, registro.listar())),
        ),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="mediciones-historia-viva.csv"'},
    )
