"""El registro de auditoría: quién hizo qué, append-only (spec, módulo `auditoria`; RF-45).

`registrar` escribe en la transacción del llamador: si el cambio se revierte, el evento
tampoco queda, y si se confirma, siempre queda (ADR-0002, RNF-07). `hv_app` solo tiene
`INSERT` y `SELECT` sobre la tabla: lo registrado no se borra ni se reescribe. Es lo único que
toca `acervo.auditoria` (regla de `test_arquitectura`).

Cómo se registra desde otro módulo (recibe el `RegistroDeAuditoria` de `composicion`)::

    with bd.transaccion() as tx:
        ...  # el cambio
        auditoria.registrar(tx, actor=actor, accion=Accion.PIEZA_INGRESO,
                            objeto=ObjetoRef("pieza", str(pieza_id)), despues={...})

`Accion` es un dominio cerrado: cada ticket suma las suyas acá (los nombres, en la tarjeta de
`auditoria` del spec). Nunca se registra un secreto, el cuerpo de una Ficha ni contenido.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from historia_viva.bd import Bd, Tx

__all__ = [
    "CLAVES_PROHIBIDAS",
    "Accion",
    "Actor",
    "Evento",
    "ObjetoRef",
    "RegistroDeAuditoria",
    "TipoDeActor",
]

CLAVES_PROHIBIDAS = frozenset({"token", "token_hash", "secreto", "password", "code", "id_token"})
"""Un secreto no se filtra a la auditoría por un error de código; de un agente va
`token_prefijo`."""


class Accion(StrEnum):
    PIEZA_INGRESO = "pieza.ingreso"
    """Una Pieza entró al Acervo (historia 290): quién, vía de ingreso y SHA-256."""
    ARCHIVO_RECHAZO = "archivo.rechazo"
    """Un archivo que no entró (DEC-106, historia 290): el motivo y, si se conocen, clase,
    tamaño y SHA-256; nunca el contenido ni el nombre."""
    APORTE_RECHAZO = "aporte.rechazo"
    """Un Aporte con etiquetas de control (DEC-133, R-07): el agente, cuáles y dónde (sección y
    línea), el tamaño y el SHA-256; nunca el contenido."""
    SESION_RECHAZO = "sesion.rechazo"
    """Un acceso denegado. Si no entró, el actor es `Actor.rechazado`, nunca el email completo;
    un 403 de `exigir` lleva a la persona, el permiso que le faltó y la ruta."""
    CATALOGO_CARGA = "catalogo.carga"
    """`catalogo carreras --cargar` (R-42): qué Carreras insertó, actualizó y desactivó."""
    SESION_INICIO = "sesion.inicio"
    """Una persona entró: con qué método, nunca la cookie."""
    SESION_CIERRE = "sesion.cierre"
    """Una persona salió: la sesión quedó revocada."""
    PERSONA_ALTA = "persona.alta"
    """Alta en el padrón; la del primer RGC lleva `interinato_terminado` (R-01)."""
    PERSONA_ROL = "persona.rol"
    PERSONA_BAJA = "persona.baja"
    """Baja lógica: sus sesiones quedaron revocadas."""
    PERSONA_REACTIVACION = "persona.reactivacion"
    AGENTE_ALTA = "agente.alta"
    """Un Agente externo nuevo, con su nombre y sus alcances (DEC-146 punto 13)."""
    AGENTE_EMISION = "agente.emision"
    """Un token emitido: el `token_prefijo` y el vencimiento, nunca el token."""
    AGENTE_REVOCACION = "agente.revocacion"
    """Un token revocado, o programado para revocarse al rotar: el `token_prefijo`."""
    PIEZA_REPROCESO = "pieza.reproceso"
    """Un reintento encolado: el trabajo, su tipo y la marca `forzar` si superó el tope de 6
    con `--forzar` (DEC-137)."""
    IMAGEN_DESCRIPCION_PEDIDA = "imagen.descripcion_pedida"
    """«Describir esta imagen» (148, SEG-19): la Pieza y el número de imagen; nunca su texto."""
    PIEZA_COMPLETAR_A_MANO = "pieza.completar_a_mano"
    """Una Recibida fallida pasó a `Pendiente` con la v1 `sistema` vacía (DEC-122, R-07)."""
    PIEZA_COMPLETAR_EN_RECIBIDA = "pieza.completar_en_recibida"
    """Campos duros o Contexto guardados con la Pieza Recibida, mientras se lee (152): cuáles
    campos cambiaron y si cambió el Contexto; nunca el texto. No crea versión ni firma."""
    LOTE_CREACION_IMPLICITA = "lote.creacion_implicita"
    """Arrastrar varios archivos creó el Lote sin pedir campos antes (151): su nombre provisorio."""
    LOTE_HERENCIA_TARDIA = "lote.herencia_tardia"
    """Un Lote con Piezas completó lo que tenía vacío (151, historia 74): qué campos y, por Pieza
    Recibida o Pendiente que no los tenía, cuáles se le copiaron."""
    AFIRMACION_DECISION = "afirmacion.decision"
    """Una Afirmación aceptada o rechazada (ADR-0007, 144): su número, cómo estaba y cómo quedó;
    nunca el texto. El actor es quien la decidió."""
    PIEZA_RETIRO = "pieza.retiro"
    """Una Pieza retirada del Acervo (DEC-29, R-07): el motivo, el estado en que estaba (`antes`),
    cuántos trabajos se cancelaron y la marca `interino` si la retiró el Administrador
    técnico durante el interinato (DEC-124)."""
    PIEZA_RESTAURACION = "pieza.restauracion"
    """Una retirada vuelve al Acervo (DEC-138): el motivo, el estado (`antes`), si se encoló su
    indexación o un procesamiento nuevo y la marca `interino` (DEC-124)."""
    PIEZA_SUPRESION = "pieza.supresion"
    """La supresión efectiva (runbook R14, 125): el registro sin contenido ni nombre. Quién (actor
    `cli`), cuándo, el SHA-256 del original, el motivo y cuántas filas se borraron."""
    BLOQUE_APROBACION = "bloque.aprobacion"
    """Una aprobación en bloque confirmada (CU-07, DEC-146 punto 13): el Lote, la clase, lo que
    aplicó, las Piezas firmadas y cuántas siguieron Pendientes."""
    INDICE_RECONSTRUCCION = "indice.reconstruccion"
    """`reindexar`: quién y cuántas indexaciones encoló (DEC-146 punto 13)."""
    MODELO_CAMBIO = "modelo.cambio"
    """La fila de una capacidad editada desde la pantalla (CU-19, DEC-87): solo los campos que
    cambiaron, antes y después."""
    CONVERSACION_INICIO = "conversacion.inicio"
    """Una Conversación nueva de la persona (DEC-129)."""
    CONVERSACION_MENSAJE = "conversacion.mensaje"
    """Un turno guardado: los ids de la pregunta y la respuesta, el estado y cuántas Fichas y
    Citas se mostraron; nunca el texto, que es de la persona (spec, módulo 31)."""
    EFEMERIDE_GENERACION = "efemeride.generacion"
    """Una efeméride Propuesta: su objetivo, sus fuentes y el modelo; nunca el texto (057)."""
    EFEMERIDE_PEDIDO = "efemeride.pedido"
    """«Generar ahora» (058): quién lo pidió y el objetivo, o ninguno si corre la ventana."""
    EFEMERIDE_CORRIDA = "efemeride.corrida"
    """El planificador terminó un pedido (058) o una corrida programada (060): el objetivo,
    cuántas generó y qué omitió y por qué."""
    EFEMERIDE_PROGRAMACION = "efemeride.programacion"
    """La programación de efemérides cambió (060): los valores de antes y los nuevos."""
    EFEMERIDE_APROBACION = "efemeride.aprobacion"
    """Una efeméride Aprobada (061): la versión que se firmó y con qué se entró."""
    EFEMERIDE_EDICION = "efemeride.edicion"
    """Una Propuesta editada (062): versión, título y texto de antes y de después."""
    EFEMERIDE_DESCARTE = "efemeride.descarte"
    """Una Propuesta Descartada (062): la versión y el motivo."""
    RELATO_PROPUESTA = "relato.propuesta"
    """«Proponer como relato» (064): la respuesta de origen y las Piezas fuente; nunca el
    texto."""
    RELATO_EDICION = "relato.edicion"
    """Un Relato Propuesto editado (065): versión, título y texto de antes y de después."""
    RELATO_APROBACION = "relato.aprobacion"
    """Un Relato Aprobado (065): la versión que se firmó y con qué se entró."""
    RELATO_DESCARTE = "relato.descarte"
    """Un Relato Propuesto Descartado (065): la versión y el motivo."""
    INFORME_GENERACION = "informe.generacion"
    """Un Borrador del Informe de acreditación: período, Carreras, cuántas Piezas y su huella
    (DEC-131)."""
    INFORME_APROBACION = "informe.aprobacion"
    """Una firma del informe: rol, método de autenticación, huella firmada y el estado en que
    quedó (068)."""
    INFORME_DESCARTE = "informe.descarte"
    """Un Borrador descartado (069): deja de listarse y no admite aprobaciones."""
    INFORME_EXPORTACION = "informe.exportacion"
    """Cada descarga del informe (070, R-23): formato, huella y si fue la primera, la que lo
    pasó a Exportado."""


class TipoDeActor(StrEnum):
    PERSONA = "persona"
    CLI = "cli"
    AGENTE = "agente"
    ANONIMO = "anonimo"
    SISTEMA = "sistema"


@dataclass(frozen=True)
class Actor:
    tipo: TipoDeActor
    etiqueta: str
    """Email de la persona, usuario del sistema operativo (`cli`), prefijo del token
    (`agente`) o el dominio y un hash corto del email en un acceso denegado."""
    usuario_id: UUID | None = None
    ip: str | None = None
    solicitud_id: str | None = None

    @classmethod
    def rechazado(
        cls, email: str, *, ip: str | None = None, solicitud_id: str | None = None
    ) -> Actor:
        """Quien intentó entrar y no pudo: el dominio y un hash corto del email (spec, Auditoría).
        El hash deja ver que es el mismo que insiste sin guardar quién es."""
        normalizado = email.strip().lower()
        _, arroba, dominio = normalizado.rpartition("@")
        corto = hashlib.sha256(normalizado.encode()).hexdigest()[:12]
        return cls(
            TipoDeActor.ANONIMO,
            f"{dominio if arroba else ''}#{corto}",
            ip=ip,
            solicitud_id=solicitud_id,
        )


@dataclass(frozen=True)
class ObjetoRef:
    tipo: str
    id: str | None = None


@dataclass(frozen=True)
class Evento:
    id: int
    cuando: datetime
    actor: Actor
    accion: Accion
    objeto: ObjetoRef
    antes: dict[str, Any] | None
    despues: dict[str, Any] | None
    motivo: str | None


class RegistroDeAuditoria:
    def __init__(self, *, bd: Bd) -> None:
        self._bd = bd

    def registrar(
        self,
        tx: Tx,
        *,
        actor: Actor,
        accion: Accion,
        objeto: ObjetoRef,
        antes: Mapping[str, Any] | None = None,
        despues: Mapping[str, Any] | None = None,
        motivo: str | None = None,
    ) -> None:
        """Escribe el evento en `tx`, la transacción del cambio que registra.

        Raises:
            ValueError: Si `antes` o `despues` traen, a cualquier profundidad, una clave de
                `CLAVES_PROHIBIDAS`, o si un actor anónimo trae un email.
        """
        if prohibidas := _claves_prohibidas(antes) | _claves_prohibidas(despues):
            raise ValueError(f"La auditoría no guarda secretos: {', '.join(sorted(prohibidas))}.")
        if actor.tipo is TipoDeActor.ANONIMO and "@" in actor.etiqueta:
            raise ValueError("Un anónimo no se registra con su email: usá `Actor.rechazado`.")
        tx.execute(
            "INSERT INTO acervo.auditoria (actor_tipo, actor_id, actor_etiqueta, ip,"
            " solicitud_id, accion, objeto_tipo, objeto_id, antes, despues, motivo)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                actor.tipo,
                actor.usuario_id,
                actor.etiqueta,
                actor.ip,
                actor.solicitud_id,
                accion,
                objeto.tipo,
                objeto.id,
                None if antes is None else Jsonb(antes),
                None if despues is None else Jsonb(despues),
                motivo,
            ),
        )

    def listar(
        self,
        *,
        objeto: ObjetoRef | None = None,
        accion: Accion | None = None,
        limite: int = 50,
        antes_de: int | None = None,
    ) -> list[Evento]:
        """Los eventos del más nuevo al más viejo.

        `objeto` sin `id` trae todos los de ese tipo. `antes_de` es el `id` del último evento de
        la página anterior: los de una misma transacción comparten `cuando` y paginar por fecha
        los saltearía.
        """
        with self._bd.transaccion() as tx:
            filas = tx.execute(
                "SELECT * FROM acervo.auditoria"
                " WHERE (%(tipo)s::text IS NULL OR objeto_tipo = %(tipo)s)"
                " AND (%(objeto_id)s::text IS NULL OR objeto_id = %(objeto_id)s)"
                " AND (%(accion)s::text IS NULL OR accion = %(accion)s)"
                " AND (%(antes_de)s::bigint IS NULL OR id < %(antes_de)s)"
                " ORDER BY id DESC LIMIT %(limite)s",
                {
                    "tipo": objeto and objeto.tipo,
                    "objeto_id": objeto and objeto.id,
                    "accion": accion,
                    "antes_de": antes_de,
                    "limite": limite,
                },
            ).fetchall()
        return [_evento(fila) for fila in filas]


def _claves_prohibidas(valor: object) -> set[str]:
    if isinstance(valor, Mapping):
        propias = {str(c) for c in valor if str(c).lower() in CLAVES_PROHIBIDAS}
        return propias.union(*map(_claves_prohibidas, valor.values()))
    if isinstance(valor, list | tuple):
        return set().union(*map(_claves_prohibidas, valor))
    return set()


def _evento(fila: dict[str, Any]) -> Evento:
    return Evento(
        id=fila["id"],
        cuando=fila["cuando"],
        actor=Actor(
            TipoDeActor(fila["actor_tipo"]),
            fila["actor_etiqueta"],
            usuario_id=fila["actor_id"],
            ip=None if fila["ip"] is None else str(fila["ip"]),
            solicitud_id=fila["solicitud_id"],
        ),
        accion=Accion(fila["accion"]),
        objeto=ObjetoRef(fila["objeto_tipo"], fila["objeto_id"]),
        antes=fila["antes"],
        despues=fila["despues"],
        motivo=fila["motivo"],
    )
