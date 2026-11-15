"""Ingresar una Pieza: el original, la Pieza y su trabajo quedan juntos o no queda nada.

Orquesta y no tiene tablas: abre la transacción y se la pasa a `fichas` y a la cola, que no
abren conexiones (historia 54). El original se escribe a un temporal sin tocar la base y su
`os.replace` ocurre dentro de la transacción, antes del `COMMIT` (historia 231); ante cualquier
error antes del `COMMIT`, se descarta. El trabajo nace con la instantánea de la configuración que
`ingesta` le pide a `configuracion` (DEC-87): la cola no lee la fila.

Cada ingesta deja su resultado en `auditoria` (historia 290): la Pieza que entra, `pieza.ingreso`
en su misma transacción; el archivo que no entra (no admitido, duplicado o rechazado por su
Lote), `archivo.rechazo` en una transacción propia, porque la de la subida se revierte (DEC-106).

Un video no tiene plan de modelos (ADR-0005): sobre el temporal corre el `analizar` que inyecta
`composicion` (DEC-94), acá se aplica el tope de 3 h (DEC-108) y la Pieza nace `Pendiente` con
sus datos técnicos y sin trabajo.

Las subidas de video en vuelo se cuentan por persona (DEC-68, DEC-146 punto 13): la tercera
simultánea se rechaza antes de leer el cuerpo y el cupo vuelve al terminar, bien o mal.

Un Aporte de un Agente externo es otra puerta (DEC-17): su texto es el original de una Pieza
`texto` que nace `Pendiente` con la v1 `agente_externo` y sin trabajo. Es idempotente por
`(agente, clave)` con un candado de transacción y la huella del pedido ya normalizado (R-36).
Antes de todo eso, un Aporte con etiquetas de control se rechaza sin guardar nada ni consumir la
clave (DEC-133): solo queda `aporte.rechazo`, en una transacción propia. La verificación la
inyecta `composicion` (`EtiquetasDeControl`): `ingesta` no importa `chequeo_inyeccion`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO, Protocol
from uuid import UUID, uuid4

from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Bd, Tx
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.errores import (
    ErrorHistoriaViva,
    LoteLleno,
    LoteNoEncontrado,
    PermisoInsuficiente,
    PiezaDuplicada,
    PiezaNoEncontrada,
    TopeDeReprocesos,
    TransicionInvalida,
)
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.originales import ClaseDeOriginal, Contenedor
from historia_viva.dominio.tipos import (
    DetallePieza,
    EtiquetaHallada,
    FichaVigente,
    ViaDeIngreso,
)
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.lotes import Lotes
from historia_viva.originales import (
    AlmacenDeOriginales,
    ArchivoDemasiadoGrande,
    ArchivoIlegible,
    OriginalTemporal,
    RechazoDeAdmision,
    TipoNoAdmitido,
)
from historia_viva.procesamiento.cola import (
    REPROCESABLES,
    TOPE_DE_REPROCESOS,
    Cola,
    TipoDeTrabajo,
)

__all__ = [
    "SUBIDAS_DE_VIDEO_POR_PERSONA",
    "TOPE_VIDEO_S",
    "AcuseDeAporte",
    "AnalisisDeVideoNoDisponible",
    "AnalizarVideo",
    "AporteRechazado",
    "AporteRecibido",
    "ClaveReutilizada",
    "DatosDelAporte",
    "DatosTecnicos",
    "DemasiadasSubidas",
    "EtiquetasDeControl",
    "FuenteInexistente",
    "Ingesta",
    "VideoDemasiadoLargo",
    "normalizar_linea",
    "normalizar_texto",
    "normalizar_titulo",
]

TOPE_VIDEO_S = 3 * 60 * 60
"""Exactamente 3 h se acepta (DEC-16, DEC-108)."""

SUBIDAS_DE_VIDEO_POR_PERSONA = 2
"""Subidas de video en vuelo por persona (DEC-68); las demás clases no tienen tope acá."""

_DEMUXER: Mapping[str, Contenedor] = {
    "video/mp4": "mov",
    "video/quicktime": "mov",
    "video/webm": "matroska,webm",
}
"""El demuxer que se fuerza sale de la firma de los primeros 4 KiB, que decidió el MIME."""


class DatosTecnicos(Protocol):
    @property
    def duracion_s(self) -> float: ...

    def a_json(self) -> dict[str, Any]: ...


AnalizarVideo = Callable[[Path, Contenedor], DatosTecnicos]
"""Lee los datos técnicos de un video: `ArchivoIlegible` si no puede leerlo y
`AnalisisDeVideoNoDisponible` si no hay con qué (lo traduce `composicion`)."""


EtiquetasDeControl = Callable[[Mapping[str, str]], Sequence[EtiquetaHallada]]
"""Las etiquetas de control de la lista cerrada en cada sección (`ingesta` pasa una sola, el
texto entero; DEC-133); vacío si no trae ninguna. La pasa `composicion`
(`fichas.postgres.etiquetas_de_control`)."""

TOPE_DE_ETIQUETAS_AUDITADAS = 50
"""Las que se nombran en `aporte.rechazo`: un cuerpo con una por línea no infla el registro."""


class VideoDemasiadoLargo(RechazoDeAdmision):
    def __init__(self, tope_segundos: int) -> None:
        super().__init__(
            f"El video dura más de lo permitido (máximo {tope_segundos // 3600} h).",
            clase=ClaseDeOriginal.VIDEO,
        )
        self.tope_segundos = tope_segundos


class AnalisisDeVideoNoDisponible(ErrorHistoriaViva):
    def __init__(self) -> None:
        super().__init__("El servidor no puede leer videos ahora: falta ffprobe.")


class DemasiadasSubidas(ErrorHistoriaViva):
    def __init__(self, maximo: int) -> None:
        super().__init__(
            f"Ya tenés {maximo} videos subiendo. Esperá a que termine uno y probá de nuevo."
        )
        self.maximo = maximo


class _Subidas:
    """Cuántas subidas de video tiene en vuelo cada persona.

    shortcut: vive en la memoria del proceso; con más de un proceso de `web` el tope se
    multiplica por proceso, y habría que llevarlo a la base o a un candado compartido.
    """

    def __init__(self, maximo: int) -> None:
        self._maximo = maximo
        self._candado = threading.Lock()
        self._en_vuelo: Counter[UUID] = Counter()

    @contextmanager
    def cupo(self, persona_id: UUID) -> Iterator[None]:
        with self._candado:
            if self._en_vuelo[persona_id] >= self._maximo:
                raise DemasiadasSubidas(self._maximo)
            self._en_vuelo[persona_id] += 1
        try:
            yield
        finally:
            with self._candado:
                self._en_vuelo[persona_id] -= 1
                if not self._en_vuelo[persona_id]:
                    del self._en_vuelo[persona_id]


@dataclass(frozen=True)
class AporteRecibido:
    """Lo que manda un Agente externo, ya validado en su forma y normalizado (R-36)."""

    titulo: str
    cuerpo: str
    tipo_contenido: str
    canal: str | None
    piezas_fuente: tuple[UUID, ...]
    fragmentos_fuente: tuple[UUID, ...]
    fecha_publicacion: date | None
    url_publicacion: str | None

    @property
    def texto(self) -> str:
        """El original de la Pieza y el cuerpo de su v1: el título y el cuerpo recibidos."""
        return f"{self.titulo}\n\n{self.cuerpo}"

    def huella(self) -> str:
        """SHA-256 del JSON canónico: misma huella con la misma clave es un reintento."""
        canonico = json.dumps(
            asdict(self), default=str, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AcuseDeAporte:
    aporte_id: UUID
    pieza_id: UUID
    recibido_el: datetime
    repetido: bool
    """La clave ya estaba con la misma huella: no se creó nada."""


@dataclass(frozen=True)
class DatosDelAporte:
    """Con qué llegó un Aporte, para que quien valida sepa quién lo mandó y por dónde (P5f)."""

    agente_id: UUID
    recibido_el: datetime
    tipo_contenido: str
    canal: str | None


class ClaveReutilizada(ErrorHistoriaViva):
    def __init__(self) -> None:
        super().__init__("La clave de idempotencia ya fue usada con un cuerpo distinto.")


class AporteRechazado(ErrorHistoriaViva):
    """Un Aporte con etiquetas de control (DEC-133): no se guardó nada y la clave sigue libre,
    así el agente lo reenvía corregido. Dice cuáles y dónde, nunca el texto."""

    def __init__(self, etiquetas: tuple[EtiquetaHallada, ...]) -> None:
        super().__init__("El aporte trae etiquetas de control y no se acepta.")
        self.etiquetas = etiquetas


class FuenteInexistente(ErrorHistoriaViva):
    """Piezas fuente que no existen o no están Validadas: no se dice cuál de las dos."""

    def __init__(self, piezas: list[UUID]) -> None:
        super().__init__("Hay Piezas fuente que no existen o no están Validadas.")
        self.piezas = piezas


_log = logging.getLogger(__name__)

_MOTIVOS: Mapping[type[RechazoDeAdmision], str] = {
    TipoNoAdmitido: "tipo-no-admitido",
    ArchivoDemasiadoGrande: "archivo-demasiado-grande",
    ArchivoIlegible: "archivo-ilegible",
    VideoDemasiadoLargo: "video-demasiado-largo",
}
"""El motivo de `archivo.rechazo` es el `type` del problema que recibe quien sube; el `motivo`
del catálogo, si lo hay, va como `causa`."""

_MOTIVOS_DEL_LOTE: Mapping[type[BaseException], str] = {
    LoteNoEncontrado: "lote-no-encontrado",
    PermisoInsuficiente: "permiso-insuficiente",
    LoteLleno: "lote-lleno",
}
"""Los rechazos de `Lotes.con_lugar`, que llegan con el archivo ya guardado (DEC-106)."""


class Ingesta:
    def __init__(
        self,
        *,
        bd: Bd,
        originales: AlmacenDeOriginales,
        fichas: ModuloFichasPostgres,
        cola: Cola,
        configuracion: ConfiguracionDeModelos,
        auditoria: RegistroDeAuditoria,
        lotes: Lotes,
        avisar: Callable[[], None],
        analizar_video: AnalizarVideo | None,
        etiquetas_de_control: EtiquetasDeControl,
    ) -> None:
        self._bd = bd
        self._etiquetas_de_control = etiquetas_de_control
        self._analizar_video = analizar_video  # `None`: sin `ffprobe`, el video responde 503
        self._lotes = lotes
        self._auditoria = auditoria
        self._configuracion = configuracion
        self._originales = originales
        self._fichas = fichas
        self._cola = cola
        self._avisar = avisar  # despierta al trabajador, siempre después del `COMMIT`
        self._subidas = _Subidas(SUBIDAS_DE_VIDEO_POR_PERSONA)

    def recibir_archivo(
        self,
        *,
        flujo: BinaryIO,
        nombre: str,
        por: Actor,
        tamano_minimo: int = 0,
        lote_id: UUID | None = None,
        puede_en_lote_ajeno: bool = False,
    ) -> tuple[DetallePieza, bool]:
        """Sube un archivo como Pieza `Recibida`, suelta o de `lote_id`, y encola su
        procesamiento. Devuelve la Pieza y si ya estaba en ese Lote: el mismo original en el
        mismo Lote no crea nada y devuelve la que ya estaba (reintento tras un corte, DEC-20).

        `por` es la persona que sube: queda como `creada_por` y como actor de `pieza.ingreso`.
        En un Lote, la Pieza hereda su Contexto (DEC-126); si `por` no creó el Lote, carga solo
        con `puede_en_lote_ajeno` (R-06).

        `tamano_minimo` es lo que el pedido declara que trae como mínimo (ver
        `AlmacenDeOriginales.guardar_en_streaming`).

        Raises:
            RechazoDeAdmision: Si `originales` no admite el archivo.
            PiezaDuplicada: Si el mismo original ya es una Pieza suelta o de otro Lote (DEC-20).
            ConfiguracionInvalida: Si una fila de la configuración no pasa `validar`.
            LoteNoEncontrado, PermisoInsuficiente, LoteLleno: Ver `Lotes.con_lugar`.
            Un video, además, los de `recibir_video`.
        """
        return self._recibir(
            flujo=flujo,
            nombre=nombre,
            por=por,
            tamano_minimo=tamano_minimo,
            lote_id=lote_id,
            puede_en_lote_ajeno=puede_en_lote_ajeno,
            solo_video=False,
        )

    def recibir_video(
        self,
        *,
        flujo: BinaryIO,
        nombre: str,
        por: Actor,
        tamano_minimo: int = 0,
        lote_id: UUID | None = None,
        puede_en_lote_ajeno: bool = False,
    ) -> tuple[DetallePieza, bool]:
        """Sube un MP4, MOV o WebM como Pieza `Pendiente` con Ficha v1 `sistema` vacía, sus
        datos técnicos en `metadata_original` y sin trabajo (ADR-0005).

        Raises:
            AnalisisDeVideoNoDisponible: Si no hay `ffprobe`; no se lee un byte.
            DemasiadasSubidas: Si `por` ya tiene `SUBIDAS_DE_VIDEO_POR_PERSONA` subidas de
                video en vuelo; no se lee un byte.
            TipoNoAdmitido: Si no es un video admitido.
            ArchivoIlegible: Si `analizar` no puede leerlo.
            VideoDemasiadoLargo: Si dura más de `TOPE_VIDEO_S`.
            Y los de `recibir_archivo`.
        """
        if self._analizar_video is None:
            raise AnalisisDeVideoNoDisponible()
        assert por.usuario_id is not None, "un video lo sube una persona"
        with self._subidas.cupo(por.usuario_id):
            return self._recibir(
                flujo=flujo,
                nombre=nombre,
                por=por,
                tamano_minimo=tamano_minimo,
                lote_id=lote_id,
                puede_en_lote_ajeno=puede_en_lote_ajeno,
                solo_video=True,
            )

    def _recibir(
        self,
        *,
        flujo: BinaryIO,
        nombre: str,
        por: Actor,
        tamano_minimo: int,
        lote_id: UUID | None,
        puede_en_lote_ajeno: bool,
        solo_video: bool,
    ) -> tuple[DetallePieza, bool]:
        assert por.usuario_id is not None, "un archivo lo sube una persona"
        try:
            temporal = self._originales.guardar_en_streaming(
                flujo, nombre=nombre, tamano_minimo=tamano_minimo, solo_video=solo_video
            )
        except RechazoDeAdmision as rechazo:
            self._registrar_admision(por, rechazo)
            raise
        try:
            datos_tecnicos = self._datos_tecnicos(temporal)
        except BaseException as error:
            self._originales.descartar(temporal)
            if isinstance(error, RechazoDeAdmision):
                error.clase = temporal.clase
                error.tamano_bytes, error.sha256 = temporal.tamano_bytes, temporal.sha256
                self._registrar_admision(por, error)
            raise
        sin_plan = temporal.clase is ClaseDeOriginal.VIDEO
        pieza_id = uuid4()
        confirmado: str | None = None
        en_commit = False
        try:
            with self._bd.transaccion() as tx:
                # La huella antes que el Lote: un reintento en un Lote lleno devuelve la Pieza
                # que ya estaba en vez de `lote-lleno`.
                self._fichas.exigir_huella_libre(tx, temporal.sha256)
                # El Lote queda bloqueado hasta el `COMMIT` para contar el tope.
                lote = (
                    None
                    if lote_id is None
                    else self._lotes.con_lugar(
                        tx,
                        lote_id,
                        cargante=por.usuario_id,
                        puede_en_lote_ajeno=puede_en_lote_ajeno,
                    )
                )
                # Después de la huella: un duplicado no encola y no depende de la configuración;
                # un video tampoco.
                instantanea = None if sin_plan else self._configuracion.instantanea(tx)
                confirmado = self._originales.confirmar(temporal, pieza_id=pieza_id)
                pieza = self._fichas.crear_pieza(
                    tx,
                    pieza_id=pieza_id,
                    nombre_archivo=_nombre_de_archivo(nombre),
                    original=temporal,
                    archivo_original=confirmado,
                    creada_por=por.usuario_id,
                    via_ingreso=ViaDeIngreso.ARCHIVO,
                    herencia=None if lote is None else lote.herencia(),
                    metadata_original=datos_tecnicos,
                )
                self._auditoria.registrar(
                    tx,
                    actor=por,
                    accion=Accion.PIEZA_INGRESO,
                    objeto=ObjetoRef("pieza", str(pieza_id)),
                    despues={"via_ingreso": ViaDeIngreso.ARCHIVO, "sha256": temporal.sha256},
                )
                # La Pieza de Lote espera detrás de la suelta (DEC-25).
                if instantanea is not None:
                    self._cola.encolar(
                        tx,
                        pieza_id,
                        tipo_mime=temporal.tipo_mime,
                        configuracion_efectiva=instantanea,
                        contexto_lote=None if lote is None else lote.contexto,
                        prioridad=0 if lote is None else 1,
                    )
                en_commit = True
        except BaseException as error:
            # Si se cortó el `COMMIT`, la Pieza pudo quedar confirmada: el original queda con su
            # marca y el barrido lo borra solo si, pasada una hora, no tiene Pieza.
            if not en_commit:
                self._originales.descartar(confirmado or temporal)
            if isinstance(error, PiezaDuplicada):
                if lote_id is not None and error.lote_id == lote_id:
                    # Ya estaba en este Lote: no es un rechazo, es la misma Pieza (DEC-20).
                    return self._fichas.obtener_pieza(error.pieza_id), True
                self._registrar_rechazo(
                    por,
                    "pieza-duplicada",
                    temporal.clase,
                    temporal.tamano_bytes,
                    temporal.sha256,
                    {"pieza_id": str(error.pieza_id)},
                )
            elif type(error) in _MOTIVOS_DEL_LOTE:
                self._registrar_rechazo(
                    por,
                    _MOTIVOS_DEL_LOTE[type(error)],
                    temporal.clase,
                    temporal.tamano_bytes,
                    temporal.sha256,
                    {"lote_id": str(lote_id)},
                )
            raise
        self._originales.asentar(confirmado)
        if not sin_plan:
            self._avisar()
        return pieza, False

    def recibir_aporte(
        self, *, agente_id: UUID, clave: str, aporte: AporteRecibido, por: Actor
    ) -> AcuseDeAporte:
        """El Aporte como Pieza `texto` `Pendiente` con su original y su v1 `agente_externo`,
        sin trabajo ni `resumir`; sus fuentes quedan en `aporte_pieza_fuente` y
        `aporte_fragmento_fuente`. La misma clave con la misma huella devuelve el acuse original
        sin crear nada, también con dos pedidos simultáneos.

        Raises:
            AporteRechazado: Ver `rechazar_etiquetas_de_control`; va antes que los demás.
            ClaveReutilizada: Si la clave del agente ya se usó con otra huella.
            FuenteInexistente: Si una Pieza fuente no existe, no está Validada o está retirada.
        """
        self.rechazar_etiquetas_de_control(agente_id=agente_id, aporte=aporte, por=por)
        huella = aporte.huella()
        pieza_id = uuid4()
        temporal: OriginalTemporal | None = None
        confirmado: str | None = None
        en_commit = False
        try:
            with self._bd.transaccion() as tx:
                # El segundo pedido con la misma clave espera al primero y ve su fila.
                tx.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"aporte:{agente_id}:{clave}",),
                )
                previo = tx.execute(
                    "SELECT aporte_id, pieza_id, recibido_el, cuerpo_huella FROM acervo.aporte"
                    " WHERE agente_id = %s AND idempotency_key = %s",
                    (agente_id, clave),
                ).fetchone()
                if previo is not None:
                    if previo["cuerpo_huella"] != huella:
                        raise ClaveReutilizada()
                    return AcuseDeAporte(
                        previo["aporte_id"], previo["pieza_id"], previo["recibido_el"], True
                    )
                if faltan := self._no_validadas(tx, aporte.piezas_fuente):
                    raise FuenteInexistente(faltan)
                temporal = self._originales.guardar_texto(aporte.texto)
                confirmado = self._originales.confirmar(temporal, pieza_id=pieza_id)
                self._fichas.crear_aporte(
                    tx,
                    pieza_id=pieza_id,
                    nombre=f"{aporte.titulo}.txt",
                    original=temporal,
                    archivo_original=confirmado,
                    texto=aporte.texto,
                )
                fila = tx.execute(
                    "INSERT INTO acervo.aporte (agente_id, idempotency_key, cuerpo_huella,"
                    " pieza_id, titulo, cuerpo, tipo_contenido, canal, fecha_publicacion,"
                    " url_publicacion) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " RETURNING aporte_id, recibido_el",
                    (
                        agente_id,
                        clave,
                        huella,
                        pieza_id,
                        aporte.titulo,
                        aporte.cuerpo,
                        aporte.tipo_contenido,
                        aporte.canal,
                        aporte.fecha_publicacion,
                        aporte.url_publicacion,
                    ),
                ).fetchone()
                assert fila is not None
                tx.execute(
                    "INSERT INTO acervo.aporte_pieza_fuente (aporte_id, pieza_id)"
                    " SELECT %s, unnest(%s::uuid[])",
                    (fila["aporte_id"], list(aporte.piezas_fuente)),
                )
                tx.execute(
                    "INSERT INTO acervo.aporte_fragmento_fuente (aporte_id, fragmento_id)"
                    " SELECT %s, unnest(%s::uuid[])",
                    (fila["aporte_id"], list(aporte.fragmentos_fuente)),
                )
                self._auditoria.registrar(
                    tx,
                    actor=por,
                    accion=Accion.PIEZA_INGRESO,
                    objeto=ObjetoRef("pieza", str(pieza_id)),
                    despues={
                        "via_ingreso": ViaDeIngreso.APORTE,
                        "sha256": temporal.sha256,
                        "aporte_id": str(fila["aporte_id"]),
                    },
                )
                en_commit = True
        except BaseException:
            # Como en `_recibir`: cortado el `COMMIT`, el original queda con su marca.
            if not en_commit and (descartable := confirmado or temporal) is not None:
                self._originales.descartar(descartable)
            raise
        assert confirmado is not None
        self._originales.asentar(confirmado)
        return AcuseDeAporte(fila["aporte_id"], pieza_id, fila["recibido_el"], False)

    def rechazar_etiquetas_de_control(
        self, *, agente_id: UUID, aporte: AporteRecibido, por: Actor
    ) -> None:
        """El chequeo de contenido del Aporte, después de su forma y antes de mirar la clave o
        las fuentes (DEC-28, DEC-133). `recibir_aporte` lo corre primero; `api_agentes` lo
        llama solo cuando rechaza por fuentes sin llegar a `recibir_aporte`.

        Raises:
            AporteRechazado: Si el título o el cuerpo traen una etiqueta de la lista cerrada,
                con `aporte.rechazo` ya registrado en su propia transacción.
        """
        # El texto entero, como se guarda: revisados por separado, una etiqueta partida entre
        # el título y el cuerpo («[» y «system]») pasaría. El título es la línea 1, la 2 queda
        # en blanco y el cuerpo empieza en la 3.
        etiquetas = tuple(
            replace(e, seccion="titulo", linea=1)
            if e.linea == 1
            else replace(e, seccion="cuerpo", linea=e.linea - 2)
            for e in self._etiquetas_de_control({"aporte": aporte.texto})
        )
        if not etiquetas:
            return
        texto = aporte.texto.encode("utf-8")
        try:
            with self._bd.transaccion() as tx:
                self._auditoria.registrar(
                    tx,
                    actor=por,
                    accion=Accion.APORTE_RECHAZO,
                    objeto=ObjetoRef("aporte"),
                    despues={
                        "agente_id": str(agente_id),
                        "etiquetas": [
                            asdict(e) for e in etiquetas[:TOPE_DE_ETIQUETAS_AUDITADAS]
                        ],
                        "total": len(etiquetas),
                        "tamano_bytes": len(texto),
                        "sha256": hashlib.sha256(texto).hexdigest(),
                    },
                    motivo="etiquetas-de-control",
                )
        except Exception:  # noqa: BLE001
            # Como `archivo.rechazo`: el agente recibe el 422 igual y el evento queda en el log.
            _log.exception("`aporte.rechazo` no quedó.")
        raise AporteRechazado(etiquetas)

    def datos_del_aporte(self, tx: Tx, pieza_id: UUID) -> DatosDelAporte | None:
        """`None` si la Pieza no es un Aporte."""
        fila = tx.execute(
            "SELECT agente_id, recibido_el, tipo_contenido, canal FROM acervo.aporte"
            " WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()
        return None if fila is None else DatosDelAporte(**fila)

    def fuentes_no_validadas(self, piezas: tuple[UUID, ...]) -> list[UUID]:
        """Las de `piezas` que no valen como fuente de un Aporte, en su orden."""
        with self._bd.transaccion() as tx:
            return self._no_validadas(tx, piezas)

    def _no_validadas(self, tx: Tx, piezas: tuple[UUID, ...]) -> list[UUID]:
        validadas = self._fichas.validadas(tx, piezas)
        return [p for p in piezas if p not in validadas]

    @property
    def admite_video(self) -> bool:
        """Si hay con qué leer los datos técnicos de un video (`ffprobe`)."""
        return self._analizar_video is not None

    def _datos_tecnicos(self, temporal: OriginalTemporal) -> dict[str, Any] | None:
        """Los datos técnicos de un video, que van a `metadata_original`; `None` si no lo es.
        Un video llega solo por `recibir_video`, que ya miró que hubiera `analizar`."""
        if temporal.clase is not ClaseDeOriginal.VIDEO:
            return None
        assert self._analizar_video is not None
        datos = self._analizar_video(temporal.ruta, _DEMUXER[temporal.tipo_mime])
        if datos.duracion_s > TOPE_VIDEO_S:
            raise VideoDemasiadoLargo(TOPE_VIDEO_S)
        return datos.a_json()

    def _registrar_admision(self, por: Actor, rechazo: RechazoDeAdmision) -> None:
        causa = {} if rechazo.motivo is None else {"causa": rechazo.motivo}
        self._registrar_rechazo(
            por,
            _MOTIVOS[type(rechazo)],
            rechazo.clase,
            rechazo.tamano_bytes,
            rechazo.sha256,
            causa,
        )

    def _registrar_rechazo(
        self,
        por: Actor,
        motivo: str,
        clase: ClaseDeOriginal | None,
        tamano_bytes: int | None,
        sha256: str | None,
        ademas: Mapping[str, Any],
    ) -> None:
        """`archivo.rechazo` en su propia transacción. Si no se puede registrar (sin base o por
        cualquier otra falla), el rechazo igual le llega a quien sube: el evento se pierde y
        queda en el log."""
        try:
            with self._bd.transaccion() as tx:
                self._auditoria.registrar(
                    tx,
                    actor=por,
                    accion=Accion.ARCHIVO_RECHAZO,
                    objeto=ObjetoRef("archivo", sha256),
                    despues={"clase": clase, "tamano_bytes": tamano_bytes, "sha256": sha256}
                    | dict(ademas),
                    motivo=motivo,
                )
        except Exception:  # noqa: BLE001
            _log.exception("`archivo.rechazo` (%s) no quedó.", motivo)

    def reprocesar(
        self, pieza_id: UUID, *, por: Actor, puede_ajena: bool, forzar: bool = False
    ) -> DetallePieza:
        """Reintenta el procesamiento de una Pieza `Recibida` cuyo trabajo falló: la regla y el
        tope son de `procesamiento` (`Cola.reprocesar`); acá se congela la configuración, se
        audita `pieza.reproceso` y se despierta al trabajador.

        Args:
            puede_ajena: Si `por` reintenta también Piezas que no subió (el RGC, 033).

        Raises:
            PiezaNoEncontrada: Si no existe o está retirada.
            PermisoInsuficiente: Si no la subió `por` y no `puede_ajena`.
            TransicionInvalida: Si no está `Recibida` o su trabajo no terminó `fallido`.
            TopeDeReprocesos: Si ya tuvo 6 reprocesos y no se pasó `forzar` (DEC-137).
            ConfiguracionInvalida: Si una fila de la configuración no pasa `validar`.
        """
        detalle = self._reprocesar(
            pieza_id, TipoDeTrabajo.PROCESAMIENTO, por=por, forzar=forzar, puede_ajena=puede_ajena
        )
        self._avisar()
        return detalle

    def describir_imagen(self, pieza_id: UUID, imagen: int, *, por: Actor) -> None:
        """«Describir esta imagen» (148): encola la descripción de una imagen embebida sin texto
        de una Pieza Pendiente o Incompleta, con la configuración de hoy, y despierta al
        trabajador. Una por imagen. Audita `imagen.descripcion_pedida` (SEG-19).

        Raises:
            PiezaNoEncontrada: Si no existe o está retirada.
            ImagenNoEncontrada: Si la Pieza no tiene esa imagen.
            TransicionInvalida: Si la Pieza ya no se valida, la imagen ya está descrita o su
                descripción ya está en la cola.
        """
        with self._bd.transaccion() as tx:
            self._fichas.encolar_imagen(tx, pieza_id=pieza_id, imagen=imagen)
            self._cola.encolar_descripcion(
                tx,
                pieza_id,
                imagen=imagen,
                configuracion_efectiva=self._configuracion.instantanea(tx),
                solicitado_por=por.usuario_id,
            )
            self._auditoria.registrar(
                tx,
                actor=por,
                accion=Accion.IMAGEN_DESCRIPCION_PEDIDA,
                objeto=ObjetoRef("pieza", str(pieza_id)),
                despues={"imagen": imagen},
            )
        self._avisar()

    def completar_a_mano(self, pieza_id: UUID, *, por: Actor, puede_ajena: bool) -> FichaVigente:
        """Pasa una `Recibida` cuyo último procesamiento falló a `Pendiente`, con la Ficha v1
        `sistema` vacía y «Requiere análisis humano» (DEC-122): un fallo determinista no traba
        la Pieza ni su Lote. El trabajo fallido queda como historial; audita
        `pieza.completar_a_mano`.

        Args:
            puede_ajena: Si `por` completa también Piezas que no subió (el RGC, R-06).

        Raises:
            PiezaNoEncontrada: Si no existe o está retirada.
            PermisoInsuficiente: Si no la subió `por` y no `puede_ajena`.
            TransicionInvalida: Si no está `Recibida` o su último procesamiento no falló.
        """
        with self._bd.transaccion() as tx:
            # La Pieza bloqueada antes de mirar la cola: un reproceso (que también la bloquea)
            # encola antes o después, nunca en el medio.
            detalle, creada_por = self._fichas.bloquear_vigente(tx, pieza_id)
            if not puede_ajena and por.usuario_id != creada_por:
                raise PermisoInsuficiente("Completa a mano una Pieza quien la subió o el RGC.")
            estado = detalle.fila.estado
            if estado is not Estado.RECIBIDA or not self._cola.fallo_el_ultimo(tx, pieza_id):
                raise TransicionInvalida(pieza_id, estado.value, Estado.PENDIENTE.value)
            ficha = self._fichas.completar_a_mano(tx, pieza_id)
            self._auditoria.registrar(
                tx,
                actor=por,
                accion=Accion.PIEZA_COMPLETAR_A_MANO,
                objeto=ObjetoRef("pieza", str(pieza_id)),
                antes={"estado": Estado.RECIBIDA.value},
                despues={"estado": Estado.PENDIENTE.value},
            )
        return ficha

    def reprocesar_fallidos(
        self,
        *,
        por: Actor,
        lote_id: UUID | None = None,
        tipo: TipoDeTrabajo | None = None,
        forzar: bool = False,
    ) -> tuple[int, list[UUID]]:
        """`reprocesar --fallidos`: un trabajo nuevo por cada último trabajo `fallido` del
        alcance, sin las retiradas (DEC-86). No aborta: las Piezas que llegaron al tope se
        saltean y se devuelven, junto con cuántos se encolaron (R-42). Cada reproceso va en su
        transacción.
        """
        with self._bd.transaccion() as tx:
            # El resumen rehecho no se reprocesa: lo vuelve a encolar el próximo rechazo (146).
            fallidos = self._cola.fallidos(tx, [tipo] if tipo else REPROCESABLES)
            vigentes = self._fichas.vigentes(tx, {p for p, _ in fallidos}, lote_id=lote_id)
        encolados, salteadas = 0, []
        for pieza_id, tipo_ in fallidos:
            if pieza_id not in vigentes:
                continue
            try:
                # En tanda no es un reproceso individual: cada uno conserva su prioridad.
                self._reprocesar(
                    pieza_id, tipo_, por=por, forzar=forzar, puede_ajena=True, prioridad=None
                )
            except TopeDeReprocesos:
                salteadas.append(pieza_id)
                continue
            # Se retiró, dejó de estar Recibida (completada a mano, 034) o la reintentó otro
            # mientras tanto: no hay nada que encolar.
            except (PiezaNoEncontrada, TransicionInvalida):
                continue
            encolados += 1
        if encolados:
            self._avisar()
        return encolados, salteadas

    def _reprocesar(
        self,
        pieza_id: UUID,
        tipo: TipoDeTrabajo,
        *,
        por: Actor,
        forzar: bool,
        puede_ajena: bool,
        prioridad: int | None = 0,
    ) -> DetallePieza:
        with self._bd.transaccion() as tx:
            # La Pieza bloqueada: entre leer su estado y encolar, nadie la valida ni la retira.
            detalle, creada_por = self._fichas.bloquear_vigente(tx, pieza_id)
            if not puede_ajena and por.usuario_id != creada_por:
                raise PermisoInsuficiente("Reintenta una Pieza quien la subió o el RGC.")
            estado = detalle.fila.estado
            if tipo is TipoDeTrabajo.PROCESAMIENTO and estado is not Estado.RECIBIDA:
                raise TransicionInvalida(pieza_id, estado.value, Estado.RECIBIDA.value)
            resultado = self._cola.reprocesar(
                tx,
                pieza_id,
                tipo=tipo,
                tipo_mime=detalle.tipo_mime,
                configuracion_efectiva=self._configuracion.instantanea(tx),
                solicitado_por=por.usuario_id,
                forzar=forzar,
                prioridad=prioridad,
            )
            self._auditoria.registrar(
                tx,
                actor=por,
                accion=Accion.PIEZA_REPROCESO,
                objeto=ObjetoRef("pieza", str(pieza_id)),
                despues={
                    "trabajo_id": str(resultado.trabajo_id),
                    "tipo": tipo.value,
                    "forzar": resultado.forzado,
                },
                motivo=(
                    f"--forzar sobre el tope de {TOPE_DE_REPROCESOS}" if resultado.forzado else None
                ),
            )
        return detalle


def normalizar_linea(texto: str) -> str:
    """Un texto de una sola línea (un nombre de archivo, el Contexto del Lote) en UTF-8 NFC y
    sin caracteres de control (R-36, DIR-19): tampoco quedan saltos ni tabulaciones."""
    # NFC después de quitar los controles: uno entre una letra y su tilde impide componerla.
    return unicodedata.normalize(
        "NFC", "".join(c for c in texto if unicodedata.category(c) != "Cc")
    )


def normalizar_titulo(texto: str) -> str:
    """El título de un Aporte en una sola línea: el salto y la tabulación pasan a espacio, así
    el original (`título`, línea en blanco, cuerpo) no queda ambiguo."""
    return normalizar_texto(texto).replace("\n", " ").replace("\t", " ")


def normalizar_texto(texto: str) -> str:
    """Un texto de varias líneas (el título y el cuerpo de un Aporte) en UTF-8 NFC y sin
    caracteres de control salvo el salto de línea y la tabulación (R-36, DIR-19)."""
    return unicodedata.normalize(
        "NFC", "".join(c for c in texto if c in "\n\t" or unicodedata.category(c) != "Cc")
    )


def _nombre_de_archivo(nombre: str) -> str:
    """El nombre normalizado (`normalizar_linea`); el original no se toca."""
    limpio = normalizar_linea(nombre)
    # Algunos navegadores mandan la ruta completa (`C:\fakepath\acta.pdf`): vale el nombre.
    return PureWindowsPath(limpio).name or "archivo"
