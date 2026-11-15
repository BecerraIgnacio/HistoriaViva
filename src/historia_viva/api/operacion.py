"""Salud: si la base, la capa de modelos y los trabajadores responden, y cómo está la cola.

Sin sesión, o sin OPERAR_SISTEMA, responde solo `{estado}` (DEC-102): el healthcheck del
compose no necesita más y nadie de afuera ve qué falla. El detalle es del Administrador
técnico (historia 233).
"""

from __future__ import annotations

import time
from dataclasses import asdict

from fastapi import APIRouter, Request

from historia_viva import drive
from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import principal_actual
from historia_viva.bd import Bd
from historia_viva.dominio.errores import ConfiguracionInvalida
from historia_viva.dominio.permisos import Permiso
from historia_viva.interruptor import Freno, habilitado_en
from historia_viva.procesamiento.latido import latidos_por_tipo

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["Operación"])


def _ve_el_detalle(request: Request) -> bool:
    try:
        return principal_actual(request).puede(Permiso.OPERAR_SISTEMA)
    except Problema:  # sin sesión: la salud no la exige
        return False


_SALUD_SIN_DETALLE_S = 5
"""Sin sesión la salud es pública: un mismo `{estado}` sirve 5 s y una ráfaga anónima no le pega
a la base ni al motor en cada pedido (SEG-08, 165). Sin 429: el healthcheck del compose usa la
misma ruta, y una ráfaga ajena dejaría a `web` como `unhealthy`."""


@router.get("/salud")
def consultar_salud(request: Request) -> dict[str, object]:
    detalle = _ve_el_detalle(request)
    hasta, estado_guardado = request.app.state.salud_sin_detalle
    if not detalle and time.monotonic() < hasta:
        return {"estado": estado_guardado}
    bd: Bd = request.app.state.bd
    # Base caída: `BaseNoDisponible` sale como 503 antes de preguntarle nada al motor.
    bd.ping()
    # El motor de modelos no responde no es un 503: no procesar Piezas nuevas no impide
    # validar ni guardar las que ya estaban listas (ADR-0004, interruptor global). Un modelo
    # sin descargar es un aviso, no un bloqueo (historia 165).
    faltantes: list[str] = []
    try:
        capa = request.app.state.capa_vigente()
        if capa.verificar_salud():
            faltantes = list(capa.modelos_faltantes())
            capa_modelos = "modelos_faltantes" if faltantes else "ok"
        else:
            capa_modelos = "sin_respuesta"
    except ConfiguracionInvalida:  # una fila editada por SQL con la aplicación en marcha
        capa_modelos = "configuracion_invalida"
    except Exception:  # noqa: BLE001 — cualquier falla al preguntar también es "no responde"
        capa_modelos = "sin_respuesta"
    # Sin `ffprobe` la subida de video responde 503: el resto sigue (DEC-16).
    video = "ok" if request.app.state.ingesta.admite_video else "sin_ffprobe"
    # Un tipo esperado, o que tuvo trabajador, sin latido de menos de 60 s está caído (DEC-106).
    trabajadores = latidos_por_tipo(bd, esperados=request.app.state.tipos_esperados)
    sano = capa_modelos == "ok" and video == "ok" and all(t.vivo for t in trabajadores)
    estado = "ok" if sano else "degradado"
    if not detalle:
        request.app.state.salud_sin_detalle = (time.monotonic() + _SALUD_SIN_DETALLE_S, estado)
        return {"estado": estado}
    # Lo que el índice no refleja del Acervo validado: se informa, no degrada (DEC-146 punto 6).
    indice = request.app.state.indexacion.progreso()
    return {
        "estado": estado,
        "base_de_datos": "ok",
        "capa_modelos": capa_modelos,
        "modelos_faltantes": faltantes,
        "video": video,
        # El freno es una decisión, no una falla: se informa y no degrada.
        "procesamiento": "habilitado" if habilitado_en(bd, Freno.PROCESAMIENTO) else "pausado",
        "api_agentes": "habilitada" if habilitado_en(bd, Freno.API_AGENTES) else "pausada",
        "trabajadores": {
            t.tipo: {"vivo": t.vivo, "latido_el": t.latido_el} for t in trabajadores
        },
        "cola": asdict(request.app.state.cola.resumen()),
        "indice": {**asdict(indice), "incompleto": indice.incompleto},
        # Filas de la réplica por estado (124): un `fallido` se ve acá y no degrada, porque
        # Drive caído no frena nada (DRV-04). Apagada, todo en cero.
        "drive": drive.resumen(bd),
        "version": request.app.version,
    }
