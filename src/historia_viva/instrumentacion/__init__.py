"""Instrumentacion: convierte cada validacion en una medicion.

Las filas que produce son lo que se lleva a la documentacion (user stories 22 a 25).

**No toca la Ficha.** Una medicion se guarda aparte y nunca escribe en el frontmatter: si la
medicion ensuciara la Ficha, el dato del acervo quedaria contaminado por el instrumento que
lo miraba.
"""

from __future__ import annotations

from historia_viva.instrumentacion.registro import (
    COLUMNAS,
    Apertura,
    Cierre,
    Medicion,
    RegistroMediciones,
    medir_cierre,
)

__all__ = ["COLUMNAS", "Apertura", "Cierre", "Medicion", "RegistroMediciones", "medir_cierre"]
