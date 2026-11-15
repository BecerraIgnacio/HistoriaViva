"""Conversación: la consulta del Acervo para los cuatro roles (spec, módulo 31; DEC-129).

Un turno muestra primero las Fichas encontradas con sus Citas (evento `fuentes`) y queda
guardado en la Conversación de la persona, con auditoría. Es dueño de `conversacion`,
`mensaje_conversacion` y `mensaje_cita`: nadie más las toca. Una Conversación es de quien la
creó; la de otra persona responde igual que una inexistente (R-31).

Tres tiempos, sin modelos dentro de una transacción (DEC-99): una transacción guarda la pregunta,
fuera de transacción se recupera con `busqueda` (embeber la consulta es un modelo) y se redacta
en vivo con `responder_en_vivo` (R-20), y otra guarda la respuesta con sus fuentes. Las Citas
`[n]` de la redacción se verifican con `dominio.verificar_citas` contra los fragmentos que vio el
modelo: una que no se verifica se marca y el texto no cambia (R-15). No importa `interruptor` ni
`indexacion`: la consulta del Acervo no se pausa (DEC-136).

Sin evidencia hay tres formas, todas con el aviso fijo y la lista a la vista: sin fragmentos (no
se llama al modelo), el modelo lo declara, o ninguna Cita se verifica. Si el modelo falla después
de `fuentes`, el turno cierra con `error`, la respuesta queda `error` y se puede reintentar.
Desde el segundo turno la pregunta se reformula con los últimos 6 turnos (`reformular`, R-18),
también fuera de transacción, y se recupera y se redacta con la reformulada; si la reformulación
falla, con la original. El historial lo arma el servidor desde la base: el cliente manda solo la
pregunta nueva.

Bajo límites (055): un turno por Conversación, que vence si quedó colgado; las preguntas de
cada persona por hora y por minuto, contadas en la base (R-18); si el cliente corta, se cancela
la redacción y se guarda lo entregado como `interrumpida` (R-17), que se puede reintentar. Sin
`embeber` el turno ofrece la lista por filtros, sin redacción, y queda `sin_semantica` (R-16).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from functools import partial
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from historia_viva.auditoria import Accion, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import AJUSTES_DE_SESION, Bd, Tx
from historia_viva.busqueda import (
    BuscadorNoDisponible,
    Busqueda,
    Consulta,
    ConsultaVacia,
    Recuperacion,
    ResultadoDeBusqueda,
    ubicar,
)
from historia_viva.capa_modelos.configuracion import CapaPara, ConfiguracionEfectiva
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CONSULTA,
    ETIQUETA_FRAGMENTO,
    ETIQUETA_HISTORIAL,
    BloqueDatos,
    Capacidad,
    Limites,
)
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.citas import CitaDelTexto, quitar_citas, verificar_citas
from historia_viva.dominio.errores import LimiteExcedido, MotorNoDisponible
from historia_viva.dominio.tipos import FiltroDeBusqueda, PiezaBuscable
from historia_viva.efemerides import (
    Efemerides,
    FuenteDeRelato,
    RespuestaNoProponible,
    VistaDeRelato,
)
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Principal

__all__ = [
    "LIMITES_DE_REFORMULAR",
    "SIN_EVIDENCIA",
    "SIN_SEMANTICA",
    "TOPES",
    "TiempoGuardado",
    "ConversacionNoEncontrada",
    "ConversacionRef",
    "Conversaciones",
    "EventoDeConversacion",
    "LimiteDeConsultas",
    "MensajeNoEncontrado",
    "TurnoEnCurso",
    "TurnoEnMarcha",
    "Topes",
    "Turno",
    "TurnoNoReintentable",
    "VistaDeConversacion",
    "declara_sin_evidencia",
    "tiempos_guardados",
]

_log = logging.getLogger(__name__)

SIN_EVIDENCIA = "No encontré evidencia suficiente en el Acervo."
"""El contenido de una respuesta sin Fichas: la pantalla lo dice y no se inventa prosa."""


def declara_sin_evidencia(texto: str) -> bool:
    """El modelo respondió solo la frase fija de `SIN_EVIDENCIA` (con o sin punto final). La
    usa también el banco de `responder` (078)."""
    return texto.strip().rstrip(".") == SIN_EVIDENCIA.rstrip(".")


LARGO_DEL_TITULO = 80
"""El título es el comienzo de la primera pregunta (spec §1.20)."""

FRAGMENTOS_DEL_MODELO = 5
"""Los fragmentos que ve el modelo: las primeras Citas de `fuentes`, en su orden."""

SIN_SEMANTICA = "Búsqueda semántica no disponible."
"""El contenido de una respuesta sin `embeber` (R-16): solo la lista por filtros, sin prosa."""


_OCIOSA_S = int(re.findall(r"idle_in_transaction_session_timeout=(\d+)s", AJUSTES_DE_SESION)[0])


@dataclass(frozen=True)
class Topes:
    """Los de la Conversación en la tabla de valores operativos (DEC-145, R-18): ajustables."""

    largo_pregunta: int = 2000
    turnos_de_historial: int = 6
    """Los turnos anteriores con los que se reformula la pregunta."""
    mensajes_por_hora: int = 30
    mensajes_por_minuto: int = 3
    """Los turnos de una persona en todas sus Conversaciones: cada pregunta y cada reintento."""
    vencimiento_del_turno_s: int = 10 * _OCIOSA_S
    """10 veces `idle_in_transaction_session_timeout`: un turno colgado más que eso se da por
    vencido (spec, `conversacion.turno_desde`)."""


TOPES = Topes()

LIMITES_DE_REFORMULAR = Limites(
    tiempo_maximo_s=30, tope_tokens_entrada=3000, tope_tokens_salida=100
)
"""Una pregunta corta, antes de las fuentes: no los 180 s ni los 600 tokens de la redacción.
De entrada, la pregunta y 6 turnos; si no entran, se pierden los más viejos."""

class ConversacionNoEncontrada(Exception):
    """No existe o es de otra persona: no se distingue (R-31)."""


class MensajeNoEncontrado(Exception):
    """El mensaje no es de esa Conversación: 404 `recurso-no-encontrado` (`recurso: mensaje`)."""


class TurnoEnCurso(Exception):
    """La Conversación todavía responde el turno anterior: 409 `turno-en-curso` (R-43 f)."""


class TurnoNoReintentable(Exception):
    """Solo se reintenta la última respuesta de la Conversación, si falló: 409
    `turno-no-reintentable`."""


class LimiteDeConsultas(Exception):
    """La persona llegó a su tope de preguntas por hora o por minuto: 429 `limite-de-consultas`
    (R-18)."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"Llegó al tope de preguntas; puede volver en {retry_after_s} s.")
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Turno:
    """Un turno previo del historial que se pasa armado (`persistir=False`, solo `banco`)."""

    pregunta: str
    respuesta: str | None = None
    """La que vio la persona; `None` si falló (no entra al historial)."""


@dataclass(frozen=True)
class ConversacionRef:
    conversacion_id: UUID
    titulo: str | None
    mensajes: int
    creada_el: datetime
    actualizada_el: datetime


@dataclass(frozen=True)
class TiempoGuardado:
    """Los tres tiempos de una respuesta, como los guardó el turno (RNF-10, R-20)."""

    conversacion_id: UUID
    orden: int
    estado: str
    fuentes_ms: int
    primer_token_ms: int | None
    """`None` sin redacción: sin evidencia, sin semántica o falló antes del primer token."""
    total_ms: int
    num_thread: Mapping[str, int | None]
    """El de cada capacidad en la configuración efectiva guardada; `None` si la fila no lo fija
    (lo elige Ollama). Vacío sin redacción: no se guarda la configuración."""
    con_historial: bool
    """Hubo otra pregunta antes de la suya: se reformuló antes de las fuentes (R-20). El
    reintento del primer turno no tiene historial."""


def tiempos_guardados(tx: Tx, *, desde: datetime, hasta: datetime) -> tuple[TiempoGuardado, ...]:
    """Las respuestas guardadas en `[desde, hasta)`, de todas las personas: el guion de RNF-10
    mide una ventana por condición en una máquina dedicada (056)."""
    # `dict_row` propio: el guion puede leer con una conexión que no lo trae.
    filas = tx.cursor(row_factory=dict_row).execute(
        "SELECT conversacion_id, orden, estado, ms_hasta_fuentes, ms_hasta_primer_token,"
        " ms_hasta_respuesta, configuracion_efectiva,"
        " (SELECT count(*) FROM acervo.mensaje_conversacion p"
        "  WHERE p.conversacion_id = m.conversacion_id AND p.autor = 'persona'"
        "  AND p.orden < m.orden) > 1 AS con_historial"
        " FROM acervo.mensaje_conversacion m"
        " WHERE autor = 'historia_viva' AND creado_el >= %s AND creado_el < %s"
        " ORDER BY creado_el, conversacion_id, orden",
        (desde, hasta),
    ).fetchall()
    return tuple(
        TiempoGuardado(
            f["conversacion_id"], f["orden"], f["estado"], f["ms_hasta_fuentes"],
            f["ms_hasta_primer_token"], f["ms_hasta_respuesta"],
            {
                capacidad: fila["parametros"].get("num_thread")
                for capacidad, fila in (f["configuracion_efectiva"] or {})
                .get("por_capacidad", {}).items()
            },
            con_historial=f["con_historial"],
        )
        for f in filas
    )


@dataclass(frozen=True)
class VistaDeConversacion:
    """Una Conversación reabierta: sus mensajes en orden, con las fuentes y las Citas de cada
    respuesta (Contrato HTTP, `GET /api/conversaciones/{id}`)."""

    conversacion: ConversacionRef
    mensajes: tuple[dict[str, Any], ...]
    """UUID y fechas tal cual: los serializa la API."""


@dataclass(frozen=True)
class EventoDeConversacion:
    tipo: str
    """`fuentes`, `token`, `citas` y `fin`, o `error` en lugar de `fin`."""
    datos: dict[str, Any]
    """UUID y fechas tal cual: los serializa la API."""


class TurnoEnMarcha:
    """Los eventos de un turno, que sueltan la Conversación al agotarse, al fallar o con
    `close()`. No se confía en el `finally` de un generador: si el cliente corta, nadie lo cierra
    hasta que pasa el recolector, y si nunca arrancó no corre nunca."""

    def __init__(
        self,
        eventos: Generator[EventoDeConversacion],
        *,
        soltar: Callable[[], None],
        primero: EventoDeConversacion | None = None,
    ):
        """`primero` es el evento que ya se le pidió a `eventos` (`fuentes`): sale antes."""
        self._eventos = eventos
        self._soltar = soltar
        self._soltado = False
        self._primero = primero

    def __iter__(self) -> TurnoEnMarcha:
        return self

    def __next__(self) -> EventoDeConversacion:
        if self._primero is not None:
            primero, self._primero = self._primero, None
            return primero
        try:
            return next(self._eventos)
        except BaseException:  # también StopIteration: el turno terminó
            self.close()
            raise

    def close(self) -> None:
        """Idempotente. No se llama mientras corre un `next` (quien sirve el flujo lo espera)."""
        if self._soltado:
            return
        self._soltado = True
        try:
            self._eventos.close()  # ya pasó `fuentes`: guarda la respuesta `interrumpida`
        finally:
            self._soltar()


class Conversaciones:
    def __init__(
        self,
        *,
        bd: Bd,
        busqueda: Busqueda,
        fichas: ModuloFichasPostgres,
        auditoria: RegistroDeAuditoria,
        configuracion: ConfiguracionDeModelos,
        capa_para: CapaPara,
        efemerides: Efemerides,
        topes: Topes = TOPES,
    ) -> None:
        """`capa_para` arma la capa con la fila de `responder` que el turno congela; `fichas`
        dice qué fuentes siguen disponibles al reabrir; `efemerides` guarda los Relatos."""
        self.topes = topes
        self._efemerides = efemerides
        self._bd = bd
        self._busqueda = busqueda
        self._fichas = fichas
        self._auditoria = auditoria
        self._configuracion = configuracion
        self._capa_para = capa_para

    def iniciar(self, tx: Tx, *, principal: Principal) -> ConversacionRef:
        """Una Conversación vacía de la persona, sin título hasta la primera pregunta."""
        fila = tx.execute(
            "INSERT INTO acervo.conversacion (persona_id) VALUES (%s)"
            " RETURNING conversacion_id, creada_el, actualizada_el",
            (principal.usuario_id,),
        ).fetchone()
        assert fila is not None
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.CONVERSACION_INICIO,
            objeto=ObjetoRef("conversacion", str(fila["conversacion_id"])),
        )
        return ConversacionRef(
            fila["conversacion_id"], None, 0, fila["creada_el"], fila["actualizada_el"]
        )

    def listar(
        self,
        tx: Tx,
        *,
        persona_id: UUID,
        limite: int,
        antes_de: tuple[datetime, UUID] | None = None,
    ) -> list[ConversacionRef]:
        """Las de la persona con algún mensaje, la más reciente primero (`actualizada_el`,
        `conversacion_id`): hasta `limite`, desde la que sigue a `antes_de`. Nunca las de otra
        persona (R-31)."""
        cuando, ultima = antes_de or (None, None)
        filas = tx.execute(
            "SELECT c.conversacion_id, c.titulo, c.creada_el, c.actualizada_el,"
            " (SELECT count(*) FROM acervo.mensaje_conversacion m"
            "  WHERE m.conversacion_id = c.conversacion_id) AS mensajes"
            " FROM acervo.conversacion c WHERE c.persona_id = %s"
            # Una vacía no se lista: no se puede borrar (R-19) y quedaría para siempre.
            " AND EXISTS (SELECT 1 FROM acervo.mensaje_conversacion m"
            "  WHERE m.conversacion_id = c.conversacion_id)"
            " AND (%s::timestamptz IS NULL"
            "  OR (c.actualizada_el, c.conversacion_id) < (%s::timestamptz, %s::uuid))"
            " ORDER BY c.actualizada_el DESC, c.conversacion_id DESC LIMIT %s",
            (persona_id, cuando, cuando, ultima, limite),
        ).fetchall()
        return [_ref(fila) for fila in filas]

    def obtener(self, conversacion_id: UUID, *, principal: Principal) -> VistaDeConversacion:
        """La Conversación propia con sus mensajes en orden: cada respuesta con las Fichas que
        se mostraron y las `[n]` de su texto. Una Pieza que ya no es Validada o fue retirada se
        muestra «no disponible», sin su texto ni su original (E8).

        Raises:
            ConversacionNoEncontrada: No existe o es de otra persona (R-31).
        """
        with self._bd.transaccion() as tx:
            fila = tx.execute(
                "SELECT conversacion_id, titulo, creada_el, actualizada_el, 0 AS mensajes"
                " FROM acervo.conversacion WHERE conversacion_id = %s AND persona_id = %s",
                (conversacion_id, principal.usuario_id),
            ).fetchone()
            if fila is None:
                raise ConversacionNoEncontrada(f"No existe la Conversación {conversacion_id}.")
            mensajes = tx.execute(
                "SELECT mensaje_id, autor, contenido, filtros, estado, pregunta_reformulada,"
                " configuracion_efectiva, citas_no_verificadas, creado_el"
                " FROM acervo.mensaje_conversacion WHERE conversacion_id = %s ORDER BY orden",
                (conversacion_id,),
            ).fetchall()
            citas: dict[UUID, list[Any]] = {}
            for cita in tx.execute(
                "SELECT c.mensaje_id, c.pieza_id, c.fragmento_id, c.ubicacion"
                " FROM acervo.mensaje_cita c JOIN acervo.mensaje_conversacion m USING (mensaje_id)"
                " WHERE m.conversacion_id = %s ORDER BY c.mensaje_id, c.posicion",
                (conversacion_id,),
            ):
                citas.setdefault(cita["mensaje_id"], []).append(cita)
        disponibles = self._fichas.disponibles(
            {c["pieza_id"] for guardadas in citas.values() for c in guardadas}
        )
        return VistaDeConversacion(
            _ref(fila | {"mensajes": len(mensajes)}),
            tuple(
                _pregunta_http(m) if m["autor"] == "persona"
                else _respuesta_http(m, citas.get(m["mensaje_id"], []), disponibles)
                for m in mensajes
            ),
        )

    def enviar_mensaje(
        self,
        conversacion_id: UUID | None,
        *,
        principal: Principal | None,
        texto: str,
        filtro: FiltroDeBusqueda,
        persistir: bool = True,
        historial: Sequence[Turno] = (),
    ) -> TurnoEnMarcha:
        """Un turno. Lo que falla antes de abrir el flujo se lanza al llamar; después, el
        iterador emite `fuentes`, los `token` de la redacción, `citas` y `fin`, y guarda la
        respuesta antes de `fin`; sin fragmentos, `fuentes` y `fin`, sin llamar al modelo. La
        Conversación queda `respondiendo` desde la pregunta hasta que el iterador se agota,
        falla o se cierra con `close()`, haya arrancado o no: quien lo sirve tiene que
        cerrarlo siempre.

        `persistir=False` (con `conversacion_id` y `principal` en `None`) es solo para medir
        M3 (`banco`): no toma la Conversación, no cuenta topes, no guarda ni audita nada, y es
        la única forma de pasar un `historial` ya armado (R-20).

        Raises:
            ValueError: Un turno que se guarda sin Conversación o sin persona, o con `historial`.
            ConversacionNoEncontrada: No existe o es de otra persona (R-31).
            TurnoEnCurso: Otro turno de la misma Conversación todavía responde (R-43 f).
            LimiteDeConsultas: La persona llegó a su tope de preguntas (R-18).
            ConsultaInvalida: El filtro no valida.
            ConfiguracionInvalida: La fila de `responder` falta o no valida.
            IndiceNoDisponible, LimiteExcedido: Los de `busqueda`; sin `embeber` el turno
                sigue con la lista por filtros (R-16).
            BaseNoDisponible: Antes de abrir el flujo.
        """
        comienzo = time.monotonic()
        Consulta(texto=texto, filtro=filtro)  # valida antes de tomar el turno
        # La fila con la que se redacta, congelada antes de tomar el turno: el mensaje la guarda.
        configuracion = self._configuracion.instantanea(capacidades=(Capacidad.RESPONDER,))
        if not persistir:
            return self._turno(
                None, None, None, None, 0, texto, filtro, self._texto_del_historial(historial),
                configuracion, comienzo,
            )
        if conversacion_id is None or principal is None or historial:
            # El historial de un turno guardado sale de la base, nunca de quien llama (054).
            raise ValueError("Un turno guardado va con Conversación y persona, sin historial.")
        with self._bd.transaccion() as tx:  # (1) la pregunta
            orden, turno = self._tomar_turno(tx, conversacion_id, principal.usuario_id)
            self._aplicar_topes(tx, principal.usuario_id)
            texto_historial = self._historial(tx, conversacion_id, antes_de=orden)
            pregunta = tx.execute(
                "INSERT INTO acervo.mensaje_conversacion"
                " (conversacion_id, orden, autor, contenido, filtros)"
                " VALUES (%s, %s, 'persona', %s, %s) RETURNING mensaje_id",
                (conversacion_id, orden, texto, Jsonb(_filtros(filtro))),
            ).fetchone()
            assert pregunta is not None
            tx.execute(
                "UPDATE acervo.conversacion SET titulo = coalesce(titulo, %s)"
                " WHERE conversacion_id = %s",
                (texto[:LARGO_DEL_TITULO], conversacion_id),
            )
        return self._turno(
            conversacion_id, turno, principal, pregunta["mensaje_id"], orden + 1, texto, filtro,
            texto_historial, configuracion, comienzo,
        )

    def reintentar(
        self, conversacion_id: UUID, mensaje_id: UUID, *, principal: Principal
    ) -> TurnoEnMarcha:
        """Vuelve a responder la última pregunta cuando su respuesta `mensaje_id` falló o se
        interrumpió («Reintentar este turno», E3 y E9): una respuesta nueva, con la misma
        pregunta y los mismos filtros, sin guardar la pregunta otra vez. Los eventos, como
        `enviar_mensaje`.

        Raises:
            ConversacionNoEncontrada: No existe o es de otra persona (R-31).
            TurnoEnCurso: Otro turno de la misma Conversación todavía responde (R-43 f).
            TurnoNoReintentable: `mensaje_id` no es la última respuesta o no falló ni se cortó.
            LimiteDeConsultas: Un reintento cuenta como un turno más (R-18).
            ConfiguracionInvalida, IndiceNoDisponible, LimiteExcedido, BaseNoDisponible:
                Como `enviar_mensaje`.
        """
        comienzo = time.monotonic()
        configuracion = self._configuracion.instantanea(capacidades=(Capacidad.RESPONDER,))
        with self._bd.transaccion() as tx:  # si no se puede, el rollback suelta el turno
            orden, turno = self._tomar_turno(tx, conversacion_id, principal.usuario_id)
            # Cortar y reintentar en bucle también redacta: cuenta como un turno.
            self._aplicar_topes(tx, principal.usuario_id)
            pregunta = tx.execute(
                "SELECT p.mensaje_id, p.orden, p.contenido, p.filtros"
                " FROM acervo.mensaje_conversacion r CROSS JOIN LATERAL ("
                "   SELECT mensaje_id, orden, contenido, filtros FROM acervo.mensaje_conversacion"
                "   WHERE conversacion_id = r.conversacion_id AND autor = 'persona'"
                "   AND orden < r.orden ORDER BY orden DESC LIMIT 1) p"
                " WHERE r.conversacion_id = %s AND r.mensaje_id = %s AND r.orden = %s"
                " AND r.estado IN ('error', 'interrumpida')",
                (conversacion_id, mensaje_id, orden - 1),
            ).fetchone()
            if pregunta is None:
                raise TurnoNoReintentable(
                    "Solo se reintenta la última respuesta, si falló o se cortó."
                )
            historial = self._historial(tx, conversacion_id, antes_de=pregunta["orden"])
        return self._turno(
            conversacion_id, turno, principal, pregunta["mensaje_id"], orden, pregunta["contenido"],
            _filtro_guardado(pregunta["filtros"]), historial, configuracion, comienzo,
        )

    def proponer_relato(
        self, tx: Tx, conversacion_id: UUID, mensaje_id: UUID, *, principal: Principal
    ) -> VistaDeRelato:
        """«Proponer como relato» (DEC-147): la respuesta `completa` `mensaje_id` pasa a
        `efemerides` como Relato Propuesto, con la pregunta del turno como título (auditoría de
        tickets, decisión 18), el texto guardado sin las `[n]` no verificadas y sus Citas
        verificadas como fuentes, cada una con su número. No llama a ningún modelo.

        Raises:
            ConversacionNoEncontrada: No existe o es de otra persona (R-31).
            MensajeNoEncontrado: El mensaje no es de esa Conversación.
            RespuestaNoProponible: No es una respuesta `completa` con alguna Cita verificada.
            PermisoInsuficiente, RelatoYaPropuesto: Los de `efemerides.proponer_relato`.
        """
        if tx.execute(
            "SELECT 1 FROM acervo.conversacion WHERE conversacion_id = %s AND persona_id = %s",
            (conversacion_id, principal.usuario_id),
        ).fetchone() is None:
            raise ConversacionNoEncontrada(f"No existe la Conversación {conversacion_id}.")
        mensaje = tx.execute(
            "SELECT m.estado, m.contenido, m.configuracion_efectiva, ("
            "   SELECT p.contenido FROM acervo.mensaje_conversacion p"
            "   WHERE p.conversacion_id = m.conversacion_id AND p.autor = 'persona'"
            "   AND p.orden < m.orden ORDER BY p.orden DESC LIMIT 1) AS pregunta"
            " FROM acervo.mensaje_conversacion m WHERE m.mensaje_id = %s AND m.conversacion_id = %s",
            (mensaje_id, conversacion_id),
        ).fetchone()
        if mensaje is None:
            raise MensajeNoEncontrado(f"No existe el mensaje {mensaje_id} en esa Conversación.")
        # La `[n]` verificada es la Cita que quedó `citada_en_respuesta` (R-15).
        citadas = tx.execute(
            "SELECT posicion, pieza_id, ficha_id FROM acervo.mensaje_cita"
            " WHERE mensaje_id = %s AND citada_en_respuesta ORDER BY posicion",
            (mensaje_id,),
        ).fetchall()
        if mensaje["estado"] != "completa" or not citadas:
            raise RespuestaNoProponible(
                "Solo se propone una respuesta completa con al menos una Cita verificada.",
                estado_actual=mensaje["estado"],
            )
        en_el_texto = {cita.marca for cita in verificar_citas(mensaje["contenido"], 0)}
        return self._efemerides.proponer_relato(
            tx,
            mensaje_id=mensaje_id,
            titulo=mensaje["pregunta"],
            texto=quitar_citas(
                mensaje["contenido"], en_el_texto - {c["posicion"] for c in citadas}
            ).strip(),
            fuentes=[FuenteDeRelato(c["posicion"], c["pieza_id"], c["ficha_id"]) for c in citadas],
            configuracion_efectiva=mensaje["configuracion_efectiva"],
            principal=principal,
        )

    def _turno(
        self,
        conversacion_id: UUID | None,
        turno: datetime | None,
        principal: Principal | None,
        pregunta_id: UUID | None,
        orden: int,
        texto: str,
        filtro: FiltroDeBusqueda,
        historial: str,
        configuracion: ConfiguracionEfectiva,
        comienzo: float,
    ) -> TurnoEnMarcha:
        """Reformula y recupera fuera de transacción y arma los eventos hasta `fuentes`; la
        Conversación ya está tomada por el turno que empezó en `turno`. Desde ahí, un corte
        siempre deja la respuesta `interrumpida` y reintentable. Sin `conversacion_id` no se
        persiste y no hay Conversación que soltar."""
        soltar: Callable[[], None] = (
            (lambda: None) if conversacion_id is None or turno is None
            else partial(self._soltar_turno, conversacion_id, turno)
        )
        try:
            reformulada = self._reformular(configuracion, texto, historial)
            consulta = reformulada or texto
            sin_semantica = False
            try:
                recuperacion = self._busqueda.recuperar(
                    Consulta(texto=consulta, filtro=filtro), max_fragmentos=FRAGMENTOS_DEL_MODELO
                )
            except BuscadorNoDisponible as error:  # sin `embeber`: la lista por filtros (R-16)
                _log.warning("Sin búsqueda semántica en el turno: %s", error)
                sin_semantica = True
                recuperacion = self._por_filtros(filtro)
            eventos = self._responder(
                conversacion_id, turno, principal, pregunta_id, orden, consulta, reformulada,
                recuperacion, configuracion, comienzo, sin_semantica=sin_semantica,
            )
            fuentes = next(eventos)
        except BaseException:
            soltar()
            raise
        return TurnoEnMarcha(eventos, primero=fuentes, soltar=soltar)

    def _por_filtros(self, filtro: FiltroDeBusqueda) -> Recuperacion:
        """La lista sin texto, que no toca el motor; sin Fechas no hay criterio y queda vacía
        (`busqueda` no recorre el Acervo entero)."""
        try:
            return self._busqueda.recuperar(Consulta(filtro=filtro), max_fragmentos=0)
        except ConsultaVacia:
            return Recuperacion(ResultadoDeBusqueda(filtro, 0, (), False, 0), ())

    def _reformular(
        self, configuracion: ConfiguracionEfectiva, pregunta: str, historial: str
    ) -> str | None:
        """La pregunta autónoma con la que se recupera, o `None` en el primer turno o si la
        reformulación falla, se agota o no sirve: el turno sigue con la original."""
        if not historial:
            return None
        try:
            reformulada = (
                self._capa_para(configuracion.por_capacidad)
                .reformular(
                    pregunta=BloqueDatos(ETIQUETA_CONSULTA, pregunta),
                    historial=BloqueDatos(ETIQUETA_HISTORIAL, historial),
                    limites=LIMITES_DE_REFORMULAR,
                )
                .texto.strip()
            )
            Consulta(texto=reformulada)  # en blanco o con un NUL no llega a la búsqueda
            # Una sola pregunta: varias líneas o un texto largo es el modelo respondiendo.
            if "\n" in reformulada or len(reformulada) > self.topes.largo_pregunta:
                raise ValueError("la reformulada no es una sola pregunta")
        except Exception as error:  # noqa: BLE001
            _log.warning(
                "Falló la reformulación (%s): se recupera con la pregunta original. %s",
                type(error).__name__, error,
                exc_info=not isinstance(error, (MotorNoDisponible, LimiteExcedido)),
            )
            return None
        return reformulada

    def _responder(
        self,
        conversacion_id: UUID | None,
        turno: datetime | None,
        principal: Principal | None,
        pregunta_id: UUID | None,
        orden: int,
        texto: str,
        reformulada: str | None,
        recuperacion: Recuperacion,
        configuracion: ConfiguracionEfectiva,
        comienzo: float,
        *,
        sin_semantica: bool = False,
    ) -> Generator[EventoDeConversacion]:
        """Los eventos del turno. Si quien los consume lo cierra antes de guardar la respuesta
        (el cliente cortó), se cancela la redacción y se guarda lo entregado como `interrumpida`
        (R-17); `citas` sale después de guardar."""
        resultado = recuperacion.resultado
        respuesta_id = uuid4()
        # Sin fragmentos o sin `embeber` no se llama al modelo.
        sin_redaccion = sin_semantica or not recuperacion.mostrados
        estado = (
            "sin_semantica" if sin_semantica else "sin_evidencia" if sin_redaccion else "completa"
        )
        contenido = SIN_SEMANTICA if sin_semantica else SIN_EVIDENCIA
        citas: tuple[CitaDelTexto, ...] = ()
        ms_primer_token: int | None = None
        modelo: str | None = None
        efectiva: dict[str, Any] | None = None
        falla: dict[str, Any] | None = None
        partes: list[str] = []
        cortado = False
        # M3 es hasta que salen las fuentes, no hasta que el cliente pide lo que sigue.
        ms_fuentes = _ms_desde(comienzo)
        try:
            yield EventoDeConversacion(
                "fuentes",
                {
                    "mensaje_id": respuesta_id,
                    "consulta_reformulada": reformulada,
                    "total": resultado.total,
                    # Sin puntaje: la pantalla no muestra la similitud (P6).
                    "fichas": [_sin_puntaje(asdict(pieza)) for pieza in resultado.resultados],
                    "filtros": _filtros_http(resultado.filtro_aplicado),
                    "avisos": (["indice_incompleto"] if resultado.indice_incompleto else [])
                    + (["busqueda_semantica_no_disponible"] if sin_semantica else []),
                },
            )
            if not sin_redaccion:
                efectiva = configuracion.a_json()
                modelo = configuracion.por_capacidad[Capacidad.RESPONDER].modelo
                capa = self._capa_para(configuracion.por_capacidad)
                # Fuera de transacción (DEC-99): la pregunta ya se guardó; la respuesta, después.
                try:
                    # Cerrarlo corta la conexión con el motor: deja de generar (R-17).
                    with closing(
                        capa.responder_en_vivo(
                            consulta=BloqueDatos(ETIQUETA_CONSULTA, texto),
                            fragmentos=tuple(
                                BloqueDatos(ETIQUETA_FRAGMENTO, f.texto)
                                for f in recuperacion.fragmentos
                            ),
                        )
                    ) as vivo:
                        for tramo in vivo:
                            if tramo.resultado is None:
                                if ms_primer_token is None:
                                    ms_primer_token = _ms_desde(comienzo)
                                yield EventoDeConversacion("token", {"texto": tramo.token})
                                partes.append(tramo.token)  # el cliente pidió el siguiente
                except Exception as error:  # noqa: BLE001 — cualquier falla del motor: no se corta en seco
                    # Las fuentes ya salieron: el turno se cierra con `error` y la lista queda.
                    # El tipo de falla (motor, tope de tiempo u otra) va solo al log.
                    _log.warning(
                        "Falló la redacción del turno %s (%s): %s",
                        respuesta_id, type(error).__name__, error,
                        exc_info=not isinstance(error, (MotorNoDisponible, LimiteExcedido)),
                    )
                    estado, falla = "error", PROBLEMA_DE_REDACCION
                # El texto tal como lo vio la persona: una Cita que no se verifica no lo cambia.
                contenido = "".join(partes)
                if falla is None:
                    citas = verificar_citas(contenido, len(recuperacion.fragmentos))
                    # El modelo declara que no alcanza, o ninguna Cita se verifica: sin prosa
                    # inventada. Una respuesta con Citas que solo menciona la frase no se pisa.
                    declara = declara_sin_evidencia(contenido)
                    if declara or not any(c.verificada for c in citas):
                        estado, contenido = "sin_evidencia", SIN_EVIDENCIA
        except GeneratorExit:  # el cliente cortó: después de guardar no se emite nada más
            cortado = True
            if not sin_redaccion:
                estado, contenido = "interrumpida", "".join(partes)
        citadas = {c.marca for c in citas if c.verificada and estado == "completa"}
        no_verificadas = sum(not c.verificada for c in citas)
        if conversacion_id is None:  # sin persistir (M3): ni guarda ni audita
            mio, ms_total = True, _ms_desde(comienzo)
        else:
            assert turno is not None and principal is not None and pregunta_id is not None
            with self._bd.transaccion() as tx:  # (2) la respuesta y lo que vio la persona
                # Solo se mira y se guarda: nada se emite con la conexión y el candado tomados.
                mio = self._sigue_siendo_del_turno(tx, conversacion_id, turno)
                if mio:
                    tx.execute(
                        "INSERT INTO acervo.mensaje_conversacion (mensaje_id, conversacion_id,"
                        " orden, autor, contenido, estado, pregunta_reformulada, modelo_embedding,"
                        " configuracion_efectiva, citas_no_verificadas, ms_hasta_fuentes,"
                        " ms_hasta_primer_token, ms_hasta_respuesta)"
                        " VALUES (%s, %s, %s, 'historia_viva', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            respuesta_id,
                            conversacion_id,
                            orden,
                            contenido,
                            estado,
                            reformulada,
                            next((f.modelo_embedding for f in recuperacion.fragmentos), None),
                            efectiva and Jsonb(efectiva),
                            no_verificadas,
                            ms_fuentes,
                            ms_primer_token,
                            ms_total := _ms_desde(comienzo),
                        ),
                    )
                    tx.cursor().executemany(
                        "INSERT INTO acervo.mensaje_cita (mensaje_id, posicion, pieza_id, ficha_id,"
                        " fragmento_id, ubicacion, citada_en_respuesta)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        [
                            (respuesta_id, posicion, f.pieza_id, f.ficha_id, f.fragmento_id,
                             Jsonb(dict(f.ubicacion)), posicion in citadas)
                            for posicion, f in enumerate(recuperacion.mostrados, start=1)
                        ],
                    )
                    self._auditoria.registrar(
                        tx,
                        actor=principal.actor,
                        accion=Accion.CONVERSACION_MENSAJE,
                        objeto=ObjetoRef("conversacion", str(conversacion_id)),
                        despues={
                            "pregunta_id": str(pregunta_id),
                            "respuesta_id": str(respuesta_id),
                            "estado": estado,
                            "fichas": resultado.total,
                            "citas": len(recuperacion.mostrados),
                        },
                    )
        if not mio:
            _log.warning(
                "El turno %s de la Conversación %s venció y lo tomó otro: no se guarda.",
                respuesta_id, conversacion_id,
            )
            if not cortado:  # sin `mensaje_id` guardado no hay nada que reintentar
                yield EventoDeConversacion("error", PROBLEMA_DE_TURNO_TOMADO)
            return
        if cortado:
            return
        if falla is not None:
            yield EventoDeConversacion("error", falla)  # en lugar de `fin` (§1.8)
            return
        if not sin_redaccion:
            yield EventoDeConversacion("citas", _citas_http(citas, recuperacion))
        yield EventoDeConversacion(
            "fin",
            {
                "mensaje_id": respuesta_id,
                "sin_evidencia": estado == "sin_evidencia",
                "modelo": modelo,
                # Los guardados: M3 los lee de la base (DEC-128, R-20).
                "tiempos_ms": {
                    "fuentes": ms_fuentes,
                    "primer_token": ms_primer_token,
                    "total": ms_total,
                },
            },
        )

    def _tomar_turno(
        self, tx: Tx, conversacion_id: UUID, persona_id: UUID
    ) -> tuple[int, datetime]:
        """Marca la Conversación propia `respondiendo` y da el orden de la pregunta: dos turnos
        a la vez chocarían en el orden de sus mensajes.

        Raises:
            ConversacionNoEncontrada: No existe o es de otra persona.
            TurnoEnCurso: Ya está respondiendo.
        """
        fila = tx.execute(
            "SELECT estado, turno_desde > now() - make_interval(secs => %s) AS vivo"
            " FROM acervo.conversacion"
            " WHERE conversacion_id = %s AND persona_id = %s FOR UPDATE",
            (self.topes.vencimiento_del_turno_s, conversacion_id, persona_id),
        ).fetchone()
        if fila is None:
            raise ConversacionNoEncontrada(f"No existe la Conversación {conversacion_id}.")
        # Un turno colgado (el proceso murió a mitad) vence y el nuevo toma la Conversación.
        if fila["estado"] == "respondiendo" and fila["vivo"]:
            raise TurnoEnCurso("Ya hay un turno respondiendo en esta Conversación.")
        turno = tx.execute(
            "UPDATE acervo.conversacion SET estado = 'respondiendo', turno_desde = now(),"
            " actualizada_el = now() WHERE conversacion_id = %s RETURNING turno_desde",
            (conversacion_id,),
        ).fetchone()
        assert turno is not None
        orden = tx.execute(
            "SELECT coalesce(max(orden), 0) + 1 AS orden FROM acervo.mensaje_conversacion"
            " WHERE conversacion_id = %s",
            (conversacion_id,),
        ).fetchone()
        assert orden is not None
        return int(orden["orden"]), turno["turno_desde"]

    def _sigue_siendo_del_turno(self, tx: Tx, conversacion_id: UUID, turno: datetime) -> bool:
        """Si la Conversación sigue tomada por el turno que empezó en `turno`: uno que tardó
        más que el vencimiento pudo perderla a manos de otro (el candado dura hasta el INSERT)."""
        fila = tx.execute(
            "SELECT turno_desde = %s AS mio FROM acervo.conversacion"
            " WHERE conversacion_id = %s FOR UPDATE",
            (turno, conversacion_id),
        ).fetchone()
        return bool(fila and fila["mio"])

    def _aplicar_topes(self, tx: Tx, persona_id: UUID) -> None:
        """Cuenta los turnos de la persona en la última hora, en todas sus Conversaciones, en
        la base (valen igual con varios procesos de la API): cada pregunta y cada respuesta que
        sigue a otra respuesta, es decir, un reintento.

        Raises:
            LimiteDeConsultas: Con los segundos hasta que la más vieja sale de su ventana.
        """
        # Dos preguntas a la vez en Conversaciones distintas no pasan las dos el mismo cupo.
        tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"topes-de-conversacion:{persona_id}",),
        )
        cuenta = tx.execute(
            "SELECT count(*) AS hora,"
            " count(*) FILTER (WHERE m.creado_el > now() - interval '1 minute') AS minuto,"
            " ceil(extract(epoch FROM min(m.creado_el) + interval '1 hour' - now()))"
            "  AS espera_hora,"
            " ceil(extract(epoch FROM min(m.creado_el)"
            "  FILTER (WHERE m.creado_el > now() - interval '1 minute')"
            "  + interval '1 minute' - now())) AS espera_minuto"
            " FROM acervo.mensaje_conversacion m JOIN acervo.conversacion c USING (conversacion_id)"
            " WHERE c.persona_id = %s AND m.creado_el > now() - interval '1 hour'"
            " AND (m.autor = 'persona' OR EXISTS (SELECT 1 FROM acervo.mensaje_conversacion p"
            "  WHERE p.conversacion_id = m.conversacion_id AND p.orden = m.orden - 1"
            "  AND p.autor = 'historia_viva'))",
            (persona_id,),
        ).fetchone()
        assert cuenta is not None
        esperas = [
            espera
            for usadas, tope, espera in (
                (cuenta["hora"], self.topes.mensajes_por_hora, cuenta["espera_hora"]),
                (cuenta["minuto"], self.topes.mensajes_por_minuto, cuenta["espera_minuto"]),
            )
            if usadas >= tope
        ]
        if esperas:
            raise LimiteDeConsultas(max(1, int(max(esperas))))

    def _soltar_turno(self, conversacion_id: UUID, turno: datetime) -> None:
        """Suelta la Conversación solo si sigue siendo de este turno: si venció y la tomó
        otro, es del otro."""
        with self._bd.transaccion() as tx:
            tx.execute(
                "UPDATE acervo.conversacion SET estado = 'activa', turno_desde = NULL,"
                " actualizada_el = now() WHERE conversacion_id = %s AND turno_desde = %s",
                (conversacion_id, turno),
            )

    def _historial(self, tx: Tx, conversacion_id: UUID, *, antes_de: int) -> str:
        """Los últimos turnos antes del mensaje `antes_de`, como texto: cada pregunta con la
        última respuesta que vio la persona. Una respuesta que falló no cuenta: la pantalla la
        borra."""
        turnos: list[Turno] = []
        for mensaje in tx.execute(
            "SELECT autor, contenido, estado FROM acervo.mensaje_conversacion"
            " WHERE conversacion_id = %s AND orden < %s ORDER BY orden",
            (conversacion_id, antes_de),
        ):
            if mensaje["autor"] == "persona":
                turnos.append(Turno(mensaje["contenido"]))
            elif turnos and mensaje["estado"] != "error":
                # Un reintento reemplaza a la respuesta anterior del mismo turno.
                turnos[-1] = Turno(turnos[-1].pregunta, mensaje["contenido"])
        return self._texto_del_historial(turnos)

    def _texto_del_historial(self, turnos: Sequence[Turno]) -> str:
        """Los últimos turnos como los lee `reformular`; vacío en el primer turno."""
        return "\n\n".join(
            f"Persona: {t.pregunta}"
            + ("" if t.respuesta is None else f"\nHistoria Viva: {t.respuesta}")
            for t in turnos[-self.topes.turnos_de_historial:]
        )


def _filtros(filtro: FiltroDeBusqueda) -> dict[str, Any]:
    """Los filtros con los nombres de la columna `filtros` (spec §1.20)."""
    return {
        "desde": filtro.desde and filtro.desde.isoformat(),
        "hasta": filtro.hasta and filtro.hasta.isoformat(),
        "tipos": list(filtro.tipos),
        "tipos_actividad": list(filtro.tipos_actividad),
        "carreras": list(filtro.carreras),
    }


def _filtro_guardado(guardados: dict[str, Any]) -> FiltroDeBusqueda:
    """El filtro de una pregunta guardada, para reintentar su turno."""
    return FiltroDeBusqueda(
        desde=guardados["desde"] and date.fromisoformat(guardados["desde"]),
        hasta=guardados["hasta"] and date.fromisoformat(guardados["hasta"]),
        tipos=tuple(guardados["tipos"]),
        tipos_actividad=tuple(guardados["tipos_actividad"]),
        carreras=tuple(guardados["carreras"]),
    )


def _filtros_http(filtro: FiltroDeBusqueda) -> dict[str, Any]:
    """Los filtros aplicados con los nombres del pedido, como `GET /api/busqueda`."""
    guardados = _filtros(filtro)
    return {
        "desde": guardados["desde"],
        "hasta": guardados["hasta"],
        "tipo": guardados["tipos"],
        "tipo_actividad": guardados["tipos_actividad"],
        "carrera": guardados["carreras"],
    }


def _citas_http(citas: tuple[CitaDelTexto, ...], recuperacion: Recuperacion) -> dict[str, Any]:
    """El evento `citas` (Contrato HTTP §1.8): la `[n]` verificada apunta a la Cita `n` de
    `fuentes`; la que no, a nada (R-15)."""
    enlaces = [c.enlace for pieza in recuperacion.resultado.resultados for c in pieza.citas]
    salida = []
    for cita in citas:
        if not cita.verificada:
            salida.append(_marca_http(cita, None, None))
            continue
        fragmento = recuperacion.fragmentos[cita.marca - 1]
        salida.append(
            _marca_http(
                cita,
                (fragmento.fragmento_id, fragmento.pieza_id, fragmento.ubicacion),
                # Los fragmentos del modelo son las primeras Citas de `fuentes`, en su orden.
                enlaces[cita.marca - 1],
            )
        )
    return {"citas": salida, "no_verificadas": sum(not c.verificada for c in citas)}


def _marca_http(
    cita: CitaDelTexto,
    citada: tuple[UUID, UUID, Mapping[str, Any]] | None,
    url_original: str | None,
) -> dict[str, Any]:
    """Una `[n]` del texto (§1.8): la verificada apunta a su Cita `(fragmento, Pieza,
    ubicación)`; la que no, a nada (R-15)."""
    fragmento_id, pieza_id, ubicacion = citada or (None, None, {})
    return {
        "marca": cita.marca,
        "verificada": cita.verificada,
        "fragmento_id": fragmento_id,
        "pieza_id": pieza_id,
        "pagina": ubicacion.get("pagina"),
        "seccion": ubicacion.get("seccion"),
        "url_original": url_original,
    }


def _ref(fila: Mapping[str, Any]) -> ConversacionRef:
    return ConversacionRef(
        fila["conversacion_id"], fila["titulo"], fila["mensajes"], fila["creada_el"],
        fila["actualizada_el"],
    )


def _pregunta_http(mensaje: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "mensaje_id": mensaje["mensaje_id"],
        "autor": "persona",
        "texto": mensaje["contenido"],
        "filtros": _filtros_http(_filtro_guardado(mensaje["filtros"])),
        "creado_el": mensaje["creado_el"],
    }


def _respuesta_http(
    mensaje: Mapping[str, Any],
    guardadas: list[Any],
    disponibles: Mapping[UUID, PiezaBuscable],
) -> dict[str, Any]:
    """Una respuesta reabierta: las Fichas en el orden en que se mostraron y las `[n]` de su
    texto, verificadas contra los fragmentos que vio el modelo, como el evento `citas`."""
    fichas: dict[UUID, dict[str, Any]] = {}
    enlaces: list[str | None] = []
    for cita in guardadas:
        pieza = disponibles.get(cita["pieza_id"])
        ficha = fichas.setdefault(cita["pieza_id"], _ficha_http(cita["pieza_id"], pieza))
        if pieza is None:  # «Pieza no disponible»: ni su texto ni su original (E8)
            enlaces.append(None)
            continue
        legible, enlace = ubicar(cita["pieza_id"], cita["ubicacion"])
        enlaces.append(enlace)
        ficha["citas"].append(
            {"fragmento_id": cita["fragmento_id"], "ubicacion_legible": legible, "enlace": enlace}
        )
    vistas = guardadas[:FRAGMENTOS_DEL_MODELO]
    marcas = (
        verificar_citas(mensaje["contenido"], len(vistas))
        if mensaje["estado"] == "completa" else ()
    )
    citadas = [(c["fragmento_id"], c["pieza_id"], c["ubicacion"]) for c in vistas]
    efectiva = mensaje["configuracion_efectiva"]
    return {
        "mensaje_id": mensaje["mensaje_id"],
        "autor": "historia_viva",
        "texto": mensaje["contenido"],
        "estado": mensaje["estado"],
        "consulta_reformulada": mensaje["pregunta_reformulada"],
        "fichas": list(fichas.values()),
        "citas": [
            _marca_http(m, citadas[m.marca - 1], enlaces[m.marca - 1])
            if m.verificada else _marca_http(m, None, None)
            for m in marcas
        ],
        "citas_no_verificadas": mensaje["citas_no_verificadas"],
        "modelo": efectiva and _modelo(efectiva),
        "creado_el": mensaje["creado_el"],
    }


def _modelo(efectiva: dict[str, Any]) -> str | None:
    """El modelo con el que se redactó, de la fila de `responder` que se guardó."""
    return ConfiguracionEfectiva.desde_json(efectiva).por_capacidad[Capacidad.RESPONDER].modelo


def _ficha_http(pieza_id: UUID, pieza: PiezaBuscable | None) -> dict[str, Any]:
    if pieza is None:
        return {"pieza_id": pieza_id, "disponible": False}
    datos = asdict(pieza)
    for interna in ("ficha_id", "validada_el"):
        datos.pop(interna)
    return {**datos, "disponible": True, "citas": []}


PROBLEMA_DE_REDACCION = {
    "type": "/errores/capa-modelos-no-disponible",
    "title": "La lectura automática no está disponible",
    "status": 503,
    "detail": "El motor dejó de responder. Probá de nuevo en un momento.",
}
"""El evento `error` después de `fuentes`, sea cual sea la falla (spec, módulo 31): el 504
`tiempo-agotado` es solo antes del primer byte."""

PROBLEMA_DE_TURNO_TOMADO = {
    "type": "/errores/turno-en-curso",
    "title": "Ya hay un turno respondiendo",
    "status": 409,
    "detail": "Esta respuesta tardó demasiado y otro turno tomó la conversación; no se guardó.",
}
"""El evento `error` de un turno que venció mientras redactaba: su respuesta no se guardó."""


def _sin_puntaje(pieza: dict[str, Any]) -> dict[str, Any]:
    pieza.pop("puntaje")
    return pieza


def _ms_desde(comienzo: float) -> int:
    return int((time.monotonic() - comienzo) * 1000)
