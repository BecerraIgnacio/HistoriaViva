"""`/api/admin/modelos`: la configuración de los modelos desde la pantalla (spec, tabla de
endpoints; P7a, CU-19, RF-32, DEC-53, DEC-87).

El guardado es atómico sobre el conjunto y valida todas las filas juntas; rige para los trabajos
nuevos. Que el modelo no esté descargado avisa y no bloquea: se pregunta al motor fuera de la
transacción, con la capa armada sobre las filas que se miran.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from historia_viva.api.auditoria import _antes_de, _cursor
from historia_viva.api.mediciones import momento
from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import exigir
from historia_viva.capa_modelos import entradas
from historia_viva.capa_modelos.configuracion import (
    MOTOR_LOCAL,
    ConfiguracionDeCapacidad,
    Disponibilidad,
    avisos_de,
)
from historia_viva.capa_modelos.contrato import Capacidad
from historia_viva.configuracion import (
    Cambio,
    ConfiguracionDeModelos,
    ConfiguracionDesactualizada,
    FilaVigente,
)
from historia_viva.dominio.errores import ConfiguracionInvalida
from historia_viva.dominio.permisos import Permiso
from historia_viva.identidad import Principal

__all__ = ["router"]

_OPERAR_SISTEMA = exigir(Permiso.OPERAR_SISTEMA)

router = APIRouter(
    prefix="/api/admin/modelos",
    tags=["Administración"],
    dependencies=[Depends(_OPERAR_SISTEMA)],
)

Conjunto = dict[Capacidad, ConfiguracionDeCapacidad]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motor: str = Field(min_length=1, max_length=100)
    # Sin espacios: un nombre de modelo de Ollama no los lleva. Vacío lo informa `validar`.
    modelo: str | None = Field(default=None, max_length=200, pattern=r"^\S*$")
    instruccion_id: str | None = Field(default=None, max_length=100)
    parametros: dict[str, Any]


class CambioPedido(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacidad: Capacidad
    version_base: int = Field(ge=1)
    config: Config


class PedidoCambios(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cambios: list[CambioPedido] = Field(min_length=1, max_length=len(Capacidad))


def _cambios(pedido: PedidoCambios) -> list[Cambio]:
    capacidades = [c.capacidad for c in pedido.cambios]
    if len(set(capacidades)) < len(capacidades):
        raise Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle="Cada capacidad va una sola vez en `cambios`.",
            campo="cambios",
        )
    return [
        Cambio(c.capacidad, ConfiguracionDeCapacidad(**c.config.model_dump()), c.version_base)
        for c in pedido.cambios
    ]


def _configuracion(request: Request) -> ConfiguracionDeModelos:
    configuracion: ConfiguracionDeModelos = request.app.state.configuracion
    return configuracion


def _disponibilidad(request: Request, conjunto: Conjunto) -> dict[Capacidad, Disponibilidad]:
    """Si el modelo de cada fila está en su motor. Vacío si el conjunto no arma una capa."""
    try:
        capa = request.app.state.capa_para(conjunto)
    except ConfiguracionInvalida:
        return {}
    def estado(capacidad: Capacidad, faltantes: set[Capacidad]) -> Disponibilidad:
        if conjunto[capacidad].motor == MOTOR_LOCAL:
            return Disponibilidad.NO_APLICA
        if not capa.verificar_salud((capacidad,)):
            return Disponibilidad.MOTOR_NO_RESPONDE
        return Disponibilidad.NO_DESCARGADO if capacidad in faltantes else Disponibilidad.DESCARGADO

    try:
        # La capa consulta una vez por motor y lo recuerda: son dos `/api/tags`.
        faltantes = set(capa.modelos_faltantes())
        return {capacidad: estado(capacidad, faltantes) for capacidad in conjunto}
    except Exception:  # noqa: BLE001 — cualquier falla al preguntar es «no responde», como en la salud
        return {
            c: Disponibilidad.NO_APLICA if config.motor == MOTOR_LOCAL
            else Disponibilidad.MOTOR_NO_RESPONDE
            for c, config in conjunto.items()
        }


def _fila(
    request: Request,
    capacidad: Capacidad,
    fila: FilaVigente,
    disponibilidad: dict[Capacidad, Disponibilidad],
) -> dict[str, Any]:
    estado = disponibilidad.get(capacidad)
    eleccion = request.app.state.eleccion_del_banco(capacidad.value)
    return {
        "capacidad": capacidad.value,
        "motor": fila.config.motor,
        "modelo": fila.config.modelo,
        "instruccion_id": fila.config.instruccion_id,
        "parametros": dict(fila.config.parametros),
        "version": fila.version,
        "actualizada_por": fila.actualizada_por,
        "actualizada_el": momento(fila.actualizada_el),
        # Nulo si no aplica (`transcribir`) o no se sabe (motor sin respuesta, fila rota).
        "descargado": {
            Disponibilidad.DESCARGADO: True, Disponibilidad.NO_DESCARGADO: False
        }.get(estado) if estado else None,
        "disponibilidad": estado and estado.value,
        "banco": eleccion and {"ganador": eleccion[0], "mejor_no_chino": eleccion[1]},
    }


def _filas(request: Request, filas: dict[Capacidad, FilaVigente]) -> list[dict[str, Any]]:
    disponibilidad = _disponibilidad(request, {c: f.config for c, f in filas.items()})
    return [_fila(request, c, filas[c], disponibilidad) for c in Capacidad if c in filas]


def _avisos(
    request: Request, nuevo: Conjunto, anterior: Conjunto, cambiadas: list[Capacidad]
) -> tuple[list[dict[str, str]], dict[Capacidad, Disponibilidad]]:
    disponibilidad = _disponibilidad(request, nuevo)
    avisos = avisos_de(
        nuevo, anterior, {c: d for c, d in disponibilidad.items() if c in cambiadas}
    )
    return [
        {"capacidad": a.capacidad.value, "codigo": a.codigo, "mensaje": a.mensaje} for a in avisos
    ], disponibilidad


@router.get("")
def leer_modelos(request: Request) -> dict[str, Any]:
    """Las seis filas con su estado de descarga, la nota del banco y las instrucciones que
    puede nombrar cada una. No valida: una fila rota por SQL también se ve, para arreglarla."""
    filas = _configuracion(request).filas()
    return {
        "capacidades": _filas(request, filas),
        "instrucciones": {c.value: list(entradas.instrucciones_de(c)) for c in Capacidad},
    }


@router.put("")
def guardar_modelos(
    pedido: PedidoCambios,
    request: Request,
    principal: Annotated[Principal, Depends(_OPERAR_SISTEMA)],
) -> dict[str, Any]:
    """Todo o nada, con `version_base` por fila: 409 con la fila actual si otra persona la
    guardó antes; 422 `configuracion-invalida` con todos los errores del conjunto."""
    cambios = _cambios(pedido)
    configuracion = _configuracion(request)
    try:
        with request.app.state.bd.transaccion() as tx:
            resultado = configuracion.actualizar(tx, cambios, principal.actor)
    except ConfiguracionDesactualizada as conflicto:
        raise Problema(
            tipo="/errores/version-desactualizada",
            titulo="La configuración cambió mientras la editabas",
            estado=409,
            detalle=str(conflicto),
            extensiones={
                "capacidad": conflicto.capacidad.value,
                "version_base": conflicto.version_base,
                "version_actual": conflicto.actual.version,
                "fila_actual": jsonable_encoder(
                    _fila(request, conflicto.capacidad, conflicto.actual, {})
                ),
            },
        ) from conflicto
    anterior = resultado.anterior
    nuevo = {c: f.config for c, f in resultado.filas.items()}
    cambiadas = [c.capacidad for c in cambios if c.config != anterior[c.capacidad]]
    avisos, disponibilidad = _avisos(request, nuevo, anterior, cambiadas)
    return {
        "filas": [
            _fila(request, c, resultado.filas[c], disponibilidad)
            for c in Capacidad if c in resultado.filas
        ],
        "avisos": avisos,
        "aplica_a": "trabajos nuevos",
        "requiere_reindexado": any(a["codigo"] == "requiere_reindexado" for a in avisos),
    }


@router.post("/verificacion")
def verificar_modelos(pedido: PedidoCambios, request: Request) -> dict[str, Any]:
    """Lo mismo que guardar, sin guardar ni mirar `version_base`: los errores, los avisos y si
    cada modelo está descargado. Con errores, sin disponibilidad (no se arma la capa)."""
    cambios = _cambios(pedido)
    configuracion = _configuracion(request)
    anterior = configuracion.vigente()
    try:
        nuevo = configuracion.con_cambios(anterior, cambios)
    except ConfiguracionInvalida as error:
        return {
            "ok": False,
            "errores": [e._asdict() for e in error.errores],
            "avisos": [],
            "disponibilidad": {},
        }
    cambiadas = [c.capacidad for c in cambios if c.config != anterior[c.capacidad]]
    avisos, disponibilidad = _avisos(request, nuevo, anterior, cambiadas)
    return {
        "ok": True,
        "errores": [],
        "avisos": avisos,
        "disponibilidad": {c.value: d.value for c, d in disponibilidad.items()},
    }


@router.get("/{capacidad}/historial")
def historial_de_modelo(
    capacidad: str,
    request: Request,
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Los `modelo.cambio` de la capacidad, del más nuevo al más viejo, con antes y después."""
    if capacidad not in {c.value for c in Capacidad}:
        raise Problema(
            tipo="/errores/recurso-no-encontrado",
            titulo="No existe el recurso",
            estado=404,
            detalle=f"No hay una capacidad «{capacidad}».",
            extensiones={"recurso": "capacidad"},
        )
    # Uno de más para saber si hay otra página sin contar.
    eventos = _configuracion(request).historial(
        Capacidad(capacidad),
        limite=limite + 1,
        antes_de=None if cursor is None else _antes_de(cursor),
    )
    return {
        "eventos": [
            {
                "fecha": momento(e.cuando),
                "persona": e.actor.etiqueta,
                "antes": e.antes,
                "despues": e.despues,
            }
            for e in eventos[:limite]
        ],
        "siguiente": _cursor(eventos[limite - 1]) if len(eventos) > limite else None,
    }
