"""Los cuatro roles del Director y qué permite cada uno (DEC-124).

La tabla es la única fuente de «quién puede»: la API pregunta `rol.puede(permiso)` y no compara
roles. Los nueve permisos son los únicos que existen (R-04).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum

__all__ = [
    "PERMISOS_INTERINOS_ADMIN_TECNICO",
    "PERMISOS_POR_ROL",
    "Permiso",
    "PERMISOS_CON_TITULAR",
    "Rol",
    "interinato_vigente",
    "puede_cargar_en_lote_ajeno",
    "puede_otorgar",
    "ve_piezas_no_validadas",
]


class Permiso(StrEnum):
    CARGAR = "CARGAR"
    VALIDAR = "VALIDAR"
    CONSULTAR = "CONSULTAR"
    GESTIONAR_PERSONAS = "GESTIONAR_PERSONAS"
    RETIRAR_PIEZAS = "RETIRAR_PIEZAS"
    REVISAR_EFEMERIDES = "REVISAR_EFEMERIDES"
    GENERAR_INFORME = "GENERAR_INFORME"
    APROBAR_INFORME = "APROBAR_INFORME"
    OPERAR_SISTEMA = "OPERAR_SISTEMA"


class Rol(StrEnum):
    """Los valores son los de `usuario.rol`."""

    RGC = "rgc"
    COORDINADOR = "coordinador"
    """Coordinador de Extensión."""
    DIRECTOR_CARRERA = "director_carrera"
    ADMIN_TECNICO = "admin_tecnico"

    def puede(self, permiso: Permiso, *, interino: bool = False) -> bool:
        """`interino`: el interinato del Administrador técnico está vigente (lo calcula
        `identidad`); no le suma nada a otro rol."""
        return permiso in PERMISOS_POR_ROL[self] or (
            interino and self is Rol.ADMIN_TECNICO and permiso in PERMISOS_INTERINOS_ADMIN_TECNICO
        )


PERMISOS_POR_ROL: Mapping[Rol, frozenset[Permiso]] = {
    Rol.RGC: frozenset({
        Permiso.CARGAR, Permiso.VALIDAR, Permiso.CONSULTAR, Permiso.GESTIONAR_PERSONAS,
        Permiso.RETIRAR_PIEZAS, Permiso.REVISAR_EFEMERIDES, Permiso.APROBAR_INFORME,
    }),
    Rol.COORDINADOR: frozenset({
        Permiso.CARGAR, Permiso.VALIDAR, Permiso.CONSULTAR, Permiso.REVISAR_EFEMERIDES,
        Permiso.GENERAR_INFORME, Permiso.APROBAR_INFORME,
    }),
    Rol.DIRECTOR_CARRERA: frozenset({Permiso.CONSULTAR}),
    # Quien opera el sistema nunca decide sobre el contenido del Acervo: nunca VALIDAR.
    Rol.ADMIN_TECNICO: frozenset({Permiso.CONSULTAR, Permiso.OPERAR_SISTEMA}),
}

PERMISOS_INTERINOS_ADMIN_TECNICO: frozenset[Permiso] = frozenset({
    Permiso.GESTIONAR_PERSONAS, Permiso.RETIRAR_PIEZAS,
})
"""Hasta que la SEU nombre al RGC (C47). Sin VALIDAR (DEC-124)."""

PERMISOS_CON_TITULAR: tuple[Permiso, ...] = (Permiso.GESTIONAR_PERSONAS, Permiso.OPERAR_SISTEMA)
"""Siempre tienen que tener al menos una persona activa que los tenga (R-03)."""


def interinato_vigente(roles_activos: Collection[Rol]) -> bool:
    """El interinato del Administrador técnico dura mientras no haya un RGC activo (R-01): lo
    termina el alta del primero, y R-03 no deja dar de baja ni degradar al último."""
    return Rol.RGC not in roles_activos


def puede_otorgar(quien: Rol, rol: Rol) -> bool:
    """Solo un Administrador técnico otorga o quita el rol `admin_tecnico`; los otros tres los
    gestiona quien tenga GESTIONAR_PERSONAS (R-02)."""
    return rol is not Rol.ADMIN_TECNICO or quien is Rol.ADMIN_TECNICO


def puede_reprocesar_ajena(rol: Rol) -> bool:
    """Una Pieza fallida la reintenta o la completa a mano quien la subió o el RGC (R-06, 033,
    034); las dos acciones exigen además CARGAR."""
    return rol is Rol.RGC


def puede_marcar_publicable(rol: Rol) -> bool:
    """`publicable` lo cambia el RGC desde Validar (125; auditoría, decisión 15): decide qué sale
    al Agente 1 por un pedido de datos personales; firmar exige además VALIDAR."""
    return rol is Rol.RGC


def puede_cargar_en_lote_ajeno(rol: Rol) -> bool:
    """En un Lote que no creó carga el RGC o el Coordinador de Extensión (R-06); cargar exige
    además CARGAR."""
    return rol in (Rol.RGC, Rol.COORDINADOR)


def ve_piezas_no_validadas(rol: Rol) -> bool:
    """Un rol que solo tiene CONSULTAR no ve Piezas no Validadas (R-05): el Director de Carrera
    lee el Acervo Validado; el Administrador técnico, que opera el sistema, ve las demás sin
    poder validarlas."""
    return bool(PERMISOS_POR_ROL[rol] - {Permiso.CONSULTAR})
