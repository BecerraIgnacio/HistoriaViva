"""Quién es la persona y hasta cuándo: la sesión de servidor (spec, módulo `identidad`; DEC-37).

La cookie lleva un id aleatorio de 32 bytes y la base guarda solo su `sha256`: quien lee la
tabla no puede presentarse como nadie. Una sesión está `activa` hasta que pasan 60 minutos sin
actividad o 8 horas desde que se abrió (`vencida`, derivado del reloj), o hasta que se cierra o
la persona se da de baja (`revocada`). La actividad se anota a lo sumo una vez por minuto: la
sesión puede vencer a los 59 minutos reales del último pedido. Cerrar marca `revocada_el`,
nunca borra. Es lo único que toca `acervo.sesion` (regla de `test_arquitectura`).

`Padron` da de alta, cambia el rol y da de baja: es lo que usa `alta-persona` y, desde 111, la
pestaña Personas. Las reglas viven acá y no en la API: quién otorga qué (R-02), el último titular
de un permiso (R-03) y el fin del interinato del Administrador técnico (R-01).

El seam es la sesión (spec): la suite abre las suyas con `abrir`, sin pasar por `/auth/dev`.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria, TipoDeActor
from historia_viva.bd import Bd, Tx
from historia_viva.dominio.errores import PermisoInsuficiente
from historia_viva.dominio.permisos import (
    PERMISOS_CON_TITULAR,
    Permiso,
    Rol,
    interinato_vigente,
    puede_otorgar,
)
from historia_viva.dominio.tipos import MetodoAutenticacion

__all__ = [
    "INACTIVIDAD",
    "SESIONES_VIVAS",
    "VIDA",
    "DominioNoPermitido",
    "Padron",
    "PersonaExistente",
    "PersonaNoEncontrada",
    "PersonaNoHabilitada",
    "PersonaVista",
    "Principal",
    "RechazoDelPadron",
    "Sesiones",
    "UltimoTitularDePermiso",
    "contar_personas",
]

INACTIVIDAD = timedelta(minutes=60)
"""Sin actividad, la sesión vence (C32: no corta una validación larga)."""
VIDA = timedelta(hours=8)
"""Vencimiento absoluto, desde que se abrió."""
SESIONES_VIVAS = 5
"""Las activas que puede tener una persona; la sexta revoca la más vieja (DEC-145)."""
_ACTIVIDAD_CADA = timedelta(minutes=1)
"""`ultima_actividad_el` se escribe a lo sumo una vez por minuto, no en cada pedido."""


class PersonaNoHabilitada(Exception):
    """El email no es de una persona activa del padrón: no se abre sesión."""


class RechazoDelPadron(Exception):
    """Un cambio del padrón que no se hace; `codigo` es el `type` del catálogo de errores."""

    codigo = ""


class PersonaExistente(RechazoDelPadron):
    codigo = "persona-existente"

    def __init__(self) -> None:
        super().__init__("Ya hay una persona con ese email.")


class DominioNoPermitido(RechazoDelPadron):
    codigo = "dominio-no-permitido"

    def __init__(self) -> None:
        super().__init__("El email no es de un dominio permitido (HV_DOMINIOS_PERMITIDOS).")


class PersonaNoEncontrada(RechazoDelPadron):
    codigo = "recurso-no-encontrado"

    def __init__(self) -> None:
        super().__init__("No hay una persona con ese email o identificador.")


class UltimoTitularDePermiso(RechazoDelPadron):
    """R-03: dejaría sin ninguna persona activa a GESTIONAR_PERSONAS u OPERAR_SISTEMA."""

    codigo = "ultimo-titular-de-permiso"

    def __init__(self, permiso: Permiso) -> None:
        self.permiso = permiso
        super().__init__(f"Es la última persona activa con {permiso}: no puede quedar sin titular.")


@dataclass(frozen=True)
class Principal:
    """La persona de una sesión activa. El rol se lee de la base en cada pedido: una baja o un
    cambio de rol rige en el siguiente."""

    usuario_id: UUID
    nombre: str
    email: str
    rol: Rol
    metodo: MetodoAutenticacion
    """Con qué se entró: lo firmado con `dev` es una firma de ensayo (DEC-139)."""
    vence_el: datetime
    """El vencimiento absoluto de la sesión; la inactividad la puede cortar antes."""
    interino: bool = False
    """El interinato del Administrador técnico está vigente (R-01); a otro rol no le suma."""

    def puede(self, permiso: Permiso) -> bool:
        return self.rol.puede(permiso, interino=self.interino)

    @property
    def actor(self) -> Actor:
        return Actor(TipoDeActor.PERSONA, self.email, usuario_id=self.usuario_id)


def _huella(sesion: str) -> str:
    return hashlib.sha256(sesion.encode()).hexdigest()


def _ahora() -> datetime:
    return datetime.now(UTC)


class Sesiones:
    def __init__(
        self,
        *,
        bd: Bd,
        auditoria: RegistroDeAuditoria,
        reloj: Callable[[], datetime] | None = None,
    ) -> None:
        self._bd = bd
        self._auditoria = auditoria
        self._reloj = reloj or _ahora

    def abrir(self, tx: Tx, usuario_id: UUID, metodo: MetodoAutenticacion) -> str:
        """Una sesión nueva de la persona, con id nuevo (anti fijación); devuelve el id de la
        cookie. Si ya tenía `SESIONES_VIVAS` activas, revoca las más viejas; las vencidas no
        cuentan."""
        sesion = secrets.token_urlsafe(32)
        ahora = self._reloj()
        tx.execute(
            "INSERT INTO acervo.sesion (sesion_id, usuario_id, metodo, creada_el,"
            " ultima_actividad_el, vence_el) VALUES (%s, %s, %s, %s, %s, %s)",
            (_huella(sesion), usuario_id, metodo, ahora, ahora, ahora + VIDA),
        )
        # La nueva no compite: con el reloj quieto, varias comparten `creada_el`.
        tx.execute(
            "UPDATE acervo.sesion SET revocada_el = %(ahora)s WHERE sesion_id IN ("
            " SELECT sesion_id FROM acervo.sesion WHERE usuario_id = %(usuario_id)s"
            " AND revocada_el IS NULL AND sesion_id <> %(nueva)s AND vence_el > %(ahora)s"
            " AND ultima_actividad_el > %(inactiva)s ORDER BY creada_el DESC OFFSET %(otras)s)",
            {
                "ahora": ahora,
                "inactiva": ahora - INACTIVIDAD,
                "usuario_id": usuario_id,
                "nueva": _huella(sesion),
                "otras": SESIONES_VIVAS - 1,
            },
        )
        return sesion

    def ingresar_dev(self, email: str, *, previa: str | None) -> str:
        """El ingreso de desarrollo (DEC-112): abre una sesión `dev` de la persona activa con
        ese email, descarta la `previa` y deja `sesion.inicio`; devuelve el id de la cookie.

        Raises:
            PersonaNoHabilitada: Si el email no es de una persona activa; el rechazo queda en
                `auditoria` con el dominio y un hash corto del email, nunca el email.
        """
        email = email.strip().lower()
        with self._bd.transaccion() as tx:
            fila = tx.execute(
                "SELECT usuario_id, email FROM acervo.usuario WHERE activo AND email = %s",
                (email,),
            ).fetchone()
            if fila is not None:
                return self._iniciar(tx, fila, MetodoAutenticacion.DEV, previa)
            self._rechazar(tx, email, "persona_no_habilitada")
        raise PersonaNoHabilitada

    def ingresar_oidc(self, sub: str, email: str, *, previa: str | None) -> str:
        """El ingreso con Google, con la identidad ya verificada (spec, «Pasos del OIDC» 4 y 5):
        entra la persona activa con ese `sub`; si no hay, la persona activa con ese email y sin
        `sub`, a la que se le guarda. Desde ahí, solo `sub`: otro email con el mismo `sub` entra.

        Raises:
            PersonaNoHabilitada: Si no hay persona activa para ese `sub` ni ese email, o si el
                email ya está vinculado a otro `sub` (motivo `sub_distinto` en `auditoria`: lo
                decide quien tiene GESTIONAR_PERSONAS).
        """
        email = email.strip().lower()
        por_sub = "SELECT usuario_id, email, activo FROM acervo.usuario WHERE sub_google = %s"
        with self._bd.transaccion() as tx:
            # El `SELECT` se repite después del `UPDATE`: si un ingreso concurrente de la misma
            # persona vinculó el `sub` mientras se esperaba el candado, no es un `sub` distinto.
            fila = (
                tx.execute(por_sub, (sub,)).fetchone()
                or tx.execute(
                    "UPDATE acervo.usuario SET sub_google = %s"
                    " WHERE email = %s AND activo AND sub_google IS NULL"
                    " RETURNING usuario_id, email, activo",
                    (sub, email),
                ).fetchone()
                or tx.execute(por_sub, (sub,)).fetchone()
            )
            if fila is not None and fila["activo"]:
                return self._iniciar(tx, fila, MetodoAutenticacion.OIDC, previa)
            otro_sub = fila is None and tx.execute(
                "SELECT 1 FROM acervo.usuario WHERE email = %s AND sub_google IS NOT NULL",
                (email,),
            ).fetchone()
            self._rechazar(tx, email, "sub_distinto" if otro_sub else "persona_no_habilitada")
        raise PersonaNoHabilitada

    def rechazar_ingreso(self, email: str, motivo: str) -> None:
        """Deja en `auditoria` un ingreso rechazado antes de buscar a la persona (el dominio o
        el correo sin verificar), con el dominio y un hash corto del email, nunca el email."""
        with self._bd.transaccion() as tx:
            self._rechazar(tx, email, motivo)

    def _iniciar(
        self, tx: Tx, persona: dict[str, Any], metodo: MetodoAutenticacion, previa: str | None
    ) -> str:
        """Descarta la sesión `previa` (anti fijación), abre la nueva y deja `sesion.inicio`."""
        if previa:
            self._revocar(tx, previa)
        sesion = self.abrir(tx, persona["usuario_id"], metodo)
        tx.execute(
            "UPDATE acervo.usuario SET ultimo_acceso_el = %s WHERE usuario_id = %s",
            (self._reloj(), persona["usuario_id"]),
        )
        self._auditoria.registrar(
            tx,
            actor=Actor(TipoDeActor.PERSONA, persona["email"], usuario_id=persona["usuario_id"]),
            accion=Accion.SESION_INICIO,
            objeto=ObjetoRef("sesion"),
            despues={"metodo": metodo.value},
        )
        return sesion

    def _rechazar(self, tx: Tx, email: str, motivo: str) -> None:
        self._auditoria.registrar(
            tx,
            actor=Actor.rechazado(email),
            accion=Accion.SESION_RECHAZO,
            objeto=ObjetoRef("sesion"),
            motivo=motivo,
        )

    def validar(self, sesion: str) -> Principal | None:
        """La persona de una sesión activa, o `None` si la sesión no existe, venció o fue
        revocada. Si la persona se dio de baja, sus sesiones se revocan acá: la baja rige en el
        pedido siguiente."""
        ahora = self._reloj()
        with self._bd.transaccion() as tx:
            fila = tx.execute(
                "SELECT s.usuario_id, s.metodo, s.ultima_actividad_el, s.vence_el, u.nombre,"
                " u.email, u.rol, u.activo,"
                " ARRAY(SELECT DISTINCT rol FROM acervo.usuario WHERE activo) AS roles_activos"
                " FROM acervo.sesion s JOIN acervo.usuario u USING (usuario_id)"
                " WHERE s.sesion_id = %s AND s.revocada_el IS NULL AND s.vence_el > %s"
                " AND s.ultima_actividad_el > %s",
                (_huella(sesion), ahora, ahora - INACTIVIDAD),
            ).fetchone()
            if fila is None:
                return None
            if not fila["activo"]:
                self.revocar_de(tx, fila["usuario_id"])
                return None
            if fila["ultima_actividad_el"] <= ahora - _ACTIVIDAD_CADA:
                tx.execute(
                    "UPDATE acervo.sesion SET ultima_actividad_el = %s WHERE sesion_id = %s",
                    (ahora, _huella(sesion)),
                )
        return Principal(
            usuario_id=fila["usuario_id"],
            nombre=fila["nombre"],
            email=fila["email"],
            rol=Rol(fila["rol"]),
            metodo=MetodoAutenticacion(fila["metodo"]),
            vence_el=fila["vence_el"],
            interino=interinato_vigente({Rol(rol) for rol in fila["roles_activos"]}),
        )

    def registrar_rechazo(self, principal: Principal, *, permiso: Permiso, ruta: str) -> None:
        """Deja en `auditoria` el 403 de `exigir`: quién, qué permiso le faltó y en qué ruta."""
        with self._bd.transaccion() as tx:
            self._auditoria.registrar(
                tx,
                actor=principal.actor,
                accion=Accion.SESION_RECHAZO,
                objeto=ObjetoRef("sesion"),
                despues={"permiso": permiso.value, "ruta": ruta},
                motivo="permiso_insuficiente",
            )

    def cerrar(self, sesion: str, *, por: Actor) -> None:
        """Salir: marca `revocada_el` (no borra) y deja `sesion.cierre`."""
        with self._bd.transaccion() as tx:
            self._revocar(tx, sesion)
            self._auditoria.registrar(
                tx, actor=por, accion=Accion.SESION_CIERRE, objeto=ObjetoRef("sesion")
            )

    def _revocar(self, tx: Tx, sesion: str) -> None:
        tx.execute(
            "UPDATE acervo.sesion SET revocada_el = %s"
            " WHERE sesion_id = %s AND revocada_el IS NULL",
            (self._reloj(), _huella(sesion)),
        )

    def revocar_de(self, tx: Tx, usuario_id: UUID) -> int:
        """Revoca todas las sesiones vivas de la persona (su baja); devuelve cuántas."""
        return tx.execute(
            "UPDATE acervo.sesion SET revocada_el = %s"
            " WHERE usuario_id = %s AND revocada_el IS NULL",
            (self._reloj(), usuario_id),
        ).rowcount


@dataclass(frozen=True)
class PersonaVista:
    usuario_id: UUID
    email: str
    nombre: str
    rol: Rol
    activo: bool
    ultimo_acceso_el: datetime | None
    """El último ingreso; `None`: «Habilitada, todavía no ingresó»."""
    vinculada: bool
    """Su cuenta de Google ya quedó atada por `sub` (el primer ingreso con OIDC)."""


_COLUMNAS = (
    "usuario_id, email, nombre, rol, activo, ultimo_acceso_el,"
    " sub_google IS NOT NULL AS vinculada"
)


class Padron:
    """Quién puede entrar y con qué rol. `por` es la persona de la sesión, a la que se le aplican
    GESTIONAR_PERSONAS y R-02, o el `Actor` de `alta-persona`, que puede todo: es la única vía de
    crear al primer Administrador técnico (R-02).

    Cada cambio bloquea las filas de las personas activas antes de mirar R-03: dos bajas a la vez
    no dejan sin titular a un permiso."""

    def __init__(
        self,
        *,
        auditoria: RegistroDeAuditoria,
        sesiones: Sesiones,
        dominios_permitidos: tuple[str, ...],
    ) -> None:
        self._auditoria = auditoria
        self._sesiones = sesiones
        self._dominios = dominios_permitidos

    def buscar(self, tx: Tx, email: str) -> PersonaVista | None:
        fila = tx.execute(
            f"SELECT {_COLUMNAS} FROM acervo.usuario WHERE email = %s",
            (email.strip().lower(),),
        ).fetchone()
        return None if fila is None else _vista(fila)

    def listar(
        self, tx: Tx, *, activo: bool | None = None, rol: Rol | None = None
    ) -> list[PersonaVista]:
        """El padrón por nombre; quién puede verlo lo decide la API (GESTIONAR_PERSONAS)."""
        filas = tx.execute(
            f"SELECT {_COLUMNAS} FROM acervo.usuario"
            " WHERE (%(activo)s::boolean IS NULL OR activo = %(activo)s)"
            " AND (%(rol)s::text IS NULL OR rol = %(rol)s) ORDER BY nombre, email",
            {"activo": activo, "rol": rol},
        ).fetchall()
        return [_vista(fila) for fila in filas]

    def alta(
        self, tx: Tx, email: str, nombre: str, rol: Rol, *, por: Principal | Actor
    ) -> PersonaVista:
        """Raises: PermisoInsuficiente, DominioNoPermitido, PersonaExistente."""
        activos = _activos(tx)
        _autorizar(por, rol, activos)
        email = email.strip().lower()
        local, arroba, dominio = email.rpartition("@")
        if not local or not arroba or "@" in local or dominio not in self._dominios:
            raise DominioNoPermitido
        fila = tx.execute(
            "INSERT INTO acervo.usuario (email, nombre, rol) VALUES (%s, %s, %s)"
            f" ON CONFLICT (email) DO NOTHING RETURNING {_COLUMNAS}",
            (email, nombre.strip(), rol),
        ).fetchone()
        if fila is None:
            raise PersonaExistente
        persona = _vista(fila)
        self._registrar(
            tx, por, Accion.PERSONA_ALTA, persona.usuario_id, activos,
            {**activos, persona.usuario_id: rol},
            despues={"email": email, "nombre": persona.nombre, "rol": rol.value},
        )
        return persona

    def cambiar_rol(self, tx: Tx, usuario_id: UUID, rol: Rol, *, por: Principal | Actor) -> None:
        """Sin cambio de rol no hace nada (no audita).

        Raises: PersonaNoEncontrada, PermisoInsuficiente, UltimoTitularDePermiso."""
        activos = _activos(tx)
        anterior = self._persona(tx, usuario_id)
        _autorizar(por, anterior.rol, activos, usuario_id)
        _autorizar(por, rol, activos, usuario_id)
        if rol is anterior.rol:
            return
        despues = {**activos, usuario_id: rol} if usuario_id in activos else activos
        _cuidar_titulares(activos, despues)
        tx.execute("UPDATE acervo.usuario SET rol = %s WHERE usuario_id = %s", (rol, usuario_id))
        self._registrar(
            tx, por, Accion.PERSONA_ROL, usuario_id, activos, despues,
            antes={"rol": anterior.rol.value}, despues={"rol": rol.value},
        )

    def desactivar(self, tx: Tx, usuario_id: UUID, *, por: Principal | Actor) -> None:
        """La baja no borra: conserva lo que firmó y revoca sus sesiones. Si ya estaba de baja,
        no hace nada.

        Raises: PersonaNoEncontrada, PermisoInsuficiente, UltimoTitularDePermiso."""
        activos = _activos(tx)
        persona = self._persona(tx, usuario_id)
        _autorizar(por, persona.rol, activos, usuario_id)
        if not persona.activo:
            return
        despues = {u: r for u, r in activos.items() if u != usuario_id}
        _cuidar_titulares(activos, despues)
        tx.execute("UPDATE acervo.usuario SET activo = false WHERE usuario_id = %s", (usuario_id,))
        revocadas = self._sesiones.revocar_de(tx, usuario_id)
        self._registrar(
            tx, por, Accion.PERSONA_BAJA, usuario_id, activos, despues,
            despues={"activo": False, "sesiones_revocadas": revocadas},
        )

    def reactivar(self, tx: Tx, usuario_id: UUID, *, por: Principal | Actor) -> None:
        """Si ya estaba activa, no hace nada.

        Raises: PersonaNoEncontrada, PermisoInsuficiente."""
        activos = _activos(tx)
        persona = self._persona(tx, usuario_id)
        _autorizar(por, persona.rol, activos, usuario_id)
        if persona.activo:
            return
        tx.execute("UPDATE acervo.usuario SET activo = true WHERE usuario_id = %s", (usuario_id,))
        self._registrar(
            tx, por, Accion.PERSONA_REACTIVACION, usuario_id, activos,
            {**activos, usuario_id: persona.rol}, despues={"activo": True},
        )

    def obtener(self, tx: Tx, usuario_id: UUID) -> PersonaVista:
        """Raises: PersonaNoEncontrada."""
        fila = tx.execute(
            f"SELECT {_COLUMNAS} FROM acervo.usuario WHERE usuario_id = %s", (usuario_id,)
        ).fetchone()
        if fila is None:
            raise PersonaNoEncontrada
        return _vista(fila)

    def _persona(self, tx: Tx, usuario_id: UUID) -> PersonaVista:
        fila = tx.execute(
            f"SELECT {_COLUMNAS} FROM acervo.usuario"
            " WHERE usuario_id = %s FOR UPDATE",
            (usuario_id,),
        ).fetchone()
        if fila is None:
            raise PersonaNoEncontrada
        return _vista(fila)

    def _registrar(
        self,
        tx: Tx,
        por: Principal | Actor,
        accion: Accion,
        usuario_id: UUID,
        antes_del_cambio: dict[UUID, Rol],
        despues_del_cambio: dict[UUID, Rol],
        *,
        despues: dict[str, Any],
        antes: dict[str, Any] | None = None,
    ) -> None:
        if interinato_vigente(antes_del_cambio.values()) and not interinato_vigente(
            despues_del_cambio.values()
        ):
            despues = {**despues, "interinato_terminado": True}
        # Toda acción hecha por el interinato lo dice (DEC-124): la de la persona que tiene el
        # permiso solo por él y, mientras dure, la del comando (runbook R4).
        if (
            not por.rol.puede(Permiso.GESTIONAR_PERSONAS)
            if isinstance(por, Principal)
            else interinato_vigente(antes_del_cambio.values())
        ):
            despues = {**despues, "interino": True}
        self._auditoria.registrar(
            tx,
            actor=por.actor if isinstance(por, Principal) else por,
            accion=accion,
            objeto=ObjetoRef("persona", str(usuario_id)),
            antes=antes,
            despues=despues,
        )


def _vista(fila: dict[str, Any]) -> PersonaVista:
    return PersonaVista(
        usuario_id=fila["usuario_id"],
        email=fila["email"],
        nombre=fila["nombre"],
        rol=Rol(fila["rol"]),
        activo=fila["activo"],
        ultimo_acceso_el=fila["ultimo_acceso_el"],
        vinculada=fila["vinculada"],
    )


def _autorizar(
    por: Principal | Actor, rol: Rol, activos: dict[UUID, Rol], sobre: UUID | None = None
) -> None:
    """GESTIONAR_PERSONAS y R-02 para la persona de la sesión, que además no se cambia el rol
    ni se da de baja a sí misma: un técnico interino se haría RGC y firmaría. El comando no se
    limita. El interinato se mira en `activos`, con las filas ya bloqueadas, y no en la sesión:
    un pedido en vuelo mientras se daba de alta al primer RGC ya no lo tiene (R-01)."""
    if isinstance(por, Principal) and not (
        por.rol.puede(Permiso.GESTIONAR_PERSONAS, interino=interinato_vigente(activos.values()))
        and puede_otorgar(por.rol, rol)
        and por.usuario_id != sobre
    ):
        raise PermisoInsuficiente("Tu rol no puede otorgar ni quitar ese rol.")


def _activos(tx: Tx) -> dict[UUID, Rol]:
    """Las personas activas y su rol, con sus filas bloqueadas hasta el fin de `tx`."""
    filas = tx.execute(
        "SELECT usuario_id, rol FROM acervo.usuario WHERE activo ORDER BY usuario_id FOR UPDATE"
    ).fetchall()
    return {fila["usuario_id"]: Rol(fila["rol"]) for fila in filas}


def _cuidar_titulares(antes: dict[UUID, Rol], despues: dict[UUID, Rol]) -> None:
    """R-03. El interinato se mira antes del cambio: el último RGC no se baja aunque el técnico
    fuera a recuperar el interinato, así el interinato no vuelve (R-01)."""
    interino = interinato_vigente(antes.values())
    for permiso in PERMISOS_CON_TITULAR:
        tenian = any(rol.puede(permiso, interino=interino) for rol in antes.values())
        tienen = any(rol.puede(permiso, interino=interino) for rol in despues.values())
        if tenian and not tienen:
            raise UltimoTitularDePermiso(permiso)


def contar_personas(tx: Tx) -> int:
    """Las del padrón, activas o no: en el servidor, solo las que dio de alta el paso 6 del
    runbook R16 (ninguna de prueba, DEC-52)."""
    fila = tx.execute("SELECT count(*) AS n FROM acervo.usuario").fetchone()
    return int(fila["n"]) if fila else 0
