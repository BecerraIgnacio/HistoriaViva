"""Cliente HTTP del motor local (Ollama). Nadie fuera de `capa_modelos/` lo importa (RNF-01).

Recibe la plantilla por identificador y los datos ya delimitados por `entradas`, y aplica los
topes de ADR-0004 en la petición misma (`arquitectura.md` §5.2):

- **El tope de tiempo es de la operación completa**, no de cada lectura. Un `timeout` de
  `httpx` acota cada fase por separado —conectar, escribir, cada lectura—, así que un motor
  que gotea tokens nunca lo dispararía. Por eso la respuesta se pide en *streaming* y se mira
  el reloj después de cada fragmento: al vencer el plazo se corta la conexión —Ollama cancela
  la generación al perderla— y se levanta `LimiteExcedido`. Antes del primer fragmento, lo
  que acota es el `timeout` de lectura, fijado en el mismo plazo.
- **El tope de tokens de salida es `num_predict`.** Una respuesta que termina por haberlo
  alcanzado (`done_reason: "length"`) está cortada a la mitad y también levanta
  `LimiteExcedido`, en vez de guardarse como si estuviera completa.

`vectorizar` llama a `POST /api/embed` con `truncate: false`: Ollama recortaría el final de
un texto largo sin avisar, y el vector sería el de otro texto.
"""

from __future__ import annotations

import base64
import json
import math
import time
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from historia_viva.capa_modelos import entradas
from historia_viva.capa_modelos.configuracion import ConfiguracionDeCapacidad
from historia_viva.capa_modelos.contrato import Capacidad, Limites, Plantilla
from historia_viva.dominio.errores import (
    LimiteExcedido,
    MotorInterrumpido,
    MotorNoAlcanzable,
    MotorNoDisponible,
)

__all__ = ["MotorOllama", "RespuestaMotor"]

_TIEMPO_DE_CONEXION_S = 5.0
"""Conectar a un motor en la misma máquina es instantáneo o no ocurre: no hace falta darle
el tope entero de la capacidad para enterarse de que está apagado."""

_TIEMPO_DE_SALUD_S = 3.0
"""`verificar_salud` corre en cada `GET /api/salud` y en el cortacircuito del trabajador: no
puede tardar lo que una capacidad (DEC-86; tabla de DEC-145)."""

_SIN_AVANCE_AL_BAJAR_S = 300.0
"""Cuánto puede pasar una descarga sin informar avance antes de darla por trabada. Holgado:
verificar el `sha256` de un modelo de gigas en un disco lento no manda nada."""


@dataclass(frozen=True)
class RespuestaMotor:
    texto: str
    tokens_entrada: int
    tokens_salida: int
    duracion_ms: int


class MotorOllama:
    """Habla con `POST /api/chat`, `POST /api/embed`, `GET /api/tags` y `POST /api/pull`."""

    def __init__(self, *, url: str, cliente: httpx.Client | None = None) -> None:
        self.url = url.rstrip("/")
        self._cliente = cliente or httpx.Client()

    def conversar(
        self,
        *,
        capacidad: Capacidad,
        plantilla: Plantilla,
        config: ConfiguracionDeCapacidad,
        datos: str,
        limites: Limites,
        imagenes: tuple[bytes, ...] = (),
        formato: Mapping[str, object] | None = None,
    ) -> RespuestaMotor:
        """Una vuelta de chat: sistema = el texto constante de `plantilla`, usuario = `datos`.

        El modelo y sus opciones (`temperatura`, `num_ctx`, `num_thread`) salen de `config`.
        `formato` es el esquema JSON de la respuesta (`format` de Ollama, 144), constante como
        la instrucción.

        Recibe el identificador y no el texto de la instrucción: ni siquiera dentro de la capa
        existe un parámetro por donde pase una instrucción armada (ADR-0003, capa 1).

        Raises:
            InstruccionContaminada: Si `plantilla` no es una plantilla de `capacidad`.
            LimiteExcedido: Si la operación supera `limites.tiempo_maximo_s` o el motor corta
                la salida en `limites.tope_tokens_salida`.
            MotorNoAlcanzable: Si el pedido no llegó al motor (conexión rechazada o sin
                conexión a tiempo): no trabajó y el trabajo no gasta la generación (DEC-86).
            MotorInterrumpido: Si el motor contesta con un error o con algo que no es una
                respuesta de Ollama.
        """
        *_, respuesta = self.conversar_en_vivo(
            capacidad=capacidad,
            plantilla=plantilla,
            config=config,
            datos=datos,
            limites=limites,
            imagenes=imagenes,
            formato=formato,
        )
        assert isinstance(respuesta, RespuestaMotor)
        return respuesta

    def conversar_en_vivo(
        self,
        *,
        capacidad: Capacidad,
        plantilla: Plantilla,
        config: ConfiguracionDeCapacidad,
        datos: str,
        limites: Limites,
        imagenes: tuple[bytes, ...] = (),
        formato: Mapping[str, object] | None = None,
    ) -> Generator[str | RespuestaMotor]:
        """`conversar` a medida que llega: cada fragmento no vacío del motor y, al final, la
        `RespuestaMotor` completa. Los mismos errores, después de lo ya entregado; un corte a
        mitad, o la línea `{"error": …}` con que Ollama aborta, es `MotorInterrumpido`. Si quien
        lo consume lo cierra antes, se corta la conexión y el motor deja de generar."""
        peticion = _peticion(
            instruccion=entradas.instruccion(capacidad, plantilla),
            config=config,
            datos=datos,
            limites=limites,
            imagenes=imagenes,
            formato=formato,
        )
        tope_s = limites.tiempo_maximo_s
        comenzado_en = time.monotonic()
        fragmentos: list[str] = []
        final: dict[str, Any] = {}
        with _errores_del_motor((capacidad, tope_s)), self._cliente.stream(
            "POST",
            f"{self.url}/api/chat",
            json=peticion,
            timeout=httpx.Timeout(tope_s, connect=min(_TIEMPO_DE_CONEXION_S, tope_s)),
        ) as respuesta:
            if respuesta.is_error:
                respuesta.read()
                raise MotorInterrumpido(
                    f"HTTP {respuesta.status_code}: {_mensaje_de_error(respuesta)}"
                )
            for linea in respuesta.iter_lines():
                transcurrido_s = time.monotonic() - comenzado_en
                if transcurrido_s > tope_s:
                    # Salir del `with` cierra la conexión: el motor deja de generar.
                    raise LimiteExcedido(
                        capacidad, "tiempo", tope_s, math.ceil(transcurrido_s)
                    )
                if not linea.strip():
                    continue
                fragmento = json.loads(linea)
                if "error" in fragmento:
                    motivo = fragmento["error"]
                    raise MotorInterrumpido(f"el motor cortó la respuesta: {motivo}")
                if contenido := fragmento.get("message", {}).get("content", ""):
                    fragmentos.append(contenido)
                    yield contenido
                if fragmento.get("done"):
                    final = fragmento
                    break
        if not final:
            raise MotorInterrumpido("la respuesta terminó sin el fragmento final.")
        tokens_salida = int(final.get("eval_count") or 0)
        if final.get("done_reason") == "length" and limites.tope_tokens_salida is not None:
            raise LimiteExcedido(
                capacidad, "tokens de salida", limites.tope_tokens_salida, tokens_salida
            )
        yield RespuestaMotor(
            texto="".join(fragmentos).strip(),
            tokens_entrada=int(final.get("prompt_eval_count") or 0),
            tokens_salida=tokens_salida,
            duracion_ms=int((time.monotonic() - comenzado_en) * 1000),
        )

    def vectorizar(
        self, *, config: ConfiguracionDeCapacidad, textos: Sequence[str]
    ) -> list[tuple[float, ...]]:
        """Un lote de `textos` a `/api/embed`, con el tope de tiempo de la fila.

        La respuesta no es un *stream*: el motor no manda nada hasta terminar, así que el
        `timeout` de lectura acota la operación entera.

        Raises:
            LimiteExcedido: Si el lote supera el `timeout_s` de la fila.
            MotorNoAlcanzable: Si el pedido no llegó al motor: el trabajo vuelve a la cola (030).
            MotorInterrumpido: Si el motor contesta con un error (un texto más largo que el
                contexto, sin truncar, es uno), con algo que no es de Ollama o sin un vector
                por texto.
        """
        tope_s = config.parametros["timeout_s"]
        opciones = {k: v for k, v in config.parametros.items() if k in ("num_ctx", "num_thread")}
        with _errores_del_motor((Capacidad.EMBEBER, tope_s)):
            respuesta = self._cliente.post(
                f"{self.url}/api/embed",
                json={
                    "model": config.modelo,
                    "input": list(textos),
                    "truncate": False,
                    "options": opciones,
                },
                timeout=httpx.Timeout(tope_s, connect=min(_TIEMPO_DE_CONEXION_S, tope_s)),
            )
            if respuesta.is_error:
                raise MotorInterrumpido(
                    f"HTTP {respuesta.status_code}: {_mensaje_de_error(respuesta)}"
                )
            vectores = [tuple(map(float, v)) for v in respuesta.json()["embeddings"]]
        if len(vectores) != len(textos):
            raise MotorInterrumpido(f"{len(vectores)} vectores para {len(textos)} textos.")
        return vectores

    def digest(self, modelo: str) -> str:
        """El digest de `modelo` en `/api/tags`: cambia con un `pull` nuevo del mismo tag.

        Raises:
            MotorNoAlcanzable: Si el pedido no llegó al motor.
            MotorInterrumpido: Si el motor contesta con un error, con algo que no es de Ollama
                o sin el digest de `modelo`: sin él no hay `modelo@digest12`.
            MotorNoDisponible: Si `modelo` no está descargado.
        """
        nombre = _nombre_completo(modelo)
        descargados = self._descargados()
        if nombre not in descargados:
            raise MotorNoDisponible(f"{modelo} no está descargado en {self.url}.")
        if not (digest := descargados[nombre]):
            raise MotorInterrumpido(f"{self.url} no informa el digest de {modelo}.")
        return digest

    def faltantes(self, modelos: Iterable[str]) -> frozenset[str] | None:
        """Cuáles de `modelos` no están descargados; `None` si el motor no contesta.

        Un modelo que falta avisa y no impide arrancar (historia 165): el motor está.
        """
        try:
            descargados = self._descargados()
        except MotorNoDisponible:
            return None
        return frozenset(m for m in modelos if _nombre_completo(m) not in descargados)

    def _descargados(self) -> dict[str, str]:
        """Nombre completo → digest de cada modelo descargado; `""` si `/api/tags` no lo trae.

        Raises:
            MotorNoAlcanzable: Si el pedido no llegó al motor.
            MotorInterrumpido: Si el motor contesta con un error o con algo que no es de Ollama.
        """
        with _errores_del_motor():
            respuesta = self._cliente.get(
                f"{self.url}/api/tags", timeout=httpx.Timeout(_TIEMPO_DE_SALUD_S)
            )
            if respuesta.is_error:
                raise MotorInterrumpido(
                    f"HTTP {respuesta.status_code}: {_mensaje_de_error(respuesta)}"
                )
            descargados = {
                _nombre_completo(modelo["name"]): str(modelo.get("digest") or "")
                for modelo in respuesta.json()["models"]
            }
        return descargados

    def sacar_de_memoria(self) -> list[str]:
        """Saca de memoria los modelos cargados (`/api/ps`) con `keep_alive: 0`, como
        `ollama stop`: la condición en frío de M3 (DEC-90). Devuelve los que sacó.

        Raises:
            MotorNoAlcanzable: Si el pedido no llegó al motor.
            MotorInterrumpido: Si el motor contesta con un error o con algo que no es de Ollama.
        """
        with _errores_del_motor():
            tiempo = httpx.Timeout(_TIEMPO_DE_SALUD_S)
            respuesta = _sin_error(self._cliente.get(f"{self.url}/api/ps", timeout=tiempo))
            cargados = [str(m["name"]) for m in respuesta.json()["models"]]
            for modelo in cargados:
                _sin_error(self._cliente.post(
                    f"{self.url}/api/generate", json={"model": modelo, "keep_alive": 0},
                    timeout=tiempo,
                ))
        return cargados

    def bajar(self, modelo: str) -> None:
        """`ollama pull` de `modelo`. Sin tope total —un modelo pesa gigas—, pero una descarga
        que pasa `_SIN_AVANCE_AL_BAJAR_S` sin avanzar se corta: no cuelga al servicio `modelos`.

        Avanzar es que cambie el estado o los bytes bajados. Ollama repite la última línea de
        progreso varias veces por segundo aunque no baje nada: el `timeout` de lectura solo ve
        un motor que se calla.

        Raises:
            MotorNoDisponible: Si el motor no está, no pudo bajarlo o la descarga se trabó.
        """
        trabada = f"la descarga de {modelo} no avanzó en {_SIN_AVANCE_AL_BAJAR_S:.0f} s."
        ultimo: dict[str, Any] = {}
        avance: tuple[object, object] | None = None
        avanzo_en = time.monotonic()
        try:
            with self._cliente.stream(
                "POST",
                f"{self.url}/api/pull",
                json={"model": modelo, "stream": True},
                timeout=httpx.Timeout(_SIN_AVANCE_AL_BAJAR_S, connect=_TIEMPO_DE_CONEXION_S),
            ) as respuesta:
                if respuesta.is_error:
                    respuesta.read()
                    raise MotorNoDisponible(
                        f"HTTP {respuesta.status_code}: {_mensaje_de_error(respuesta)}"
                    )
                # Un JSON de avance por línea; un error llega como la última, con HTTP 200.
                for linea in respuesta.iter_lines():
                    if not linea.strip():
                        continue
                    ultimo = json.loads(linea)
                    actual = (ultimo.get("status"), ultimo.get("completed"))
                    if actual != avance:
                        avance, avanzo_en = actual, time.monotonic()
                    elif time.monotonic() - avanzo_en > _SIN_AVANCE_AL_BAJAR_S:
                        raise MotorNoDisponible(trabada)  # salir del `with` corta la descarga
                if ultimo.get("status") != "success":
                    motivo = ultimo.get("error", "la descarga se cortó")
                    raise MotorNoDisponible(f"no se pudo bajar {modelo}: {motivo}.")
        except httpx.TimeoutException as error:
            raise MotorNoDisponible(trabada) from error
        except httpx.HTTPError as error:
            raise MotorNoDisponible(f"{type(error).__name__}: {error}") from error
        except (ValueError, AttributeError) as error:
            raise MotorNoDisponible(f"respuesta que no es de Ollama: {error}") from error


@contextmanager
def _errores_del_motor(limite: tuple[Capacidad, int] | None = None) -> Iterator[None]:
    """Lo que falla al hablar con Ollama, como error del dominio (DEC-86, DEC-113).

    `limite` es la capacidad y su tope en segundos: con él, un timeout de lectura es
    `LimiteExcedido`; sin él (`/api/tags`), es `MotorInterrumpido`.
    """
    comenzado_en = time.monotonic()
    try:
        yield
    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
        raise MotorNoAlcanzable(f"{type(error).__name__}: {error}") from error
    except httpx.TimeoutException as error:
        if limite is None:
            raise MotorInterrumpido(f"{type(error).__name__}: {error}") from error
        capacidad, tope_s = limite
        transcurrido_s = math.ceil(time.monotonic() - comenzado_en)
        raise LimiteExcedido(capacidad, "tiempo", tope_s, max(tope_s, transcurrido_s)) from error
    except httpx.HTTPError as error:
        raise MotorInterrumpido(f"{type(error).__name__}: {error}") from error
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise MotorInterrumpido(f"respuesta que no es de Ollama: {error}") from error


def _peticion(
    *,
    instruccion: str,
    config: ConfiguracionDeCapacidad,
    datos: str,
    limites: Limites,
    imagenes: tuple[bytes, ...],
    formato: Mapping[str, object] | None,
) -> dict[str, object]:
    usuario: dict[str, object] = {"role": "user", "content": datos}
    if imagenes:
        usuario["images"] = [base64.b64encode(imagen).decode("ascii") for imagen in imagenes]
    opciones: dict[str, object] = {
        "temperature": config.parametros["temperatura"],
        "num_ctx": config.parametros["num_ctx"],
    }
    if "num_thread" in config.parametros:
        opciones["num_thread"] = config.parametros["num_thread"]
    if limites.tope_tokens_salida is not None:
        opciones["num_predict"] = limites.tope_tokens_salida
    peticion: dict[str, object] = {
        "model": config.modelo,
        "messages": [{"role": "system", "content": instruccion}, usuario],
        "stream": True,
        "options": opciones,
    }
    if formato is not None:
        peticion["format"] = dict(formato)
    return peticion


def _sin_error(respuesta: httpx.Response) -> httpx.Response:
    if respuesta.is_error:
        raise MotorInterrumpido(f"HTTP {respuesta.status_code}: {_mensaje_de_error(respuesta)}")
    return respuesta


def _nombre_completo(modelo: str) -> str:
    """`modelo` y `modelo:latest` son el mismo modelo para Ollama."""
    return modelo if ":" in modelo else f"{modelo}:latest"


def _mensaje_de_error(respuesta: httpx.Response) -> str:
    try:
        return str(respuesta.json().get("error", respuesta.text))
    except ValueError:
        return respuesta.text
