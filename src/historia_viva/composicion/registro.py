"""Los logs de la app: una línea JSON por evento en stdout (spec §2.4 y §2.7, RNF-19).

Lista blanca de campos: nunca el texto de una Ficha, una consulta ni un mensaje. De una excepción
queda la clase y dónde saltó, no su mensaje, que puede citar lo que se estaba procesando. Del
pedido HTTP queda la ruta sin la consulta (`?q=` lleva la pregunta). La rotación la hace Docker
(`logging:` del compose).
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime

__all__ = ["RegistroJson", "configurar_registro"]

CAMPOS = ("solicitud_id", "trabajo_id", "pieza_id", "capacidad", "modelo", "duracion_ms",
          "principal_id")  # fmt: skip
"""Los que viajan si el llamador los pasa en `extra=`; cualquier otro se descarta."""


class RegistroJson(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            _, metodo, ruta, _, estado = record.args
            evento = f"{metodo} {str(ruta).partition('?')[0]} {estado}"
        else:
            evento = record.getMessage()
        linea: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "nivel": record.levelname,
            "origen": record.name,
            "evento": evento,
            **{campo: getattr(record, campo) for campo in CAMPOS if hasattr(record, campo)},
        }
        if record.exc_info and record.exc_info[0]:
            tipo, _, traza = record.exc_info
            linea["excepcion"] = tipo.__name__
            linea["traza"] = [
                f"{marco.filename}:{marco.lineno} {marco.name}"
                for marco in traceback.extract_tb(traza)
            ]
        return json.dumps(linea, ensure_ascii=False, default=str)


def configurar_registro() -> None:
    """JSON a stdout desde INFO. No hace nada si el proceso ya configuró el registro (pytest)."""
    salida = logging.StreamHandler(sys.stdout)
    salida.setFormatter(RegistroJson())
    logging.basicConfig(level=logging.INFO, handlers=[salida])
    # Una línea por pedido a Ollama, cada pocos segundos: ruido sin información.
    logging.getLogger("httpx").setLevel(logging.WARNING)
