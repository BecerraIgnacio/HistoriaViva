"""La comparación de modelos candidatos (RF-28, DEC-132; spec, módulo 27 `banco`).

Cada candidato reemplaza la fila vigente de su capacidad, pasa por `validar` con el resto de las
filas y por `capa_para`, el mismo código que producción, y corre `repeticiones` veces sobre las
Piezas de referencia. Se puntúa sin modelo juez: los hechos esperados y los datos clave que
aparecen en el texto, y las invenciones de la lista de cada Pieza que aparecen. Sin tablas (DEC-97): cada corrida escribe un JSON, un CSV y una tabla markdown, y la
tabla junta solo las corridas con el mismo `hash_referencia`.

`responder` (078) corre sobre las consultas de ajuste (las de prueba son de la corrida oficial,
F-9): los fragmentos los recupera una vez
la búsqueda de producción y cada candidato redacta con `responder`, la salida en vivo junta.
Se puntúan los hechos esperados y las invenciones de las Piezas esperadas, y las Citas con la
misma `verificar_citas` que la Conversación (R-14); una consulta de control acierta solo si
declara que no hay evidencia, con la regla de la Conversación.

`candidatos.csv` (`capacidad, modelo, origen_chino, parametros`; R-39) vive en `HV_REFERENCIA`;
sin él rige el de este paquete. `parametros` es un objeto JSON que se aplica sobre los de la fila
vigente (vacío: los mismos). `origen_chino` es un dato de la lista, no se infiere del nombre: el
banco informa el ganador y el mejor no chino por si la FIE restringe esos modelos (C44).
"""

from __future__ import annotations

import json
import mimetypes
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from historia_viva.banco import celda_md, fila_md, percentil, porcentaje
from historia_viva.banco.referencia import (
    ConjuntoDeReferencia,
    ConsultaDeReferencia,
    PiezaDeReferencia,
    ReferenciaInvalida,
    _registros,
)
from historia_viva.capa_modelos.configuracion import (
    ConfiguracionDeCapacidad,
    ConfiguracionEfectiva,
)
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CONSULTA,
    ETIQUETA_CUERPO_FICHA,
    ETIQUETA_DOCUMENTO,
    ETIQUETA_FRAGMENTO,
    ETIQUETA_IMAGEN,
    BloqueBinario,
    BloqueDatos,
    CapaModelos,
    Capacidad,
    Plantilla,
    ResultadoTexto,
)
from historia_viva.conversacion import declara_sin_evidencia
from historia_viva.dominio.citas import CitaDelTexto, verificar_citas
from historia_viva.dominio.errores import ConfiguracionInvalida, LimiteExcedido, MotorNoDisponible
from historia_viva.dominio.originales import es_imagen
from historia_viva.exportacion import escribir_csv
from historia_viva.procesamiento.manejador import cuerpo_de_la_foto

__all__ = [
    "UMBRAL_DE_ACIERTO",
    "Banco",
    "CandidatoDeReferencia",
    "CandidatoInvalido",
    "Evaluacion",
    "Medicion",
    "Puntaje",
    "Resumen",
    "eleccion_del_banco",
    "elegir_ganador_y_no_chino",
    "escribir_comparacion",
    "leer_candidatos",
    "puntuar",
    "puntuar_respuesta",
    "tabla_comparacion",
]

UMBRAL_DE_ACIERTO = 0.6
"""Una salida acierta si cubre al menos esta fracción de lo esperado y no inventa nada."""

_CANDIDATOS = "candidatos.csv"
_DEFECTO = Path(__file__).parent / _CANDIDATOS


class CandidatoInvalido(Exception):
    """La fila del candidato no pasa `validar`: en producción tampoco correría."""

    def __init__(self, modelo: str, error: ConfiguracionInvalida) -> None:
        super().__init__(f"El candidato {modelo} no pasa la validación:\n{error}")
        self.modelo = modelo


@dataclass(frozen=True)
class CandidatoDeReferencia:
    capacidad: Capacidad
    modelo: str
    origen_chino: bool
    parametros: Mapping[str, Any]
    """Sobre los de la fila vigente."""


def leer_candidatos(directorio: Path) -> list[CandidatoDeReferencia]:
    """`candidatos.csv` de `directorio` o, si no está, el del paquete.

    Raises:
        ReferenciaInvalida: Con todas las filas que no sirven.
    """
    origen = directorio if (directorio / _CANDIDATOS).is_file() else _DEFECTO.parent
    errores: list[str] = []
    candidatos: list[CandidatoDeReferencia] = []
    for n, fila in enumerate(_registros(origen, _CANDIDATOS, errores), start=2):
        donde = f"{_CANDIDATOS}, línea {n}"
        try:
            capacidad = Capacidad(fila.get("capacidad", ""))
        except ValueError:
            errores.append(f"{donde}: capacidad «{fila.get('capacidad', '')}» desconocida.")
            continue
        origen_chino = fila.get("origen_chino", "").lower()
        if origen_chino not in ("true", "false"):
            errores.append(f"{donde}: «origen_chino» tiene que ser true o false.")
        try:
            parametros = json.loads(fila.get("parametros") or "{}")
        except json.JSONDecodeError:
            parametros = None
        if not isinstance(parametros, dict):
            errores.append(f"{donde}: «parametros» tiene que ser un objeto JSON.")
            parametros = {}
        if not fila.get("modelo"):
            errores.append(f"{donde}: falta el modelo.")
        candidatos.append(
            CandidatoDeReferencia(capacidad, fila.get("modelo", ""), origen_chino == "true",
                                  parametros)
        )
    if errores:
        raise ReferenciaInvalida(errores)
    return candidatos


def _normal(texto: str) -> str:
    """Sin tildes ni mayúsculas, y solo letras y números entre espacios: «Estado-Mayor» y
    «estado mayor» son lo mismo, y «color» no aparece en «colores»."""
    plano = "".join(
        c for c in unicodedata.normalize("NFKD", texto.casefold()) if not unicodedata.combining(c)
    )
    return " " + " ".join("".join(c if c.isalnum() else " " for c in plano).split()) + " "


@dataclass(frozen=True)
class Puntaje:
    presentes: tuple[str, ...]
    faltan: tuple[str, ...]
    invenciones: tuple[str, ...]
    """Las de la lista de la Pieza que aparecen en el texto."""
    citas: tuple[CitaDelTexto, ...] | None = None
    """`responder`: las `[n]` de la respuesta; `None` en las capacidades que no citan."""
    control: bool = False
    """`responder`: una consulta de control, que acierta solo si declara que no hay evidencia."""
    sin_evidencia: bool = False
    """`responder`: la respuesta declara que no hay evidencia, con la regla de la Conversación."""

    @property
    def cobertura(self) -> float:
        esperados = len(self.presentes) + len(self.faltan)
        return len(self.presentes) / esperados if esperados else 0.0

    @property
    def citas_validas(self) -> int:
        return sum(c.verificada for c in self.citas or ())

    @property
    def acierto(self) -> bool:
        """Sin un hecho medible presente no hay acierto: una salida vacía o basura no acierta
        aunque no invente nada. Una respuesta acierta además si cita y todas sus Citas son de
        fragmentos recuperados: sin una Cita verificada la Conversación la muestra como «sin
        evidencia»."""
        if self.control:
            return self.sin_evidencia
        citas_bien = self.citas is None or (
            bool(self.citas) and self.citas_validas == len(self.citas)
        )
        return (
            bool(self.presentes) and self.cobertura >= UMBRAL_DE_ACIERTO and not self.invenciones
            and citas_bien
        )


def puntuar(texto: str, esperados: Sequence[str], invenciones: Sequence[str]) -> Puntaje:
    """Qué hechos esperados (o datos clave) dice el texto y qué invenciones de la lista trae,
    por palabras enteras, sin tildes ni mayúsculas. Sin modelo juez."""
    normal = _normal(texto)
    aparece = [_normal(e) in normal for e in esperados]
    return Puntaje(
        presentes=tuple(e for e, si in zip(esperados, aparece, strict=True) if si),
        faltan=tuple(e for e, si in zip(esperados, aparece, strict=True) if not si),
        invenciones=tuple(i for i in invenciones if _normal(i) in normal),
    )


def puntuar_respuesta(
    texto: str,
    fragmentos: int,
    esperados: Sequence[str],
    invenciones: Sequence[str],
    *,
    control: bool = False,
) -> Puntaje:
    """`puntuar` más las Citas de la respuesta contra los `fragmentos` que vio el modelo y, en
    una consulta de control, si declara que no hay evidencia."""
    return replace(
        puntuar(texto, esperados, invenciones),
        citas=verificar_citas(texto, fragmentos),
        control=control,
        sin_evidencia=declara_sin_evidencia(texto),
    )


@dataclass(frozen=True)
class Medicion:
    referencia_id: str
    """La Pieza o, en `responder`, el `consulta_id`."""
    repeticion: int
    tiempo_ms: int
    tokens_entrada: int
    tokens_salida: int
    texto: str
    puntaje: Puntaje | None
    error: str | None = None
    """El tope o el motor cortó la invocación: cuenta como desacierto."""


@dataclass(frozen=True)
class Resumen:
    """Una fila de la tabla: lo que se compara entre corridas (y lo que se relee de ellas)."""

    modelo: str
    origen_chino: bool
    digest: str
    """El de la configuración efectiva del candidato: dos filas con el mismo modelo y otros
    parámetros son candidatos distintos."""
    corrida: str
    mediciones: int
    aciertos: int
    errores: int
    cobertura: float | None
    con_invenciones: int
    tiempo_mediano_ms: float | None
    tiempo_p95_ms: float | None
    tokens_entrada: float | None
    tokens_salida: float | None
    tope_s: int | None
    citas_validas: float | None = None
    """`responder`: de todas las `[n]` de las respuestas, las de fragmentos recuperados;
    `None` sin Citas."""
    controles: int = 0
    controles_acertados: int = 0
    """`responder`: las consultas de control medidas y las que declararon que no hay evidencia."""

    @property
    def acierto(self) -> float | None:
        """`None` si no midió nada: no compite."""
        return self.aciertos / self.mediciones if self.mediciones > self.errores else None

    @property
    def sobre_el_tope(self) -> bool:
        """Informativo: el tope es un objetivo medido, no descarta al candidato."""
        return bool(self.tope_s and self.tiempo_mediano_ms and
                    self.tiempo_mediano_ms > self.tope_s * 1000)


@dataclass(frozen=True)
class Evaluacion:
    capacidad: Capacidad
    candidato: CandidatoDeReferencia
    configuracion_efectiva: Mapping[str, Any]
    corrida: str
    mediciones: tuple[Medicion, ...]
    sin_fragmentos: tuple[ConsultaDeReferencia, ...] = ()
    """`responder`: las consultas que quedaron afuera porque la búsqueda no les recuperó nada."""

    @property
    def resumen(self) -> Resumen:
        medidas = [m for m in self.mediciones if m.puntaje is not None]
        puntajes = [m.puntaje for m in medidas if m.puntaje]
        # Una consulta de control no tiene hechos que cubrir: no baja la cobertura.
        cubren = [p for p in puntajes if not p.control]
        controles = [p for p in puntajes if p.control]
        citas = [c for p in puntajes for c in p.citas or ()]
        tiempos = [m.tiempo_ms for m in medidas]
        fila = self.configuracion_efectiva["por_capacidad"][self.capacidad.value]
        return Resumen(
            modelo=self.candidato.modelo,
            origen_chino=self.candidato.origen_chino,
            digest=self.configuracion_efectiva["digest"],
            corrida=self.corrida,
            mediciones=len(self.mediciones),
            aciertos=sum(m.puntaje.acierto for m in medidas if m.puntaje),
            errores=len(self.mediciones) - len(medidas),
            cobertura=sum(p.cobertura for p in cubren) / len(cubren) if cubren else None,
            con_invenciones=sum(bool(m.puntaje.invenciones) for m in medidas if m.puntaje),
            tiempo_mediano_ms=percentil(tiempos, 50),
            tiempo_p95_ms=percentil(tiempos, 95),
            tokens_entrada=percentil([m.tokens_entrada for m in medidas], 50),
            tokens_salida=percentil([m.tokens_salida for m in medidas], 50),
            tope_s=fila["parametros"].get("timeout_s"),
            citas_validas=sum(c.verificada for c in citas) / len(citas) if citas else None,
            controles=len(controles),
            controles_acertados=sum(p.acierto for p in controles),
        )


def elegir_ganador_y_no_chino(filas: Sequence[Resumen]) -> tuple[int | None, int | None]:
    """Los índices del ganador y del mejor no chino (`None` si no hay). Gana el de más acierto;
    a igual acierto, más cobertura, menos salidas con invenciones, menos tiempo mediano y, por
    último, el nombre: la elección no depende del orden de las filas."""
    def clave(i: int) -> tuple[float, float, int, float, str, str]:
        f = filas[i]
        return (-(f.acierto or 0.0), -(f.cobertura or 0.0), f.con_invenciones,
                f.tiempo_mediano_ms if f.tiempo_mediano_ms is not None else float("inf"),
                f.modelo, f.digest)

    compiten = [i for i, f in enumerate(filas) if f.acierto is not None]
    no_chinos = [i for i in compiten if not filas[i].origen_chino]
    return (min(compiten, key=clave, default=None), min(no_chinos, key=clave, default=None))


Validar = Callable[[Mapping[Capacidad, ConfiguracionDeCapacidad]], None]
FabricaDeCapa = Callable[[Mapping[Capacidad, ConfiguracionDeCapacidad]], CapaModelos]
Recuperar = Callable[[ConsultaDeReferencia], Sequence[str]]
"""Los textos de los fragmentos que la búsqueda de producción le da al modelo, en orden."""


@dataclass(frozen=True)
class _Caso:
    """Lo que corre cada candidato en cada repetición, armado una sola vez."""

    referencia_id: str
    invocar: Callable[[CapaModelos], ResultadoTexto]
    puntuar: Callable[[str], Puntaje]


class Banco:
    def __init__(
        self,
        *,
        validar: Validar,
        fabrica_capa: FabricaDeCapa,
        reloj: Callable[[], float] = time.monotonic,
        recuperar: Recuperar = lambda consulta: (),
    ) -> None:
        """`validar` es el de `web` (todas las filas, con los motores del entorno);
        `fabrica_capa`, `capa_para`, y `recuperar`, la búsqueda de la Conversación (solo
        `responder`): los arma `componer_banco`."""
        self._validar = validar
        self._fabrica_capa = fabrica_capa
        self._reloj = reloj
        self._recuperar = recuperar

    def comparar(
        self,
        vigente: Mapping[Capacidad, ConfiguracionDeCapacidad],
        referencia: ConjuntoDeReferencia,
        directorio: Path,
        candidatos: Sequence[CandidatoDeReferencia],
        *,
        repeticiones: int = 3,
        momento: datetime,
        progreso: Callable[[str, Medicion], None] = lambda modelo, medicion: None,
    ) -> list[Evaluacion]:
        """Corre cada candidato `repeticiones` veces sobre las Piezas de referencia de su
        capacidad que tienen algo que puntuar (en `responder`, sobre las consultas). Valida
        todos antes de correr el primero: una corrida dura horas.

        Raises:
            CandidatoInvalido: Alguna fila no pasa `validar` o `capa_para`.
            ReferenciaInvalida: No hay Piezas o consultas que puntuar para la capacidad.
        """
        [capacidad] = {c.capacidad for c in candidatos}
        preparados = [(c, *self.preparar(vigente, c)) for c in candidatos]
        casos, sin_fragmentos = (
            _consultas(referencia, self._recuperar) if capacidad is Capacidad.RESPONDER
            else ([
                _caso_de_pieza(capacidad, p, directorio, lambda: self._fabrica_capa(vigente))
                for p in _piezas(capacidad, referencia, directorio)
            ], ())
        )
        evaluaciones = []
        for candidato, capa, efectiva in preparados:
            mediciones = []
            for repeticion in range(1, repeticiones + 1):
                for caso in casos:
                    medicion = self._medir(capa, caso, repeticion)
                    progreso(candidato.modelo, medicion)
                    mediciones.append(medicion)
            evaluaciones.append(Evaluacion(
                capacidad, candidato, efectiva, momento.isoformat(), tuple(mediciones),
                sin_fragmentos,
            ))
        return evaluaciones

    def preparar(
        self, vigente: Mapping[Capacidad, ConfiguracionDeCapacidad],
        candidato: CandidatoDeReferencia,
    ) -> tuple[CapaModelos, dict[str, Any]]:
        """La capa con la fila del candidato en lugar de la vigente, y su configuración efectiva.

        Raises:
            CandidatoInvalido: La fila no pasa `validar` o `capa_para`.
        """
        fila = vigente[candidato.capacidad]
        filas = {**vigente, candidato.capacidad: replace(
            fila, modelo=candidato.modelo, parametros={**fila.parametros, **candidato.parametros},
        )}
        try:
            self._validar(filas)
            capa = self._fabrica_capa(filas)
        except ConfiguracionInvalida as error:
            raise CandidatoInvalido(candidato.modelo, error) from error
        fila = filas[candidato.capacidad]
        return capa, ConfiguracionEfectiva({candidato.capacidad: fila}).a_json()

    def _medir(self, capa: CapaModelos, caso: _Caso, repeticion: int) -> Medicion:
        inicio = self._reloj()
        try:
            resultado = caso.invocar(capa)
        except (LimiteExcedido, MotorNoDisponible) as error:
            return Medicion(caso.referencia_id, repeticion,
                            int((self._reloj() - inicio) * 1000), 0, 0, "", None, str(error))
        return Medicion(
            referencia_id=caso.referencia_id,
            repeticion=repeticion,
            tiempo_ms=int((self._reloj() - inicio) * 1000),
            tokens_entrada=resultado.tokens_entrada,
            tokens_salida=resultado.tokens_salida,
            texto=resultado.texto,
            puntaje=caso.puntuar(resultado.texto),
        )


def _consultas(
    referencia: ConjuntoDeReferencia, recuperar: Recuperar
) -> tuple[list[_Caso], tuple[ConsultaDeReferencia, ...]]:
    """Las consultas de ajuste con algo que puntuar: las de control, y las demás cuyas Piezas
    esperadas tienen hechos esperados o datos clave. Las de prueba no entran: son las de la
    corrida oficial (F-9). Los fragmentos se recuperan una sola vez, con la búsqueda vigente:
    no es lo que se compara. Una consulta sin fragmentos no entra (la Conversación no llama al
    modelo y dice que no hay evidencia) y se devuelve aparte, para avisarlo."""
    por_id = {p.referencia_id: p for p in referencia.piezas}
    casos = []
    sin_fragmentos = []
    for consulta in referencia.consultas:
        if consulta.conjunto != "ajuste":
            continue
        esperadas = [por_id[r] for r in consulta.piezas_esperadas if r in por_id]
        esperados = tuple(h for p in esperadas for h in (*p.hechos_esperados, *p.datos_clave))
        if not (consulta.sin_evidencia or esperados):
            continue
        if not (textos := tuple(recuperar(consulta))):
            sin_fragmentos.append(consulta)
            continue
        fragmentos = tuple(BloqueDatos(ETIQUETA_FRAGMENTO, t) for t in textos)
        pregunta = BloqueDatos(ETIQUETA_CONSULTA, consulta.texto)
        casos.append(_Caso(
            consulta.consulta_id,
            invocar=partial(_responder, pregunta, fragmentos),
            puntuar=partial(
                puntuar_respuesta, fragmentos=len(fragmentos), esperados=esperados,
                invenciones=tuple(i for p in esperadas for i in p.invenciones),
                control=consulta.sin_evidencia,
            ),
        ))
    if not casos:
        raise ReferenciaInvalida([
            ("consultas.csv no tiene consultas de ajuste que puntuar para responder: hacen falta"
            " consultas de control o Piezas esperadas con hechos_esperados o datos_clave, y que"
            " la búsqueda les recupere fragmentos.")
        ])
    return casos, tuple(sin_fragmentos)


def _responder(
    pregunta: BloqueDatos, fragmentos: tuple[BloqueDatos, ...], capa: CapaModelos
) -> ResultadoTexto:
    return capa.responder(consulta=pregunta, fragmentos=fragmentos)


def _caso_de_pieza(
    capacidad: Capacidad,
    pieza: PiezaDeReferencia,
    directorio: Path,
    capa_vigente: Callable[[], CapaModelos],
) -> _Caso:
    entrada = _entrada(capacidad, pieza, directorio, capa_vigente)
    return _Caso(
        pieza.referencia_id,
        invocar=(
            (lambda capa: capa.describir_imagen(imagen=entrada))
            if isinstance(entrada, BloqueBinario)
            else (lambda capa: capa.resumir(cuerpo=entrada))
        ),
        puntuar=partial(
            puntuar, esperados=(*pieza.hechos_esperados, *pieza.datos_clave),
            invenciones=pieza.invenciones,
        ),
    )


def _tipo_mime(archivo: str) -> str:
    return mimetypes.guess_type(archivo)[0] or "application/octet-stream"


def _piezas(
    capacidad: Capacidad, referencia: ConjuntoDeReferencia, directorio: Path
) -> list[PiezaDeReferencia]:
    piezas = [
        p for p in referencia.piezas
        if (p.hechos_esperados or p.datos_clave)
        and (capacidad is not Capacidad.DESCRIBIR_IMAGEN or es_imagen(_tipo_mime(p.archivo)))
    ]
    if not piezas:
        raise ReferenciaInvalida([
            f"piezas.csv no tiene Piezas que puntuar para {capacidad.value}: hacen falta"
            " hechos_esperados o datos_clave"
            + (" en una imagen." if capacidad is Capacidad.DESCRIBIR_IMAGEN else ".")
        ])
    return piezas


def _entrada(
    capacidad: Capacidad,
    pieza: PiezaDeReferencia,
    directorio: Path,
    capa_vigente: Callable[[], CapaModelos],
) -> BloqueBinario | BloqueDatos:
    """Lo que recibe el candidato. `resumir` resume el `texto_esperado` o, si la Pieza no lo
    tiene, el cuerpo que armaría producción con la capa vigente (la transcripción; de una foto,
    descripción más texto visible); se arma una sola vez: no es lo que se compara."""
    tipo_mime = _tipo_mime(pieza.archivo)
    if capacidad is Capacidad.RESUMIR and pieza.texto_esperado:
        return BloqueDatos(etiqueta=ETIQUETA_CUERPO_FICHA, contenido=pieza.texto_esperado)
    contenido = (directorio / pieza.archivo).read_bytes()
    if tipo_mime.startswith("text/"):
        return BloqueDatos(etiqueta=ETIQUETA_CUERPO_FICHA, contenido=contenido.decode("utf-8"))
    foto = es_imagen(tipo_mime)
    archivo = BloqueBinario(
        etiqueta=ETIQUETA_IMAGEN if foto else ETIQUETA_DOCUMENTO,
        contenido=contenido,
        tipo_mime=tipo_mime,
        nombre_archivo=Path(pieza.archivo).name,
    )
    if capacidad is Capacidad.DESCRIBIR_IMAGEN:
        return archivo
    capa = capa_vigente()
    transcripcion = capa.transcribir(
        archivo=archivo,
        plantilla=Plantilla.TRANSCRIBIR_IMAGEN if foto else Plantilla.TRANSCRIBIR_DOCUMENTO,
    )
    # Como `procesamiento`: de una foto, su descripción y su texto visible juntos.
    cuerpo = (
        cuerpo_de_la_foto(capa.describir_imagen(imagen=archivo).texto, transcripcion.texto)
        if foto else transcripcion.texto
    )
    return BloqueDatos(etiqueta=ETIQUETA_CUERPO_FICHA, contenido=transcripcion.digest or cuerpo)


_COLUMNAS = (
    "modelo", "origen_chino", "acierto", "cobertura", "con_invenciones", "errores",
    "mediciones", "tiempo_mediano_ms", "tiempo_p95_ms", "tokens_entrada", "tokens_salida",
    "citas_validas", "controles", "controles_acertados", "sobre_el_tope", "ganador",
    "mejor_no_chino", "digest", "corrida", "hash_referencia",
)


def _anteriores(
    directorio: Path, capacidad: Capacidad, hash_referencia: str
) -> tuple[list[Resumen], int]:
    """Los resúmenes de las corridas anteriores con la misma huella, y cuántas no la tienen
    (o no se pueden leer) y quedan afuera."""
    resumenes: list[Resumen] = []
    afuera = 0
    for corrida in sorted(directorio.glob(f"comparar-{capacidad.value}-*.json")):
        try:
            datos = json.loads(corrida.read_text(encoding="utf-8"))
            if datos["hash_referencia"] != hash_referencia:
                afuera += 1
                continue
            resumenes += [Resumen(**e["resumen"]) for e in datos["evaluaciones"]]
        except (ValueError, KeyError, TypeError):
            afuera += 1
    return resumenes, afuera


def _tabla(resumenes: Sequence[Resumen]) -> list[Resumen]:
    """La última corrida de cada candidato (modelo y configuración), por modelo."""
    ultimas: dict[tuple[str, str], Resumen] = {}
    for r in sorted(resumenes, key=lambda r: r.corrida):
        ultimas[r.modelo, r.digest] = r
    return sorted(ultimas.values(), key=lambda r: (r.modelo, r.digest))


def _segundos(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f}".replace(".", ",")


def _numero(valor: float | None) -> str:
    return "—" if valor is None else f"{valor:g}".replace(".", ",")


def tabla_comparacion(
    capacidad: Capacidad,
    filas: Sequence[Resumen],
    *,
    hash_referencia: str,
    afuera: int,
    sin_fragmentos: Sequence[ConsultaDeReferencia] = (),
) -> str:
    ganador, no_chino = elegir_ganador_y_no_chino(filas)
    lineas = [
        f"Comparación de modelos para {capacidad.value} (RF-28).",
        "",
        ("| Modelo | Origen chino | Acierto | Cobertura | Con invenciones | Errores | Mediciones"
        " | Tiempo mediano (s) | p95 (s) | Tokens de entrada | Tokens de salida | Citas válidas"
        " | Control | Sobre el tope | Ganador | Mejor no chino |"),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        *(
            fila_md(
                f.modelo, "sí" if f.origen_chino else "no", porcentaje(f.acierto),
                porcentaje(f.cobertura), f.con_invenciones, f.errores, f.mediciones,
                _segundos(f.tiempo_mediano_ms), _segundos(f.tiempo_p95_ms),
                _numero(f.tokens_entrada), _numero(f.tokens_salida),
                porcentaje(f.citas_validas),
                f"{f.controles_acertados} de {f.controles}" if f.controles else "—",
                "sí" if f.sobre_el_tope else "no", "**sí**" if i == ganador else "",
                "**sí**" if i == no_chino else "",
            )
            for i, f in enumerate(filas)
        ),
        "",
        f"Acierto: la salida cubre al menos el {UMBRAL_DE_ACIERTO:.0%} de los hechos esperados y"
        " datos clave y no trae ninguna invención de la lista; un error (tope o motor) cuenta"
        " como desacierto. Sin modelo juez. El tope de tiempo se informa: no descarta. En"
        " responder, además, la respuesta cita y todas sus Citas son de fragmentos recuperados;"
        " una consulta de control acierta solo si declara que no hay evidencia.".replace(
            "%", " %"
        ),
    ]
    if ganador is None:
        lineas.append("Ningún candidato llegó a medir: no hay ganador.")
    else:
        lineas.append(f"Ganador: {celda_md(filas[ganador].modelo)}.")
        if no_chino is None:
            lineas.append(
                "**No hay candidato no chino que haya medido:** sumá uno a candidatos.csv por si"
                " la FIE restringe los modelos de origen chino (C44)."
            )
        elif no_chino != ganador:
            lineas.append(
                f"El ganador es de origen chino: si la FIE restringe esos modelos (C44), queda"
                f" propuesto el mejor no chino, {celda_md(filas[no_chino].modelo)}."
            )
        else:
            lineas.append("El ganador no es de origen chino: es también el mejor no chino.")
    lineas.append(f"Solo se comparan corridas con hash_referencia {celda_md(hash_referencia)}.")
    if afuera:
        lineas.append(f"{afuera} corrida(s) anteriores con otra huella, o ilegibles, quedaron"
                      " afuera.")
    if sin_fragmentos:
        lineas.append(
            f"**{len(sin_fragmentos)} consulta(s) sin fragmentos recuperados quedaron afuera de"
            " esta corrida:** " + ", ".join(
                celda_md(c.consulta_id) + (" (de control)" if c.sin_evidencia else "")
                for c in sin_fragmentos
            ) + "."
        )
    return "\n".join(lineas) + "\n"


def escribir_comparacion(
    directorio: Path,
    capacidad: Capacidad,
    evaluaciones: Sequence[Evaluacion],
    *,
    hash_referencia: str,
    momento: datetime,
) -> tuple[list[Path], str]:
    """El JSON (con cada medición), el CSV y la tabla de la corrida. La tabla suma las
    corridas anteriores de `directorio` con la misma huella. Devuelve las rutas y la tabla."""
    directorio.mkdir(parents=True, exist_ok=True)
    # Las mismas para todos los candidatos: la búsqueda corre una vez por consulta.
    sin_fragmentos = evaluaciones[0].sin_fragmentos if evaluaciones else ()
    anteriores, afuera = _anteriores(directorio, capacidad, hash_referencia)
    filas = _tabla([*anteriores, *(e.resumen for e in evaluaciones)])
    ganador, no_chino = elegir_ganador_y_no_chino(filas)
    tabla = [
        {**asdict(f), "acierto": f.acierto, "sobre_el_tope": f.sobre_el_tope,
         "ganador": i == ganador, "mejor_no_chino": i == no_chino}
        for i, f in enumerate(filas)
    ]
    datos = {
        "medida": "comparacion",
        "capacidad": capacidad.value,
        "momento": momento.isoformat(),
        "hash_referencia": hash_referencia,
        "umbral_de_acierto": UMBRAL_DE_ACIERTO,
        "evaluaciones": [
            {
                "modelo": e.candidato.modelo,
                "origen_chino": e.candidato.origen_chino,
                "configuracion_efectiva": e.configuracion_efectiva,
                "resumen": asdict(e.resumen),
                "mediciones": [
                    {
                        **{k: v for k, v in asdict(m).items() if k != "puntaje"},
                        "acierto": None if m.puntaje is None else m.puntaje.acierto,
                        "cobertura": None if m.puntaje is None else m.puntaje.cobertura,
                        "presentes": None if m.puntaje is None else list(m.puntaje.presentes),
                        "faltan": None if m.puntaje is None else list(m.puntaje.faltan),
                        "invenciones": None if m.puntaje is None else list(m.puntaje.invenciones),
                        "citas": None if m.puntaje is None or m.puntaje.citas is None
                        else [asdict(c) for c in m.puntaje.citas],
                        "sin_evidencia": None if m.puntaje is None else m.puntaje.sin_evidencia,
                    }
                    for m in e.mediciones
                ],
            }
            for e in evaluaciones
        ],
        "tabla": tabla,
        "corridas_afuera": afuera,
        "sin_fragmentos": [
            {"consulta_id": c.consulta_id, "control": c.sin_evidencia} for c in sin_fragmentos
        ],
    }
    nombre = f"comparar-{capacidad.value}-{momento:%Y%m%dT%H%M%S}"
    rutas = [directorio / f"{nombre}.{extension}" for extension in ("json", "csv", "md")]
    markdown = tabla_comparacion(capacidad, filas, hash_referencia=hash_referencia, afuera=afuera,
                                 sin_fragmentos=sin_fragmentos)
    rutas[0].write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    rutas[1].write_bytes(escribir_csv(
        _COLUMNAS, ([*(t[c] for c in _COLUMNAS[:-1]), hash_referencia] for t in tabla)
    ))
    rutas[2].write_text(markdown, encoding="utf-8")
    return rutas, markdown


def eleccion_del_banco(directorio: Path, capacidad: str) -> tuple[str | None, str | None] | None:
    """El ganador y el mejor no chino de la última corrida de `capacidad` en `directorio` (la
    nota de la pantalla de Modelos, P7a); `None` si no hay corrida legible. La tabla de cada
    corrida ya junta las anteriores con la misma huella."""
    for corrida in sorted(directorio.glob(f"comparar-{capacidad}-*.json"), reverse=True):
        try:
            tabla = json.loads(corrida.read_text(encoding="utf-8"))["tabla"]
            ganador = next((f["modelo"] for f in tabla if f["ganador"]), None)
            no_chino = next((f["modelo"] for f in tabla if f["mejor_no_chino"]), None)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return ganador, no_chino
    return None
