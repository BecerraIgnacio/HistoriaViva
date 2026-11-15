"""La Ficha como texto: frontmatter más cuerpo, en un solo markdown (ADR-0001).

`acervo.ficha.contenido_markdown` guarda la Ficha **entera y no partida**, para que mudarla a
Git o al Drive sea copiar el texto (decisión diferida D5). Este módulo es el único que sabe
cómo se arma y cómo se vuelve a leer ese texto, y vive dentro del Módulo de Fichas porque
nadie fuera de él manipula la Ficha como texto.

## Por qué no hay una biblioteca de YAML acá

El frontmatter del prototipo es un subconjunto deliberado: **una línea por campo, la lista de
campos es fija y los valores son escalares**. Un `null`, un booleano o una cadena entre
comillas dobles con los escapes de siempre, o una lista de esas cadenas en una línea (`carreras`,
estilo flujo: `["a", "b"]`); nada de anidamiento ni bloques. Con esa forma, escribirlo y volver
a leerlo son unas pocas funciones, y agregar una dependencia que nadie revisó al VPS de 8 GB
cuesta más que eso (`contratos.md` §10.3).

El texto que produce `serializar` es YAML válido, de modo que el día que el MVP necesite
anidamiento la lectura la puede tomar un analizador de verdad sin migrar lo guardado.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import fields
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from historia_viva.dominio.estados import Estado
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.tipos import CamposFicha

__all__ = ["analizar", "serializar"]

_DELIMITADOR = "---"

_CONVERSORES: Mapping[str, Callable[[str], Any]] = {
    "id": UUID,
    "tipo": str,
    "tipo_actividad": str,
    "fecha_inicio": date.fromisoformat,
    "fecha_fin": date.fromisoformat,
    "precision_fecha": PrecisionFecha,
    "origen": str,
    "propietario": str,
    "autor": str,
    "carreras": tuple,
    "lote": UUID,
    "resumen": str,
    "estado": Estado,
    "validada_por": UUID,
    "validada_el": datetime.fromisoformat,
    "modo_validacion": str,
    "archivo_original": str,
    "alerta_inyeccion": bool,
    "publicable": bool,
}
"""Los campos del frontmatter, en el orden de `spec.md` §4, con cómo se lee cada uno.

Es la misma lista que `CamposFicha` y el orden en que se escribe: un frontmatter con otro
campo, o sin uno de estos (salvo los de `_AGREGADOS`), no es una Ficha de este prototipo y
se rechaza al leerlo.
"""

_AGREGADOS = (
    frozenset({"tipo_actividad", "propietario"}),  # 023
    frozenset({"autor", "carreras"}),  # 024
    frozenset({"publicable"}),  # 123
)
"""Campos que una Ficha guardada antes de su ticket no trae: faltar el grupo entero no la vuelve
ilegible, quedan en su valor por omisión; faltar uno solo del grupo, sí."""

_ELEMENTO = re.compile(r'"(?:[^"\\]|\\.)*"')
"""Una cadena entrecomillada de una lista del frontmatter."""

if tuple(_CONVERSORES) != tuple(campo.name for campo in fields(CamposFicha)):
    # Un campo agregado a `CamposFicha` y olvidado acá dejaría de escribirse en la Ficha sin
    # que nada fallara. Falla al importar, que es cuando todavía es barato.
    raise RuntimeError(
        "Los campos de `CamposFicha` y los del frontmatter dejaron de coincidir."
    )


def serializar(campos: CamposFicha, cuerpo: str) -> str:
    """Arma el markdown de una Ficha: frontmatter delimitado y debajo el cuerpo."""
    lineas = "\n".join(
        f"{nombre}: {_escribir(getattr(campos, nombre))}" for nombre in _CONVERSORES
    )
    return f"{_DELIMITADOR}\n{lineas}\n{_DELIMITADOR}\n\n{cuerpo}"


def analizar(markdown: str) -> tuple[CamposFicha, str]:
    """Devuelve los campos y el cuerpo de un markdown escrito por `serializar`.

    Es lo que permite leer la **versión 1** —la que propuso el modelo— tal como quedó
    escrita, y no como quedó la Pieza después de que alguien la editara (user story 23).

    Raises:
        ValueError: Si el texto no tiene la forma que produce `serializar`.
    """
    if not markdown.startswith(f"{_DELIMITADOR}\n"):
        raise ValueError("La Ficha no empieza con el frontmatter delimitado por '---'.")
    resto = markdown[len(_DELIMITADOR) + 1 :]
    frente, separador, cuerpo = resto.partition(f"\n{_DELIMITADOR}\n")
    if not separador:
        raise ValueError("El frontmatter de la Ficha no está cerrado por '---'.")

    valores: dict[str, Any] = {}
    for linea in frente.split("\n"):
        nombre, _, crudo = linea.partition(": ")
        if nombre not in _CONVERSORES:
            raise ValueError(f"El frontmatter trae un campo desconocido: {nombre!r}.")
        valores[nombre] = _leer(nombre, crudo)
    # Una Ficha anterior a un grupo no trae ninguno de sus campos; una posterior, todos.
    ausentes = frozenset().union(*(g for g in _AGREGADOS if g.isdisjoint(valores)))
    faltantes = [
        nombre for nombre in _CONVERSORES if nombre not in valores and nombre not in ausentes
    ]
    if faltantes:
        raise ValueError(f"Al frontmatter le faltan campos: {', '.join(faltantes)}.")

    return CamposFicha(**valores), cuerpo.removeprefix("\n")


def _escribir(valor: object) -> str:
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "true" if valor else "false"
    if isinstance(valor, Enum):
        return _entrecomillar(str(valor.value))
    if isinstance(valor, (date, datetime)):
        return _entrecomillar(valor.isoformat())
    if isinstance(valor, tuple):
        return f"[{', '.join(_entrecomillar(str(elemento)) for elemento in valor)}]"
    return _entrecomillar(str(valor))


def _leer(nombre: str, crudo: str) -> Any:
    if crudo == "null":
        return None
    conversor = _CONVERSORES[nombre]
    if conversor is bool:
        return crudo == "true"
    if conversor is tuple:
        return _leer_lista(nombre, crudo)
    return conversor(_desentrecomillar(nombre, crudo))


def _leer_lista(nombre: str, crudo: str) -> tuple[str, ...]:
    interior = crudo[1:-1] if crudo.startswith("[") and crudo.endswith("]") else None
    elementos = _ELEMENTO.findall(interior or "")
    if interior is None or ", ".join(elementos) != interior:
        raise ValueError(f"El campo {nombre!r} del frontmatter no es una lista entrecomillada.")
    return tuple(_desentrecomillar(nombre, elemento) for elemento in elementos)


def _entrecomillar(texto: str) -> str:
    escapado = texto.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escapado}"'


def _desentrecomillar(nombre: str, crudo: str) -> str:
    if len(crudo) < 2 or not crudo.startswith('"') or not crudo.endswith('"'):
        raise ValueError(f"El campo {nombre!r} del frontmatter no está entrecomillado.")
    texto, escape = "", False
    for caracter in crudo[1:-1]:
        if escape:
            texto += {"n": "\n", '"': '"', "\\": "\\"}.get(caracter, caracter)
            escape = False
        elif caracter == "\\":
            escape = True
        else:
            texto += caracter
    return texto
