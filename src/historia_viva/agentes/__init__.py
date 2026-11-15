"""Los Agentes externos y sus tokens: emitir, autenticar, revocar (spec, módulo `agentes`; RF-26).

El token, `hv_<8 hex>_<43 base64url>`, existe en claro solo en `TokenEmitido`: la base guarda su
SHA-256 (256 bits de entropía: argon2 en cada pedido sería costo sin beneficio, DEC-32) y el
prefijo `hv_<8 hex>`, su id público, que es lo único que va a listados, logs y auditoría.
`autenticar` lee la base en cada pedido, sin caché: revocar rige en el siguiente. Un token
revocado, vencido, malformado o desconocido da el mismo `TokenInvalido`.

Revocar es marcar `revocado_el`, nunca borrar. Rotar emite otro token y programa la revocación
de los anteriores al fin del solape (`revocado_el` futuro): `hv_app` no actualiza `vence_el`.
Nada de acá toca el freno `api_agentes_habilitada` (DEC-136). Es lo único que toca
`agente_externo` y `token_agente` (regla de `test_arquitectura`).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Bd
from historia_viva.dominio.errores import ErrorHistoriaViva

__all__ = [
    "SOLAPE_MAXIMO",
    "VIDA",
    "AgenteExistente",
    "AgenteListado",
    "Credenciales",
    "EstadoDeToken",
    "PrincipalAgente",
    "Scope",
    "TokenEmitido",
    "TokenInexistente",
    "TokenInvalido",
    "TokenListado",
    "TokenYaRevocado",
    "contar_tokens",
    "es_prefijo",
    "generar",
    "huella",
    "tiene_forma",
]

VIDA = timedelta(days=180)
"""Vencimiento por defecto de un token (DEC-32)."""
SOLAPE_MAXIMO = timedelta(days=7)
"""Cuánto pueden convivir el token viejo y el nuevo al rotar (DEC-32, propuesta)."""
_USO_CADA = timedelta(minutes=1)
"""`ultimo_uso_el` se escribe a lo sumo una vez por minuto: no hay escritura en el camino
caliente."""
_PREFIJO = r"hv_[0-9a-f]{8}"
_FORMA = re.compile(_PREFIJO + r"_[A-Za-z0-9_-]{43}")


class Scope(StrEnum):
    FRAGMENTOS_LEER = "fragmentos:leer"
    PIEZAS_LEER = "piezas:leer"
    APORTES_ESCRIBIR = "aportes:escribir"
    EFEMERIDES_LEER = "efemerides:leer"
    """Cubre `GET /v1/efemerides` y `GET /v1/relatos` (R-11)."""


class TokenInvalido(ErrorHistoriaViva):
    """El mismo para ausente, malformado, desconocido, revocado y vencido: afuera no se
    distinguen."""

    def __init__(self) -> None:
        super().__init__("Token inválido.")


class TokenInexistente(ErrorHistoriaViva):
    """No hay un token con ese prefijo o id, o ya está revocado."""

    def __init__(self, token: str) -> None:
        super().__init__(f"No hay un token sin revocar {token}.")


class TokenYaRevocado(TokenInexistente):
    """El token existe pero ya está revocado: la API lo distingue (409) del inexistente (404)."""


class AgenteExistente(ErrorHistoriaViva):
    """Ya hay un Agente externo con ese nombre (`nombre` es UNIQUE)."""

    def __init__(self, nombre: str) -> None:
        super().__init__(f"Ya existe un Agente «{nombre}».")


class EstadoDeToken(StrEnum):
    ACTIVO = "activo"
    """También con `revocado_el` futuro: una rotación programada todavía lo deja valer."""
    VENCIDO = "vencido"
    REVOCADO = "revocado"


@dataclass(frozen=True)
class TokenListado:
    """Lo que se puede mostrar de un token: nunca el token ni su hash."""

    token_id: UUID
    prefijo: str
    estado: EstadoDeToken
    creado_el: datetime
    vence_el: datetime | None
    ultimo_uso_el: datetime | None
    revocado_el: datetime | None


@dataclass(frozen=True)
class AgenteListado:
    agente_id: UUID
    nombre: str
    scopes: tuple[Scope, ...]
    tokens: tuple[TokenListado, ...]
    """Del más nuevo al más viejo."""


@dataclass(frozen=True)
class TokenEmitido:
    token_id: UUID
    agente_id: UUID
    token: str = field(repr=False)
    """En claro: se muestra una sola vez y no se guarda; fuera del `repr` para que no llegue a
    un log o a una traza."""
    prefijo: str
    vence_el: datetime | None


@dataclass(frozen=True)
class PrincipalAgente:
    agente_id: UUID
    nombre: str
    token_id: UUID
    token_prefijo: str
    scopes: frozenset[Scope]

    def puede(self, scope: Scope) -> bool:
        return scope in self.scopes


def generar() -> tuple[str, str, str]:
    """Un token nuevo: `(token, prefijo, huella)`. El secreto tiene 256 bits."""
    prefijo = f"hv_{secrets.token_hex(4)}"
    token = f"{prefijo}_{secrets.token_urlsafe(32)}"
    return token, prefijo, huella(token)


def huella(token: str) -> str:
    """SHA-256 hex del token completo: lo único que se guarda."""
    return hashlib.sha256(token.encode()).hexdigest()


def tiene_forma(token: str) -> bool:
    return _FORMA.fullmatch(token) is not None


def es_prefijo(texto: str) -> bool:
    """Si `texto` es un id público de token, `hv_` y 8 hex: lo que se puede repetir en logs."""
    return re.fullmatch(_PREFIJO, texto) is not None


def _ahora() -> datetime:
    return datetime.now(UTC)


class Credenciales:
    """La única puerta a `agente_externo` y `token_agente`. Lo que escribe va en la transacción
    de quien llama, con su evento de auditoría; `autenticar` abre la suya. El reloj es inyectable
    para los vencimientos, el solape y el último uso."""

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

    def crear_agente(
        self, tx: Connection[Any], *, nombre: str, scopes: frozenset[Scope], por: Actor
    ) -> UUID:
        """Da de alta un Agente externo con sus alcances y deja `agente.alta`.

        Raises:
            AgenteExistente: Si ya hay uno con ese nombre, también si otra alta confirmó a la
                vez; la transacción de quien llama queda abortada.
        """
        try:
            fila = (
                tx.cursor(row_factory=dict_row)
                .execute(
                    "INSERT INTO acervo.agente_externo (nombre, scopes, creado_el, creado_por)"
                    " VALUES (%s, %s, %s, %s) RETURNING agente_id",
                    (nombre, sorted(scopes), self._reloj(), por.usuario_id),
                )
                .fetchone()
            )
        except psycopg.errors.UniqueViolation as error:
            raise AgenteExistente(nombre) from error
        assert fila is not None
        agente_id: UUID = fila["agente_id"]
        self._auditoria.registrar(
            tx,
            actor=por,
            accion=Accion.AGENTE_ALTA,
            objeto=ObjetoRef("agente", str(agente_id)),
            despues={"nombre": nombre, "scopes": sorted(scopes)},
        )
        return agente_id

    def agente_id(self, tx: Connection[Any], nombre: str) -> UUID | None:
        fila = (
            tx.cursor(row_factory=dict_row)
            .execute("SELECT agente_id FROM acervo.agente_externo WHERE nombre = %s", (nombre,))
            .fetchone()
        )
        return None if fila is None else fila["agente_id"]

    def nombre(self, tx: Connection[Any], agente_id: UUID) -> str | None:
        fila = (
            tx.cursor(row_factory=dict_row)
            .execute("SELECT nombre FROM acervo.agente_externo WHERE agente_id = %s", (agente_id,))
            .fetchone()
        )
        return None if fila is None else fila["nombre"]

    def emitir(
        self,
        tx: Connection[Any],
        *,
        agente_id: UUID,
        por: Actor,
        vence_el: datetime | None = None,
    ) -> TokenEmitido:
        """El único momento en que el token existe en claro. Vence a los `VIDA` si no se dice
        otra cosa. Deja `agente.emision` con el prefijo."""
        ahora = self._reloj()
        vence_el = vence_el or ahora + VIDA
        token, prefijo, huella_ = generar()
        fila = (
            tx.cursor(row_factory=dict_row)
            .execute(
                "INSERT INTO acervo.token_agente (agente_id, token_hash, token_prefijo,"
                " creado_el, creado_por, vence_el) VALUES (%s, %s, %s, %s, %s, %s)"
                " RETURNING token_id",
                (agente_id, huella_, prefijo, ahora, por.usuario_id, vence_el),
            )
            .fetchone()
        )
        assert fila is not None
        self._auditoria.registrar(
            tx,
            actor=por,
            accion=Accion.AGENTE_EMISION,
            objeto=ObjetoRef("agente", str(agente_id)),
            despues={"token_prefijo": prefijo, "vence_el": vence_el.isoformat()},
        )
        return TokenEmitido(fila["token_id"], agente_id, token, prefijo, vence_el)

    def autenticar(self, token: str) -> PrincipalAgente:
        """El agente del token, contra la base y sin caché.

        Raises:
            TokenInvalido: Si el token está malformado, no existe, está revocado o venció.
        """
        if not tiene_forma(token):  # regex barata antes de tocar la base
            raise TokenInvalido
        ahora = self._reloj()
        huella_ = huella(token)
        with self._bd.transaccion() as tx:
            fila = tx.execute(
                "SELECT t.token_id, t.token_hash, t.token_prefijo, t.vence_el, t.revocado_el,"
                " a.agente_id, a.nombre, a.scopes FROM acervo.token_agente t"
                " JOIN acervo.agente_externo a USING (agente_id) WHERE t.token_hash = %s",
                (huella_,),
            ).fetchone()
            if (
                fila is None
                or not hmac.compare_digest(fila["token_hash"], huella_)
                or (fila["revocado_el"] is not None and fila["revocado_el"] <= ahora)
                or (fila["vence_el"] is not None and fila["vence_el"] <= ahora)
            ):
                raise TokenInvalido
            tx.execute(
                "UPDATE acervo.token_agente SET ultimo_uso_el = %s WHERE token_id = %s"
                " AND (ultimo_uso_el IS NULL OR ultimo_uso_el < %s)",
                (ahora, fila["token_id"], ahora - _USO_CADA),
            )
        return PrincipalAgente(
            fila["agente_id"],
            fila["nombre"],
            fila["token_id"],
            fila["token_prefijo"],
            frozenset(Scope(s) for s in fila["scopes"]),
        )

    def rotar(
        self, tx: Connection[Any], *, agente_id: UUID, vencen_anteriores_el: datetime, por: Actor
    ) -> TokenEmitido:
        """Emite un token nuevo y programa la revocación de los vivos del agente para
        `vencen_anteriores_el`: hasta entonces valen los dos (el solape).

        Raises:
            ValueError: Si `vencen_anteriores_el` ya pasó o está a más de `SOLAPE_MAXIMO`.
        """
        ahora = self._reloj()
        if not ahora <= vencen_anteriores_el <= ahora + SOLAPE_MAXIMO:
            raise ValueError(
                f"Los anteriores tienen que vencer entre ahora y dentro de {SOLAPE_MAXIMO.days}"
                " días."
            )
        self._revocar(
            tx, por=por, hasta=vencen_anteriores_el, agente_id=agente_id, vigentes_a=ahora
        )
        return self.emitir(tx, agente_id=agente_id, por=por)

    def revocar_token(
        self, tx: Connection[Any], token_id: UUID, *, por: Actor, agente_id: UUID | None = None
    ) -> datetime:
        """Revoca ya: el pedido siguiente da `TokenInvalido`. Con `agente_id`, el token tiene
        que ser de ese agente. Devuelve el `revocado_el`.

        Raises:
            TokenYaRevocado: Si el token ya está revocado (no cuenta una rotación programada).
            TokenInexistente: Si no hay un token con ese id (y de ese agente).
        """
        # FOR UPDATE: de dos revocaciones a la vez, la segunda ve la primera y da ya revocado.
        fila = (
            tx.cursor(row_factory=dict_row)
            .execute(
                "SELECT agente_id, revocado_el FROM acervo.token_agente WHERE token_id = %s"
                " FOR UPDATE",
                (token_id,),
            )
            .fetchone()
        )
        if fila is None or agente_id not in (None, fila["agente_id"]):
            raise TokenInexistente(str(token_id))
        ahora = self._reloj()
        if not self._revocar(tx, por=por, hasta=ahora, token_id=token_id):
            raise TokenYaRevocado(str(token_id))
        return ahora

    def listar(self, tx: Connection[Any]) -> list[AgenteListado]:
        """Los agentes por nombre con sus tokens y el estado de cada uno a la hora del reloj."""
        ahora = self._reloj()
        cursor = tx.cursor(row_factory=dict_row)
        tokens: dict[UUID, list[TokenListado]] = {}
        for fila in cursor.execute(
            "SELECT agente_id, token_id, token_prefijo, creado_el, vence_el, ultimo_uso_el,"
            " revocado_el FROM acervo.token_agente ORDER BY creado_el DESC, token_prefijo"
        ).fetchall():
            revocado, vence = fila["revocado_el"], fila["vence_el"]
            estado = (
                EstadoDeToken.REVOCADO
                if revocado is not None and revocado <= ahora
                else EstadoDeToken.VENCIDO
                if vence is not None and vence <= ahora
                else EstadoDeToken.ACTIVO
            )
            tokens.setdefault(fila["agente_id"], []).append(
                TokenListado(
                    fila["token_id"],
                    fila["token_prefijo"],
                    estado,
                    fila["creado_el"],
                    vence,
                    fila["ultimo_uso_el"],
                    revocado,
                )
            )
        return [
            AgenteListado(
                fila["agente_id"],
                fila["nombre"],
                tuple(Scope(s) for s in fila["scopes"]),
                tuple(tokens.get(fila["agente_id"], ())),
            )
            for fila in cursor.execute(
                "SELECT agente_id, nombre, scopes FROM acervo.agente_externo ORDER BY nombre"
            ).fetchall()
        ]

    def revocar_agente(self, tx: Connection[Any], agente_id: UUID, *, por: Actor) -> int:
        """Revoca ya todos los tokens vivos del agente; devuelve cuántos."""
        return self._revocar(tx, por=por, hasta=self._reloj(), agente_id=agente_id)

    def token_id(self, tx: Connection[Any], prefijo: str) -> UUID:
        """El id del token con ese prefijo, el que muestran los logs y los listados.

        Raises:
            TokenInexistente: Si ningún token tiene ese prefijo.
        """
        fila = (
            tx.cursor(row_factory=dict_row)
            .execute(
                "SELECT token_id FROM acervo.token_agente WHERE token_prefijo = %s", (prefijo,)
            )
            .fetchone()
        )
        if fila is None:
            raise TokenInexistente(prefijo)
        token_id: UUID = fila["token_id"]
        return token_id

    def _revocar(
        self,
        tx: Connection[Any],
        *,
        por: Actor,
        hasta: datetime,
        token_id: UUID | None = None,
        agente_id: UUID | None = None,
        vigentes_a: datetime | None = None,
    ) -> int:
        """Marca `revocado_el = hasta` en los tokens vivos que elige `token_id` o `agente_id`
        (una revocación ya programada para antes no se corre) y deja un `agente.revocacion` por
        cada uno. Un vencido también se revoca (spec §1.14), salvo con `vigentes_a`: al rotar,
        programar la revocación de uno ya vencido solo sería ruido en la auditoría."""
        filas = (
            tx.cursor(row_factory=dict_row)
            .execute(
                "UPDATE acervo.token_agente SET revocado_el = %(hasta)s, revocado_por = %(por)s"
                " WHERE (token_id = %(token_id)s OR agente_id = %(agente_id)s)"
                " AND (revocado_el IS NULL OR revocado_el > %(hasta)s)"
                " AND (%(vigentes_a)s::timestamptz IS NULL OR vence_el IS NULL"
                " OR vence_el > %(vigentes_a)s)"
                " RETURNING agente_id, token_prefijo",
                {
                    "hasta": hasta,
                    "por": por.usuario_id,
                    "token_id": token_id,
                    "agente_id": agente_id,
                    "vigentes_a": vigentes_a,
                },
            )
            .fetchall()
        )
        for fila in filas:
            self._auditoria.registrar(
                tx,
                actor=por,
                accion=Accion.AGENTE_REVOCACION,
                objeto=ObjetoRef("agente", str(fila["agente_id"])),
                despues={"token_prefijo": fila["token_prefijo"], "revocado_el": hasta.isoformat()},
            )
        return len(filas)


def contar_tokens(conn: Connection[Any]) -> int:
    """Todos los tokens de Agente, vigentes, vencidos o revocados: los del MVP no viajan al
    servidor de la universidad, que emite los suyos (DEC-119, runbook R16)."""
    fila = (
        conn.cursor(row_factory=dict_row)
        .execute("SELECT count(*) AS n FROM acervo.token_agente")
        .fetchone()
    )
    return int(fila["n"]) if fila else 0
