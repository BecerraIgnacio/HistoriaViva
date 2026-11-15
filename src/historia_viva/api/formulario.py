"""El campo `archivo` de un `multipart/form-data`, entregado a medida que llega (DEC-47).

`UploadFile` no sirve: Starlette junta el formulario entero en un temporal propio antes de
llamar al endpoint, y entonces ni el espacio libre se mira antes de aceptar bytes ni el tope
corta mientras llegan. Acá el cuerpo se pasa por el parser de `python-multipart` pedazo a
pedazo, y `originales` lee el archivo como un flujo común.

El endpoint es sincrónico (corre en un hilo de anyio): cada pedazo del cuerpo se trae del
bucle de eventos con `anyio.from_thread.run`. Una subida en curso ocupa ese hilo mientras
dure: el tiempo máximo de lectura del cuerpo lo pone Caddy con `request_body` (spec,
despliegue, «Cuerpo»), no esta capa.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Buffer
from typing import BinaryIO

import anyio.from_thread
from fastapi import Request
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from historia_viva.api.problemas import Problema
from historia_viva.originales import BLOQUE_BYTES

__all__ = ["archivo_del_formulario", "cuerpo_crudo"]

CAMPO = "archivo"

CAMPOS_DE_TEXTO = frozenset({"lote_id"})
"""Los otros campos que se leen. Van antes que `archivo`: después de él ya se guardó el
original, y quien lo recibe los necesita antes."""

_TOPE_CAMPO_DE_TEXTO = 256


def archivo_del_formulario(request: Request) -> tuple[str, BinaryIO, dict[str, str]]:
    """Lee el cuerpo hasta los encabezados del campo `archivo` y devuelve su nombre, su flujo y
    los `CAMPOS_DE_TEXTO` que llegaron antes.

    Raises:
        Problema: 422 `pedido-invalido` si no es un multipart válido o no trae el campo; al
            terminar de leer el flujo, si un campo de texto vino después del archivo.
    """
    parte = _ParteArchivo(request)
    while parte.nombre is None:
        if not parte.recibir():
            raise _pedido_invalido(f"Falta el campo {CAMPO}.")
    return parte.nombre, io.BufferedReader(parte, BLOQUE_BYTES), parte.campos


def _pedido_invalido(detalle: str, campo: str = CAMPO) -> Problema:
    return Problema(
        tipo="/errores/pedido-invalido",
        titulo="El pedido es inválido",
        estado=422,
        detalle=detalle,
        campo=campo,
    )


async def _siguiente(cuerpo: AsyncIterator[bytes]) -> bytes | None:
    return await anext(cuerpo, None)


def cuerpo_crudo(request: Request) -> BinaryIO:
    """El cuerpo binario crudo del pedido como flujo, a medida que llega (`/api/piezas/video`)."""
    return io.BufferedReader(_CuerpoCrudo(request), BLOQUE_BYTES)


class _CuerpoCrudo(io.RawIOBase):
    def __init__(self, request: Request) -> None:
        self._cuerpo = request.stream()
        self._pendiente = b""

    def readable(self) -> bool:
        return True

    def readinto(self, destino: Buffer, /) -> int:
        while not self._pendiente:
            pedazo = anyio.from_thread.run(_siguiente, self._cuerpo)
            if pedazo is None:
                return 0
            self._pendiente = pedazo
        with memoryview(destino) as vista:
            cantidad = min(len(vista), len(self._pendiente))
            vista[:cantidad] = self._pendiente[:cantidad]
        self._pendiente = self._pendiente[cantidad:]
        return cantidad


class _ParteArchivo(io.RawIOBase):
    def __init__(self, request: Request) -> None:
        tipo, opciones = parse_options_header(request.headers.get("content-type"))
        frontera = opciones.get(b"boundary")
        if tipo != b"multipart/form-data" or not frontera:
            raise _pedido_invalido("El archivo tiene que llegar como multipart/form-data.")
        self.nombre: str | None = None
        self.campos: dict[str, str] = {}
        self._campo: str | None = None
        self._valor_campo = bytearray()
        self._cuerpo = request.stream()
        self._pendiente = bytearray()
        self._en_archivo = False
        self._completo = False
        self._encabezado = b""
        self._valor = b""
        self._disposicion = b""
        self._parser = MultipartParser(
            frontera,
            {
                "on_part_begin": self._empieza_parte,
                "on_header_field": self._encabezado_de,
                "on_header_value": self._valor_de,
                "on_header_end": self._termina_encabezado,
                "on_headers_finished": self._terminan_encabezados,
                "on_part_data": self._datos,
                "on_part_end": self._termina_parte,
            },
        )

    def recibir(self) -> bool:
        """Pasa el próximo pedazo del cuerpo por el parser; `False` si el cuerpo terminó."""
        pedazo = anyio.from_thread.run(_siguiente, self._cuerpo)
        if pedazo is None:
            return False
        try:
            self._parser.write(pedazo)
        except MultipartParseError as error:
            # Lo que ya se escribió del archivo lo borra `originales` al ver pasar el error.
            raise _pedido_invalido("El cuerpo no es un multipart/form-data válido.") from error
        return True

    def readable(self) -> bool:
        return True

    def readinto(self, destino: Buffer, /) -> int:
        while not self._pendiente and not self._completo:
            if not self.recibir():
                raise _pedido_invalido("El archivo llegó cortado.")
        if not self._pendiente:
            self._despues_del_archivo()
        with memoryview(destino) as vista:
            cantidad = min(len(vista), len(self._pendiente))
            vista[:cantidad] = self._pendiente[:cantidad]
        del self._pendiente[:cantidad]
        return cantidad

    def _despues_del_archivo(self) -> None:
        """Lee el resto del cuerpo: un campo de texto después del archivo no se ignora en
        silencio (una Pieza de Lote quedaría suelta)."""
        while self.recibir():
            pass
        if self._campo is not None:
            raise _pedido_invalido(
                f"El campo {self._campo} tiene que ir antes que el archivo.", self._campo
            )

    def _empieza_parte(self) -> None:
        self._disposicion = b""

    def _encabezado_de(self, datos: bytes, inicio: int, fin: int) -> None:
        self._encabezado += datos[inicio:fin]

    def _valor_de(self, datos: bytes, inicio: int, fin: int) -> None:
        self._valor += datos[inicio:fin]

    def _termina_encabezado(self) -> None:
        if self._encabezado.lower() == b"content-disposition":
            self._disposicion = self._valor
        self._encabezado = self._valor = b""

    def _terminan_encabezados(self) -> None:
        _, opciones = parse_options_header(self._disposicion)
        nombre = opciones.get(b"filename")
        # `filename=""` es el `<input type="file">` sin archivo elegido: falta el campo.
        campo = opciones.get(b"name", b"").decode("latin-1")
        if self.nombre is None and campo == CAMPO and nombre:
            # Los navegadores mandan el nombre en UTF-8 crudo; si no decodifica, Latin-1 (como
            # Starlette).
            try:
                self.nombre = nombre.decode("utf-8")
            except UnicodeDecodeError:
                self.nombre = nombre.decode("latin-1")
            self._en_archivo = True
        elif campo in CAMPOS_DE_TEXTO:
            self._campo = campo

    def _datos(self, datos: bytes, inicio: int, fin: int) -> None:
        if self._en_archivo:
            self._pendiente += datos[inicio:fin]
        elif self._campo is not None and not self._completo:
            self._valor_campo += datos[inicio:fin]
            if len(self._valor_campo) > _TOPE_CAMPO_DE_TEXTO:
                raise _pedido_invalido(f"El campo {self._campo} es demasiado largo.", self._campo)

    def _termina_parte(self) -> None:
        if self._en_archivo:
            self._en_archivo = False
            self._completo = True
        elif self._campo is not None and not self._completo:
            self.campos[self._campo] = self._valor_campo.decode("utf-8", "replace")
            self._campo = None
            self._valor_campo.clear()
