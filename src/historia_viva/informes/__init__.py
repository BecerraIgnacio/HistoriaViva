"""El Informe de acreditación: armado determinista, sin modelo (spec, módulo 33; DEC-131).

Un documento de acreditación no puede tener alucinaciones: el informe es una consulta ordenada
sobre Fichas ya firmadas por personas, y este módulo no importa `capa_modelos` (regla de
`test_arquitectura`). Es dueño de `informe`, `informe_pieza` e `informe_aprobacion`.

Al generar se congelan las Piezas con su versión de Ficha (`informe_pieza`): el contenido sale de
esas versiones, que son inmutables, así que el informe se relee igual. Los faltantes no se
congelan: se calculan al leer, para reflejar el avance (R-21).

067 suma la integridad (M2 sobre sus Piezas), 068 las aprobaciones, 069 regenerar y descartar, y
070 y 071 la exportación. Lo que cambió después de generar (una Pieza retirada o con otra Ficha)
tampoco se guarda: se compara al leer y al aprobar (R-22).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from typing import Any, Literal
from uuid import UUID

from historia_viva.auditoria import Accion, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Tx
from historia_viva.catalogos import carreras as carreras_del_catalogo
from historia_viva.catalogos import es_actividad, tipos_actividad, validar_carreras
from historia_viva.dominio.errores import ConsultaInvalida, PermisoInsuficiente
from historia_viva.dominio.fechas import PrecisionFecha, fecha_legible
from historia_viva.dominio.permisos import Permiso, ve_piezas_no_validadas
from historia_viva.dominio.tipos import CambioPosterior, CamposFicha
from historia_viva.exportacion import (
    BloqueDeDocumento,
    Celda,
    HojaDeSalida,
    Parrafo,
    Tabla,
    Titulo,
    escribir_docx,
    escribir_xlsx,
)
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Principal

__all__ = [
    "SIN_PIEZAS",
    "Aprobacion",
    "AprobacionRechazada",
    "AprobadorRepetido",
    "ArchivoExportado",
    "Faltante",
    "Formato",
    "GrupoDeActividad",
    "InformeDesactualizado",
    "InformeNoAprobable",
    "InformeNoAprobado",
    "InformeNoEncontrado",
    "Informes",
    "PiezaDeInforme",
    "RolYaAprobo",
    "VistaDeInforme",
    "contar_firmas_de_ensayo",
]

SIN_PIEZAS = "No hay Piezas Validadas que coincidan. No se generó el informe."
"""Recorrido R22 E1: un período sin Piezas Validadas no crea Borrador (auditoría, decisión 5)."""


class InformeNoEncontrado(Exception):
    """404 `recurso-no-encontrado` con `recurso: informe`."""


_NOMBRE_DEL_ROL = {"rgc": "RGC", "coordinador": "Coordinador de Extensión"}
_OTRO_ROL = {"rgc": "coordinador", "coordinador": "rgc"}


class AprobacionRechazada(Exception):
    """409: la aprobación, el descarte o el reemplazo no se registra. `codigo` es el `type` del
    catálogo de errores."""

    codigo = ""


class AprobadorRepetido(AprobacionRechazada):
    codigo = "aprobador-repetido"

    def __init__(self, falta: str) -> None:
        super().__init__(
            f"Ya aprobaste este informe. Falta la aprobación del {_NOMBRE_DEL_ROL[falta]}."
        )


class RolYaAprobo(AprobacionRechazada):
    codigo = "informe-ya-aprobado-por-rol"

    def __init__(self, falta: str) -> None:
        super().__init__(
            f"Hace falta la aprobación de una persona con rol {_NOMBRE_DEL_ROL[falta]}."
        )


class InformeNoAprobable(AprobacionRechazada):
    """Ya no es un Borrador vigente: aprobado, exportado, descartado o reemplazado."""

    codigo = "transicion-invalida"

    def __init__(self, accion: str = "aprobar") -> None:
        super().__init__(f"Este informe ya no es un Borrador vigente: no se puede {accion}.")


class InformeDesactualizado(AprobacionRechazada):
    codigo = "informe-desactualizado"

    def __init__(self, cambios: Sequence[CambioPosterior]) -> None:
        nombres = ", ".join(f"«{c.nombre_archivo}»" for c in cambios)
        sujeto = f"Las Piezas {nombres} cambiaron" if len(cambios) > 1 else (
            f"La Pieza {nombres} cambió"
        )
        super().__init__(
            f"{sujeto} desde que se generó el Borrador: generalo de nuevo para aprobarlo."
        )


class InformeNoAprobado(Exception):
    """409 `informe-no-aprobado`: se exporta un Aprobado o un Exportado (R-23)."""

    def __init__(self, estado: str) -> None:
        self.estado_actual = estado.capitalize()
        super().__init__(
            "El informe todavía no está aprobado: se exporta con la aprobación del RGC y la del"
            " Coordinador de Extensión."
        )


Formato = Literal["docx", "xlsx"]
"""Cada formato es un armador en `_ARMADORES`."""


@dataclass(frozen=True)
class ArchivoExportado:
    nombre: str
    tipo_mime: str
    contenido: bytes


@dataclass(frozen=True)
class PiezaDeInforme:
    pieza_id: UUID
    ficha_id: UUID
    tipo: str | None
    fecha_inicio: date | None
    fecha_fin: date | None
    precision_fecha: str | None
    resumen: str | None
    """El validado, de la versión congelada."""
    propietario: str | None
    autor: str | None
    carreras: tuple[str, ...]
    cita: dict[str, str]
    """`pza_<id>` y el enlace al original con `HV_URL_PUBLICA`."""


@dataclass(frozen=True)
class GrupoDeActividad:
    tipo_actividad: str
    piezas: tuple[PiezaDeInforme, ...]


@dataclass(frozen=True)
class Faltante:
    pieza_id: UUID
    nombre_archivo: str
    estado: str
    lote_id: UUID | None
    falta_para_validar: tuple[str, ...]


@dataclass(frozen=True)
class Aprobacion:
    rol: str
    aprobada_por: UUID
    aprobada_el: datetime
    nombre: str
    """De quien firmó: P9 muestra quién aprobó."""
    metodo_autenticacion: str
    """`dev` es una firma de ensayo (DEC-139)."""


@dataclass(frozen=True)
class VistaDeInforme:
    informe_id: UUID
    desde: date
    hasta: date
    carreras: tuple[str, ...]
    estado: str
    huella: str
    integridad: float | None
    generado_por: UUID
    generado_el: datetime
    total_piezas: int
    aprobaciones: tuple[Aprobacion, ...]
    reemplazado_por: UUID | None
    """El Borrador que lo regeneró: este deja de listarse (R-22)."""
    descartado_el: datetime | None
    grupos: tuple[GrupoDeActividad, ...] = ()
    faltantes: tuple[Faltante, ...] = ()
    cambios: tuple[CambioPosterior, ...] = ()
    """Las Piezas que cambiaron desde que se congelaron: se calculan al leer el detalle."""

    @property
    def cambio_despues_de_aprobar(self) -> bool:
        """Aprobado o Exportado con una Pieza retirada o con otra Ficha: conserva lo congelado
        y la aprobación, y lo avisa (R-22)."""
        return self.estado != "borrador" and bool(self.cambios)


_LEE_BORRADORES = (Permiso.GENERAR_INFORME, Permiso.APROBAR_INFORME)

_SELECT_INFORME = (
    "SELECT informe_id, desde, hasta, carreras, estado, huella, integridad, generado_por,"
    " generado_el, reemplazado_por, descartado_el, (SELECT count(*) FROM acervo.informe_pieza ip"
    " WHERE ip.informe_id = informe.informe_id) AS total_piezas FROM acervo.informe"
)


Integridad = Callable[[Tx, Sequence[UUID]], float | None]
"""M2 sobre las Piezas pedidas (`banco.integridad`): llega de `composicion`, así `informes` no
depende de `banco` (DEC-128, R-21)."""


class Informes:
    def __init__(
        self,
        *,
        fichas: ModuloFichasPostgres,
        auditoria: RegistroDeAuditoria,
        url_publica: str,
        integridad: Integridad,
    ) -> None:
        self._fichas = fichas
        self._auditoria = auditoria
        self._url_publica = url_publica
        self._integridad = integridad

    def generar(
        self,
        tx: Tx,
        *,
        desde: date,
        hasta: date,
        carreras: Sequence[str],
        principal: Principal,
        reemplaza: UUID | None = None,
    ) -> VistaDeInforme:
        """Congela en Borrador las Validadas del período, sin «No es una actividad», por tipo de
        actividad en el orden del catálogo y, dentro, por Fecha y `pieza_id`. Con `reemplaza`
        regenera ese Borrador: queda con `reemplazado_por` y sus aprobaciones no pasan (R-22).

        Raises:
            PermisoInsuficiente: Sin GENERAR_INFORME (DEC-124).
            ConsultaInvalida: `desde > hasta`, o ninguna Pieza Validada coincide (422).
            CarreraDesconocida: Una Carrera repetida o que nunca existió (422).
            InformeNoEncontrado: `reemplaza` no existe.
            InformeNoAprobable: `reemplaza` ya no es un Borrador vigente (409).
        """
        if not principal.puede(Permiso.GENERAR_INFORME):
            raise PermisoInsuficiente("Los informes los genera el Coordinador de Extensión.")
        if desde > hasta:
            raise ConsultaInvalida("El período termina antes de empezar.", campo="hasta")
        if reemplaza is not None:
            self._bloquear_borrador(tx, reemplaza, accion="generar de nuevo")
        # Una Carrera dada de baja vale como filtro: las Fichas que la tenían la conservan.
        validar_carreras(tx, carreras, conserva=carreras)
        orden = {t.nombre: t.orden for t in tipos_actividad(tx, solo_activos=False)}
        piezas = sorted(
            (
                p
                for p in self._fichas.validadas_del_periodo(tx, desde, hasta, carreras=carreras)
                if es_actividad(p.tipo_actividad)
            ),
            key=lambda p: (orden[p.tipo_actividad], p.fecha_inicio, p.pieza_id),
        )
        if not piezas:
            raise ConsultaInvalida(SIN_PIEZAS, campo=None)
        carreras = sorted(carreras)
        # El contenido canónico: período, Carreras y cada Pieza con su versión, en orden.
        congelado = [[str(p.pieza_id), str(p.ficha_id)] for p in piezas]
        huella = hashlib.sha256(
            json.dumps([str(desde), str(hasta), carreras, congelado]).encode()
        ).hexdigest()
        # No bloquea: un informe con integridad baja se genera igual y lo avisa (DIR-38).
        integridad = self._integridad(tx, [p.pieza_id for p in piezas])
        fila = tx.execute(
            "INSERT INTO acervo.informe (desde, hasta, carreras, huella, integridad, generado_por)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING informe_id",
            (desde, hasta, carreras, huella, integridad, principal.usuario_id),
        ).fetchone()
        assert fila is not None
        informe_id: UUID = fila["informe_id"]
        tx.cursor().executemany(
            "INSERT INTO acervo.informe_pieza (informe_id, pieza_id, ficha_id, tipo_actividad,"
            " fecha_inicio, fecha_fin, orden) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (informe_id, p.pieza_id, p.ficha_id, p.tipo_actividad, p.fecha_inicio,
                 p.fecha_fin, n)
                for n, p in enumerate(piezas, start=1)
            ],
        )
        if reemplaza is not None:
            tx.execute(
                "UPDATE acervo.informe SET reemplazado_por = %s WHERE informe_id = %s",
                (informe_id, reemplaza),
            )
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.INFORME_GENERACION,
            objeto=ObjetoRef("informe", str(informe_id)),
            despues={
                "desde": str(desde), "hasta": str(hasta), "carreras": carreras,
                "piezas": len(piezas), "huella": huella,
            } | ({"reemplaza": str(reemplaza)} if reemplaza else {}),
        )
        return self.obtener(tx, informe_id, principal=principal)

    def recalcular_integridad(self, tx: Tx, informe_id: UUID) -> float | None:
        """M2 otra vez sobre las mismas Piezas congeladas, y queda como la del informe: lo llama
        la exportación (070, 071), que la escribe en el documento. No bloquea exportar.

        Raises:
            InformeNoEncontrado: No existe.
        """
        fila = tx.execute(
            "UPDATE acervo.informe SET integridad = %s WHERE informe_id = %s"
            " RETURNING integridad",
            (self._integridad(tx, self.piezas(tx, informe_id)), informe_id),
        ).fetchone()
        assert fila is not None  # `piezas` ya comprobó que existe
        return None if fila["integridad"] is None else float(fila["integridad"])

    def piezas(self, tx: Tx, informe_id: UUID) -> list[UUID]:
        """Las Piezas congeladas en el informe, en su orden: sobre ellas se mide la integridad
        (también `banco m2 --informe`, R-21), sin mirar el estado ni quién pide.

        Raises:
            InformeNoEncontrado: No existe.
        """
        if tx.execute(
            "SELECT 1 FROM acervo.informe WHERE informe_id = %s", (informe_id,)
        ).fetchone() is None:
            raise InformeNoEncontrado(informe_id)
        filas = tx.execute(
            "SELECT pieza_id FROM acervo.informe_pieza WHERE informe_id = %s ORDER BY orden",
            (informe_id,),
        ).fetchall()
        return [fila["pieza_id"] for fila in filas]

    def aprobar(self, tx: Tx, informe_id: UUID, *, principal: Principal) -> VistaDeInforme:
        """Firma el Borrador con el rol de la sesión; con la del otro rol, de otra persona, pasa
        a Aprobado en la misma transacción (`informe_aprobado_dos_firmas` lo exige sobre la
        huella). La fila del informe queda bloqueada: dos aprobaciones a la vez se ordenan.

        Raises:
            PermisoInsuficiente: Sin APROBAR_INFORME (DEC-124).
            InformeNoEncontrado: No existe.
            AprobacionRechazada: 409 `aprobador-repetido`, `informe-ya-aprobado-por-rol`,
                `transicion-invalida` (ya no es un Borrador vigente) o `informe-desactualizado`
                (una Pieza cambió desde que se generó, R-43 g).
        """
        if not principal.puede(Permiso.APROBAR_INFORME):
            raise PermisoInsuficiente("Los informes los aprueban el RGC y el Coordinador.")
        huella = self._bloquear_borrador(tx, informe_id, accion="aprobar")
        # shortcut: no bloquea las Piezas; una edición que confirma entre esta lectura y el
        # COMMIT no se ve. Bloquearlas con `FOR SHARE` si esa carrera llega a importar.
        if cambios := self._fichas.cambios_desde(tx, self._congeladas(tx, informe_id)):
            raise InformeDesactualizado(cambios)
        previas = {
            a["rol"]: a["aprobada_por"]
            for a in tx.execute(
                "SELECT rol, aprobada_por FROM acervo.informe_aprobacion WHERE informe_id = %s",
                (informe_id,),
            ).fetchall()
        }
        rol = principal.rol.value
        falta = _OTRO_ROL[rol]
        if principal.usuario_id in previas.values():
            raise AprobadorRepetido(falta)
        if rol in previas:
            raise RolYaAprobo(falta)
        tx.execute(
            "INSERT INTO acervo.informe_aprobacion"
            " (informe_id, rol, aprobada_por, metodo_autenticacion, huella)"
            " VALUES (%s, %s, %s, %s, %s)",
            (informe_id, rol, principal.usuario_id, principal.metodo.value, huella),
        )
        estado = "borrador"
        if falta in previas:
            estado = "aprobado"
            tx.execute(
                "UPDATE acervo.informe SET estado = 'aprobado', aprobado_el = now()"
                " WHERE informe_id = %s",
                (informe_id,),
            )
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.INFORME_APROBACION,
            objeto=ObjetoRef("informe", str(informe_id)),
            despues={
                "rol": rol, "metodo_autenticacion": principal.metodo.value,
                "huella": huella, "estado": estado,
            },
        )
        return self.obtener(tx, informe_id, principal=principal)

    def descartar(self, tx: Tx, informe_id: UUID, *, principal: Principal) -> VistaDeInforme:
        """El Borrador deja de listarse y no admite aprobaciones (R-22). Para volver a tener un
        informe del período se genera otro.

        Raises:
            PermisoInsuficiente: Sin GENERAR_INFORME (DEC-124).
            InformeNoEncontrado: No existe.
            InformeNoAprobable: Ya no es un Borrador vigente (409 `transicion-invalida`).
        """
        if not principal.puede(Permiso.GENERAR_INFORME):
            raise PermisoInsuficiente("Los informes los descarta el Coordinador de Extensión.")
        self._bloquear_borrador(tx, informe_id, accion="descartar")
        tx.execute(
            "UPDATE acervo.informe SET descartado_por = %s, descartado_el = now()"
            " WHERE informe_id = %s",
            (principal.usuario_id, informe_id),
        )
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.INFORME_DESCARTE,
            objeto=ObjetoRef("informe", str(informe_id)),
        )
        return self.obtener(tx, informe_id, principal=principal)

    def exportar(
        self, tx: Tx, informe_id: UUID, formato: Formato, *, principal: Principal
    ) -> ArchivoExportado:
        """El informe como archivo, desde las versiones congeladas, con la integridad medida de
        nuevo (no bloquea). La primera exportación lo pasa a Exportado con quién y cuándo; cada
        una se audita (R-23). Si algo cambió después de aprobar, el documento lo avisa (R-22).

        Raises:
            PermisoInsuficiente: Sin GENERAR_INFORME ni APROBAR_INFORME (R-23).
            InformeNoEncontrado: No existe.
            InformeNoAprobado: Es un Borrador (409 `informe-no-aprobado`).
        """
        # Exportan los mismos que leen Borradores (R-23).
        if not self._lee_borradores(principal):
            raise PermisoInsuficiente("Los informes los exportan el RGC y el Coordinador.")
        # Bloquea: dos primeras exportaciones a la vez se ordenan y solo una fija quién y cuándo.
        informe = tx.execute(
            "SELECT estado, huella FROM acervo.informe WHERE informe_id = %s FOR UPDATE",
            (informe_id,),
        ).fetchone()
        if informe is None:
            raise InformeNoEncontrado(informe_id)
        if informe["estado"] == "borrador":
            raise InformeNoAprobado(informe["estado"])
        self.recalcular_integridad(tx, informe_id)
        primera = informe["estado"] == "aprobado"
        if primera:
            tx.execute(
                "UPDATE acervo.informe SET estado = 'exportado', exportado_por = %s,"
                " exportado_el = now() WHERE informe_id = %s",
                (principal.usuario_id, informe_id),
            )
        self._auditoria.registrar(
            tx,
            actor=principal.actor,
            accion=Accion.INFORME_EXPORTACION,
            objeto=ObjetoRef("informe", str(informe_id)),
            despues={"formato": formato, "huella": informe["huella"], "primera": primera},
        )
        vista = self.obtener(tx, informe_id, principal=principal)
        nombres = {c.carrera_id: c.nombre for c in carreras_del_catalogo(tx)}
        return _ARMADORES[formato](vista, nombres)

    def _bloquear_borrador(self, tx: Tx, informe_id: UUID, *, accion: str) -> str:
        """Bloquea el informe hasta el COMMIT y devuelve su huella si es un Borrador vigente."""
        informe = tx.execute(
            "SELECT estado, huella, reemplazado_por, descartado_el FROM acervo.informe"
            " WHERE informe_id = %s FOR UPDATE",
            (informe_id,),
        ).fetchone()
        if informe is None:
            raise InformeNoEncontrado(informe_id)
        if (
            informe["estado"] != "borrador"
            or informe["reemplazado_por"] is not None
            or informe["descartado_el"] is not None
        ):
            raise InformeNoAprobable(accion)
        huella: str = informe["huella"]
        return huella

    def _congeladas(self, tx: Tx, informe_id: UUID) -> dict[UUID, UUID]:
        filas = tx.execute(
            "SELECT pieza_id, ficha_id FROM acervo.informe_pieza WHERE informe_id = %s",
            (informe_id,),
        ).fetchall()
        return {f["pieza_id"]: f["ficha_id"] for f in filas}

    def listar(self, tx: Tx, *, principal: Principal) -> list[VistaDeInforme]:
        """Los vigentes, del más reciente al más viejo, sin grupos ni faltantes. Quien solo
        consulta ve únicamente los Aprobados y Exportados (R-05)."""
        filas = tx.execute(
            f"{_SELECT_INFORME} WHERE reemplazado_por IS NULL AND descartado_el IS NULL"
            " AND (%s OR estado <> 'borrador') ORDER BY generado_el DESC, informe_id",
            (self._lee_borradores(principal),),
        ).fetchall()
        return [self._vista(tx, fila) for fila in filas]

    def obtener(self, tx: Tx, informe_id: UUID, *, principal: Principal) -> VistaDeInforme:
        """El informe con sus grupos congelados y los faltantes de hoy.

        Raises:
            InformeNoEncontrado: No existe, o es un Borrador y quien pide solo consulta: no se
                entera de que existe (R-05, como las Piezas no Validadas en 038).
        """
        fila = tx.execute(f"{_SELECT_INFORME} WHERE informe_id = %s", (informe_id,)).fetchone()
        if fila is None or (fila["estado"] == "borrador" and not self._lee_borradores(principal)):
            raise InformeNoEncontrado(informe_id)
        congeladas = tx.execute(
            "SELECT pieza_id, ficha_id, tipo_actividad FROM acervo.informe_pieza"
            " WHERE informe_id = %s ORDER BY orden",
            (informe_id,),
        ).fetchall()
        versiones = self._fichas.leer_versiones(tx, [c["ficha_id"] for c in congeladas])
        grupos = tuple(
            GrupoDeActividad(
                tipo_actividad,
                tuple(self._pieza(versiones[c["ficha_id"]].campos, c) for c in del_grupo),
            )
            for tipo_actividad, del_grupo in groupby(congeladas, key=lambda c: c["tipo_actividad"])
        )
        faltantes = tuple(
            Faltante(
                pieza_id=d.fila.pieza_id,
                nombre_archivo=d.fila.nombre_archivo,
                estado=d.fila.estado.value,
                lote_id=d.lote_id,
                falta_para_validar=tuple(d.fila.campos_faltantes),
            )
            for d in self._fichas.faltantes_del_periodo(
                tx, fila["desde"], fila["hasta"], carreras=fila["carreras"]
            )
        ) if ve_piezas_no_validadas(principal.rol) else ()  # R-05
        cambios = tuple(self._fichas.cambios_desde(
            tx, {c["pieza_id"]: c["ficha_id"] for c in congeladas}
        ))
        return replace(
            self._vista(tx, fila), grupos=grupos, faltantes=faltantes, cambios=cambios
        )

    def _lee_borradores(self, principal: Principal) -> bool:
        return any(principal.puede(permiso) for permiso in _LEE_BORRADORES)

    def _vista(self, tx: Tx, fila: dict[str, Any]) -> VistaDeInforme:
        aprobaciones = tx.execute(
            "SELECT a.rol, a.aprobada_por, a.aprobada_el, u.nombre, a.metodo_autenticacion"
            " FROM acervo.informe_aprobacion a"
            " JOIN acervo.usuario u ON u.usuario_id = a.aprobada_por"
            " WHERE a.informe_id = %s ORDER BY a.aprobada_el",
            (fila["informe_id"],),
        ).fetchall()
        integridad = fila["integridad"]
        return VistaDeInforme(
            informe_id=fila["informe_id"],
            desde=fila["desde"],
            hasta=fila["hasta"],
            carreras=tuple(fila["carreras"]),
            estado=fila["estado"],
            huella=fila["huella"],
            integridad=None if integridad is None else float(integridad),
            generado_por=fila["generado_por"],
            generado_el=fila["generado_el"],
            total_piezas=fila["total_piezas"],
            aprobaciones=tuple(Aprobacion(**a) for a in aprobaciones),
            reemplazado_por=fila["reemplazado_por"],
            descartado_el=fila["descartado_el"],
        )

    def _pieza(self, campos: CamposFicha, congelada: dict[str, Any]) -> PiezaDeInforme:
        pieza_id: UUID = congelada["pieza_id"]
        return PiezaDeInforme(
            pieza_id=pieza_id,
            ficha_id=congelada["ficha_id"],
            tipo=campos.tipo,
            fecha_inicio=campos.fecha_inicio,
            fecha_fin=campos.fecha_fin,
            precision_fecha=campos.precision_fecha and campos.precision_fecha.value,
            resumen=campos.resumen,
            propietario=campos.propietario,
            autor=campos.autor,
            carreras=tuple(campos.carreras),
            cita={
                "id": f"pza_{pieza_id}",
                "enlace": f"{self._url_publica}/api/piezas/{pieza_id}/original",
            },
        )


# Exportación: `informes` arma el contenido y `exportacion` escribe el archivo (DEC-131).

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_ROLES = {"coordinador": "Coordinador de Extensión", "rgc": "RGC"}
_HORA_ARGENTINA = timezone(timedelta(hours=-3))
"""Sin horario de verano desde 2009: la hora de las firmas no depende del servidor."""
_META_INTEGRIDAD = 0.95
"""DIR-38."""


def _periodo(vista: VistaDeInforme) -> str:
    return f"del {vista.desde:%d/%m/%Y} al {vista.hasta:%d/%m/%Y}"


def _carreras(carreras: Sequence[str], nombres: Mapping[str, str]) -> str:
    # Una Carrera dada de baja no está en el catálogo activo: va por su slug, como en P9.
    return ", ".join(nombres.get(c, c) for c in carreras) or "Todas"


def _integridad(vista: VistaDeInforme) -> str:
    if vista.integridad is None:
        return "Integridad: sin medir."
    # Truncado, como en P9: 94,99 % no se lee 95 %.
    texto = (
        f"Integridad: {math.floor(round(vista.integridad * 10000) / 100)} % (M2 sobre las"
        f" {vista.total_piezas} Piezas incluidas)."
    )
    if vista.integridad < _META_INTEGRIDAD:
        texto += " Aviso: la integridad no llega a 95 %."
    return texto


def _aviso_de_cambios(cambios: Sequence[CambioPosterior]) -> str:
    """E9, con el mismo texto que P9 (R-22)."""
    partes = "; ".join(
        f"la Pieza «{c.nombre_archivo}» fue retirada" if c.retirada
        else f"la Ficha de la Pieza «{c.nombre_archivo}» cambió"
        for c in cambios
    )
    return (
        f"Cambió después de aprobar: {partes}. Generá un Borrador nuevo para incorporar el"
        " cambio."
    )


def _fecha_de_pieza(pieza: PiezaDeInforme) -> str:
    if pieza.fecha_inicio is None or pieza.precision_fecha is None:
        return "Sin Fecha"
    return fecha_legible(
        pieza.fecha_inicio, pieza.fecha_fin or pieza.fecha_inicio,
        PrecisionFecha(pieza.precision_fecha),
    )


def _bloques(vista: VistaDeInforme, nombres: Mapping[str, str]) -> list[BloqueDeDocumento]:
    """Portada (período, Carreras, integridad, aviso y aprobaciones), un capítulo por tipo de
    actividad y, al final, los faltantes de hoy."""
    bloques: list[BloqueDeDocumento] = [
        Titulo(0, "Informe de acreditación"),
        Parrafo(f"Período: {_periodo(vista)}."),
        Parrafo(f"Carreras: {_carreras(vista.carreras, nombres)}."),
        Parrafo(_integridad(vista)),
        Parrafo("Armado a partir de las Fichas validadas. Ningún modelo escribió este informe."),
    ]
    if vista.cambio_despues_de_aprobar:
        bloques.append(Parrafo(_aviso_de_cambios(vista.cambios)))
    bloques += [
        Titulo(1, "Aprobaciones"),
        Tabla(("Rol", "Persona", "Fecha", "Firma"), tuple(
            (
                _ROLES[a.rol], a.nombre,
                f"{a.aprobada_el.astimezone(_HORA_ARGENTINA):%d/%m/%Y %H:%M}",
                "De ensayo (sin OIDC)" if a.metodo_autenticacion == "dev" else "OIDC",
            )
            for a in vista.aprobaciones
        )),
    ]
    for grupo in vista.grupos:
        cantidad = "1 Pieza" if len(grupo.piezas) == 1 else f"{len(grupo.piezas)} Piezas"
        bloques.append(Titulo(1, f"{grupo.tipo_actividad} ({cantidad})"))
        for pieza in grupo.piezas:
            datos = " · ".join(
                f"{rotulo}: {valor}" for rotulo, valor in (
                    ("Propietario", pieza.propietario), ("Autor", pieza.autor),
                    ("Carreras", pieza.carreras and _carreras(pieza.carreras, nombres)),
                ) if valor
            )
            bloques += [
                Titulo(2, f"{_fecha_de_pieza(pieza)} · {pieza.tipo or 'Pieza'}"),
                Parrafo(pieza.resumen or ""),
                *([Parrafo(datos)] if datos else []),
                # Texto, no vínculo: el documento no tiene vínculos activos.
                Parrafo(f"Cita: {pieza.cita['id']} · {pieza.cita['enlace']}"),
            ]
    bloques.append(Titulo(1, f"Faltantes ({len(vista.faltantes)})"))
    if vista.faltantes:
        bloques += [
            Parrafo("Piezas del período que no están Validadas: no son parte de lo aprobado."),
            Tabla(("Archivo", "Estado", "Le falta para validarse"), tuple(
                (f.nombre_archivo, f.estado.capitalize(), ", ".join(f.falta_para_validar))
                for f in vista.faltantes
            )),
        ]
    elif vista.carreras:
        bloques.append(Parrafo("Ninguna Pieza del período sin validar tiene esas Carreras."))
    else:
        bloques.append(Parrafo("Ninguna: todas las Piezas del período están Validadas."))
    return bloques


def _como_docx(vista: VistaDeInforme, nombres: Mapping[str, str]) -> ArchivoExportado:
    contenido = escribir_docx(
        _bloques(vista, nombres),
        titulo=f"Informe de acreditación {_periodo(vista)}",
        autor="Historia Viva",
        fecha=vista.generado_el,  # la de generación: el mismo informe da los mismos bytes
    )
    return ArchivoExportado(f"informe-{vista.desde}-a-{vista.hasta}.docx", _DOCX, contenido)


def _hojas(vista: VistaDeInforme, nombres: Mapping[str, str]) -> list[HojaDeSalida]:
    """«Resumen» (período, Carreras, integridad, aviso y aprobaciones), «Piezas» (una fila por
    Pieza, por tipo de actividad y Fecha) y «Faltantes» (los de hoy)."""
    resumen: list[tuple[Celda, ...]] = [
        ("Informe de acreditación", _periodo(vista)),
        ("Desde", vista.desde),
        ("Hasta", vista.hasta),
        ("Carreras", _carreras(vista.carreras, nombres)),
        ("Piezas incluidas", vista.total_piezas),
        ("Integridad", _integridad(vista).removeprefix("Integridad: ")),
        ("Faltantes", len(vista.faltantes)),
        ("Origen", "Armado a partir de las Fichas validadas. Ningún modelo escribió este informe."),
    ]
    if vista.cambio_despues_de_aprobar:
        resumen.append(("Aviso", _aviso_de_cambios(vista.cambios)))
    resumen += [
        (
            f"Aprobación {_ROLES[a.rol]}", a.nombre,
            # Una fecha de planilla no tiene zona: va en la hora de Argentina, como en el .docx.
            a.aprobada_el.astimezone(_HORA_ARGENTINA),
            "De ensayo (sin OIDC)" if a.metodo_autenticacion == "dev" else "OIDC",
        )
        for a in vista.aprobaciones
    ]
    piezas = tuple(
        (
            grupo.tipo_actividad, _fecha_de_pieza(pieza), pieza.fecha_inicio, pieza.fecha_fin,
            pieza.tipo, pieza.resumen, pieza.propietario, pieza.autor,
            _carreras(pieza.carreras, nombres) if pieza.carreras else None,
            pieza.cita["id"], pieza.cita["enlace"],  # texto, no vínculo
        )
        for grupo in vista.grupos
        for pieza in grupo.piezas
    )
    faltantes = tuple(
        (f.nombre_archivo, f.estado.capitalize(), ", ".join(f.falta_para_validar))
        for f in vista.faltantes
    )
    return [
        HojaDeSalida("Resumen", ("Dato", "Valor"), tuple(resumen)),
        HojaDeSalida("Piezas", (
            "Tipo de actividad", "Fecha", "Desde", "Hasta", "Tipo", "Resumen", "Propietario",
            "Autor", "Carreras", "Cita", "Original",
        ), piezas),
        HojaDeSalida("Faltantes", ("Archivo", "Estado", "Le falta para validarse"), faltantes),
    ]


def _como_xlsx(vista: VistaDeInforme, nombres: Mapping[str, str]) -> ArchivoExportado:
    contenido = escribir_xlsx(
        _hojas(vista, nombres),
        titulo=f"Informe de acreditación {_periodo(vista)}",
        autor="Historia Viva",
        fecha=vista.generado_el,  # la de generación: el mismo informe da los mismos bytes
    )
    return ArchivoExportado(f"informe-{vista.desde}-a-{vista.hasta}.xlsx", _XLSX, contenido)


_ARMADORES: dict[str, Callable[[VistaDeInforme, Mapping[str, str]], ArchivoExportado]] = {
    "docx": _como_docx,
    "xlsx": _como_xlsx,
}


def contar_firmas_de_ensayo(tx: Tx) -> int:
    """Las aprobaciones de Informe firmadas con una sesión `dev` (DEC-139)."""
    fila = tx.execute(
        "SELECT count(*) AS n FROM acervo.informe_aprobacion WHERE metodo_autenticacion = 'dev'"
    ).fetchone()
    return int(fila["n"]) if fila else 0
