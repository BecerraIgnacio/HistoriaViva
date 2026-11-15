"""Subir exige CARGAR y guardar la Ficha exige VALIDAR (DEC-124).

Las personas son las de prueba de desarrollo, una por rol.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts

from validacion import (
    ADMINISTRADOR_TECNICO,
    DIRECTORA_DE_CARRERA,
    EDICION_COMPLETA,
    JORGE,
    MARIA,
    PDF,
)
from aplicacion import entrar
from espera import esperar_estado
from historia_viva.auditoria import Accion, RegistroDeAuditoria
from historia_viva.dominio.permisos import Permiso

pytestmark = [
    pytest.mark.rf("RF-53"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]


def _como(cliente, persona):
    entrar(cliente, persona)
    return cliente


@pytest.mark.parametrize("persona", [DIRECTORA_DE_CARRERA, ADMINISTRADOR_TECNICO],
                         ids=["director_carrera", "admin_tecnico"])
def test_quien_no_tiene_cargar_no_sube_una_pieza(cliente, persona):
    respuesta = _como(cliente, persona).post(
        "/api/piezas", files={"archivo": ("acta.pdf", PDF, "application/pdf")}
    )

    assert respuesta.status_code == 403
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    problema = respuesta.json()
    assert problema["type"] == "/errores/permiso-insuficiente"
    # Catálogo de errores del spec: el 403 no dice qué permiso falta.
    assert problema["title"] == "No tenés permiso para esto"
    assert set(problema) == {"type", "title", "status", "detail"}
    assert "CARGAR" not in problema["detail"]
    assert cliente.get("/api/piezas").json() == {"piezas": []}


@pytest.mark.historia(280)
def test_el_administrador_tecnico_no_firma_una_ficha(cliente, pieza_pendiente):
    respuesta = _como(cliente, ADMINISTRADOR_TECNICO).put(
        f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA
    )

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/permiso-insuficiente"
    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()
    assert (ficha["version"], ficha["origen_version"]) == (1, "modelo")


def _validada(cliente):
    """Otra Pieza de María, ya Validada (un PDF distinto: el mismo sería un duplicado)."""
    entrar(cliente, MARIA)
    pieza_id = cliente.post(
        "/api/piezas", files={"archivo": ("acta-2.pdf", PDF + b"%2\n", "application/pdf")}
    ).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    guardado = cliente.put(f"/api/piezas/{pieza_id}/ficha", json=EDICION_COMPLETA)
    assert guardado.json()["estado"] == "Validada"
    return pieza_id


@pytest.mark.historia(282)
def test_el_director_de_carrera_ve_solo_las_validadas_y_no_guarda(cliente, pieza_pendiente):
    validada = _validada(cliente)
    _como(cliente, DIRECTORA_DE_CARRERA)

    respuesta = cliente.put(f"/api/piezas/{validada}/ficha", json=EDICION_COMPLETA)

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/permiso-insuficiente"
    # La Carrera es un filtro, no un permiso: ve lo que validó el RGC (DEC-125); quien solo
    # tiene CONSULTAR no ve Piezas no Validadas (R-05).
    piezas = cliente.get("/api/piezas").json()["piezas"]
    assert [pieza["pieza_id"] for pieza in piezas] == [validada]
    assert cliente.get("/api/piezas", params={"estado": "Pendiente"}).json() == {"piezas": []}


@pytest.mark.historia(282)
def test_quien_solo_consulta_abre_una_validada_y_no_una_pendiente(cliente, pieza_pendiente):
    validada = _validada(cliente)
    _como(cliente, DIRECTORA_DE_CARRERA)

    def estados(pieza_id):
        return [cliente.get(f"/api/piezas/{pieza_id}{ruta}").status_code
                for ruta in ("", "/ficha", "/original")]

    # R-05: la vista de solo lectura de una Validada y su original; una Pendiente no existe
    # para él (404, no 403: tampoco se entera de que está).
    assert estados(validada) == [200, 200, 200]
    assert estados(pieza_pendiente) == [404, 404, 404]
    assert cliente.get(f"/api/piezas/{pieza_pendiente}").json()["type"] == (
        "/errores/pieza-no-encontrada"
    )


@pytest.mark.historia(280)
def test_el_administrador_tecnico_ve_la_pendiente_aunque_no_la_firme(cliente, pieza_pendiente):
    _como(cliente, ADMINISTRADOR_TECNICO)

    piezas = cliente.get("/api/piezas").json()["piezas"]

    assert [pieza["pieza_id"] for pieza in piezas] == [pieza_pendiente]
    assert cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").status_code == 200


def test_sin_sesion_guardar_la_ficha_responde_401(cliente, pieza_pendiente):
    cliente.cookies.clear()

    respuesta = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)

    assert respuesta.status_code == 401
    assert respuesta.json()["type"] == "/errores/sin-sesion"


def test_el_coordinador_sube_una_pieza(cliente):
    coordinador = "22222222-2222-4222-8222-222222222222"
    subida = _como(cliente, coordinador).post(
        "/api/piezas", files={"archivo": ("acta.pdf", PDF, "application/pdf")}
    )

    assert subida.status_code == 201


@pytest.mark.historia(146)
def test_exigir_registra_el_403_en_auditoria_como_sesion_rechazo(cliente):
    _como(cliente, DIRECTORA_DE_CARRERA).post(
        "/api/piezas", files={"archivo": ("acta.pdf", PDF, "application/pdf")}
    )

    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.SESION_RECHAZO)
    assert (evento.actor.usuario_id, evento.motivo, evento.despues) == (
        UUID(DIRECTORA_DE_CARRERA),
        "permiso_insuficiente",
        {"permiso": "CARGAR", "ruta": "POST /api/piezas"},
    )


MATRIZ: dict[tuple[str, str], Permiso | None] = {
    ("GET", "/api/yo"): None,
    ("GET", "/api/salud"): None,
    ("GET", "/api/carreras"): Permiso.CONSULTAR,
    ("GET", "/api/tipos-actividad"): Permiso.CONSULTAR,
    ("GET", "/api/tipos"): Permiso.CONSULTAR,
    ("POST", "/api/piezas"): Permiso.CARGAR,
    ("POST", "/api/piezas/video"): Permiso.CARGAR,
    ("GET", "/api/piezas"): Permiso.CONSULTAR,
    ("GET", "/api/piezas/{pieza_id}"): Permiso.CONSULTAR,
    ("GET", "/api/piezas/{pieza_id}/original"): Permiso.CONSULTAR,
    ("GET", "/api/piezas/{pieza_id}/planilla"): Permiso.CONSULTAR,
    ("POST", "/api/piezas/{pieza_id}/reproceso"): Permiso.CARGAR,
    # Además, quien la subió o el RGC (R-06): `test_completar_a_mano`.
    ("POST", "/api/piezas/{pieza_id}/completar-a-mano"): Permiso.CARGAR,
    ("POST", "/api/piezas/{pieza_id}/apertura"): Permiso.VALIDAR,
    ("GET", "/api/piezas/{pieza_id}/ficha"): Permiso.CONSULTAR,
    ("PUT", "/api/piezas/{pieza_id}/ficha"): Permiso.VALIDAR,
    ("GET", "/api/piezas/{pieza_id}/en-lectura"): Permiso.CONSULTAR,
    ("PUT", "/api/piezas/{pieza_id}/en-lectura"): Permiso.VALIDAR,
    ("GET", "/api/piezas/{pieza_id}/afirmaciones"): Permiso.CONSULTAR,
    ("PUT", "/api/piezas/{pieza_id}/afirmaciones/{orden}"): Permiso.VALIDAR,
    # 148: «Describir esta imagen» y la miniatura de una imagen embebida sin texto.
    ("POST", "/api/piezas/{pieza_id}/imagenes/{imagen}/descripcion"): Permiso.VALIDAR,
    ("GET", "/api/piezas/{pieza_id}/imagenes/{imagen}/miniatura"): Permiso.CONSULTAR,
    ("GET", "/api/mediciones"): Permiso.OPERAR_SISTEMA,
    ("GET", "/api/mediciones.csv"): Permiso.OPERAR_SISTEMA,
    ("POST", "/api/lotes"): Permiso.CARGAR,
    ("POST", "/api/lotes/implicito"): Permiso.CARGAR,
    ("GET", "/api/lotes"): Permiso.CARGAR,
    ("GET", "/api/lotes/{lote_id}"): Permiso.CONSULTAR,
    ("PATCH", "/api/lotes/{lote_id}"): Permiso.CARGAR,
    # La aprobación en bloque (108): además, `HV_BLOQUE_HABILITADO`, `test_validacion`.
    ("POST", "/api/lotes/{lote_id}/aprobaciones"): Permiso.VALIDAR,
    ("POST", "/api/lotes/{lote_id}/aprobaciones/{aprobacion_id}/confirmacion"): Permiso.VALIDAR,
    ("GET", "/api/admin/auditoria"): Permiso.OPERAR_SISTEMA,
    ("GET", "/api/admin/interruptor"): Permiso.OPERAR_SISTEMA,
    ("PUT", "/api/admin/interruptor"): Permiso.OPERAR_SISTEMA,
    ("GET", "/api/busqueda"): Permiso.CONSULTAR,
    # Solo las propias: la ajena da el mismo 404 que una inexistente (R-31), `test_conversacion`.
    ("GET", "/api/conversaciones"): Permiso.CONSULTAR,
    ("GET", "/api/conversaciones/{conversacion_id}"): Permiso.CONSULTAR,
    ("POST", "/api/conversaciones"): Permiso.CONSULTAR,
    ("POST", "/api/conversaciones/{conversacion_id}/mensajes"): Permiso.CONSULTAR,
    ("POST", "/api/conversaciones/{conversacion_id}/mensajes/{mensaje_id}/reintentar"):
        Permiso.CONSULTAR,
    # Las Propuestas y Descartadas exigen además REVISAR_EFEMERIDES (R-05): `test_efemerides`.
    ("GET", "/api/efemerides"): Permiso.CONSULTAR,
    ("GET", "/api/efemerides/{efemeride_id}"): Permiso.CONSULTAR,
    ("POST", "/api/efemerides/{efemeride_id}/aprobacion"): Permiso.REVISAR_EFEMERIDES,
    ("PUT", "/api/efemerides/{efemeride_id}"): Permiso.REVISAR_EFEMERIDES,
    ("POST", "/api/efemerides/{efemeride_id}/descarte"): Permiso.REVISAR_EFEMERIDES,
    # Los Relatos, igual que las efemérides (064, R-05); proponerlo exige REVISAR_EFEMERIDES (R-42).
    ("GET", "/api/relatos"): Permiso.CONSULTAR,
    ("GET", "/api/relatos/{relato_id}"): Permiso.CONSULTAR,
    ("PUT", "/api/relatos/{relato_id}"): Permiso.REVISAR_EFEMERIDES,  # 065
    ("POST", "/api/relatos/{relato_id}/aprobacion"): Permiso.REVISAR_EFEMERIDES,
    ("POST", "/api/relatos/{relato_id}/descarte"): Permiso.REVISAR_EFEMERIDES,
    ("POST", "/api/conversaciones/{conversacion_id}/mensajes/{mensaje_id}/relato"):
        Permiso.REVISAR_EFEMERIDES,
    ("POST", "/api/admin/efemerides/generacion"): Permiso.OPERAR_SISTEMA,  # «Generar ahora»
    ("GET", "/api/admin/efemerides/programacion"): Permiso.OPERAR_SISTEMA,  # P7f (059)
    ("PUT", "/api/admin/efemerides/programacion"): Permiso.OPERAR_SISTEMA,  # P7f (060)
    ("POST", "/api/informes"): Permiso.GENERAR_INFORME,
    # Un Borrador exige además GENERAR_INFORME o APROBAR_INFORME (R-05): `test_informes`.
    ("GET", "/api/informes"): Permiso.CONSULTAR,
    ("GET", "/api/informes/{informe_id}"): Permiso.CONSULTAR,
    ("POST", "/api/informes/{informe_id}/aprobaciones"): Permiso.APROBAR_INFORME,
    ("POST", "/api/informes/{informe_id}/descarte"): Permiso.GENERAR_INFORME,
    # Exportan además solo GENERAR_INFORME o APROBAR_INFORME (R-23): `test_informes`.
    ("GET", "/api/informes/{informe_id}/exportacion"): Permiso.CONSULTAR,
    ("GET", "/api/admin/agentes"): Permiso.OPERAR_SISTEMA,
    ("POST", "/api/admin/agentes"): Permiso.OPERAR_SISTEMA,
    ("POST", "/api/admin/agentes/{agente_id}/tokens"): Permiso.OPERAR_SISTEMA,
    ("POST", "/api/admin/agentes/{agente_id}/tokens/{token_id}/revocacion"): Permiso.OPERAR_SISTEMA,
    ("GET", "/api/admin/modelos"): Permiso.OPERAR_SISTEMA,  # P7a (110)
    ("PUT", "/api/admin/modelos"): Permiso.OPERAR_SISTEMA,
    ("POST", "/api/admin/modelos/verificacion"): Permiso.OPERAR_SISTEMA,
    ("GET", "/api/admin/modelos/{capacidad}/historial"): Permiso.OPERAR_SISTEMA,
    ("GET", "/api/admin/personas"): Permiso.GESTIONAR_PERSONAS,
    ("POST", "/api/admin/personas"): Permiso.GESTIONAR_PERSONAS,
    ("PATCH", "/api/admin/personas/{usuario_id}"): Permiso.GESTIONAR_PERSONAS,
    ("POST", "/api/admin/piezas/{pieza_id}/retiro"): Permiso.RETIRAR_PIEZAS,
    ("POST", "/api/admin/piezas/{pieza_id}/restauracion"): Permiso.RETIRAR_PIEZAS,
    ("GET", "/api/admin/retiradas"): Permiso.RETIRAR_PIEZAS,
}
"""Qué exige cada ruta de `/api`, copiado de la tabla de endpoints del spec (DEC-124, R-06).
`None`: `/api/yo` pide solo sesión y `/api/salud` es pública (DEC-102)."""


def _permisos(dependencia: Dependant) -> set[Permiso]:
    propio = getattr(dependencia.call, "permiso", None)
    return {propio} if propio else set().union(*map(_permisos, dependencia.dependencies))


@pytest.mark.historia(146)
def test_seg_permisos_rutas(cliente):
    declarados = {}
    # Con los routers incluidos tal como los ve FastAPI, con las dependencias del router.
    for ruta in iter_route_contexts(cliente.app.routes):
        if ruta.path and ruta.path.startswith("/api/"):
            permisos = _permisos(ruta.dependant)
            assert len(permisos) <= 1, ruta.path
            for metodo in ruta.methods:
                declarados[(metodo, ruta.path)] = next(iter(permisos), None)

    assert declarados == MATRIZ
    # Una ruta nueva sin `exigir` no está en la matriz con `None`: falla arriba.
    assert {p for p in MATRIZ.values() if p} <= set(Permiso)
    assert len(Permiso) == 9


P = Permiso
DEC_124 = {
    MARIA: {P.CARGAR, P.VALIDAR, P.CONSULTAR, P.GESTIONAR_PERSONAS, P.RETIRAR_PIEZAS,
            P.REVISAR_EFEMERIDES, P.APROBAR_INFORME},
    JORGE: {P.CARGAR, P.VALIDAR, P.CONSULTAR, P.REVISAR_EFEMERIDES, P.GENERAR_INFORME,
            P.APROBAR_INFORME},
    DIRECTORA_DE_CARRERA: {P.CONSULTAR},
    ADMINISTRADOR_TECNICO: {P.CONSULTAR, P.OPERAR_SISTEMA},
}
"""La matriz del Director (DEC-124), copiada a mano: es contra lo que se prueba `dominio`."""


@pytest.mark.historia(146)
@pytest.mark.parametrize("persona", list(DEC_124), ids=["rgc", "coordinador", "director_carrera",
                                                         "admin_tecnico"])
def test_seg_rbac_matriz(cliente, persona):
    entrar(cliente, persona)
    rechazos = {}
    for (metodo, ruta), permiso in MATRIZ.items():
        if permiso is not None:
            respuesta = cliente.request(metodo, re.sub(r"\{[^}]+\}", str(uuid4()), ruta), json={})
            rechazos[(metodo, ruta)] = respuesta.status_code == 403

    assert rechazos == {
        clave: permiso not in DEC_124[persona]
        for clave, permiso in MATRIZ.items()
        if permiso is not None
    }


@pytest.mark.historia(280)
@pytest.mark.parametrize("interinato", [False, True], ids=["sin_interinato", "con_interinato"])
def test_seg_admin_no_firma(cliente, padron_restaurado, request, interinato):
    if interinato:
        request.getfixturevalue("sin_rgc")
    tecnico = entrar(cliente, ADMINISTRADOR_TECNICO)
    assert ("GESTIONAR_PERSONAS" in tecnico.get("/api/yo").json()["permisos"]) is interinato

    pieza = uuid4()
    firma = tecnico.put(f"/api/piezas/{pieza}/ficha", json=EDICION_COMPLETA)
    apertura = tecnico.post(f"/api/piezas/{pieza}/apertura")

    assert (firma.status_code, apertura.status_code) == (403, 403)
    assert firma.json()["type"] == "/errores/permiso-insuficiente"
