"""El banco de ingesta (ticket 137; RF-07, RF-08, RF-28; RNF-12; DEC-132).

M1 y M2 miden la búsqueda y la integridad de las Fichas; este banco mide lo que produce la
ingesta de cada Pieza de un corpus etiquetado, con el mismo camino que `procesamiento`: de una
imagen, `describir_imagen` más el texto visible de `transcribir`, juntos con `cuerpo_de_la_foto`;
de un documento, `transcribir`; un texto plano es su cuerpo, sin modelo. Por Pieza informa:

- **Invenciones:** las invenciones conocidas de la etiqueta que aparecen en el texto del modelo
  (la descripción), con la coincidencia de `comparacion.puntuar` (sin tildes ni mayúsculas, por
  palabras enteras). El texto leído no cuenta: no lo escribe un modelo, y lo que está impreso en
  el original no es una invención. Las frases de la descripción que no contienen ningún hecho
  esperado se listan para que una persona marque si son invenciones nuevas: no hay modelo juez.
- **Cobertura:** los hechos esperados que aparecen en el cuerpo (descripción y texto leído).
- **Error del OCR:** la tasa de error por carácter del texto leído contra la transcripción de
  referencia (distancia de edición sobre la longitud de la referencia, con los espacios
  colapsados), y la proporción de líneas basura: líneas no vacías sin ninguna letra.
- **Tiempo por Pieza**, en la máquina donde corre; el informe anota el hardware.

Los candidatos salen de `candidatos.csv` (capacidad `describir_imagen`) y pasan por
`Banco.preparar`, el mismo `validar` y la misma `capa_para` que producción. Gana el de menos
Piezas cortadas por el motor, después menos invenciones, más cobertura, menos tiempo mediano y,
por último, el nombre; si gana uno de origen chino se informa además el mejor no chino. El tope
de tiempo se informa y no descarta a nadie (DEC-132).

**El corpus** es un directorio con los originales y `etiquetas.json`: una lista con un objeto
por Pieza.

    [
      {
        "archivo": "cartel.jpg",
        "hechos_esperados": ["Hackathon Cyber.AR 2026", "Av. Cabildo 15"],
        "invenciones": ["entorno de oficina"],
        "transcripcion": "cartel.txt"
      }
    ]

`archivo` y `transcripcion` son rutas relativas al directorio; `transcripcion` es un texto
UTF-8 con lo que el original tiene escrito, y se puede omitir (sin ella no hay error del OCR).
Las dos listas se pueden omitir o dejar vacías. Un hecho esperado o una invención es una frase
corta que tiene que aparecer tal cual, salvo tildes, mayúsculas y signos.
"""

from __future__ import annotations

import json
import mimetypes
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from historia_viva.banco import celda_md, fila_md, percentil, porcentaje
from historia_viva.banco.comparacion import CandidatoDeReferencia, Puntaje, _normal, puntuar
from historia_viva.banco.referencia import ReferenciaInvalida
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_DOCUMENTO,
    ETIQUETA_IMAGEN,
    BloqueBinario,
    CapaModelos,
    Plantilla,
)
from historia_viva.dominio.errores import (
    DescripcionFueraDeEsquema,
    LimiteExcedido,
    MotorNoDisponible,
)
from historia_viva.dominio.originales import es_imagen, es_texto
from historia_viva.exportacion import escribir_csv
from historia_viva.procesamiento.manejador import _cuerpo_del_texto, cuerpo_de_la_foto

__all__ = [
    "ETIQUETAS",
    "CorridaDeIngesta",
    "MedicionDeIngesta",
    "PiezaDelCorpus",
    "elegir",
    "escribir_ingesta",
    "frases_sin_coincidencia",
    "leer_corpus",
    "lineas_basura",
    "medir_ingesta",
    "tabla_ingesta",
    "tasa_de_error",
]

ETIQUETAS = "etiquetas.json"


@dataclass(frozen=True)
class PiezaDelCorpus:
    archivo: str
    hechos_esperados: tuple[str, ...]
    invenciones: tuple[str, ...]
    transcripcion: str | None
    """El texto de referencia, ya leído del archivo que nombra la etiqueta."""


def leer_corpus(directorio: Path) -> tuple[PiezaDelCorpus, ...]:
    """Las Piezas de `etiquetas.json`, validadas.

    Raises:
        ReferenciaInvalida: Con todos los problemas encontrados.
    """
    raiz = directorio.resolve()
    try:
        etiquetas = json.loads((raiz / ETIQUETAS).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReferenciaInvalida([f"Falta {ETIQUETAS} en {directorio}."]) from None
    except (ValueError, UnicodeDecodeError) as error:
        raise ReferenciaInvalida([f"{ETIQUETAS} no es un JSON válido: {error}"]) from None
    if not isinstance(etiquetas, list) or not etiquetas:
        raise ReferenciaInvalida([f"{ETIQUETAS} tiene que ser una lista con al menos una Pieza."])
    errores: list[str] = []
    piezas: list[PiezaDelCorpus] = []

    def dentro(valor: object) -> Path | None:
        """El archivo, si existe y no se sale del directorio."""
        ruta = (raiz / valor).resolve() if isinstance(valor, str) and valor else None
        return ruta if ruta and ruta.is_relative_to(raiz) and ruta.is_file() else None

    for n, etiqueta in enumerate(etiquetas, start=1):
        donde = f"{ETIQUETAS}, Pieza {n}"
        if not isinstance(etiqueta, dict):
            errores.append(f"{donde}: tiene que ser un objeto.")
            continue
        archivo = etiqueta.get("archivo")
        if dentro(archivo) is None:
            errores.append(f"{donde}: falta el archivo «{archivo}» en el directorio.")
        listas = {}
        for campo in ("hechos_esperados", "invenciones"):
            valor = etiqueta.get(campo, [])
            if not isinstance(valor, list) or not all(isinstance(v, str) for v in valor):
                errores.append(f"{donde}: «{campo}» tiene que ser una lista de textos.")
                valor = []
            listas[campo] = tuple(v.strip() for v in valor if v.strip())
        transcripcion = None
        if (nombre := etiqueta.get("transcripcion")) is not None:
            if (ruta := dentro(nombre)) is None:
                errores.append(f"{donde}: falta la transcripción «{nombre}» en el directorio.")
            else:
                transcripcion = ruta.read_text(encoding="utf-8")
        piezas.append(PiezaDelCorpus(
            str(archivo), listas["hechos_esperados"], listas["invenciones"], transcripcion
        ))
    if errores:
        raise ReferenciaInvalida(errores)
    return tuple(piezas)


def tasa_de_error(leido: str, referencia: str) -> float | None:
    """La tasa de error por carácter: la distancia de Levenshtein sobre la longitud de la
    referencia, con los espacios colapsados. `None` sin referencia."""
    a, b = " ".join(leido.split()), " ".join(referencia.split())
    if not b:
        return None
    anterior = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        actual = [i]
        for j, cb in enumerate(b, start=1):
            actual.append(min(anterior[j] + 1, actual[j - 1] + 1, anterior[j - 1] + (ca != cb)))
        anterior = actual
    return anterior[-1] / len(b)


def lineas_basura(leido: str) -> float | None:
    """La proporción de líneas no vacías sin ninguna letra; `None` si no leyó nada."""
    lineas = [linea for linea in leido.splitlines() if linea.strip()]
    if not lineas:
        return None
    return sum(not any(c.isalpha() for c in linea) for linea in lineas) / len(lineas)


_CLASES_DE_IMAGEN = frozenset(
    _normal(c) for c in ("Fotografía", "Cartel o gráfico", "Página impresa", "Otra")
)
"""Las clases con que empieza la respuesta de `describir_imagen_por_clase` (139)."""


def frases_sin_coincidencia(texto: str, esperados: Sequence[str]) -> tuple[str, ...]:
    """Las frases (por punto final o salto de línea) que no contienen ningún hecho esperado. Un
    punto tras menos de tres letras o cifras seguidas («Av.», «Dr.») no corta la frase. La
    primera no cuenta si es solo la clase de la imagen: no afirma nada que revisar."""
    normales = [_normal(e) for e in esperados]
    frases = [f for f in (f.strip() for f in re.split(r"(?<=\w\w\w[.!?])\s+|\n+", texto)) if f]
    if frases and _normal(frases[0]) in _CLASES_DE_IMAGEN:
        frases = frases[1:]
    return tuple(f for f in frases if not any(e in _normal(f) for e in normales))


@dataclass(frozen=True)
class MedicionDeIngesta:
    archivo: str
    tiempo_ms: int
    descripcion: str | None
    """El texto del modelo; `None` en un documento, que no se describe."""
    leido: str
    """Lo que devolvió `transcribir`: el OCR de una imagen o el texto de un documento."""
    puntaje: Puntaje
    """La cobertura, sobre el cuerpo; las invenciones, sobre la descripción."""
    tasa_de_error: float | None
    lineas_basura: float | None
    sin_coincidencia: tuple[str, ...]
    tope_s: int | None
    error: str | None = None
    """El motor o un tope cortó la Pieza: no se puntúa."""

    @property
    def sobre_el_tope(self) -> bool:
        """Informativo: el tope es un objetivo medido, no descarta al candidato."""
        return bool(self.tope_s and self.tiempo_ms > self.tope_s * 1000)


@dataclass(frozen=True)
class CorridaDeIngesta:
    candidato: CandidatoDeReferencia
    configuracion_efectiva: Mapping[str, Any]
    mediciones: tuple[MedicionDeIngesta, ...]

    @property
    def _medidas(self) -> list[MedicionDeIngesta]:
        return [m for m in self.mediciones if m.error is None]

    @property
    def errores(self) -> int:
        return len(self.mediciones) - len(self._medidas)

    @property
    def invenciones(self) -> int:
        return sum(len(m.puntaje.invenciones) for m in self._medidas)

    @property
    def con_invenciones(self) -> int:
        return sum(bool(m.puntaje.invenciones) for m in self._medidas)

    @property
    def cobertura(self) -> float | None:
        """Promedio por Pieza con hechos esperados; una cortada cuenta 0."""
        con_hechos = [m for m in self.mediciones if m.puntaje.presentes or m.puntaje.faltan]
        if not con_hechos:
            return None
        return sum(0.0 if m.error else m.puntaje.cobertura for m in con_hechos) / len(con_hechos)

    @property
    def tasa_de_error(self) -> float | None:
        return _promedio([m.tasa_de_error for m in self._medidas])

    @property
    def lineas_basura(self) -> float | None:
        return _promedio([m.lineas_basura for m in self._medidas])

    @property
    def tiempo_mediano_ms(self) -> float | None:
        return percentil([m.tiempo_ms for m in self._medidas], 50)

    @property
    def tiempo_p95_ms(self) -> float | None:
        return percentil([m.tiempo_ms for m in self._medidas], 95)

    @property
    def sobre_el_tope(self) -> int:
        return sum(m.sobre_el_tope for m in self.mediciones)


def _promedio(valores: Sequence[float | None]) -> float | None:
    presentes = [v for v in valores if v is not None]
    return sum(presentes) / len(presentes) if presentes else None


def _tipo_mime(archivo: str) -> str:
    return mimetypes.guess_type(archivo)[0] or "application/octet-stream"


def medir_ingesta(
    capa: CapaModelos,
    candidato: CandidatoDeReferencia,
    configuracion_efectiva: Mapping[str, Any],
    corpus: Sequence[PiezaDelCorpus],
    directorio: Path,
    *,
    tope_transcribir_s: int | None,
    reloj: Callable[[], float] = time.monotonic,
    progreso: Callable[[MedicionDeIngesta], None] = lambda medicion: None,
) -> CorridaDeIngesta:
    """Cada Pieza del corpus, una vez, con la capa del candidato (la de `Banco.preparar`)."""
    tope_describir = configuracion_efectiva["por_capacidad"]["describir_imagen"]["parametros"].get(
        "timeout_s"
    )
    mediciones = []
    for pieza in corpus:
        tipo_mime = _tipo_mime(pieza.archivo)
        foto, texto = es_imagen(tipo_mime), es_texto(tipo_mime)
        contenido = (directorio / pieza.archivo).read_bytes()
        archivo = BloqueBinario(
            etiqueta=ETIQUETA_IMAGEN if foto else ETIQUETA_DOCUMENTO,
            contenido=contenido,
            tipo_mime=tipo_mime,
            nombre_archivo=Path(pieza.archivo).name,
        )
        topes = [t for t in (tope_describir if foto else None,
                             None if texto else tope_transcribir_s) if t]
        inicio = reloj()
        descripcion: str | None = None
        leido, error = "", None
        try:
            # Como `procesamiento`: un texto plano es su cuerpo, sin modelo; una foto se
            # describe antes de leerse.
            if texto:
                leido = _cuerpo_del_texto(contenido, tipo_mime)
            else:
                descripcion = capa.describir_imagen(imagen=archivo).texto if foto else None
                leido = capa.transcribir(
                    archivo=archivo,
                    plantilla=(
                        Plantilla.TRANSCRIBIR_IMAGEN if foto else Plantilla.TRANSCRIBIR_DOCUMENTO
                    ),
                ).texto
        except (LimiteExcedido, MotorNoDisponible, DescripcionFueraDeEsquema) as e:
            error = str(e)
        tiempo_ms = int((reloj() - inicio) * 1000)
        cuerpo = leido if descripcion is None else cuerpo_de_la_foto(descripcion, leido)
        puntaje = replace(
            puntuar(cuerpo, pieza.hechos_esperados, ()),
            invenciones=puntuar(descripcion or "", (), pieza.invenciones).invenciones,
        )
        medicion = MedicionDeIngesta(
            archivo=pieza.archivo,
            tiempo_ms=tiempo_ms,
            descripcion=descripcion,
            leido=leido,
            puntaje=puntaje,
            tasa_de_error=(
                None if error or pieza.transcripcion is None
                else tasa_de_error(leido, pieza.transcripcion)
            ),
            lineas_basura=None if error else lineas_basura(leido),
            sin_coincidencia=frases_sin_coincidencia(descripcion or "", pieza.hechos_esperados),
            tope_s=sum(topes) if topes else None,
            error=error,
        )
        progreso(medicion)
        mediciones.append(medicion)
    return CorridaDeIngesta(candidato, configuracion_efectiva, tuple(mediciones))


def elegir(corridas: Sequence[CorridaDeIngesta]) -> tuple[int | None, int | None]:
    """Los índices del ganador y del mejor no chino (`None` si no hay). Un candidato sin ninguna
    Pieza medida no compite; una Pieza cortada no se puede puntuar y pesa antes que las
    invenciones. La elección no depende del orden."""
    def clave(i: int) -> tuple[int, int, float, float, str]:
        c = corridas[i]
        return (c.errores, c.invenciones, -(c.cobertura or 0.0),
                c.tiempo_mediano_ms if c.tiempo_mediano_ms is not None else float("inf"),
                c.candidato.modelo)

    compiten = [i for i, c in enumerate(corridas) if c.errores < len(c.mediciones)]
    no_chinos = [i for i in compiten if not corridas[i].candidato.origen_chino]
    return min(compiten, key=clave, default=None), min(no_chinos, key=clave, default=None)


def _decimal(valor: float | None) -> str:
    return "—" if valor is None else f"{valor:.3f}".replace(".", ",")


def _segundos(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f}".replace(".", ",")


def tabla_ingesta(corridas: Sequence[CorridaDeIngesta], *, hardware: str) -> str:
    ganador, no_chino = elegir(corridas)
    lineas = [
        "Banco de ingesta: invenciones, cobertura, error del OCR y tiempo por Pieza.",
        "",
        ("| Modelo | Origen chino | Invenciones | Piezas con invenciones | Cobertura"
        " | Error del OCR (CER) | Líneas basura | Tiempo mediano (s) | p95 (s) | Sobre el tope"
        " | Cortadas | Ganador | Mejor no chino |"),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        *(
            fila_md(
                c.candidato.modelo, "sí" if c.candidato.origen_chino else "no", c.invenciones,
                c.con_invenciones, porcentaje(c.cobertura), _decimal(c.tasa_de_error),
                porcentaje(c.lineas_basura), _segundos(c.tiempo_mediano_ms),
                _segundos(c.tiempo_p95_ms), c.sobre_el_tope, c.errores,
                "**sí**" if i == ganador else "", "**sí**" if i == no_chino else "",
            )
            for i, c in enumerate(corridas)
        ),
        "",
        "Por Pieza:",
        "",
        ("| Modelo | Pieza | Invenciones | Cobertura | Error del OCR (CER) | Líneas basura"
        " | Tiempo (s) | Sobre el tope |"),
        "|---|---|---|---|---|---|---|---|",
        *(
            fila_md(
                c.candidato.modelo, m.archivo,
                f"cortada: {m.error}" if m.error else ", ".join(m.puntaje.invenciones) or "—",
                porcentaje(m.puntaje.cobertura if m.puntaje.presentes or m.puntaje.faltan
                           else None),
                _decimal(m.tasa_de_error), porcentaje(m.lineas_basura), _segundos(m.tiempo_ms),
                "sí" if m.sobre_el_tope else "no",
            )
            for c in corridas for m in c.mediciones
        ),
        "",
        ("Invenciones: las de la etiqueta que aparecen en la descripción del modelo, sin tildes ni"
        " mayúsculas y por palabras enteras; el texto leído no cuenta. Cobertura: los hechos"
        " esperados presentes en el cuerpo. CER: distancia de edición sobre la longitud de la"
        " transcripción de referencia. Líneas basura: líneas leídas sin ninguna letra. Sin modelo"
        " juez. El tope de tiempo se informa: no descarta."),
    ]
    if ganador is None:
        lineas.append("Ningún candidato llegó a medir: no hay ganador.")
    else:
        lineas.append(f"Ganador: {celda_md(corridas[ganador].candidato.modelo)}.")
        if no_chino is None:
            lineas.append(
                "**No hay candidato no chino que haya medido:** sumá uno con --modelo por si la"
                " FIE restringe los modelos de origen chino (C44)."
            )
        elif no_chino != ganador:
            lineas.append(
                "El ganador es de origen chino: si la FIE restringe esos modelos (C44), queda"
                f" propuesto el mejor no chino, {celda_md(corridas[no_chino].candidato.modelo)}."
            )
    lineas.append(f"Hardware: {celda_md(hardware)}.")
    revisar = [(c, m) for c in corridas for m in c.mediciones if m.sin_coincidencia]
    if revisar:
        lineas += ["", ("Frases sin ningún hecho esperado, para que una persona marque si son"
                   " invenciones nuevas:"), ""]
        lineas += [
            f"- {celda_md(c.candidato.modelo)} · {celda_md(m.archivo)}: «{celda_md(frase)}»"
            for c, m in revisar for frase in m.sin_coincidencia
        ]
    return "\n".join(lineas) + "\n"


_COLUMNAS = (
    "modelo", "origen_chino", "archivo", "tiempo_ms", "sobre_el_tope", "invenciones",
    "cobertura", "faltan", "tasa_de_error", "lineas_basura", "sin_coincidencia", "error",
)


def escribir_ingesta(
    directorio: Path,
    corridas: Sequence[CorridaDeIngesta],
    *,
    corpus: Path,
    hardware: str,
    transcribir: Mapping[str, Any],
    momento: datetime,
) -> tuple[list[Path], str]:
    """El JSON (con cada texto), el CSV (una Pieza por fila) y la tabla. `transcribir`, la
    configuración efectiva de la fila vigente de `transcribir`, la misma para todos."""
    directorio.mkdir(parents=True, exist_ok=True)
    ganador, no_chino = elegir(corridas)
    datos = {
        "medida": "ingesta",
        "momento": momento.isoformat(),
        "corpus": str(corpus),
        "hardware": hardware,
        "transcribir": transcribir,
        "corridas": [
            {
                "modelo": c.candidato.modelo,
                "origen_chino": c.candidato.origen_chino,
                "configuracion_efectiva": c.configuracion_efectiva,
                "invenciones": c.invenciones,
                "con_invenciones": c.con_invenciones,
                "cobertura": c.cobertura,
                "tasa_de_error": c.tasa_de_error,
                "lineas_basura": c.lineas_basura,
                "tiempo_mediano_ms": c.tiempo_mediano_ms,
                "tiempo_p95_ms": c.tiempo_p95_ms,
                "sobre_el_tope": c.sobre_el_tope,
                "errores": c.errores,
                "ganador": i == ganador,
                "mejor_no_chino": i == no_chino,
                "mediciones": [
                    {**asdict(m), "cobertura": m.puntaje.cobertura,
                     "sobre_el_tope": m.sobre_el_tope}
                    for m in c.mediciones
                ],
            }
            for i, c in enumerate(corridas)
        ],
    }
    nombre = f"ingesta-{momento:%Y%m%dT%H%M%S}"
    rutas = [directorio / f"{nombre}.{extension}" for extension in ("json", "csv", "md")]
    tabla = tabla_ingesta(corridas, hardware=hardware)
    rutas[0].write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    rutas[1].write_bytes(escribir_csv(_COLUMNAS, (
        [c.candidato.modelo, c.candidato.origen_chino, m.archivo, m.tiempo_ms, m.sobre_el_tope,
         list(m.puntaje.invenciones), m.puntaje.cobertura, list(m.puntaje.faltan),
         m.tasa_de_error, m.lineas_basura, list(m.sin_coincidencia), m.error]
        for c in corridas for m in c.mediciones
    )))
    rutas[2].write_text(tabla, encoding="utf-8")
    return rutas, tabla
