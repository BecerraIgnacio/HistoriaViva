"""El aviso de DNI, CUIL o CBU al validar (historia 243, RF-42; auditoría, decisión 16 B).

Una función pura: no es inyección (no la mira `chequeo_inyeccion`), no se guarda y no bloquea.
La consulta `validacion`, que la apaga hasta que C31 la encienda. Prefiere callar a avisar de
más: un CUIL o un CBU cuentan solo si su dígito verificador da, y un DNI solo si lo anuncia la
palabra (`DNI`, `D.N.I.`, `documento`) o si está escrito con sus puntos (`12.345.678`) y no es
un importe. Un número suelto de ocho cifras no avisa: también es una fecha o un código.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["DatoPersonal", "Hallazgo", "detectar_datos_personales"]


class DatoPersonal(StrEnum):
    DNI = "dni"
    CUIL = "cuil"
    CBU = "cbu"


@dataclass(frozen=True)
class Hallazgo:
    """Dónde está, no qué dice: el número no sale del texto que ya tiene la pantalla."""

    tipo: DatoPersonal
    inicio: int
    fin: int


# Sin `\b`: un dígito pegado a una letra (un hash, un UUID) no es un número aparte.
_ANTES = r"(?<![\w.,/-])"
_DESPUES = r"(?![\w]|[.,/-]\d)"

_CUIL = re.compile(
    rf"{_ANTES}(?P<prefijo>2[0347]|3[034])[-\s.]?(?P<dni>\d{{2}}\.?\d{{3}}\.?\d{{3}})"
    rf"[-\s.]?(?P<verificador>\d){_DESPUES}"
)
_CBU = re.compile(rf"{_ANTES}(?P<cbu>\d{{8}}[-\s]?\d{{14}}){_DESPUES}")
_DNI_ANUNCIADO = re.compile(
    r"(?<!\w)(?:D\.?\s?N\.?\s?I\.?|documento(?:\s+nacional\s+de\s+identidad)?)"
    r"(?:\s*(?:N[°ºo.]+|nro\.?|n[úu]mero))?\s*[:#]?\s*"
    rf"(?<!\d)(?P<dni>\d{{1,2}}\.?\d{{3}}\.?\d{{3}}){_DESPUES}",
    re.I,
)
_DNI_CON_PUNTOS = re.compile(rf"{_ANTES}(?P<dni>\d{{1,2}}\.\d{{3}}\.\d{{3}}){_DESPUES}")
_IMPORTE = re.compile(r"(\$|ARS|U\$S|USD|pesos)\s*$", re.I)

_PESOS_CUIL = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
_CICLO_CBU = (3, 1, 7, 9)
"""Los pesos del CBU, de derecha a izquierda: los dos bloques terminan en 3."""


def _digitos(texto: str) -> list[int]:
    return [int(c) for c in texto if c.isdigit()]


def _cuil_valido(digitos: list[int]) -> bool:
    resto = 11 - sum(d * p for d, p in zip(digitos[:10], _PESOS_CUIL, strict=True)) % 11
    # 11 da 0; 10 no es un verificador posible (por eso existen los prefijos 23 y 33).
    return {11: 0, 10: None}.get(resto, resto) == digitos[10]


def _verificador_cbu(digitos: list[int]) -> int:
    suma = sum(d * _CICLO_CBU[i % 4] for i, d in enumerate(reversed(digitos)))
    return (10 - suma % 10) % 10


def _cbu_valido(digitos: list[int]) -> bool:
    """El bloque del banco y la sucursal (8) y el de la cuenta (14), cada uno con su dígito."""
    return _verificador_cbu(digitos[:7]) == digitos[7] and _verificador_cbu(
        digitos[8:21]
    ) == digitos[21]


def detectar_datos_personales(texto: str) -> tuple[Hallazgo, ...]:
    """Los DNI, CUIL y CBU de `texto`, en orden de aparición. Un DNI dentro de un CUIL cuenta
    una vez, como CUIL."""
    hallazgos: list[Hallazgo] = []
    ocupado: list[tuple[int, int]] = []

    def libre(inicio: int, fin: int) -> bool:
        return all(fin <= a or inicio >= b for a, b in ocupado)

    def anotar(tipo: DatoPersonal, inicio: int, fin: int) -> None:
        if libre(inicio, fin):
            hallazgos.append(Hallazgo(tipo, inicio, fin))
            ocupado.append((inicio, fin))

    for hallado in _CBU.finditer(texto):
        if _cbu_valido(_digitos(hallado["cbu"])):
            anotar(DatoPersonal.CBU, *hallado.span())
    for hallado in _CUIL.finditer(texto):
        if _cuil_valido(_digitos(hallado.group(0))):
            anotar(DatoPersonal.CUIL, *hallado.span())
    for hallado in _DNI_ANUNCIADO.finditer(texto):
        anotar(DatoPersonal.DNI, *hallado.span("dni"))
    for hallado in _DNI_CON_PUNTOS.finditer(texto):
        if not _IMPORTE.search(texto[max(0, hallado.start() - 8) : hallado.start()]):
            anotar(DatoPersonal.DNI, *hallado.span())
    return tuple(sorted(hallazgos, key=lambda h: h.inicio))
