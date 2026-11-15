"""Con qué motor, modelo, instrucción y parámetros corre cada capacidad (RNF-01, DEC-87).

Es el valor de una fila de `configuracion_capacidad`. La fila la lee el módulo `configuracion`
(el único que conoce la tabla); la capa la recibe ya leída en `capa_para`. Ningún nombre de
modelo vive en el código: están en la semilla de la tabla.

`validar` revisa el conjunto entero y devuelve todos los errores juntos. Lo llaman `capa_para`
(no hay capa construida con una fila inválida) y `configuracion` al tomar la instantánea (no hay
trabajo encolado con una fila inválida).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from historia_viva.capa_modelos import entradas
from historia_viva.capa_modelos.contrato import CapaModelos, Capacidad, Limites, Plantilla
from historia_viva.dominio.errores import (
    ConfiguracionInvalida,
    ErrorDeConfiguracion,
    InstruccionContaminada,
)

__all__ = [
    "DIMENSION_MAXIMA",
    "MOTOR_LOCAL",
    "Aviso",
    "ConfiguracionDeCapacidad",
    "ConfiguracionEfectiva",
    "Disponibilidad",
    "avisos_de",
    "validar",
]

DIMENSION_MAXIMA = 1024
"""La columna del índice es `vector(1024)` (DEC-03): un modelo de menos se rellena, uno de
más no entra."""

_ENTEROS = (
    "timeout_s",
    "num_ctx",
    "num_thread",
    "max_tokens_entrada",
    "max_tokens_salida",
    "dimension",
)
_PREFIJOS = ("prefijo_fragmento", "prefijo_consulta")
"""Lo que un modelo de embeddings antepone a cada texto según su uso (`passage: ` y `query: `
en algunos, nada en otros): es dato del modelo, no del código."""
_PARAMETROS = frozenset((*_ENTEROS, *_PREFIJOS, "temperatura"))
_PARAMETROS_OCR = frozenset(
    (
        "ocr_escala",
        "ocr_invertir",
        "ocr_binarizar",
        "ocr_confianza_minima",
        "imagenes_maximo",
        "imagenes_tiempo_maximo_s",
    )
)
"""La preparación de la imagen y el umbral de confianza del OCR (140) y los topes de las imágenes
embebidas (143): solo en `transcribir`. Qué hace cada uno y por qué tiene su valor está en `ocr`
y en `imagenes`."""

_PARAMETROS_DE_LA_DESCRIPCION = frozenset(("imagenes_a_describir",))
"""Cuántas imágenes embebidas sin texto se describen solas por Pieza (148): solo en
`describir_imagen`. El resto espera «Describir esta imagen»."""

_OBLIGATORIOS: Mapping[Capacidad, tuple[str, ...]] = {
    Capacidad.DESCRIBIR_IMAGEN: ("timeout_s", "num_ctx", "temperatura", "max_tokens_salida"),
    Capacidad.TRANSCRIBIR: ("timeout_s",),
    Capacidad.RESUMIR: (
        "timeout_s",
        "num_ctx",
        "temperatura",
        "max_tokens_entrada",
        "max_tokens_salida",
    ),
}
"""Las capacidades que corre el procesamiento (R1): sin su fila no hay capa, y sin estos
parámetros la llamada sale sin tope (ADR-0004, límite 3)."""

_PARAMETROS_OBLIGATORIOS: Mapping[Capacidad, tuple[str, ...]] = {
    **_OBLIGATORIOS,
    # Sin `temperatura` ni `num_ctx`: no genera texto. Sin `dimension` no se sabe si el vector
    # entra en la columna del índice.
    Capacidad.EMBEBER: ("timeout_s", "dimension", *_PREFIJOS),
}

TIEMPO_MAXIMO_DEL_TRABAJO_S = 1800
"""El `CHECK` de `trabajo_procesamiento.tiempo_maximo_ms`: el trabajo de una foto corre las
tres capacidades de `_OBLIGATORIOS` y su presupuesto es la suma de sus `timeout_s`."""

MOTOR_LOCAL = "local"
"""El extractor determinista de `transcribir` (PyMuPDF, python-docx, Tesseract): no es un
motor de red ni usa modelo de lenguaje."""


@dataclass(frozen=True)
class ConfiguracionDeCapacidad:
    """Una fila de `configuracion_capacidad`, sin la capacidad (es la clave del conjunto)."""

    motor: str
    """Id del mapa de motores (`ollama`) o `local`."""

    modelo: str | None
    """Nulo con el motor `local`."""

    instruccion_id: str | None
    """Un valor de `Plantilla` de esta capacidad. Nulo en `transcribir`."""

    parametros: Mapping[str, Any]
    """`temperatura`, `num_ctx`, `num_thread`, `timeout_s`, `max_tokens_entrada`,
    `max_tokens_salida`, `dimension`."""

    @property
    def plantilla(self) -> Plantilla:
        # Nula solo en `transcribir`, que recibe su plantilla en cada llamada (`validar`).
        return Plantilla(self.instruccion_id or "")

    @property
    def limites(self) -> Limites:
        return Limites(
            tiempo_maximo_s=self.parametros["timeout_s"],
            tope_tokens_entrada=self.parametros.get("max_tokens_entrada"),
            tope_tokens_salida=self.parametros.get("max_tokens_salida"),
        )


CapaPara = Callable[[Mapping[Capacidad, ConfiguracionDeCapacidad]], CapaModelos]
"""Arma la capa con unas filas: la de un trabajo, con la instantánea que congeló."""


@dataclass(frozen=True)
class ConfiguracionEfectiva:
    """Las filas con las que corre un trabajo, congeladas al encolar (DEC-87, RNF-16).

    El trabajo la guarda y la Ficha que propone la registra: un cambio de fila rige solo para
    los trabajos nuevos, y la propuesta dice con qué modelo, instrucción y topes se generó.
    """

    por_capacidad: Mapping[Capacidad, ConfiguracionDeCapacidad]

    @property
    def digest(self) -> str:
        """`sha256` de las filas: dos propuestas con el mismo digest salieron de lo mismo."""
        canonica = json.dumps(self._filas(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonica.encode()).hexdigest()

    def a_json(self) -> dict[str, Any]:
        return {"digest": self.digest, "por_capacidad": self._filas()}

    @classmethod
    def desde_json(cls, datos: Mapping[str, Any]) -> ConfiguracionEfectiva:
        return cls(
            {
                Capacidad(capacidad): ConfiguracionDeCapacidad(**fila)
                for capacidad, fila in datos["por_capacidad"].items()
            }
        )

    def _filas(self) -> dict[str, dict[str, Any]]:
        return {
            capacidad.value: {
                "motor": config.motor,
                "modelo": config.modelo,
                "instruccion_id": config.instruccion_id,
                "parametros": dict(config.parametros),
            }
            for capacidad, config in self.por_capacidad.items()
        }


class Disponibilidad(StrEnum):
    """Si el modelo de una fila está en su motor (`verificar`). Nunca es un error."""

    DESCARGADO = "descargado"
    NO_DESCARGADO = "no_descargado"
    MOTOR_NO_RESPONDE = "motor_no_responde"
    NO_APLICA = "no_aplica"
    """El extractor `local`: no tiene modelo que descargar."""


@dataclass(frozen=True)
class Aviso:
    """Lo que el Administrador técnico tiene que saber de un cambio de filas, sin bloquearlo."""

    capacidad: Capacidad
    codigo: str
    """`modelo_no_descargado` (historia 165) o `requiere_reindexado` (historia 166)."""
    mensaje: str


def avisos_de(
    nuevo: Mapping[Capacidad, ConfiguracionDeCapacidad],
    anterior: Mapping[Capacidad, ConfiguracionDeCapacidad],
    disponibilidad: Mapping[Capacidad, Disponibilidad],
) -> list[Aviso]:
    """Los avisos de pasar de `anterior` a `nuevo`, ya validado (`validar`)."""
    avisos = [
        Aviso(
            capacidad,
            "modelo_no_descargado",
            f"{nuevo[capacidad].modelo} no está descargado: el primer trabajo va a fallar"
            " hasta bajarlo con el comando `modelos`.",
        )
        for capacidad, estado in disponibilidad.items()
        if estado is Disponibilidad.NO_DESCARGADO
    ]
    if _perfil(nuevo.get(Capacidad.EMBEBER)) != _perfil(anterior.get(Capacidad.EMBEBER)):
        avisos.append(
            Aviso(
                Capacidad.EMBEBER,
                "requiere_reindexado",
                "Cambió cómo se embebe: los vectores del índice no se comparan con los nuevos."
                " Reconstruilo con el comando `reindexar`.",
            )
        )
    return avisos


def _perfil(config: ConfiguracionDeCapacidad | None) -> tuple[object, ...] | None:
    """Lo que cambia los vectores de `embeber`; el resto (tiempo, hilos) no los cambia."""
    if config is None:
        return None
    return (config.modelo, *(config.parametros.get(clave) for clave in _PREFIJOS))


def _de_la_nube(modelo: str) -> bool:
    """Un modelo «cloud» de Ollama (`nombre:cloud`, `nombre:120b-cloud`): corre en ollama.com
    y no en la PC, contra RNF-02 (S-MOD-05). La convención es la etiqueta, después de `:`."""
    nombre, _, etiqueta = modelo.casefold().rpartition(":")
    return bool(nombre) and (etiqueta == "cloud" or etiqueta.endswith("-cloud"))


def validar(
    configuracion: Mapping[Capacidad, ConfiguracionDeCapacidad],
    motores: Collection[str],
    *,
    completa: bool = True,
) -> None:
    """Lanza `ConfiguracionInvalida` con todos los errores del conjunto, o no hace nada.

    `motores` son los ids del mapa de motores del entorno; `local` es solo de `transcribir`.
    Con `completa=False` no exige las filas que faltan: la instantánea de un trabajo trae solo
    las de su tipo (la indexación, `embeber`).
    """
    errores = [
        ErrorDeConfiguracion(capacidad.value, "", "falta la fila")
        for capacidad in _OBLIGATORIOS
        if completa and capacidad not in configuracion
    ]
    errores += [
        ErrorDeConfiguracion(capacidad.value, campo, mensaje)
        for capacidad, config in configuracion.items()
        for campo, mensaje in _errores_de_la_fila(capacidad, config, motores)
    ]
    errores += _num_ctx_compartido(configuracion)
    tiempos = {
        capacidad: configuracion[capacidad].parametros.get("timeout_s")
        for capacidad in _OBLIGATORIOS
        if capacidad in configuracion
    }
    total = sum(t for t in tiempos.values() if type(t) is int and t > 0)
    if total > TIEMPO_MAXIMO_DEL_TRABAJO_S:
        errores += [
            ErrorDeConfiguracion(
                capacidad.value,
                "timeout_s",
                f"con las otras capacidades del procesamiento suma {total} s y un trabajo"
                f" admite {TIEMPO_MAXIMO_DEL_TRABAJO_S}",
            )
            for capacidad in tiempos
        ]
    if errores:
        raise ConfiguracionInvalida(errores)


def _errores_de_la_fila(
    capacidad: Capacidad, config: ConfiguracionDeCapacidad, motores: Collection[str]
) -> Iterator[tuple[str, str]]:
    p = config.parametros
    con_modelo = capacidad is not Capacidad.TRANSCRIBIR
    if config.motor not in (motores if con_modelo else (MOTOR_LOCAL,)):
        yield "motor", f"'{config.motor}' no es un motor de {capacidad}"
    propios = {
        Capacidad.TRANSCRIBIR: _PARAMETROS_OCR,
        Capacidad.DESCRIBIR_IMAGEN: _PARAMETROS_DE_LA_DESCRIPCION,
    }.get(capacidad, frozenset())
    for clave in sorted(p.keys() - _PARAMETROS - propios):
        yield clave, "parámetro desconocido"
    a_describir = p.get("imagenes_a_describir", 0)
    if not (type(a_describir) is int and a_describir >= 0):
        yield "imagenes_a_describir", f"tiene que ser un entero desde 0, no {a_describir!r}"
    escala = p.get("ocr_escala", 1)
    if not (type(escala) in (int, float) and 1 <= escala <= 4):
        yield "ocr_escala", f"tiene que ser un número entre 1 y 4, no {escala!r}"
    for clave in ("ocr_invertir", "ocr_binarizar"):
        if clave in p and type(p[clave]) is not bool:
            yield clave, f"tiene que ser true o false, no {p[clave]!r}"
    minima = p.get("ocr_confianza_minima", 0)
    if not (type(minima) in (int, float) and 0 <= minima <= 100):
        yield "ocr_confianza_minima", f"tiene que ser un número entre 0 y 100, no {minima!r}"
    maximo = p.get("imagenes_maximo", 0)
    if not (type(maximo) is int and maximo >= 0):
        yield "imagenes_maximo", f"tiene que ser un entero desde 0, no {maximo!r}"
    tiempo = p.get("imagenes_tiempo_maximo_s", 1)
    if not (type(tiempo) is int and tiempo > 0):
        yield "imagenes_tiempo_maximo_s", f"tiene que ser un entero positivo, no {tiempo!r}"
    for clave in _PARAMETROS_OBLIGATORIOS.get(capacidad, ("timeout_s", "num_ctx", "temperatura")):
        if clave not in p:
            yield clave, "falta"
    for clave in _ENTEROS:
        if clave in p and (type(p[clave]) is not int or p[clave] <= 0):
            yield clave, f"tiene que ser un entero positivo, no {p[clave]!r}"
    if type(p.get("dimension")) is int and p["dimension"] > DIMENSION_MAXIMA:
        yield "dimension", f"{p['dimension']} pasa de {DIMENSION_MAXIMA}"
    if "temperatura" in p and not (
        type(p["temperatura"]) in (int, float) and 0 <= p["temperatura"] <= 2
    ):
        yield "temperatura", f"tiene que ser un número entre 0 y 2, no {p['temperatura']!r}"
    for clave in _PREFIJOS:
        if clave in p and type(p[clave]) is not str:
            yield clave, f"tiene que ser un texto, no {p[clave]!r}"
    if not con_modelo:
        return
    if not config.modelo:
        yield "modelo", "falta"
    elif _de_la_nube(config.modelo):
        yield "modelo", (
            f"'{config.modelo}' corre en la nube de Ollama, fuera de la PC: los modelos tienen"
            " que ser locales (RNF-02)"
        )
    if capacidad is Capacidad.EMBEBER:
        if config.instruccion_id is not None:
            yield "instruccion_id", "embeber no lleva instrucción: los prefijos van en parametros"
        return
    try:
        entradas.instruccion(capacidad, config.plantilla)
    except (ValueError, InstruccionContaminada):
        yield "instruccion_id", f"{config.instruccion_id!r} no es una instrucción de {capacidad}"
    # Ollama recorta por el principio lo que no entra en el contexto: se llevaría la instrucción.
    topes = [p.get("max_tokens_entrada") or 0, p.get("max_tokens_salida") or 0]
    contexto = p.get("num_ctx")
    if type(contexto) is int and all(type(t) is int for t in topes) and contexto < sum(topes):
        yield "num_ctx", f"{contexto} no alcanza para {sum(topes)} tokens de entrada y salida"


def _num_ctx_compartido(
    configuracion: Mapping[Capacidad, ConfiguracionDeCapacidad],
) -> list[ErrorDeConfiguracion]:
    """El mismo modelo en dos capacidades con otro `num_ctx`: Ollama lo recargaría en cada
    cambio de capacidad (DEC-06)."""
    contextos: dict[tuple[str, str | None], set[int]] = {}
    for config in configuracion.values():
        if type(config.parametros.get("num_ctx")) is int:
            clave = (config.motor, config.modelo)
            contextos.setdefault(clave, set()).add(config.parametros["num_ctx"])
    return [
        ErrorDeConfiguracion(
            capacidad.value, "num_ctx", f"otra capacidad usa '{config.modelo}' con otro num_ctx"
        )
        for capacidad, config in configuracion.items()
        if len(contextos.get((config.motor, config.modelo), ())) > 1
    ]
