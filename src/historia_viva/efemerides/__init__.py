"""Efemérides: textos breves de difusión redactados desde Fichas Validadas (spec, módulo 32;
DEC-130).

Es dueño de `efemeride`, `efemeride_fuente` y `programacion_efemerides`. Lo que redacta el modelo
queda Propuesto, con sus Fichas fuente, y nada llega al Agente 1 sin la firma de una persona
(ADR-0002). Nada se regenera solo (ADR-0004): un objetivo que ya tiene efeméride, en cualquier
estado, se omite; una Descartada tampoco vuelve.

Tres tiempos, sin modelos dentro de una transacción (DEC-99): una transacción elige las
candidatas, fuera de transacción redacta `redactar_efemeride` con las Fichas como datos
delimitados (ADR-0003) y otra guarda la Propuesta si las Fichas no cambiaron. Las Citas `[n]` se
verifican con `dominio.verificar_citas`: sin ninguna válida no hay Propuesta, y una que no es de
ninguna fuente se quita del texto.

«Generar ahora» no genera: marca el pedido en `programacion_efemerides` y lo atiende
`correr_programadas`, la tarea del planificador del trabajador (R-08), que corre con el
procesamiento habilitado y el motor vivo (DEC-136). Un objetivo pedido a mano se genera aunque tenga una
Descartada; la ventana, nunca. Mientras hay un pedido sin atender, otro de distinto objetivo se
rechaza y el mismo no cambia nada, salvo que el planificador esté caído (sin latido): entonces
el pedido nuevo reemplaza al que quedó trabado (059). Sin pedido, la misma tarea corre la
ventana cuando la programación está activa y su periodicidad toca (060).

La de día se difunde ese día; la de mes guarda su objetivo en el día 1 y se difunde el mes
entero, con las Fichas de mes conocido (059, R-11).

057 trae la efeméride de día y su lectura; 058 el planificador y «Generar ahora»; 059 las de mes;
060 la programación; 061 a 063 la revisión (061: el filtro por difusión y la aprobación
firmada; 062: la edición con `version_base` y el descarte con motivo; 063: la marca «fuente
retirada» y `aprobadas`, lo que entrega `/v1`).

También es dueño de `relato` y `relato_fuente` (DEC-147): un Relato nace de una respuesta de la
Conversación, que es quien llama a `proponer_relato` con el texto y las Citas ya verificadas; acá
no se redacta nada ni se lee la Conversación (064).
"""

from __future__ import annotations

import logging
from calendar import monthrange
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria, TipoDeActor
from historia_viva.bd import Bd, Tx
from historia_viva.capa_modelos.configuracion import CapaPara, ConfiguracionEfectiva
from historia_viva.capa_modelos.contrato import ETIQUETA_FICHA, BloqueDatos, Capacidad, Plantilla
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.citas import quitar_citas, verificar_citas
from historia_viva.dominio.errores import (
    ConfiguracionInvalida,
    LimiteExcedido,
    MotorNoAlcanzable,
    MotorNoDisponible,
    PermisoInsuficiente,
)
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.permisos import Permiso
from historia_viva.dominio.tipos import FuenteDeFecha
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Principal

__all__ = [
    "MAXIMO_DE_FUENTES",
    "Alcance",
    "EfemerideDesactualizada",
    "EfemerideEntregable",
    "EfemerideExistente",
    "EfemerideNoEncontrada",
    "EfemerideNoPropuesta",
    "Efemerides",
    "Estado",
    "FuenteDeEfemeride",
    "Objetivo",
    "ObjetivoOmitido",
    "FuenteDeRelato",
    "PedidoPendiente",
    "Periodicidad",
    "Programacion",
    "ProgramacionDesactualizada",
    "RelatoDesactualizado",
    "RelatoEntregable",
    "RelatoNoEncontrado",
    "RelatoNoPropuesto",
    "RelatoYaPropuesto",
    "RespuestaNoProponible",
    "TextoRedactado",
    "VistaDeEfemeride",
    "VistaDeRelato",
    "contar_firmas_de_ensayo",
    "textos_redactados",
]

MAXIMO_DE_FUENTES = 5
"""Fichas fuente por efeméride (ajustable, DEC-145): las que ve el modelo y puede citar."""

LISTA_MAXIMA = 200
"""El `limite` máximo de `GET /api/efemerides`."""

_MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
    "octubre", "noviembre", "diciembre",
)  # fmt: skip

_PLANTILLAS = {
    "dia": Plantilla.REDACTAR_EFEMERIDE_DIA,
    "mes": Plantilla.REDACTAR_EFEMERIDE_MES,
}
"""La variante de la familia `REDACTAR_EFEMERIDE` de cada alcance (R-10)."""

_SISTEMA = Actor(TipoDeActor.SISTEMA, "efemerides")
"""Quien genera lo programado: no hay persona detrás (spec, pseudocódigo de `guardar`)."""

_PROGRAMACION = ObjetoRef("programacion_efemerides", "1")

_PERIODO = (
    "CASE p.periodicidad WHEN 'diaria' THEN interval '1 day'"
    " WHEN 'semanal' THEN interval '7 days' ELSE interval '1 month' END"
)
"""Cada cuánto toca la corrida programada, en SQL sobre `programacion_efemerides p`."""

_log = logging.getLogger(__name__)


class Alcance(StrEnum):
    DIA = "dia"
    """Un día y mes, en cualquier año: «un día como hoy»."""
    MES = "mes"
    """Un mes, en cualquier año: para las Fichas que no tienen día (historia 260)."""


class Estado(StrEnum):
    """Los valores son los de `efemeride.estado`; la API los muestra con mayúscula inicial."""

    PROPUESTA = "propuesta"
    APROBADA = "aprobada"
    DESCARTADA = "descartada"


@dataclass(frozen=True)
class Objetivo:
    """Lo que se conmemora: un día o un mes."""

    alcance: Alcance
    fecha: date
    """La fecha en que se difunde: de ella se toman el día y el mes. La de mes, el día 1."""

    def __post_init__(self) -> None:
        if self.alcance is Alcance.MES and self.fecha.day != 1:
            raise ValueError("El objetivo de una efeméride de mes es su día 1.")

    @property
    def difusion(self) -> tuple[date, date]:
        """Desde y hasta cuándo se difunde (R-11): ese día, o el mes entero."""
        if self.alcance is Alcance.DIA:
            return self.fecha, self.fecha
        ultimo = monthrange(self.fecha.year, self.fecha.month)[1]
        return self.fecha, self.fecha.replace(day=ultimo)

    @property
    def nombre(self) -> str:
        """«3 de mayo» o «mayo»."""
        mes = _MESES[self.fecha.month - 1]
        return f"{self.fecha.day} de {mes}" if self.alcance is Alcance.DIA else mes


class ObjetivoOmitido(Exception):
    """El objetivo no deja Propuesta; el mensaje es el motivo. No se guarda nada: la próxima
    corrida lo vuelve a intentar."""


class NadaQueGenerar(ObjetivoOmitido):
    """El objetivo ya tiene efeméride o no tiene candidatas: la ventana lo saltea sin anotarlo
    como omitido (spec, pseudocódigo de `correr_programadas`)."""


Periodicidad = Literal["diaria", "semanal", "mensual"]


class EfemerideExistente(Exception):
    """«Generar ahora» nombra un objetivo que ya tiene una Propuesta o una Aprobada."""

    def __init__(self, efemeride_id: UUID, alcance: Alcance) -> None:
        cual = "Ese día" if alcance is Alcance.DIA else "Ese mes"
        super().__init__(f"{cual} ya tiene una efeméride Propuesta o Aprobada.")
        self.efemeride_id = efemeride_id


class PedidoPendiente(Exception):
    """«Generar ahora» de otro objetivo mientras uno sigue sin atender: no se pisa."""

    def __init__(self, pendiente: dict[str, Any]) -> None:
        super().__init__(
            "Ya hay un pedido de generación sin atender; esperá a que el planificador lo tome."
        )
        self.pendiente = pendiente
        """`objetivo` (o `None`, la ventana) y `pedido_manual_el` del pedido pendiente."""


@dataclass(frozen=True)
class Programacion:
    """Lo que muestra P7f de `programacion_efemerides`."""

    habilitada: bool
    periodicidad: Periodicidad
    anticipacion_dias: int
    maximo_por_corrida: int
    pedido_manual_el: datetime | None
    pedido_manual_objetivo: dict[str, str] | None
    ultima_corrida_el: datetime | None
    ultima_corrida: dict[str, Any] | None
    """La de `efemeride.corrida`, con el `pedido_manual_el` que atendió."""
    proxima_corrida_el: datetime | None
    """Cuándo toca la corrida programada (si nunca corrió, desde que se guardó; si ya pasó, en la
    próxima vuelta del planificador); `None` si está apagada."""
    version: int
    actualizada_por: str | None
    """El email de quien la editó por última vez."""
    actualizada_el: datetime
    falta_modelo: bool
    """La fila de `redactar_efemeride` falta o no valida: «Generar» no puede redactar."""


class ProgramacionDesactualizada(Exception):
    """La programación cambió desde la versión que vio quien la edita (`version_base`)."""

    def __init__(self, version_base: int, actual: Programacion) -> None:
        super().__init__(
            f"La programación está en la versión {actual.version} y editaste la {version_base}."
            " Recargala para ver sus cambios."
        )
        self.version_base = version_base
        self.actual = actual


class EfemerideNoEncontrada(Exception):
    """No existe, o quien pide solo puede leer las Aprobadas y esta no lo es (R-05)."""


class EfemerideNoPropuesta(Exception):
    """Solo se revisa una Propuesta: una Aprobada o una Descartada es inmutable (R-12)."""

    def __init__(self, estado: Estado, por: str | None) -> None:
        quien = f" por {por}" if por else ""
        super().__init__(
            f"La efeméride ya fue {estado.value.capitalize()}{quien}."
            " Solo se revisa una Propuesta."
        )
        self.estado = estado


class EfemerideDesactualizada(Exception):
    """La efeméride cambió desde la versión que vio quien revisa (`version_base`)."""

    cual = "La efeméride"

    def __init__(self, version_base: int, version_actual: int) -> None:
        super().__init__(
            f"{self.cual} está en la versión {version_actual} y revisaste la {version_base}."
            " Recargá para ver sus cambios."
        )
        self.version_base = version_base
        self.version_actual = version_actual


class RelatoDesactualizado(EfemerideDesactualizada):
    """El Relato cambió desde `version_base`: el mismo 409 `version-desactualizada` (065)."""

    cual = "El relato"


class RelatoNoEncontrado(Exception):
    """No existe, o quien pide solo puede leer los Aprobados y este no lo es (R-05)."""


class RelatoNoPropuesto(Exception):
    """Solo se revisa un Relato Propuesto: uno Aprobado o Descartado es inmutable (R-12)."""

    def __init__(self, estado: Estado, por: str | None) -> None:
        quien = f" por {por}" if por else ""
        super().__init__(
            f"El relato ya fue {estado.value[:-1].capitalize()}o{quien}."
            " Solo se revisa un Relato Propuesto."
        )
        self.estado = estado


class RelatoYaPropuesto(Exception):
    """La respuesta ya tiene un Relato que no se descartó: 409 `relato-ya-propuesto`."""

    def __init__(self, relato_id: UUID) -> None:
        super().__init__("Esa respuesta ya se propuso como relato.")
        self.relato_id = relato_id


class RespuestaNoProponible(Exception):
    """La respuesta no es `completa`, no tiene Citas verificadas o una Ficha fuente dejó de estar
    Validada: 409 `respuesta-no-proponible`."""

    def __init__(self, detalle: str, *, estado_actual: str | None) -> None:
        super().__init__(detalle)
        self.estado_actual = estado_actual
        """El estado de la respuesta (`None` si el mensaje es una pregunta)."""


@dataclass(frozen=True)
class FuenteDeRelato:
    """Una Cita verificada de la respuesta de origen."""

    posicion: int
    """El `n` de las `[n]` del texto: la Cita `n` de la respuesta."""
    pieza_id: UUID
    ficha_id: UUID
    """La versión citada."""


@dataclass(frozen=True)
class FuenteDeEfemeride:
    posicion: int
    """El `n` de las `[n]` del texto."""
    pieza_id: UUID
    ficha_id: UUID
    """La versión citada."""
    alerta_inyeccion: bool
    """La de la Ficha al generar."""
    resumen: str | None
    fecha_inicio: date | None
    fecha_fin: date | None
    precision_fecha: PrecisionFecha | None
    retirada: bool
    """«Retirada del Acervo»: hoy la Pieza está retirada o ya no está Validada (se lee, no se
    marca)."""


@dataclass(frozen=True)
class EfemerideEntregable:
    """Una Aprobada como la entrega `/v1/efemerides` (R-11): sin la alerta de inyección (C33)."""

    efemeride_id: UUID
    alcance: Alcance
    fecha_objetivo: date
    difusion_desde: date
    difusion_hasta: date
    titulo: str
    cuerpo: str
    piezas_fuente: tuple[UUID, ...]
    """En el orden de sus `[n]`."""
    aprobada_por: str
    """El email de quien la firmó."""
    aprobada_el: datetime


@dataclass(frozen=True)
class RelatoEntregable:
    """Un Relato Aprobado como lo entrega `/v1/relatos` (DEC-147): sin la alerta (C33)."""

    relato_id: UUID
    titulo: str
    cuerpo: str
    fuentes: tuple[FuenteDeRelato, ...]
    """Con su `posicion`: las `[n]` del texto pueden saltear números."""
    aprobado_por: str
    """El email de quien lo firmó."""
    aprobado_el: datetime


@dataclass(frozen=True)
class VistaDeEfemeride:
    efemeride_id: UUID
    alcance: Alcance
    fecha_objetivo: date
    difusion_desde: date
    difusion_hasta: date
    titulo: str
    cuerpo: str
    version: int
    estado: Estado
    alerta_inyeccion: bool
    fuente_retirada: bool
    plantilla: str
    configuracion_efectiva: dict[str, Any]
    generada_por: str | None
    """El email de quien la pidió a mano; `None` si fue el sistema."""
    generada_el: datetime
    aprobada_por: str | None
    """La firma: email, nombre, cuándo y con qué se entró (`dev` es de ensayo, DEC-139)."""
    aprobada_por_nombre: str | None
    aprobada_el: datetime | None
    metodo_autenticacion: str | None
    editada_por: str | None
    """El email de quien la editó por última vez: P8 marca «Editado por vos» (062)."""
    editada_el: datetime | None
    descartada_por: str | None
    """El email; el nombre es para «Descartada por …» (062)."""
    descartada_por_nombre: str | None
    descartada_el: datetime | None
    descarte_motivo: str | None
    fuentes: tuple[FuenteDeEfemeride, ...] = ()
    """Solo en `obtener`."""


@dataclass(frozen=True)
class VistaDeRelato:
    relato_id: UUID
    mensaje_id: UUID
    titulo: str
    cuerpo: str
    version: int
    estado: Estado
    alerta_inyeccion: bool
    fuente_retirada: bool
    propuesto_por: str
    """El email de quien lo propuso."""
    propuesto_el: datetime
    editado_por: str | None
    editado_el: datetime | None
    aprobado_por: str | None
    """La firma: email, nombre, cuándo y con qué se entró, como en la efeméride (065)."""
    aprobado_por_nombre: str | None
    aprobado_el: datetime | None
    metodo_autenticacion: str | None
    descartado_por: str | None
    descartado_por_nombre: str | None
    descartado_el: datetime | None
    descarte_motivo: str | None
    cantidad_fuentes: int
    """Las Piezas distintas que cita: la lista de P8 la muestra sin traer las fuentes."""
    fuentes: tuple[FuenteDeEfemeride, ...] = ()
    """Solo en `obtener_relato`."""


@dataclass(frozen=True)
class TextoRedactado:
    """Una efeméride o un Relato como lo evalúa M4 (075): el texto y el modelo que lo redactó."""

    texto_id: UUID
    tipo: Literal["efemeride", "relato"]
    titulo: str
    estado: Estado
    modelo: str | None
    """`redactar_efemeride` en la efeméride; `responder` en el Relato, que es una respuesta."""


def textos_redactados(tx: Tx) -> list[TextoRedactado]:
    """Todas las efemérides y todos los Relatos, en cualquier estado, sin escribir nada: la tanda
    que evalúa la SEU con la checklist de M4 (R-13, DEC-147)."""
    filas = tx.execute(
        "SELECT efemeride_id AS texto_id, 'efemeride' AS tipo, titulo, estado,"
        " configuracion_efectiva #>> '{por_capacidad,redactar_efemeride,modelo}' AS modelo,"
        " generada_el AS momento FROM acervo.efemeride"
        " UNION ALL SELECT relato_id, 'relato', titulo, estado,"
        " configuracion_efectiva #>> '{por_capacidad,responder,modelo}', propuesto_el"
        " FROM acervo.relato ORDER BY tipo, momento, texto_id"
    ).fetchall()
    return [
        TextoRedactado(
            texto_id=f["texto_id"], tipo=f["tipo"], titulo=f["titulo"],
            estado=Estado(f["estado"]), modelo=f["modelo"],
        )
        for f in filas
    ]


class Efemerides:
    def __init__(
        self,
        *,
        bd: Bd,
        fichas: ModuloFichasPostgres,
        configuracion: ConfiguracionDeModelos,
        capa_para: CapaPara,
        auditoria: RegistroDeAuditoria,
    ) -> None:
        """`capa_para` arma la capa con la fila de `redactar_efemeride` que congela cada
        generación."""
        self._bd = bd
        self._fichas = fichas
        self._configuracion = configuracion
        self._capa_para = capa_para
        self._auditoria = auditoria

    def candidatas(self, tx: Tx, objetivo: Objetivo) -> list[FuenteDeFecha]:
        """Las Fichas Validadas, no retiradas, hasta `MAXIMO_DE_FUENTES`: para un día, las de
        ese día y mes, por `fecha_inicio` y `pieza_id`; para un mes, las de mes conocido,
        primero las que no tienen día (DEC-130)."""
        if objetivo.alcance is Alcance.MES:
            return self._fichas.validadas_del_mes(
                tx, mes=objetivo.fecha.month, limite=MAXIMO_DE_FUENTES
            )
        return self._fichas.validadas_con_fecha(
            tx, mes=objetivo.fecha.month, dia=objetivo.fecha.day, limite=MAXIMO_DE_FUENTES
        )

    def generar(
        self,
        objetivo: Objetivo,
        *,
        pedida_por: UUID | None = None,
        sobre_descartada: bool = False,
    ) -> UUID:
        """Redacta la efeméride de `objetivo` y la deja Propuesta con sus fuentes; devuelve su id.

        Con `pedida_por` queda `manual`, a nombre de quien hizo «Generar ahora». Con
        `sobre_descartada` una Descartada no lo impide: solo para un objetivo nombrado a mano.

        Raises:
            ObjetivoOmitido: Ya tiene una efeméride (con `sobre_descartada`, Propuesta o
                Aprobada), no tiene candidatas, el texto no trae ninguna Cita válida o una Ficha
                fuente cambió mientras se redactaba.
            ConfiguracionInvalida: La fila de `redactar_efemeride` falta o no valida.
            MotorNoAlcanzable, LimiteExcedido: Los de la capa.
        """
        configuracion = self._configuracion.instantanea(
            capacidades=(Capacidad.REDACTAR_EFEMERIDE,)
        )
        with self._bd.transaccion() as tx:  # (1) qué se redacta
            if self._existente(tx, objetivo, descartadas=not sobre_descartada):
                raise NadaQueGenerar(f"{objetivo.fecha} ya tiene una efeméride.")
            fuentes = self.candidatas(tx, objetivo)
        if not fuentes:
            de = "del" if objetivo.alcance is Alcance.DIA else "de"
            raise NadaQueGenerar(f"no hay Fichas Validadas {de} {objetivo.nombre}.")
        cuerpo = self._redactar(objetivo, fuentes, configuracion)  # fuera de transacción
        with self._bd.transaccion() as tx:  # (2) la Propuesta
            return self._guardar(tx, objetivo, fuentes, cuerpo, configuracion, pedida_por)

    def pedir_generacion(
        self,
        tx: Tx,
        *,
        por: Actor,
        objetivo: Objetivo | None = None,
        trabado_tras: timedelta | None = None,
    ) -> datetime:
        """«Generar ahora»: marca el pedido que atiende el planificador y devuelve cuándo quedó
        marcado. Sin `objetivo`, el planificador corre la ventana. Repetir el pedido pendiente
        no cambia nada (queda en la auditoría, con el pendiente como `antes`). Con el
        planificador caído (lo dice su latido), quien llama pasa `trabado_tras`: el pedido de
        otro objetivo reemplaza al pendiente más viejo que eso, que nadie iba a atender; uno
        más reciente no se pisa (059).

        Raises:
            EfemerideExistente: `objetivo` ya tiene una Propuesta o una Aprobada (auditoría de
                tickets, decisión 6); una Descartada no lo impide.
            PedidoPendiente: Hay un pedido sin atender de otro objetivo y el planificador está
                vivo, o el pedido es más reciente que `trabado_tras`: no se pisa.
        """
        if objetivo is not None and (
            existente := self._existente(tx, objetivo, descartadas=False)
        ):
            raise EfemerideExistente(existente, objetivo.alcance)
        descripcion = None if objetivo is None else _objetivo_json(objetivo)
        actual = tx.execute(
            "SELECT pedido_manual_el, pedido_manual_objetivo,"
            " coalesce(now() - pedido_manual_el > %s, false) AS trabado"
            " FROM acervo.programacion_efemerides FOR UPDATE",
            (trabado_tras,),
        ).fetchone()
        assert actual is not None
        antes = None
        if actual["pedido_manual_el"] is not None:
            antes = {
                "objetivo": actual["pedido_manual_objetivo"],
                "pedido_manual_el": actual["pedido_manual_el"].isoformat(),
            }
        if antes is not None and actual["pedido_manual_objetivo"] == descripcion:
            marcado: datetime = actual["pedido_manual_el"]
        elif antes is not None and not actual["trabado"]:
            raise PedidoPendiente(antes)
        else:
            fila = tx.execute(
                "UPDATE acervo.programacion_efemerides SET pedido_manual_el = now(),"
                " pedido_manual_objetivo = %s, pedido_manual_por = %s RETURNING pedido_manual_el",
                (None if descripcion is None else Jsonb(descripcion), por.usuario_id),
            ).fetchone()
            assert fila is not None
            marcado = fila["pedido_manual_el"]
        self._auditoria.registrar(
            tx,
            actor=por,
            accion=Accion.EFEMERIDE_PEDIDO,
            objeto=_PROGRAMACION,
            antes=antes,
            despues={"objetivo": descripcion},
        )
        return marcado

    def correr_programadas(self, habilitado: Callable[[], bool], *, ahora: datetime) -> int:
        """La tarea del planificador (R-08): atiende «Generar ahora» o, sin pedido, la corrida
        programada cuando está activa y su periodicidad toca (060). Devuelve cuántas generó.

        Con objetivo genera solo ese. Sin objetivo, la ventana: los días de hoy (la fecha de
        `ahora`) a hoy más `anticipacion_dias`, y los meses cuyo día 1 cae ahí, según
        `alcances`, salteando los que ya tienen efeméride en cualquier estado, hasta
        `maximo_por_corrida` (ADR-0004). Lo de un pedido es `manual`, a nombre de quien pidió; lo
        programado, del sistema. Un objetivo que no deja Propuesta se omite con su motivo en el
        log y en el evento de la corrida. Si se frena o el motor no responde, corta antes del
        objetivo siguiente y ni el pedido se limpia ni la corrida programada se da por hecha: lo
        retoma la vuelta que siga. La corrida queda siempre en la auditoría y en
        `ultima_corrida_el`; el pedido se limpia solo si terminó y no cambió mientras tanto.
        """
        with self._bd.transaccion() as tx:
            p = tx.execute(
                "SELECT pedido_manual_el, pedido_manual_objetivo, pedido_manual_por, alcances,"
                " anticipacion_dias, maximo_por_corrida,"
                f" habilitada AND coalesce(ultima_programada_el + {_PERIODO} <= %s, true) AS toca"
                " FROM acervo.programacion_efemerides p",
                (ahora,),
            ).fetchone()
        assert p is not None
        manual = p["pedido_manual_el"] is not None
        if not manual and not p["toca"]:
            return 0
        pedido = p["pedido_manual_objetivo"]
        objetivos = (
            [Objetivo(Alcance(pedido["tipo"]), date.fromisoformat(pedido["fecha"]))]
            if pedido
            else _ventana(ahora.date(), p["anticipacion_dias"], p["alcances"])
        )
        generadas = 0
        omitidas: list[dict[str, str]] = []
        cortada_por: str | None = None
        for objetivo in objetivos:
            if generadas >= p["maximo_por_corrida"]:
                break
            if not habilitado():  # el interruptor, el cortacircuito o el apagado
                _log.info("Generación de efemérides frenada; la próxima vuelta la retoma.")
                cortada_por = "freno"
                break
            try:
                self.generar(
                    objetivo, pedida_por=p["pedido_manual_por"], sobre_descartada=bool(pedido)
                )
                generadas += 1
            except MotorNoAlcanzable:
                _log.warning("El motor no responde; la próxima vuelta retoma las efemérides.")
                cortada_por = "motor"
                break
            except NadaQueGenerar as motivo:
                # En la ventana no es una omisión: ya tiene efeméride (ADR-0004) o no hay Fichas.
                if pedido:
                    omitidas.append({"fecha": objetivo.fecha.isoformat(), "motivo": str(motivo)})
            except (
                ObjetivoOmitido, LimiteExcedido, MotorNoDisponible, ConfiguracionInvalida
            ) as motivo:  # sin la fila de `redactar_efemeride`, el pedido no queda trabado
                _log.info("Se omitió la efeméride del %s: %s", objetivo.fecha, motivo)
                omitidas.append({"fecha": objetivo.fecha.isoformat(), "motivo": str(motivo)})
        with self._bd.transaccion() as tx:
            resultado: dict[str, Any] = {
                "objetivo": pedido,
                "generadas": generadas,
                "omitidas": omitidas,
            }
            if manual:
                resultado["pedido_limpiado"] = cortada_por is None and tx.execute(
                    "UPDATE acervo.programacion_efemerides SET pedido_manual_el = NULL,"
                    " pedido_manual_objetivo = NULL, pedido_manual_por = NULL"
                    " WHERE pedido_manual_el = %s RETURNING 1",
                    (p["pedido_manual_el"],),
                ).fetchone() is not None
            else:
                resultado["programada"] = True
                if cortada_por is None:
                    tx.execute(
                        "UPDATE acervo.programacion_efemerides SET ultima_programada_el = %s",
                        (ahora,),
                    )
            if cortada_por:
                resultado["cortada_por"] = cortada_por
            # P7f espera el resultado de su pedido: lo reconoce por `pedido_manual_el`.
            marcado = p["pedido_manual_el"].isoformat() if manual else None
            tx.execute(
                "UPDATE acervo.programacion_efemerides SET ultima_corrida_el = now(),"
                " ultima_corrida_resultado = %s",
                (Jsonb({"pedido_manual_el": marcado, **resultado}),),
            )
            self._auditoria.registrar(
                tx,
                actor=_SISTEMA,
                accion=Accion.EFEMERIDE_CORRIDA,
                objeto=_PROGRAMACION,
                despues=resultado,
            )
        return generadas

    def programacion(self, tx: Tx) -> Programacion:
        """La programación, el pedido pendiente, el resultado de la última corrida y cuándo
        toca la próxima."""
        fila = tx.execute(
            "SELECT p.habilitada, p.periodicidad, p.anticipacion_dias, p.maximo_por_corrida,"
            " p.pedido_manual_el, p.pedido_manual_objetivo, p.ultima_corrida_el,"
            " p.ultima_corrida_resultado AS ultima_corrida,"
            # Sin corrida programada todavía, toca desde que se guardó; si ya pasó, toca ya.
            " CASE WHEN p.habilitada THEN"
            f" coalesce(p.ultima_programada_el + {_PERIODO}, p.actualizada_el)"
            " END AS proxima_corrida_el,"
            " p.version, u.email AS actualizada_por, p.actualizada_el"
            " FROM acervo.programacion_efemerides p"
            " LEFT JOIN acervo.usuario u ON u.usuario_id = p.actualizada_por"
        ).fetchone()
        assert fila is not None
        try:
            self._configuracion.instantanea(tx, capacidades=(Capacidad.REDACTAR_EFEMERIDE,))
            falta_modelo = False
        except ConfiguracionInvalida:
            falta_modelo = True
        return Programacion(**fila, falta_modelo=falta_modelo)

    def guardar_programacion(
        self,
        tx: Tx,
        *,
        version_base: int,
        habilitada: bool,
        periodicidad: Periodicidad,
        anticipacion_dias: int,
        maximo_por_corrida: int,
        por: Actor,
    ) -> Programacion:
        """P7f (060): cambia la programación si sigue en `version_base` y lo audita. Una corrida
        en curso conserva la configuración que leyó. Los rangos los validan la API y los
        `CHECK`; quien llama exige OPERAR_SISTEMA.

        Raises:
            ProgramacionDesactualizada: Otra persona la guardó desde que se leyó.
        """
        antes = tx.execute(
            "SELECT habilitada, periodicidad, anticipacion_dias, maximo_por_corrida, version"
            " FROM acervo.programacion_efemerides FOR UPDATE"
        ).fetchone()
        assert antes is not None
        if antes.pop("version") != version_base:
            raise ProgramacionDesactualizada(version_base, self.programacion(tx))
        despues = {
            "habilitada": habilitada,
            "periodicidad": periodicidad,
            "anticipacion_dias": anticipacion_dias,
            "maximo_por_corrida": maximo_por_corrida,
        }
        tx.execute(
            "UPDATE acervo.programacion_efemerides SET habilitada = %(habilitada)s,"
            " periodicidad = %(periodicidad)s, anticipacion_dias = %(anticipacion_dias)s,"
            " maximo_por_corrida = %(maximo_por_corrida)s, version = version + 1,"
            " actualizada_por = %(por)s, actualizada_el = now()",
            {**despues, "por": por.usuario_id},
        )
        self._auditoria.registrar(
            tx,
            actor=por,
            accion=Accion.EFEMERIDE_PROGRAMACION,
            objeto=_PROGRAMACION,
            antes=antes,
            despues=despues,
        )
        return self.programacion(tx)

    def listar(
        self,
        tx: Tx,
        *,
        principal: Principal,
        estados: Collection[Estado],
        desde: date | None = None,
        hasta: date | None = None,
    ) -> list[VistaDeEfemeride]:
        """De la más reciente a la más antigua, sin fuentes. Sin `estados`, todas las que puede
        leer quien pide; con `desde` o `hasta`, las que se difunden en parte de ese rango (R-11).

        Raises:
            PermisoInsuficiente: Pide otro estado que Aprobada sin REVISAR_EFEMERIDES (R-05).
        """
        if not principal.puede(Permiso.REVISAR_EFEMERIDES):
            if set(estados) - {Estado.APROBADA}:
                raise PermisoInsuficiente("Solo podés ver las efemérides Aprobadas.")
            estados = (Estado.APROBADA,)
        # shortcut: sin cursor, trunca en LISTA_MAXIMA; sumarlo cuando una ventana pase de 200.
        filas = tx.execute(
            f"{_SELECT_VISTA} WHERE (cardinality(%(estados)s::text[]) = 0"
            " OR e.estado = ANY(%(estados)s))"
            " AND (%(hasta)s::date IS NULL OR e.difusion_desde <= %(hasta)s)"
            " AND (%(desde)s::date IS NULL OR e.difusion_hasta >= %(desde)s)"
            " ORDER BY e.generada_el DESC, e.efemeride_id LIMIT %(limite)s",
            {"estados": list(estados), "desde": desde, "hasta": hasta, "limite": LISTA_MAXIMA},
        ).fetchall()
        return [_vista(fila) for fila in filas]

    def obtener(
        self, tx: Tx, efemeride_id: UUID, *, principal: Principal
    ) -> VistaDeEfemeride:
        """La efeméride con sus fuentes, en el orden de sus `[n]`.

        Raises:
            EfemerideNoEncontrada: No existe, o no está Aprobada y quien pide no tiene
                REVISAR_EFEMERIDES: no se entera de que existe (R-05).
        """
        fila = tx.execute(f"{_SELECT_VISTA} WHERE e.efemeride_id = %s", (efemeride_id,)).fetchone()
        if fila is None or (
            fila["estado"] != Estado.APROBADA and not principal.puede(Permiso.REVISAR_EFEMERIDES)
        ):
            raise EfemerideNoEncontrada(f"No existe la efeméride {efemeride_id}.")
        guardadas = tx.execute(
            "SELECT posicion, pieza_id, ficha_id, alerta_inyeccion FROM acervo.efemeride_fuente"
            " WHERE efemeride_id = %s ORDER BY posicion",
            (efemeride_id,),
        ).fetchall()
        return _vista(fila, self._con_resumen(tx, guardadas))

    def proponer_relato(
        self,
        tx: Tx,
        *,
        mensaje_id: UUID,
        titulo: str,
        texto: str,
        fuentes: Sequence[FuenteDeRelato],
        configuracion_efectiva: dict[str, Any],
        principal: Principal,
    ) -> VistaDeRelato:
        """«Proponer como relato» (DEC-147): el Relato Propuesto de la respuesta `mensaje_id`,
        con `texto` tal cual y sus fuentes, sin llamar a ningún modelo. Lo llama solo
        `conversacion`, que ya verificó las Citas. Una Ficha fuente con alerta de inyección deja
        el aviso.

        Raises:
            PermisoInsuficiente: Sin REVISAR_EFEMERIDES (R-42).
            RelatoYaPropuesto: La respuesta ya tiene un Relato no descartado.
            RespuestaNoProponible: Una Pieza fuente ya no está Validada, fue retirada o no es
                publicable (DEC-143).
        """
        if not principal.puede(Permiso.REVISAR_EFEMERIDES):
            raise PermisoInsuficiente("Proponer un relato exige REVISAR_EFEMERIDES.")
        # Dos «Proponer» de la misma respuesta a la vez: el segundo ve el Relato del primero.
        tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"relato:{mensaje_id}",)
        )
        existente = tx.execute(
            "SELECT relato_id FROM acervo.relato WHERE mensaje_id = %s AND estado <> 'descartada'",
            (mensaje_id,),
        ).fetchone()
        if existente is not None:
            raise RelatoYaPropuesto(existente["relato_id"])
        actuales = self._fichas.fuentes(tx, {f.pieza_id for f in fuentes}, bloquear=True)
        if not all(
            (actual := actuales.get(f.pieza_id)) and actual.validada and actual.publicable
            for f in fuentes
        ):
            raise RespuestaNoProponible(
                "Una Ficha citada en la respuesta ya no está Validada o no es publicable.",
                estado_actual="completa",
            )
        alertas = [actuales[f.pieza_id].alerta_inyeccion for f in fuentes]
        fila = tx.execute(
            "INSERT INTO acervo.relato (mensaje_id, titulo, cuerpo, alerta_inyeccion,"
            " configuracion_efectiva, propuesto_por) VALUES (%s, %s, %s, %s, %s, %s)"
            " RETURNING relato_id",
            (
                mensaje_id, titulo, texto, any(alertas), Jsonb(configuracion_efectiva),
                principal.usuario_id,
            ),  # fmt: skip
        ).fetchone()
        assert fila is not None
        relato_id: UUID = fila["relato_id"]
        tx.cursor().executemany(
            "INSERT INTO acervo.relato_fuente (relato_id, posicion, pieza_id, ficha_id,"
            " alerta_inyeccion) VALUES (%s, %s, %s, %s, %s)",
            [
                (relato_id, f.posicion, f.pieza_id, f.ficha_id, alerta)
                for f, alerta in zip(fuentes, alertas, strict=True)
            ],
        )
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.RELATO_PROPUESTA,
            objeto=ObjetoRef("relato", str(relato_id)),
            despues={
                "mensaje_id": str(mensaje_id),
                "fuentes": list(dict.fromkeys(str(f.pieza_id) for f in fuentes)),
            },
        )
        return self.obtener_relato(tx, relato_id, principal=principal)

    def listar_relatos(
        self,
        tx: Tx,
        *,
        principal: Principal,
        estados: Collection[Estado],
        desde: date | None = None,
        hasta: date | None = None,
    ) -> list[VistaDeRelato]:
        """Del más reciente al más antiguo, sin fuentes. Sin `estados`, todos los que puede
        leer quien pide; con `desde` o `hasta`, los aprobados esos días (un Propuesto no entra).

        Raises:
            PermisoInsuficiente: Pide otro estado que Aprobada sin REVISAR_EFEMERIDES (R-05).
        """
        if not principal.puede(Permiso.REVISAR_EFEMERIDES):
            if set(estados) - {Estado.APROBADA}:
                raise PermisoInsuficiente("Solo podés ver los relatos Aprobados.")
            estados = (Estado.APROBADA,)
        # shortcut: sin cursor, trunca en LISTA_MAXIMA; sumarlo cuando haya más de 200.
        filas = tx.execute(
            f"{_SELECT_RELATO} WHERE (cardinality(%(estados)s::text[]) = 0"
            " OR r.estado = ANY(%(estados)s))"
            f" AND {_APROBADO_ENTRE}"
            " ORDER BY r.propuesto_el DESC, r.relato_id LIMIT %(limite)s",
            {
                "estados": list(estados), "desde": desde, "hasta": hasta, "zona": _ZONA_HORARIA,
                "limite": LISTA_MAXIMA,
            },  # fmt: skip
        ).fetchall()
        return [_vista_de_relato(fila) for fila in filas]

    def obtener_relato(self, tx: Tx, relato_id: UUID, *, principal: Principal) -> VistaDeRelato:
        """El Relato con sus fuentes, en el orden de sus `[n]`.

        Raises:
            RelatoNoEncontrado: No existe, o no está Aprobado y quien pide no tiene
                REVISAR_EFEMERIDES: no se entera de que existe (R-05).
        """
        fila = tx.execute(f"{_SELECT_RELATO} WHERE r.relato_id = %s", (relato_id,)).fetchone()
        if fila is None or (
            fila["estado"] != Estado.APROBADA and not principal.puede(Permiso.REVISAR_EFEMERIDES)
        ):
            raise RelatoNoEncontrado(f"No existe el relato {relato_id}.")
        guardadas = tx.execute(
            "SELECT posicion, pieza_id, ficha_id, alerta_inyeccion FROM acervo.relato_fuente"
            " WHERE relato_id = %s ORDER BY posicion",
            (relato_id,),
        ).fetchall()
        return _vista_de_relato(fila, self._con_resumen(tx, guardadas))

    def _con_resumen(
        self, tx: Tx, guardadas: list[dict[str, Any]]
    ) -> tuple[FuenteDeEfemeride, ...]:
        """Las fuentes guardadas, con el resumen y la Fecha actuales de su Pieza."""
        piezas = self._fichas.fuentes(tx, [g["pieza_id"] for g in guardadas])
        return tuple(
            FuenteDeEfemeride(
                posicion=g["posicion"],
                pieza_id=g["pieza_id"],
                ficha_id=g["ficha_id"],
                alerta_inyeccion=g["alerta_inyeccion"],
                resumen=piezas[g["pieza_id"]].resumen,
                fecha_inicio=piezas[g["pieza_id"]].fecha_inicio,
                fecha_fin=piezas[g["pieza_id"]].fecha_fin,
                precision_fecha=piezas[g["pieza_id"]].precision_fecha,
                retirada=not piezas[g["pieza_id"]].validada,
            )
            for g in guardadas
        )

    def aprobar(
        self, tx: Tx, efemeride_id: UUID, *, version_base: int, principal: Principal
    ) -> VistaDeEfemeride:
        """Propuesta → Aprobada, firmada por `principal` con su método de autenticación
        (ADR-0002). Bloquea la fila: de dos aprobaciones a la vez, la segunda la ve Aprobada.
        Quien llama exige REVISAR_EFEMERIDES.

        Raises:
            EfemerideNoEncontrada: No existe.
            EfemerideNoPropuesta: Ya está Aprobada o Descartada (R-12).
            EfemerideDesactualizada: `version_base` no es la vigente.
        """
        self._propuesta(tx, efemeride_id, version_base)
        tx.execute(
            "UPDATE acervo.efemeride SET estado = 'aprobada', aprobada_por = %s,"
            " aprobada_el = now(), metodo_autenticacion = %s WHERE efemeride_id = %s",
            (principal.usuario_id, principal.metodo.value, efemeride_id),
        )
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.EFEMERIDE_APROBACION,
            objeto=ObjetoRef("efemeride", str(efemeride_id)),
            despues={"version": version_base, "metodo_autenticacion": principal.metodo.value},
        )
        return self.obtener(tx, efemeride_id, principal=principal)

    def editar(
        self,
        tx: Tx,
        efemeride_id: UUID,
        *,
        version_base: int,
        titulo: str | None,
        texto: str | None,
        principal: Principal,
    ) -> VistaDeEfemeride:
        """Corrige el título, el texto o los dos de una Propuesta: sube la versión, guarda quién y
        cuándo, y audita el antes y el después. Quien llama exige REVISAR_EFEMERIDES.

        Raises:
            EfemerideNoEncontrada: No existe.
            EfemerideNoPropuesta: Ya está Aprobada o Descartada (R-12).
            EfemerideDesactualizada: Otra persona la cambió desde `version_base`.
        """
        antes = self._propuesta(tx, efemeride_id, version_base)
        despues = tx.execute(
            "UPDATE acervo.efemeride SET titulo = coalesce(%s, titulo),"
            " cuerpo = coalesce(%s, cuerpo), version = version + 1, editada_por = %s,"
            " editada_el = now() WHERE efemeride_id = %s RETURNING version, titulo, cuerpo",
            (titulo, texto, principal.usuario_id, efemeride_id),
        ).fetchone()
        assert despues is not None
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.EFEMERIDE_EDICION,
            objeto=ObjetoRef("efemeride", str(efemeride_id)),
            antes={k: antes[k] for k in ("version", "titulo", "cuerpo")},
            despues=dict(despues),
        )
        return self.obtener(tx, efemeride_id, principal=principal)

    def descartar(
        self, tx: Tx, efemeride_id: UUID, *, motivo: str, principal: Principal
    ) -> VistaDeEfemeride:
        """Propuesta → Descartada con su motivo: queda inmutable y no se regenera sola
        (ADR-0004). Quien llama exige REVISAR_EFEMERIDES y valida el motivo (5 a 500).

        Raises:
            EfemerideNoEncontrada: No existe.
            EfemerideNoPropuesta: Ya está Aprobada o Descartada (R-12).
        """
        antes = self._propuesta(tx, efemeride_id)
        tx.execute(
            "UPDATE acervo.efemeride SET estado = 'descartada', descartada_por = %s,"
            " descartada_el = now(), descarte_motivo = %s WHERE efemeride_id = %s",
            (principal.usuario_id, motivo, efemeride_id),
        )
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.EFEMERIDE_DESCARTE,
            objeto=ObjetoRef("efemeride", str(efemeride_id)),
            despues={"version": antes["version"], "motivo": motivo},
        )
        return self.obtener(tx, efemeride_id, principal=principal)

    def _propuesta(
        self, tx: Tx, efemeride_id: UUID, version_base: int | None = None
    ) -> dict[str, Any]:
        """Bloquea la fila y la devuelve si sigue Propuesta (y en `version_base`, si se pasa):
        de dos revisiones a la vez, la segunda ve lo que dejó la primera."""
        fila = tx.execute(
            "SELECT e.estado, e.version, e.titulo, e.cuerpo, u.nombre FROM acervo.efemeride e"
            " LEFT JOIN acervo.usuario u"
            " ON u.usuario_id = coalesce(e.aprobada_por, e.descartada_por)"
            " WHERE e.efemeride_id = %s FOR UPDATE OF e",
            (efemeride_id,),
        ).fetchone()
        if fila is None:
            raise EfemerideNoEncontrada(f"No existe la efeméride {efemeride_id}.")
        if fila["estado"] != Estado.PROPUESTA:
            raise EfemerideNoPropuesta(Estado(fila["estado"]), fila["nombre"])
        if version_base is not None and fila["version"] != version_base:
            raise EfemerideDesactualizada(version_base, fila["version"])
        return dict(fila)

    # La revisión de los Relatos (065) es la de la efeméride sobre `relato`: mismos estados,
    # mismos chequeos y mismo orden (primero el estado, después la versión).

    def aprobar_relato(
        self, tx: Tx, relato_id: UUID, *, version_base: int, principal: Principal
    ) -> VistaDeRelato:
        """Propuesto → Aprobado, firmado por `principal` con su método de autenticación
        (ADR-0002); sus fuentes son las Citas con que se propuso. Quien llama exige
        REVISAR_EFEMERIDES.

        Raises:
            RelatoNoEncontrado: No existe.
            RelatoNoPropuesto: Ya está Aprobado o Descartado (R-12).
            RelatoDesactualizado: `version_base` no es la vigente.
        """
        self._relato_propuesto(tx, relato_id, version_base)
        tx.execute(
            "UPDATE acervo.relato SET estado = 'aprobada', aprobado_por = %s,"
            " aprobado_el = now(), metodo_autenticacion = %s WHERE relato_id = %s",
            (principal.usuario_id, principal.metodo.value, relato_id),
        )
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.RELATO_APROBACION,
            objeto=ObjetoRef("relato", str(relato_id)),
            despues={"version": version_base, "metodo_autenticacion": principal.metodo.value},
        )
        return self.obtener_relato(tx, relato_id, principal=principal)

    def editar_relato(
        self,
        tx: Tx,
        relato_id: UUID,
        *,
        version_base: int,
        titulo: str | None,
        texto: str | None,
        principal: Principal,
    ) -> VistaDeRelato:
        """Corrige el título, el texto o los dos de un Propuesto, como `editar`.

        Raises:
            RelatoNoEncontrado: No existe.
            RelatoNoPropuesto: Ya está Aprobado o Descartado (R-12).
            RelatoDesactualizado: Otra persona lo cambió desde `version_base`.
        """
        antes = self._relato_propuesto(tx, relato_id, version_base)
        despues = tx.execute(
            "UPDATE acervo.relato SET titulo = coalesce(%s, titulo),"
            " cuerpo = coalesce(%s, cuerpo), version = version + 1, editado_por = %s,"
            " editado_el = now() WHERE relato_id = %s RETURNING version, titulo, cuerpo",
            (titulo, texto, principal.usuario_id, relato_id),
        ).fetchone()
        assert despues is not None
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.RELATO_EDICION,
            objeto=ObjetoRef("relato", str(relato_id)),
            antes={k: antes[k] for k in ("version", "titulo", "cuerpo")},
            despues=dict(despues),
        )
        return self.obtener_relato(tx, relato_id, principal=principal)

    def descartar_relato(
        self, tx: Tx, relato_id: UUID, *, motivo: str, principal: Principal
    ) -> VistaDeRelato:
        """Propuesto → Descartado con su motivo: inmutable. La misma respuesta se puede volver a
        proponer. Quien llama exige REVISAR_EFEMERIDES y valida el motivo (5 a 500).

        Raises:
            RelatoNoEncontrado: No existe.
            RelatoNoPropuesto: Ya está Aprobado o Descartado (R-12).
        """
        antes = self._relato_propuesto(tx, relato_id)
        tx.execute(
            "UPDATE acervo.relato SET estado = 'descartada', descartado_por = %s,"
            " descartado_el = now(), descarte_motivo = %s WHERE relato_id = %s",
            (principal.usuario_id, motivo, relato_id),
        )
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.RELATO_DESCARTE,
            objeto=ObjetoRef("relato", str(relato_id)),
            despues={"version": antes["version"], "motivo": motivo},
        )
        return self.obtener_relato(tx, relato_id, principal=principal)

    def _relato_propuesto(
        self, tx: Tx, relato_id: UUID, version_base: int | None = None
    ) -> dict[str, Any]:
        """`_propuesta` sobre `relato`: bloquea la fila y la devuelve si sigue Propuesta."""
        fila = tx.execute(
            "SELECT r.estado, r.version, r.titulo, r.cuerpo, u.nombre FROM acervo.relato r"
            " LEFT JOIN acervo.usuario u"
            " ON u.usuario_id = coalesce(r.aprobado_por, r.descartado_por)"
            " WHERE r.relato_id = %s FOR UPDATE OF r",
            (relato_id,),
        ).fetchone()
        if fila is None:
            raise RelatoNoEncontrado(f"No existe el relato {relato_id}.")
        if fila["estado"] != Estado.PROPUESTA:
            raise RelatoNoPropuesto(Estado(fila["estado"]), fila["nombre"])
        if version_base is not None and fila["version"] != version_base:
            raise RelatoDesactualizado(version_base, fila["version"])
        return dict(fila)

    def marcar_fuente_retirada(self, tx: Tx, pieza_id: UUID) -> None:
        """La Pieza dejó de estar Validada o se retiró (R-12): las efemérides y los Relatos
        Propuestos y Aprobados que la citan quedan con «fuente retirada», sin tocar su texto.
        Corre en la transacción de la causa, que la llama por inyección (`validacion`; con 107,
        `retiro`): ningún `GET` escribe la marca (auditoría de tickets, decisión 11). Ya marcada,
        no cambia.
        """
        tx.execute(
            "UPDATE acervo.efemeride SET fuente_retirada_el = now()"
            " WHERE fuente_retirada_el IS NULL AND estado <> 'descartada' AND efemeride_id IN"
            " (SELECT efemeride_id FROM acervo.efemeride_fuente WHERE pieza_id = %s)",
            (pieza_id,),
        )
        tx.execute(
            "UPDATE acervo.relato SET fuente_retirada_el = now()"
            " WHERE fuente_retirada_el IS NULL AND estado <> 'descartada' AND relato_id IN"
            " (SELECT relato_id FROM acervo.relato_fuente WHERE pieza_id = %s)",
            (pieza_id,),
        )

    def relatos_aprobados(self, tx: Tx, desde: date, hasta: date) -> list[RelatoEntregable]:
        """Los Aprobados sin «fuente retirada» aprobados entre `desde` y `hasta` (días de la
        Facultad), del más viejo al más nuevo (DEC-147, R-12), sin los que citan una Pieza que hoy
        no es Validada o publicable. Es lo que entrega `/v1/relatos`, que valida el rango (92
        días como máximo)."""
        filas = tx.execute(
            "SELECT r.relato_id, r.titulo, r.cuerpo, a.email AS aprobado_por, r.aprobado_el,"
            " array(SELECT ARRAY[f.posicion::text, f.pieza_id::text, f.ficha_id::text]"
            " FROM acervo.relato_fuente f WHERE f.relato_id = r.relato_id ORDER BY f.posicion)"
            " AS fuentes"
            " FROM acervo.relato r JOIN acervo.usuario a ON a.usuario_id = r.aprobado_por"
            f" WHERE r.estado = 'aprobada' AND r.fuente_retirada_el IS NULL AND {_APROBADO_ENTRE}"
            " ORDER BY r.aprobado_el, r.relato_id",
            {"desde": desde, "hasta": hasta, "zona": _ZONA_HORARIA},
        ).fetchall()
        relatos = [
            RelatoEntregable(
                **{
                    **fila,
                    "fuentes": tuple(
                        FuenteDeRelato(int(posicion), UUID(pieza), UUID(ficha))
                        for posicion, pieza, ficha in fila["fuentes"]
                    ),
                }
            )
            for fila in filas
        ]
        vigentes = self._vigentes(tx, {f.pieza_id for r in relatos for f in r.fuentes})
        return [r for r in relatos if {f.pieza_id for f in r.fuentes} <= vigentes]

    def aprobadas(self, tx: Tx, desde: date, hasta: date) -> list[EfemerideEntregable]:
        """Las Aprobadas sin «fuente retirada» que se difunden en parte de `desde`..`hasta`: la
        de mes sale con cualquier día de su mes (R-11, R-12), sin las que citan una Pieza que hoy
        no es Validada o publicable. Es lo que entrega `/v1/efemerides`, que valida el rango (92
        días como máximo)."""
        filas = tx.execute(
            "SELECT e.efemeride_id, e.alcance, e.fecha_objetivo, e.difusion_desde,"
            " e.difusion_hasta, e.titulo, e.cuerpo, a.email AS aprobada_por, e.aprobada_el,"
            " array(SELECT f.pieza_id FROM acervo.efemeride_fuente f"
            " WHERE f.efemeride_id = e.efemeride_id ORDER BY f.posicion) AS piezas_fuente"
            " FROM acervo.efemeride e JOIN acervo.usuario a ON a.usuario_id = e.aprobada_por"
            " WHERE e.estado = 'aprobada' AND e.fuente_retirada_el IS NULL"
            " AND e.difusion_desde <= %s AND e.difusion_hasta >= %s"
            " ORDER BY e.difusion_desde, e.efemeride_id",
            (hasta, desde),
        ).fetchall()
        efemerides = [
            EfemerideEntregable(
                **{
                    **fila,
                    "alcance": Alcance(fila["alcance"]),
                    "piezas_fuente": tuple(fila["piezas_fuente"]),
                }
            )
            for fila in filas
        ]
        vigentes = self._vigentes(tx, {p for e in efemerides for p in e.piezas_fuente})
        return [e for e in efemerides if set(e.piezas_fuente) <= vigentes]

    def _vigentes(self, tx: Tx, pieza_ids: Collection[UUID]) -> set[UUID]:
        """De `pieza_ids`, las que hoy pueden respaldar lo que sale por `/v1`: Validadas, no
        retiradas y publicables. Cubre lo que la marca no ve: `publicable` apagado después de
        aprobar (123; aprobar no lo vuelve a mirar) y un retiro que todavía no marca (107)."""
        return {
            pieza_id
            for pieza_id, fuente in self._fichas.fuentes(tx, pieza_ids).items()
            if fuente.validada and fuente.publicable
        }

    def _existente(self, tx: Tx, objetivo: Objetivo, *, descartadas: bool) -> UUID | None:
        """La efeméride de `objetivo`, Propuesta o Aprobada, o también Descartada."""
        fila = tx.execute(
            "SELECT efemeride_id FROM acervo.efemeride WHERE alcance = %s AND fecha_objetivo = %s"
            " AND (%s OR estado <> 'descartada') LIMIT 1",
            (objetivo.alcance, objetivo.fecha, descartadas),
        ).fetchone()
        if fila is None:
            return None
        existente: UUID = fila["efemeride_id"]
        return existente

    def _redactar(
        self, objetivo: Objetivo, fuentes: list[FuenteDeFecha], configuracion: ConfiguracionEfectiva
    ) -> str:
        """El texto del modelo sin las Citas que no son de ninguna fuente.

        Raises:
            ObjetivoOmitido: Ninguna Cita es de una fuente.
        """
        capa = self._capa_para(configuracion.por_capacidad)
        texto = capa.redactar_efemeride(
            fichas=tuple(BloqueDatos(ETIQUETA_FICHA, _como_dato(f)) for f in fuentes),
            plantilla=_PLANTILLAS[objetivo.alcance],
        ).texto
        citas = verificar_citas(texto, len(fuentes))
        if not any(cita.verificada for cita in citas):
            raise ObjetivoOmitido("el texto redactado no tiene Citas válidas.")
        return quitar_citas(texto, [cita.marca for cita in citas if not cita.verificada]).strip()

    def _guardar(
        self,
        tx: Tx,
        objetivo: Objetivo,
        fuentes: list[FuenteDeFecha],
        cuerpo: str,
        configuracion: ConfiguracionEfectiva,
        pedida_por: UUID | None,
    ) -> UUID:
        """Inserta la Propuesta, sus fuentes y el evento, si las Fichas siguen como se leyeron.

        Raises:
            ObjetivoOmitido: Una Ficha fuente dejó de estar Validada o cambió de versión, u otra
                generación guardó el mismo objetivo mientras se redactaba.
        """
        actuales = self._fichas.fuentes(tx, [f.pieza_id for f in fuentes], bloquear=True)
        if any(
            not (actual := actuales.get(f.pieza_id)) or not actual.validada
            or not actual.publicable or actual.ficha_id != f.ficha_id
            for f in fuentes
        ):
            raise ObjetivoOmitido("una Ficha fuente cambió mientras se redactaba.")
        desde, hasta = objetivo.difusion  # ese día, o el mes entero (R-11)
        try:
            fila = tx.execute(
                "INSERT INTO acervo.efemeride (alcance, fecha_objetivo, difusion_desde,"
                " difusion_hasta, titulo, cuerpo, alerta_inyeccion, configuracion_efectiva,"
                " origen_generacion, generada_por) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " RETURNING efemeride_id",
                (
                    objetivo.alcance,
                    objetivo.fecha,
                    desde,
                    hasta,
                    (
                        f"Un día como hoy, {objetivo.nombre}"
                        if objetivo.alcance is Alcance.DIA
                        else f"Un mes como este, {objetivo.nombre}"
                    ),
                    cuerpo,
                    any(f.alerta_inyeccion for f in fuentes),
                    Jsonb(configuracion.a_json()),
                    "programada" if pedida_por is None else "manual",
                    pedida_por,
                ),
            ).fetchone()
        except UniqueViolation as error:
            raise ObjetivoOmitido(f"{objetivo.fecha} ya tiene una efeméride vigente.") from error
        assert fila is not None
        efemeride_id: UUID = fila["efemeride_id"]
        tx.cursor().executemany(
            "INSERT INTO acervo.efemeride_fuente (efemeride_id, pieza_id, ficha_id, posicion,"
            " alerta_inyeccion) VALUES (%s, %s, %s, %s, %s)",
            [
                (efemeride_id, f.pieza_id, f.ficha_id, posicion, f.alerta_inyeccion)
                for posicion, f in enumerate(fuentes, start=1)
            ],
        )
        self._auditoria.registrar(
            tx,
            actor=_SISTEMA,
            accion=Accion.EFEMERIDE_GENERACION,
            objeto=ObjetoRef("efemeride", str(efemeride_id)),
            despues={
                "alcance": objetivo.alcance.value,
                "fecha_objetivo": objetivo.fecha.isoformat(),
                "fuentes": [str(f.pieza_id) for f in fuentes],
                "modelo": configuracion.por_capacidad[Capacidad.REDACTAR_EFEMERIDE].modelo,
            },
        )
        return efemeride_id


_SELECT_VISTA = (
    "SELECT e.efemeride_id, e.alcance, e.fecha_objetivo, e.difusion_desde, e.difusion_hasta,"
    " e.titulo, e.cuerpo, e.version, e.estado, e.alerta_inyeccion,"
    " e.fuente_retirada_el IS NOT NULL AS fuente_retirada, e.plantilla,"
    " e.configuracion_efectiva, u.email AS generada_por, e.generada_el,"
    " a.email AS aprobada_por, a.nombre AS aprobada_por_nombre, e.aprobada_el,"
    " e.metodo_autenticacion, ed.email AS editada_por, e.editada_el,"
    " d.email AS descartada_por, d.nombre AS descartada_por_nombre, e.descartada_el,"
    " e.descarte_motivo"
    " FROM acervo.efemeride e LEFT JOIN acervo.usuario u ON u.usuario_id = e.generada_por"
    " LEFT JOIN acervo.usuario a ON a.usuario_id = e.aprobada_por"
    " LEFT JOIN acervo.usuario ed ON ed.usuario_id = e.editada_por"
    " LEFT JOIN acervo.usuario d ON d.usuario_id = e.descartada_por"
)


_ZONA_HORARIA = "-03:00"
"""La de la Facultad: «aprobado el 3 de mayo» es ese día en Buenos Aires, no en UTC. Como
intervalo y no por nombre: Argentina no tiene horario de verano desde 2009 y el Postgres de
pruebas en Windows no trae la base de zonas horarias."""

_APROBADO_ENTRE = (
    "(%(desde)s::date IS NULL"
    " OR r.aprobado_el >= %(desde)s::date::timestamp AT TIME ZONE %(zona)s::interval)"
    " AND (%(hasta)s::date IS NULL"
    " OR r.aprobado_el < (%(hasta)s::date + 1)::timestamp AT TIME ZONE %(zona)s::interval)"
)
"""Aprobado entre `desde` y `hasta`, los dos días incluidos (DEC-147)."""

_SELECT_RELATO = (
    "SELECT r.relato_id, r.mensaje_id, r.titulo, r.cuerpo, r.version, r.estado,"
    " r.alerta_inyeccion, r.fuente_retirada_el IS NOT NULL AS fuente_retirada,"
    " p.email AS propuesto_por, r.propuesto_el, e.email AS editado_por, r.editado_el,"
    " a.email AS aprobado_por, a.nombre AS aprobado_por_nombre, r.aprobado_el,"
    " r.metodo_autenticacion, d.email AS descartado_por, d.nombre AS descartado_por_nombre,"
    " r.descartado_el, r.descarte_motivo,"
    " (SELECT count(DISTINCT f.pieza_id) FROM acervo.relato_fuente f"
    " WHERE f.relato_id = r.relato_id)::int AS cantidad_fuentes"
    " FROM acervo.relato r JOIN acervo.usuario p ON p.usuario_id = r.propuesto_por"
    " LEFT JOIN acervo.usuario e ON e.usuario_id = r.editado_por"
    " LEFT JOIN acervo.usuario a ON a.usuario_id = r.aprobado_por"
    " LEFT JOIN acervo.usuario d ON d.usuario_id = r.descartado_por"
)


def _vista_de_relato(
    fila: dict[str, Any], fuentes: tuple[FuenteDeEfemeride, ...] = ()
) -> VistaDeRelato:
    return VistaDeRelato(**{**fila, "estado": Estado(fila["estado"])}, fuentes=fuentes)


def _vista(fila: dict[str, Any], fuentes: tuple[FuenteDeEfemeride, ...] = ()) -> VistaDeEfemeride:
    return VistaDeEfemeride(
        **{**fila, "alcance": Alcance(fila["alcance"]), "estado": Estado(fila["estado"])},
        fuentes=fuentes,
    )


def _como_dato(fuente: FuenteDeFecha) -> str:
    """Lo que ve el modelo de una Ficha: de cuándo es, qué es y su resumen."""
    fecha = fuente.fecha_inicio.isoformat() if fuente.fecha_inicio else ""
    return f"Fecha: {fecha}\nTipo: {fuente.tipo or ''}\nResumen: {fuente.resumen or ''}"


def _ventana(hoy: date, anticipacion_dias: int, alcances: Collection[str]) -> list[Objetivo]:
    """Los objetivos de `hoy` a `hoy` más la anticipación: cada día y, en su día 1, el mes."""
    objetivos = []
    for dias in range(anticipacion_dias + 1):
        fecha = hoy + timedelta(days=dias)
        if Alcance.DIA in alcances:
            objetivos.append(Objetivo(Alcance.DIA, fecha))
        if fecha.day == 1 and Alcance.MES in alcances:
            objetivos.append(Objetivo(Alcance.MES, fecha))
    return objetivos


def _objetivo_json(objetivo: Objetivo) -> dict[str, str]:
    """Como lo guarda `pedido_manual_objetivo` y lo manda la API."""
    return {"tipo": objetivo.alcance.value, "fecha": objetivo.fecha.isoformat()}


def contar_firmas_de_ensayo(tx: Tx) -> int:
    """Las efemérides y los Relatos aprobados con una sesión `dev` (DEC-139)."""
    fila = tx.execute(
        "SELECT (SELECT count(*) FROM acervo.efemeride WHERE metodo_autenticacion = 'dev')"
        " + (SELECT count(*) FROM acervo.relato WHERE metodo_autenticacion = 'dev') AS n"
    ).fetchone()
    return int(fila["n"]) if fila else 0
