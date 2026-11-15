"""Las Citas `[n]` de un texto redactado, contrastadas con los fragmentos recuperados (R-14).

Pura: la usan `conversacion` y `efemerides`. No reescribe el texto: en la Conversación los
tokens ya se emitieron cuando se verifica, y una Cita que no se verifica se marca, se tacha y se
avisa (R-15).
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

__all__ = ["CitaDelTexto", "quitar_citas", "verificar_citas"]

_MARCA = re.compile(r"\[(\d{1,3})\]", re.ASCII)
"""`[n]` con hasta tres dígitos: «[1975]» es un año entre corchetes, no una Cita. Solo dígitos
ASCII, como el `\\d` de la pantalla: «[١]» no es una Cita en ningún lado."""


@dataclass(frozen=True)
class CitaDelTexto:
    marca: int
    """El `n` de `[n]`: el fragmento `n` en el orden en que se le dieron al modelo."""
    verificada: bool


def verificar_citas(texto: str, n_fragmentos: int) -> tuple[CitaDelTexto, ...]:
    """Las `[n]` de `texto` en el orden en que aparecen, sin repetir; verificada si `n` es uno de
    los `n_fragmentos` recuperados (de 1 a `n_fragmentos`)."""
    marcas = dict.fromkeys(int(n) for n in _MARCA.findall(texto))
    return tuple(CitaDelTexto(n, verificada=1 <= n <= n_fragmentos) for n in marcas)


def quitar_citas(texto: str, marcas: Collection[int]) -> str:
    """`texto` sin las `[n]` de `marcas` ni el espacio que las precede. Lo usa `efemerides`,
    que guarda la Propuesta entera antes de que alguien la lea; la Conversación no reescribe."""
    return re.sub(
        r"[ \t]*" + _MARCA.pattern,
        lambda marca: "" if int(marca[1]) in marcas else marca[0],
        texto,
        flags=re.ASCII,
    )
