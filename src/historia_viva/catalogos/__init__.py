"""Los catálogos de la Ficha: Carreras, tipos de actividad y tipos de Pieza (DEC-127).

Es la única puerta a `carrera`, `tipo_actividad` y `tipo_pieza` (R-25): las siembra una migración
y `hv_app` solo las lee (DEC-105), salvo las Carreras, que carga `catalogo carreras --cargar`
hasta que C45 permita sembrarlas por migración. Se leen en cada consulta, sin caché: una lista
corregida vale para la Pieza siguiente. La baja es lógica: lo desactivado no se ofrece ni entra
en una Ficha, pero la que ya lo tenía lo conserva.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass
from typing import BinaryIO, ClassVar, LiteralString

import psycopg

from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Tx
from historia_viva.dominio.errores import ErrorHistoriaViva
from historia_viva.planillas import leer_registros

__all__ = [
    "CargaRechazada",
    "Carrera",
    "CarreraDesconocida",
    "FilaDeCarrera",
    "ResultadoDeCarga",
    "NO_ES_UNA_ACTIVIDAD",
    "TipoDeActividad",
    "TipoDeActividadDesconocido",
    "TipoDePieza",
    "TipoDesconocido",
    "ValorFueraDeCatalogo",
    "cargar_carreras",
    "carreras",
    "es_actividad",
    "leer_carreras",
    "tipos_actividad",
    "tipos_de_pieza",
    "validar_carreras",
    "validar_tipo_actividad",
    "validar_tipo_de_pieza",
]


@dataclass(frozen=True)
class Carrera:
    carrera_id: str
    """Slug estable (`^[a-z0-9-]+$`): lo que viaja en la Ficha y en los filtros."""
    nombre: str


@dataclass(frozen=True)
class TipoDeActividad:
    nombre: str
    orden: int
    es_actividad: bool
    """Falso solo en «No es una actividad»: el informe de acreditación la excluye (DEC-131)."""
    activo: bool


NO_ES_UNA_ACTIVIDAD = "No es una actividad"


def es_actividad(tipo_actividad: str) -> bool:
    """Falso solo para «No es una actividad», la que el informe excluye (DEC-131). Coincide con
    la columna `es_actividad` de la siembra (`test_catalogos`)."""
    return tipo_actividad != NO_ES_UNA_ACTIVIDAD


@dataclass(frozen=True)
class TipoDePieza:
    nombre: str
    provisorio: bool
    """La SEU todavía no lo confirmó (C4); se usa igual y la pantalla lo marca."""


class ValorFueraDeCatalogo(ErrorHistoriaViva):
    """Un valor que su catálogo no admite. El borde HTTP lo traduce a 422 con `codigo` y
    `campo`."""

    codigo: ClassVar[str]
    titulo: ClassVar[str]
    campo: ClassVar[str]


class TipoDesconocido(ValorFueraDeCatalogo):
    codigo = "tipo-desconocido"
    titulo = "Tipo de Pieza desconocido"
    campo = "tipo"


class TipoDeActividadDesconocido(ValorFueraDeCatalogo):
    codigo = "tipo-actividad-desconocido"
    titulo = "Tipo de actividad desconocido"
    campo = "tipo_actividad"


class CarreraDesconocida(ValorFueraDeCatalogo):
    codigo = "carrera-desconocida"
    titulo = "Carrera desconocida"
    campo = "carreras"


def carreras(tx: Tx) -> list[Carrera]:
    """Las activas, en el orden del catálogo."""
    filas = tx.execute(
        "SELECT carrera_id, nombre FROM acervo.carrera WHERE activo ORDER BY orden, nombre"
    )
    return [Carrera(**fila) for fila in filas]


def tipos_actividad(tx: Tx, *, solo_activos: bool = True) -> list[TipoDeActividad]:
    """En el orden del catálogo; sin `solo_activos`, también los dados de baja, que una Ficha
    puede conservar (DEC-127)."""
    filas = tx.execute(
        "SELECT nombre, orden, es_actividad, activo FROM acervo.tipo_actividad"
        " WHERE activo OR NOT %s ORDER BY orden, nombre",
        (solo_activos,),
    )
    return [TipoDeActividad(**fila) for fila in filas]


def tipos_de_pieza(tx: Tx) -> list[TipoDePieza]:
    """Los activos, en el orden del catálogo."""
    filas = tx.execute(
        "SELECT nombre, provisorio FROM acervo.tipo_pieza WHERE activo ORDER BY orden, nombre"
    )
    return [TipoDePieza(**fila) for fila in filas]


def validar_tipo_de_pieza(tx: Tx, nombre: str, *, conserva: str | None = None) -> None:
    """`conserva` es el tipo que la Ficha ya tenía: dado de baja, se acepta sin cambios.

    Raises:
        TipoDesconocido: No existe, o está dado de baja y no es `conserva`.
    """
    consulta: LiteralString = "SELECT activo FROM acervo.tipo_pieza WHERE nombre = %s"
    if not _admitido(tx, consulta, nombre, conserva):
        raise TipoDesconocido(f"'{nombre}' no está en la lista de GET /api/tipos.")


def validar_tipo_actividad(tx: Tx, nombre: str, *, conserva: str | None = None) -> None:
    """Como `validar_tipo_de_pieza`, para el tipo de actividad.

    Raises:
        TipoDeActividadDesconocido: No existe, o está dado de baja y no es `conserva`.
    """
    consulta: LiteralString = "SELECT activo FROM acervo.tipo_actividad WHERE nombre = %s"
    if not _admitido(tx, consulta, nombre, conserva):
        raise TipoDeActividadDesconocido(
            f"'{nombre}' no está en la lista de GET /api/tipos-actividad."
        )


def validar_carreras(tx: Tx, ids: Sequence[str], *, conserva: Collection[str] = ()) -> None:
    """Sin Carreras es válido: son opcionales (DEC-126). `conserva` son las que la Ficha ya
    tenía: dadas de baja, se aceptan sin cambios (DEC-127).

    Raises:
        CarreraDesconocida: Una está repetida, no existe, o está dada de baja y no está en
            `conserva`.
    """
    if len(set(ids)) < len(ids):
        raise CarreraDesconocida("Hay una Carrera repetida.")
    admitidas = {
        fila["carrera_id"]
        for fila in tx.execute(
            "SELECT carrera_id FROM acervo.carrera"
            " WHERE (activo OR carrera_id = ANY(%s)) AND carrera_id = ANY(%s)",
            (list(conserva), list(ids)),
        )
    }
    if desconocidas := [carrera_id for carrera_id in ids if carrera_id not in admitidas]:
        raise CarreraDesconocida(
            f"'{desconocidas[0]}' no está en la lista de GET /api/carreras."
        )


_SLUG = re.compile(r"[a-z0-9-]+")
"""El `CHECK` de `carrera.carrera_id`: acá da un mensaje antes de tocar la base."""


@dataclass(frozen=True)
class FilaDeCarrera:
    carrera_id: str
    nombre: str
    orden: int


@dataclass(frozen=True)
class ResultadoDeCarga:
    """Los `carrera_id` que la carga cambió; vacíos si la lista ya era esa."""

    insertadas: tuple[str, ...]
    actualizadas: tuple[str, ...]
    desactivadas: tuple[str, ...]


class CargaRechazada(ErrorHistoriaViva):
    """La lista no se carga: el CSV entero se rechaza y la tabla queda como estaba (R-42)."""


def leer_carreras(origen: BinaryIO, nombre: str) -> list[FilaDeCarrera]:
    """El CSV de `catalogo carreras --cargar`: la cabecera `carrera_id,nombre` y una Carrera por
    registro. El orden del archivo es el de la lista.

    Raises:
        CargaRechazada: No se puede leer, no trae esa cabecera ni Carreras, o un registro no es
            un slug y un nombre, o repite uno de los dos.
    """
    try:
        cabecera, *registros = leer_registros(origen, nombre) or [[]]
    except ValueError as error:
        raise CargaRechazada(str(error)) from error
    if [columna.strip().lower() for columna in cabecera] != ["carrera_id", "nombre"]:
        raise CargaRechazada("La primera fila tiene que ser la cabecera «carrera_id,nombre».")
    if not registros:
        # Una lista vacía desactivaría todas: es un archivo equivocado, no una lista oficial.
        raise CargaRechazada("El CSV no trae ninguna Carrera.")
    filas = []
    for orden, registro in enumerate(registros, start=1):
        campos = [campo.strip() for campo in registro]
        if len(campos) != 2 or not _SLUG.fullmatch(campos[0]) or not campos[1]:
            raise CargaRechazada(
                f"La Carrera {orden} no es «carrera_id,nombre» con un carrera_id de minúsculas,"
                " dígitos y guiones y un nombre."
            )
        filas.append(FilaDeCarrera(campos[0], campos[1], orden))
    for campo in ("carrera_id", "nombre"):
        valores = [getattr(fila, campo) for fila in filas]
        if repetido := next((v for v in valores if valores.count(v) > 1), None):
            raise CargaRechazada(f"El {campo} «{repetido}» está repetido.")
    return filas


def cargar_carreras(
    tx: Tx, filas: Sequence[FilaDeCarrera], *, por: Actor, auditoria: RegistroDeAuditoria
) -> ResultadoDeCarga:
    """Deja la lista de `filas`: inserta las nuevas, actualiza nombre, orden y `activo` de las
    demás por `carrera_id` y desactiva las ausentes, sin borrarlas (DEC-127). La carga queda
    auditada en `tx` como `catalogo.carga` (R-42).

    Raises:
        CargaRechazada: Un nombre ya lo tiene otra Carrera, aunque esté desactivada; quien
            llama revierte `tx` y no queda nada a medias.
    """
    actuales = {
        fila["carrera_id"]: (fila["nombre"], fila["orden"], fila["activo"])
        for fila in tx.execute("SELECT carrera_id, nombre, orden, activo FROM acervo.carrera")
    }
    nuevas = [f for f in filas if f.carrera_id not in actuales]
    cambiadas = [
        f for f in filas
        if f.carrera_id in actuales and actuales[f.carrera_id] != (f.nombre, f.orden, True)
    ]
    cargadas = {f.carrera_id for f in filas}
    ausentes = sorted(
        carrera_id
        for carrera_id, (_, _, activo) in actuales.items()
        if activo and carrera_id not in cargadas
    )
    cursor = tx.cursor()
    # Los nombres valen sobre la lista final: un intercambio no depende del orden del CSV.
    cursor.execute("SET CONSTRAINTS acervo.carrera_nombre_key DEFERRED")
    cursor.executemany(
        "UPDATE acervo.carrera SET activo = false WHERE carrera_id = %s",
        [(carrera_id,) for carrera_id in ausentes],
    )
    cursor.executemany(
        "UPDATE acervo.carrera SET nombre = %s, orden = %s, activo = true WHERE carrera_id = %s",
        [(f.nombre, f.orden, f.carrera_id) for f in cambiadas],
    )
    cursor.executemany(
        "INSERT INTO acervo.carrera (carrera_id, nombre, orden) VALUES (%s, %s, %s)",
        [(f.carrera_id, f.nombre, f.orden) for f in nuevas],
    )
    try:
        cursor.execute("SET CONSTRAINTS acervo.carrera_nombre_key IMMEDIATE")
    except psycopg.errors.UniqueViolation as error:
        raise CargaRechazada(
            f"Un nombre ya lo tiene otra Carrera ({error.diag.message_detail})."
        ) from error
    resultado = ResultadoDeCarga(
        insertadas=tuple(f.carrera_id for f in nuevas),
        actualizadas=tuple(f.carrera_id for f in cambiadas),
        desactivadas=tuple(ausentes),
    )
    auditoria.registrar(
        tx,
        actor=por,
        accion=Accion.CATALOGO_CARGA,
        objeto=ObjetoRef("carrera"),
        despues=asdict(resultado),
    )
    return resultado


def _admitido(tx: Tx, consulta: LiteralString, nombre: str, conserva: str | None) -> bool:
    fila = tx.execute(consulta, (nombre,)).fetchone()
    # Desactivar no invalida Fichas: el valor que la Ficha ya tenía se conserva (DEC-127).
    return fila is not None and (fila["activo"] or nombre == conserva)
