"""El conjunto de referencia del banco (spec, módulo 27 `banco`; DEC-97).

Vive fuera de git, en `HV_REFERENCIA` (DEC-120): `piezas.csv`, `consultas.csv` y los originales
que nombran. No hay tablas del banco: `cargar_referencia` lee los dos archivos, los valida contra
las Fichas en la transacción de quien llama, sin escribir, y calcula `hash_referencia`.

`piezas.csv`: `referencia_id, archivo, pieza_id, tipo, hechos_esperados, invenciones,
texto_esperado, datos_clave`; `archivo` es relativo al directorio y `pieza_id` queda vacío si el
archivo no está cargado en el Acervo.

`consultas.csv`: `consulta_id, texto, piezas_esperadas, desde, hasta, tipo, tipo_actividad,
carrera, conjunto, escrita_por, sin_evidencia`; `piezas_esperadas` son `referencia_id`; las
fechas, en ISO 8601; `conjunto`, `ajuste` o `prueba`; `sin_evidencia`, `true` en las preguntas
de control, que no tienen Pieza esperada.

Las listas van en una celda, separadas con `;`, como las escribe `exportacion`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from historia_viva.bd import Tx
from historia_viva.dominio.tipos import CamposFicha, FiltroDeBusqueda
from historia_viva.planillas import leer_registros

if TYPE_CHECKING:
    from historia_viva.fichas.postgres import ModuloFichasPostgres

__all__ = [
    "ARCHIVOS",
    "ConjuntoDeReferencia",
    "ConsultaDeReferencia",
    "PiezaDeReferencia",
    "ReferenciaInvalida",
    "cargar_referencia",
    "excluye",
    "huella",
]

ARCHIVOS = ("piezas.csv", "consultas.csv")
"""El manifiesto: lo que entra en `hash_referencia`."""

_CONJUNTOS = ("ajuste", "prueba")


class ReferenciaInvalida(Exception):
    """El conjunto no se puede usar; `errores` los lista todos, uno por línea."""

    def __init__(self, errores: Sequence[str]) -> None:
        super().__init__("\n".join(errores))
        self.errores = tuple(errores)


@dataclass(frozen=True)
class PiezaDeReferencia:
    referencia_id: str
    archivo: str
    pieza_id: UUID | None
    tipo: str | None
    hechos_esperados: tuple[str, ...]
    invenciones: tuple[str, ...]
    texto_esperado: str | None
    datos_clave: tuple[str, ...]


@dataclass(frozen=True)
class ConsultaDeReferencia:
    consulta_id: str
    texto: str
    piezas_esperadas: tuple[str, ...]
    """`referencia_id` de `piezas.csv`."""
    filtro: FiltroDeBusqueda
    conjunto: str
    escrita_por: str | None
    sin_evidencia: bool


@dataclass(frozen=True)
class ConjuntoDeReferencia:
    hash_referencia: str
    piezas: tuple[PiezaDeReferencia, ...]
    consultas: tuple[ConsultaDeReferencia, ...]


def huella(directorio: Path) -> str:
    """`sha256` del manifiesto: dos corridas con la misma huella midieron lo mismo.

    Raises:
        ReferenciaInvalida: Falta alguno de los dos archivos.
    """
    faltan = [nombre for nombre in ARCHIVOS if not (directorio / nombre).is_file()]
    if faltan:
        raise ReferenciaInvalida([f"Falta {nombre} en {directorio}." for nombre in faltan])
    suma = hashlib.sha256()
    for nombre in ARCHIVOS:
        contenido = (directorio / nombre).read_bytes()
        suma.update(f"{nombre}\0{len(contenido)}\0".encode())
        suma.update(contenido)
    return suma.hexdigest()


def cargar_referencia(
    tx: Tx, directorio: Path, *, fichas: ModuloFichasPostgres
) -> ConjuntoDeReferencia:
    """Lee y valida el conjunto: los archivos existen, cada consulta tiene al menos una Pieza
    esperada (salvo las de control) y su filtro no la excluye. No escribe en la base.

    Raises:
        ReferenciaInvalida: Con todos los problemas encontrados.
    """
    hash_referencia = huella(directorio)
    errores: list[str] = []
    piezas = _leer_piezas(directorio, errores)
    consultas = _leer_consultas(directorio, errores)
    por_id = {p.referencia_id: p for p in piezas}
    ids = [p.pieza_id for p in piezas if p.pieza_id]
    medibles = fichas.medibles(tx, ids)
    for pieza in piezas:
        if pieza.pieza_id and (pieza.pieza_id not in medibles or not medibles[pieza.pieza_id].ficha):
            errores.append(
                f"piezas.csv, {pieza.referencia_id}: la Pieza {pieza.pieza_id} no está en el"
                " Acervo o no tiene Ficha."
            )
    for consulta in consultas:
        if not consulta.piezas_esperadas and not consulta.sin_evidencia:
            errores.append(f"consultas.csv, {consulta.consulta_id}: no tiene Pieza esperada.")
        for referencia_id in consulta.piezas_esperadas:
            esperada = por_id.get(referencia_id)
            medible = medibles.get(esperada.pieza_id) if esperada and esperada.pieza_id else None
            if esperada is None:
                errores.append(
                    f"consultas.csv, {consulta.consulta_id}: la Pieza esperada {referencia_id}"
                    " no está en piezas.csv."
                )
            elif esperada.pieza_id is None:
                errores.append(
                    f"consultas.csv, {consulta.consulta_id}: la Pieza esperada {referencia_id}"
                    " no tiene pieza_id: no está cargada en el Acervo."
                )
            elif medible and medible.ficha and (
                motivo := excluye(consulta.filtro, medible.ficha.campos)
            ):
                errores.append(
                    f"consultas.csv, {consulta.consulta_id}: el filtro excluye a su Pieza"
                    f" esperada {referencia_id} ({motivo})."
                )
    if errores:
        raise ReferenciaInvalida(errores)
    return ConjuntoDeReferencia(hash_referencia, tuple(piezas), tuple(consultas))


def excluye(filtro: FiltroDeBusqueda, campos: CamposFicha) -> str | None:
    """Por qué la búsqueda con `filtro` no traería a la Pieza, con la regla de `indice`."""
    if filtro.hasta and (campos.fecha_inicio is None or campos.fecha_inicio > filtro.hasta):
        return "su Fecha empieza después de «hasta»"
    if filtro.desde and (campos.fecha_fin is None or campos.fecha_fin < filtro.desde):
        return "su Fecha termina antes de «desde»"
    if filtro.tipos and campos.tipo not in filtro.tipos:
        return f"es de tipo «{campos.tipo}»"
    if filtro.tipos_actividad and campos.tipo_actividad not in filtro.tipos_actividad:
        return f"su tipo de actividad es «{campos.tipo_actividad}»"
    if filtro.carreras and not set(filtro.carreras) & set(campos.carreras):
        return "no tiene ninguna de las Carreras pedidas"
    return None


def _registros(directorio: Path, nombre: str, errores: list[str]) -> list[dict[str, str]]:
    try:
        with (directorio / nombre).open("rb") as origen:
            cabecera, *filas = leer_registros(origen, nombre) or [[]]
    except ValueError as error:
        errores.append(str(error))
        return []
    columnas = [c.strip().lower() for c in cabecera]
    return [
        {c: v.strip() for c, v in zip(columnas, fila, strict=False)} for fila in filas
    ]


def _lista(valor: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (valor or "").split(";") if v.strip())


def _leer_piezas(directorio: Path, errores: list[str]) -> list[PiezaDeReferencia]:
    raiz = directorio.resolve()
    piezas: list[PiezaDeReferencia] = []
    vistos: set[str] = set()
    for n, fila in enumerate(_registros(directorio, "piezas.csv", errores), start=2):
        donde = f"piezas.csv, línea {n}"
        referencia_id, archivo = fila.get("referencia_id", ""), fila.get("archivo", "")
        if not referencia_id or referencia_id in vistos:
            errores.append(f"{donde}: referencia_id vacío o repetido.")
            continue
        vistos.add(referencia_id)
        ruta = (raiz / archivo).resolve()
        if not archivo or not ruta.is_relative_to(raiz) or not ruta.is_file():
            errores.append(f"{donde}: falta el archivo «{archivo}» en el directorio.")
        pieza_id: UUID | None = None
        if valor := fila.get("pieza_id"):
            try:
                pieza_id = UUID(valor)
            except ValueError:
                errores.append(f"{donde}: pieza_id «{valor}» no es un UUID.")
        piezas.append(
            PiezaDeReferencia(
                referencia_id=referencia_id,
                archivo=archivo,
                pieza_id=pieza_id,
                tipo=fila.get("tipo") or None,
                hechos_esperados=_lista(fila.get("hechos_esperados")),
                invenciones=_lista(fila.get("invenciones")),
                texto_esperado=fila.get("texto_esperado") or None,
                datos_clave=_lista(fila.get("datos_clave")),
            )
        )
    return piezas


def _fecha(fila: Mapping[str, str], campo: str, donde: str, errores: list[str]) -> date | None:
    if not (valor := fila.get(campo)):
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        errores.append(f"{donde}: «{campo}» = «{valor}» no es una fecha ISO 8601.")
        return None


def _leer_consultas(directorio: Path, errores: list[str]) -> list[ConsultaDeReferencia]:
    consultas: list[ConsultaDeReferencia] = []
    vistos: set[str] = set()
    for n, fila in enumerate(_registros(directorio, "consultas.csv", errores), start=2):
        donde = f"consultas.csv, línea {n}"
        consulta_id = fila.get("consulta_id", "")
        if not consulta_id or consulta_id in vistos:
            errores.append(f"{donde}: consulta_id vacío o repetido.")
            continue
        vistos.add(consulta_id)
        if not fila.get("texto"):
            errores.append(f"{donde}: la consulta no tiene texto.")
        conjunto = fila.get("conjunto", "")
        if conjunto not in _CONJUNTOS:
            errores.append(f"{donde}: «conjunto» tiene que ser ajuste o prueba.")
        sin_evidencia = fila.get("sin_evidencia", "").lower()
        if sin_evidencia not in ("", "true", "false"):
            errores.append(f"{donde}: «sin_evidencia» tiene que ser true, false o vacío.")
        desde, hasta = (_fecha(fila, c, donde, errores) for c in ("desde", "hasta"))
        if desde and hasta and desde > hasta:
            errores.append(f"{donde}: «desde» es posterior a «hasta».")
        consultas.append(
            ConsultaDeReferencia(
                consulta_id=consulta_id,
                texto=fila.get("texto", ""),
                piezas_esperadas=_lista(fila.get("piezas_esperadas")),
                filtro=FiltroDeBusqueda(
                    desde=desde,
                    hasta=hasta,
                    tipos=_lista(fila.get("tipo")),
                    tipos_actividad=_lista(fila.get("tipo_actividad")),
                    carreras=_lista(fila.get("carrera")),
                ),
                conjunto=conjunto,
                escrita_por=fila.get("escrita_por") or None,
                sin_evidencia=sin_evidencia == "true",
            )
        )
    return consultas
