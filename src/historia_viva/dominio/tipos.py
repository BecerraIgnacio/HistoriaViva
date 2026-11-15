"""Tipos compartidos por todos los paquetes del prototipo.

Todos son `dataclass` congeladas: se pasan entre la API, el Modulo de Fichas, el
procesamiento y la instrumentacion sin que ninguno pueda mutar lo que otro le entrego.

La autoridad sobre nombres y tipos de campo es `docs/proyecto/modelo-datos.md` §2.2 y
§2.3; la lista de campos del frontmatter es `docs/proyecto/spec.md` §4.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from historia_viva.dominio.errores import ConsultaInvalida
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.originales import ClaseDeOriginal

__all__ = [
    "CAMPOS_DUROS",
    "CAMPOS_OBLIGATORIOS",
    "CambioPosterior",
    "CamposDuros",
    "CamposFicha",
    "DetallePieza",
    "EdicionFicha",
    "EtiquetaHallada",
    "FichaIndexable",
    "FiltroDeBusqueda",
    "FuenteDeFecha",
    "FichaVigente",
    "MetodoAutenticacion",
    "ModoValidacion",
    "OrigenVersion",
    "OriginalGuardado",
    "PiezaBuscable",
    "PiezaDelPeriodo",
    "PiezaMedible",
    "PiezaPublicada",
    "Procedencia",
    "ResultadoDeEdicion",
    "ResumenPieza",
    "ViaDeIngreso",
    "procedencia_de_campos",
]

ModoValidacion = Literal["individual", "bloque"]
"""Como se firmo la Ficha. El prototipo solo produce `individual` (RF-18, fuera de alcance
la validacion en bloque)."""

OrigenVersion = Literal["modelo", "persona", "agente_externo", "sistema"]
"""Quien escribio esta version de la Ficha (`modelo-datos.md` §2.3). Sin este campo no se
distingue lo propuesto de lo firmado, y la instrumentacion no tiene contra que comparar."""


class ViaDeIngreso(StrEnum):
    """Por dónde entró la Pieza: un archivo que subió una persona o un Aporte (DEC-140).

    «Procedencia» es otra cosa: de dónde sale cada dato de la Ficha.
    """

    ARCHIVO = "archivo"
    APORTE = "aporte"


@dataclass(frozen=True)
class EtiquetaHallada:
    """Una etiqueta de control de la lista cerrada en un Aporte (DEC-133): cuál y dónde, nunca
    lo que escribió el agente."""

    etiqueta: str
    """Como figura en la lista cerrada (`### Instruction:`), no la variante que llegó."""

    seccion: str
    linea: int
    """Desde 1, dentro de su sección."""


@dataclass(frozen=True)
class FiltroDeBusqueda:
    """Qué Fragmentos entran en una búsqueda, antes de ordenar por similitud (RF-20).

    Todo es opcional y una lista vacía no filtra. Las Fechas se solapan, bordes incluidos: entra la
    Pieza cuyo rango toca `[desde, hasta]`. Con varias Carreras entra la Pieza que tenga alguna; la
    Carrera es un filtro, no un permiso (DEC-125).
    """

    desde: date | None = None
    hasta: date | None = None
    tipos: tuple[str, ...] = ()
    tipos_actividad: tuple[str, ...] = ()
    carreras: tuple[str, ...] = ()
    via_ingreso: ViaDeIngreso | None = None

    def validar(self) -> None:
        """Antes de llegar a `indice`: Postgres no admite un NUL en `text`.

        Raises:
            ConsultaInvalida: `desde` es posterior a `hasta`, o un valor trae un NUL; `campo`
                es el parámetro del pedido.
        """
        if self.desde and self.hasta and self.desde > self.hasta:
            raise ConsultaInvalida("La fecha «desde» es posterior a «hasta».", campo="desde")
        for campo, valores in (
            ("tipo", self.tipos),
            ("tipo_actividad", self.tipos_actividad),
            ("carrera", self.carreras),
        ):
            if any("\x00" in valor for valor in valores):
                raise ConsultaInvalida(f"El campo {campo} trae un carácter nulo.", campo=campo)


class MetodoAutenticacion(StrEnum):
    """Con qué sesión firmó una persona: `dev` es una firma de ensayo (DEC-37, RNF-07)."""

    OIDC = "oidc"
    DEV = "dev"


CAMPOS_OBLIGATORIOS: tuple[str, ...] = (
    "tipo",
    "tipo_actividad",
    "fecha_inicio",
    "fecha_fin",
    "precision_fecha",
    "origen",
    "propietario",
    "resumen",
    "archivo_original",
)
"""Los nueve campos sin los cuales una Ficha **no puede pasar a Validada** (DEC-126).

Son los mismos para toda clase de Pieza; ni el cuerpo ni el Contexto cuentan (DEC-13).

Es la lista que separa `Validada` de `Incompleta` (`spec.md` §4; invariante 5 de
`modelo-datos.md` §6, materializada en el `CHECK pieza_validada_completa`). `validada_por`,
`validada_el` y `modo_validacion` tambien son obligatorios para validar, pero **no
aparecen aca**: no los completa quien valida, los escribe el sistema al guardar (invariante
4). `archivo_original` siempre esta presente en el prototipo porque toda Pieza nace de un
archivo subido; se conserva en la lista porque en el MVP un aporte de un Agente externo
llega sin archivo y ahi si puede faltar.
"""


@dataclass(frozen=True, kw_only=True)
class CamposFicha:
    """El frontmatter de la Ficha, en orden de `spec.md` §4.

    Es la proyeccion consultable de la Ficha, no la Ficha: el markdown completo vive en
    `FichaVigente.cuerpo` mas estos campos serializados como YAML (ADR-0001).

    Todo campo que una Pieza `Recibida` todavia no puede conocer admite `None`. Eso es
    exactamente lo que vuelve `Incompleta` a una Ficha que alguien trabajo sin conocer el
    dato (`spec.md` §4).
    """

    id: UUID
    """Identificador estable de la Pieza. Es `pieza.pieza_id` y viaja a la API."""

    tipo: str | None = None
    """Taxonomia de la SEU. Texto libre validado contra una lista configurable, no un enum
    del codigo: la taxonomia todavia no existe (consulta C4)."""

    tipo_actividad: str | None = None
    """Del catálogo `tipo_actividad` (DEC-127); «No es una actividad» vale."""

    fecha_inicio: date | None = None
    """Inicio del rango. Ya normalizado por `derivar_rango`."""

    fecha_fin: date | None = None
    """Fin del rango. Se deriva de `precision_fecha`; no se pide dos veces."""

    precision_fecha: PrecisionFecha | None = None
    """Con que resolucion se conoce la Fecha. Viaja en el contrato con el Agente 1."""

    origen: str | None = None
    """De donde proviene la Pieza."""

    propietario: str | None = None
    """Quién custodia o aportó la Pieza (DEC-126): texto libre, heredable del Lote."""

    autor: str | None = None
    """Quién produjo la Pieza. Opcional; «Desconocido» vale (DEC-126)."""

    carreras: tuple[str, ...] = ()
    """`carrera_id` de la lista oficial, sin repetir y ordenados. Opcionales (DEC-126); son un
    filtro, no un permiso (DEC-125)."""

    lote: UUID | None = None
    """Lote al que pertenece; `None` en una Pieza suelta."""

    resumen: str | None = None
    """Resumen propuesto por la capacidad `resumir` y firmado por una persona."""

    estado: Estado = Estado.RECIBIDA
    """Estado de la Pieza. Lo escribe el Modulo de Fichas, nunca la pantalla."""

    validada_por: UUID | None = None
    """Persona identificada que firmo. Lo escribe el Modulo de Fichas al guardar."""

    validada_el: datetime | None = None
    """Momento de la firma. Lo escribe el Modulo de Fichas al guardar."""

    modo_validacion: ModoValidacion | None = None
    """`individual` en todo el prototipo."""

    archivo_original: str | None = None
    """Identificador del original guardado. Es `OriginalGuardado.identificador`."""

    alerta_inyeccion: bool = False
    """Marca de la capa 2 de ADR-0003. Alerta, no bloquea: no participa de ninguna
    restriccion de estado."""

    publicable: bool = True
    """Falsa, la Pieza no sale por `/v1` ni es fuente de efemérides o Relatos nuevos; las
    personas la siguen viendo (DEC-143, auditoría decisión 15). La cambia una decisión de la SEU
    (C31), no el sistema."""


class Procedencia(StrEnum):
    """De dónde sale el valor de un campo de la Ficha (DEC-19)."""

    LOTE = "lote"
    MODELO = "modelo"
    PERSONA = "persona"
    VACIO = "vacio"


def procedencia_de_campos(
    vigente: CamposFicha,
    propuesta: CamposFicha,
    heredados: Collection[str],
    de_la_persona: Collection[str] = (),
    resumenes_rehechos: Collection[str] = (),
) -> dict[str, Procedencia]:
    """La procedencia de cada obligatorio que alguien escribe, contra la v1 inmutable: lo que
    sigue igual que en la v1 es de una persona si lo escribió con la Pieza Recibida (152), del
    Lote si se heredó y del modelo si no; lo que cambió, de una persona. `archivo_original` no
    lo escribe nadie. Un resumen igual a uno de `resumenes_rehechos` también lo propuso un
    modelo: se rehízo sin las Afirmaciones rechazadas y la persona lo guardó sin cambios (146)."""
    procedencias = {}
    for campo in (c for c in CAMPOS_OBLIGATORIOS if c != "archivo_original"):
        valor = getattr(vigente, campo)
        if valor is None:
            procedencias[campo] = Procedencia.VACIO
        elif campo == "resumen" and valor in resumenes_rehechos:
            procedencias[campo] = Procedencia.MODELO
        elif valor != getattr(propuesta, campo) or campo in de_la_persona:
            procedencias[campo] = Procedencia.PERSONA
        else:
            procedencias[campo] = Procedencia.LOTE if campo in heredados else Procedencia.MODELO
    return procedencias


CAMPOS_DUROS: tuple[str, ...] = (
    "tipo",
    "tipo_actividad",
    "fecha_inicio",
    "fecha_fin",
    "precision_fecha",
    "origen",
    "propietario",
    "autor",
    "carreras",
)
"""Los campos que ningún modelo propone y completa una persona (decisión del 15-sep): se pueden
escribir con la Pieza todavía Recibida (152)."""


@dataclass(frozen=True, kw_only=True)
class CamposDuros:
    """Lo que la pantalla partida manda con la Pieza Recibida: los campos duros y el Contexto.
    Sin cuerpo ni resumen, que todavía no existen, y sin nada que firme (ADR-0002)."""

    tipo: str | None = None
    tipo_actividad: str | None = None
    fecha_inicio: date | None = None
    precision_fecha: PrecisionFecha | None = None
    fecha_fin: date | None = None
    """Solo con `precision_fecha = RANGO`, como en `EdicionFicha`."""
    origen: str | None = None
    propietario: str | None = None
    autor: str | None = None
    carreras: tuple[str, ...] = ()
    contexto: str = ""


@dataclass(frozen=True, kw_only=True)
class EdicionFicha:
    """Lo que la pantalla partida manda al guardar.

    Deliberadamente **no** trae `estado`, `validada_por`, `validada_el`,
    `modo_validacion`, `id`, `lote` ni `archivo_original`: esos no los edita una persona,
    los decide el Modulo de Fichas. Que no esten en este tipo es la forma estructural de
    impedir que la pantalla firme una Ficha por su cuenta (ADR-0002).

    Tampoco trae `fecha_fin` salvo cuando `precision_fecha` es `RANGO`: con las otras
    cuatro precisiones el fin se deriva (`derivar_rango`).
    """

    tipo: str | None = None
    fecha_inicio: date | None = None
    precision_fecha: PrecisionFecha | None = None
    fecha_fin: date | None = None
    """Solo se envia con `precision_fecha = RANGO` (decision D-P1). Con cualquier otra
    precision debe ser `None`: el Modulo de Fichas la deriva."""

    origen: str | None = None
    tipo_actividad: str | None = None
    propietario: str | None = None
    autor: str | None = None
    carreras: tuple[str, ...] = ()
    """La lista entera: la que falta se quita (DEC-143)."""
    resumen: str | None = None
    publicable: bool | None = None
    """`None` conserva el de la vigente. Lo manda solo el RGC con los controles de datos
    personales encendidos (125, C31); lo exige `validacion`."""

    cuerpo: str
    """La transcripcion o descripcion ya corregida (user story 14)."""

    contexto: str
    """Lo que aporto la persona (user story 15). Llega separado del cuerpo y el Modulo de
    Fichas los une con `CuerpoDeFicha`."""


@dataclass(frozen=True, kw_only=True)
class ResultadoDeEdicion:
    """Que quedo despues de guardar una edicion.

    `estado_nuevo` es `Validada` si `campos_faltantes` esta vacio, e `Incompleta` si no.
    Guardar incompleto **siempre se permite**: es el punto de la user story 18 y lo que la
    sesion con la SEU mide (user story 24). `estado_anterior` le dice a `validacion` si
    desindexar una Validada reabierta (DEC-91).
    """

    ficha: FichaVigente
    """La version que quedo vigente."""

    estado_anterior: Estado
    estado_nuevo: Estado
    campos_faltantes: list[str] = field(default_factory=list)
    """Nombres de `CAMPOS_OBLIGATORIOS` que quedaron vacios, en el orden de esa tupla.
    Vacio si y solo si `estado_nuevo` es `Validada`."""


@dataclass(frozen=True, kw_only=True)
class ResumenPieza:
    """Una fila de la lista "que subi y en que estado esta" (user story 21).

    Es lo minimo para pintar la cola sin abrir cada Ficha. No trae cuerpo.
    """

    pieza_id: UUID
    estado: Estado
    tipo: str | None
    resumen: str | None
    nombre_archivo: str
    """Nombre del archivo tal como lo subio la persona. Es como reconoce su Pieza en la
    lista antes de que exista un resumen."""

    creada_el: datetime
    actualizada_el: datetime
    campos_faltantes: list[str] = field(default_factory=list)
    """Que falta en esta Pieza, para saber cuanto queda por hacer sin abrirla (user story
    16). Vacio en una Pieza `Validada`."""
    lote_id: UUID | None = None
    creada_por: UUID | None = None
    """Quién la subió; nula en un Aporte (DEC-123)."""
    requiere_analisis_humano: bool = False
    """Todas sus versiones son `sistema`: un video o una completada a mano (034, 151). La traen el
    listado y `obtener_pieza`."""
    alerta_inyeccion: bool = False
    """Proyección de la Ficha vigente (DEC-26): la fila lleva «⚠ Alerta» (109)."""


@dataclass(frozen=True, kw_only=True)
class DetallePieza:
    """Lo que la pantalla sondea de una Pieza: su fila de la lista mas los datos del original.

    Es lo que `GET /api/piezas/{pieza_id}` devuelve mientras la Pieza esta `Recibida`
    (user story 3).
    """

    fila: ResumenPieza
    tipo_mime: str
    tamano_bytes: int
    tipo_actividad: str | None
    propietario: str | None
    autor: str | None = None
    carreras: tuple[str, ...] = ()
    lote_id: UUID | None = None
    campos_heredados: tuple[str, ...] = ()
    """Los campos que copió de su Lote (DEC-126); vacío en una Pieza suelta."""
    campos_de_la_persona: tuple[str, ...] = ()
    """Los campos duros que escribió una persona con la Pieza Recibida (152)."""
    clase_original: str | None = None
    huella: str | None = None
    """El SHA-256 del original; en un Aporte, el del texto recibido."""
    via_ingreso: ViaDeIngreso = ViaDeIngreso.ARCHIVO
    metadata_original: dict[str, object] | None = None
    """Los datos técnicos leídos del archivo (video: la lista blanca de `ffprobe`, DEC-16)."""


@dataclass(frozen=True, kw_only=True)
class FichaVigente:
    """La version vigente de la Ficha de una Pieza.

    Una Pieza tiene una o mas Fichas —una por version, una sola vigente (ADR-0001,
    RNF-07)—. La version 1 es siempre la propuesta por el modelo; las siguientes las firma
    una persona. `obtener_propuesta` devuelve la version 1 aunque ya no sea la vigente:
    es contra ella que la instrumentacion compara (user story 23).
    """

    ficha_id: UUID
    """Identifica **una version**, no la Ficha conceptual."""

    pieza_id: UUID
    version: int
    """Empieza en 1."""

    origen_version: OrigenVersion
    campos: CamposFicha
    cuerpo: str
    """El markdown del cuerpo, sin el frontmatter."""

    editada_por: UUID | None
    """`None` si `origen_version` no es `persona` (invariante 11)."""

    editada_el: datetime

    metodo_autenticacion: MetodoAutenticacion | None = None
    """Con qué sesión se guardó una versión `persona`: `dev` es una firma de ensayo (DEC-139)."""

    informe_inyeccion: Mapping[str, Any] | None = None
    """El `InformeInyeccion.a_json()` de esta versión, tal como se guardó (DEC-26); la pantalla
    partida pinta sus hallazgos (109)."""


@dataclass(frozen=True, kw_only=True)
class OriginalGuardado:
    """El archivo original tal como llego, ya persistido.

    Se conserva siempre y la Ficha guarda su identificador. En el prototipo el
    almacenamiento es local en vez del Drive institucional, y nada fuera del Modulo de
    Fichas sabe cual de los dos es (RNF-09, decision diferida D5).
    """

    identificador: str
    """Lo que se guarda en `CamposFicha.archivo_original`. Opaco fuera del modulo."""

    nombre_archivo: str
    """Nombre con el que la persona subio el archivo."""

    tipo_mime: str
    """`image/jpeg`, `application/pdf`, `application/vnd.openxmlformats-officedocument...`."""

    tamano_bytes: int
    creado_el: datetime
    huella: str | None
    """SHA-256 del archivo (`huella_original`); es el `ETag` con que se sirve."""


@dataclass(frozen=True, kw_only=True)
class FichaIndexable:
    """La Ficha vigente de una Pieza Validada no retirada, con lo que filtra sus fragmentos.

    Los obligatorios no son opcionales: una Validada los tiene todos (DEC-126).
    """

    pieza_id: UUID
    ficha_id: UUID
    tipo: str
    tipo_actividad: str
    carreras: tuple[str, ...]
    fecha_inicio: date
    fecha_fin: date
    precision_fecha: PrecisionFecha
    clase_original: ClaseDeOriginal
    via_ingreso: ViaDeIngreso
    cuerpo: str
    resumen: str
    """El firmado: es el único fragmento de un video o un Aporte con el cuerpo vacío (DEC-05)."""


@dataclass(frozen=True, kw_only=True)
class PiezaDelPeriodo:
    """Una Validada no retirada cuya Fecha solapa un período, con su Ficha vigente: lo que el
    informe congela (R-21, R-22)."""

    pieza_id: UUID
    ficha_id: UUID
    tipo_actividad: str
    fecha_inicio: date
    fecha_fin: date


@dataclass(frozen=True, kw_only=True)
class CambioPosterior:
    """Una Pieza congelada en un informe que ya no está como se congeló: se retiró, dejó de
    estar Validada o su Ficha vigente es otra (R-22)."""

    pieza_id: UUID
    nombre_archivo: str
    retirada: bool


@dataclass(frozen=True, kw_only=True)
class PiezaMedible:
    """Una Pieza con su Ficha vigente y su huella: lo que `banco` compara con el índice (M2)."""

    pieza_id: UUID
    validada: bool
    """Validada y no retirada hoy."""
    huella: str | None
    ficha: FichaVigente | None
    """`None` si todavía no tiene Ficha."""


@dataclass(frozen=True, kw_only=True)
class PiezaBuscable:
    """Una Pieza Validada no retirada cuya Ficha vigente es `ficha_id`: lo que la búsqueda puede
    mostrar de un fragmento hallado, leído de `fichas` y no del índice (DEC-72)."""

    pieza_id: UUID
    ficha_id: UUID
    tipo: str
    tipo_actividad: str
    carreras: tuple[str, ...]
    fecha_inicio: date
    fecha_fin: date
    precision_fecha: PrecisionFecha
    clase_original: ClaseDeOriginal
    via_ingreso: ViaDeIngreso
    resumen: str
    nombre_archivo: str | None
    validada_el: datetime
    """Cuándo una persona firmó la Ficha vigente: el `validada_el` de `/v1`."""


@dataclass(frozen=True, kw_only=True)
class PiezaPublicada(PiezaBuscable):
    """Lo que `GET /v1/piezas/{id}` publica de una Validada no retirada (contrato, `Pieza`)."""

    origen: str
    lote_id: UUID | None
    validada_por: str
    """La parte local del email de quien firmó la Ficha vigente (contrato, SUPUESTO)."""
    modo_validacion: ModoValidacion
    cuerpo: str
    """El cuerpo íntegro de la Ficha vigente, con el Contexto."""


@dataclass(frozen=True)
class FuenteDeFecha:
    """Una Pieza con su Ficha vigente, como la ve una efeméride: de qué fecha es y qué dice en
    pocas palabras (spec, `fichas/consultas.py`). La leen `validadas_con_fecha` y `fuentes`."""

    pieza_id: UUID
    ficha_id: UUID
    tipo: str | None
    fecha_inicio: date | None
    fecha_fin: date | None
    precision_fecha: PrecisionFecha | None
    resumen: str | None
    alerta_inyeccion: bool
    publicable: bool
    validada: bool
    """Validada y no retirada. Fuente de una efeméride o un Relato nuevos solo si además es
    `publicable` (DEC-143)."""
