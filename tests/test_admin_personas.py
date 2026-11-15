"""La administración de personas desde la pantalla (spec, tabla de endpoints
`/api/admin/personas`, P7b; RF-27, CU-16, DEC-113, DEC-124, R-01, R-02, R-03).

Las reglas viven en `identidad.Padron` y se prueban en `test_identidad` (032); acá se prueba
que la API las expone. Después del primer RGC, `admin_tecnico` se otorga solo por el comando
`alta-persona`: por HTTP lo otorga únicamente el técnico interino (decisión del ticket 111).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from aplicacion import CABECERAS, app_sin_trabajador, entrar
from historia_viva.auditoria import Accion, Actor, RegistroDeAuditoria, TipoDeActor
from historia_viva.dominio.permisos import Rol
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, JORGE, MARIA

pytestmark = [
    pytest.mark.rf("RF-27"),
    pytest.mark.confirma("la app confirma altas, cambios de rol y bajas desde su pool"),
]

PERSONAS = "/api/admin/personas"
OPERADOR = Actor(TipoDeActor.CLI, "operador")


@pytest.fixture
def app(url_app, capa_modelos, directorio_originales, padron_restaurado):
    return app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
    )


def _como(app, persona: str) -> TestClient:
    return entrar(TestClient(app, headers=CABECERAS), persona)


def _alta(cliente: TestClient, email: str, rol: str = "coordinador", nombre: str = "Lucía Díaz"):
    return cliente.post(PERSONAS, json={"email": email, "nombre": nombre, "rol": rol})


@pytest.mark.historia(148)
def test_el_rgc_da_de_alta_y_la_persona_queda_habilitada_sin_haber_ingresado(app):
    rgc = _como(app, MARIA)

    alta = _alta(rgc, " Lucia@FIE.undef.edu.ar")
    lista = rgc.get(PERSONAS).json()["personas"]

    assert alta.status_code == 201, alta.text
    persona = alta.json()
    assert {k: v for k, v in persona.items() if k != "usuario_id"} == {
        "email": "lucia@fie.undef.edu.ar",
        "nombre": "Lucía Díaz",
        "rol": "coordinador",
        "activo": True,
        "ultimo_acceso_el": None,
        "vinculada": False,
    }
    assert persona in lista
    assert {p["usuario_id"] for p in lista} >= {MARIA, JORGE, DIRECTORA_DE_CARRERA,
                                                ADMINISTRADOR_TECNICO}


@pytest.mark.historia(148)
def test_alta_repetida_409_y_otro_dominio_422_en_texto_llano(app):
    rgc = _como(app, MARIA)

    repetida = _alta(rgc, "mrossi@fie.undef.edu.ar")
    ajena = _alta(rgc, "alguien@gmail.com")

    assert (repetida.status_code, repetida.json()["type"]) == (409, "/errores/persona-existente")
    assert repetida.json()["detail"] == "Ya hay una persona con ese email."
    assert (ajena.status_code, ajena.json()["type"]) == (422, "/errores/dominio-no-permitido")
    assert ajena.json()["campo"] == "email"


@pytest.mark.historia(147)
def test_r02_el_rgc_no_otorga_ni_quita_admin_tecnico(app):
    rgc = _como(app, MARIA)

    otorgar = _alta(rgc, "tecnico2@fie.undef.edu.ar", rol="admin_tecnico")
    ascender = rgc.patch(f"{PERSONAS}/{JORGE}", json={"rol": "admin_tecnico"})
    quitar = rgc.patch(f"{PERSONAS}/{ADMINISTRADOR_TECNICO}", json={"rol": "coordinador"})
    bajar = rgc.patch(f"{PERSONAS}/{ADMINISTRADOR_TECNICO}", json={"activo": False})

    assert [r.status_code for r in (otorgar, ascender, quitar, bajar)] == [403] * 4
    assert {r.json()["type"] for r in (otorgar, ascender, quitar, bajar)} == {
        "/errores/permiso-insuficiente"
    }
    roles = {p["usuario_id"]: (p["rol"], p["activo"]) for p in rgc.get(PERSONAS).json()["personas"]}
    assert roles[JORGE] == ("coordinador", True)
    assert roles[ADMINISTRADOR_TECNICO] == ("admin_tecnico", True)
    assert "tecnico2@fie.undef.edu.ar" not in rgc.get(PERSONAS).text


@pytest.mark.historia(147)
def test_r02_el_rgc_no_reactiva_a_un_tecnico_de_baja(app):
    tecnico_de_baja = _por_comando(app, "tecnico2@fie.undef.edu.ar", Rol.ADMIN_TECNICO, baja=True)
    rgc = _como(app, MARIA)

    reactivar = rgc.patch(f"{PERSONAS}/{tecnico_de_baja}", json={"activo": True})
    degradar_y_reactivar = rgc.patch(
        f"{PERSONAS}/{tecnico_de_baja}", json={"rol": "coordinador", "activo": True}
    )

    assert [r.status_code for r in (reactivar, degradar_y_reactivar)] == [403, 403]
    [persona] = rgc.get(PERSONAS, params={"activo": "false"}).json()["personas"]
    assert (persona["rol"], persona["activo"]) == ("admin_tecnico", False)


def _por_comando(app, email: str, rol: Rol, *, baja: bool = False) -> str:
    """Lo que hace `alta-persona`, que puede todo: así existe un técnico de baja."""
    padron = app.state.padron
    with app.state.bd.transaccion() as tx:
        persona = padron.alta(tx, email, "Por comando", rol, por=OPERADOR)
        if baja:
            padron.desactivar(tx, persona.usuario_id, por=OPERADOR)
    return str(persona.usuario_id)


@pytest.mark.historia(279)
def test_el_rgc_asigna_desde_la_pantalla_los_roles_que_le_tocan_y_cada_uno_rige_en_la_sesion(app):
    rgc = _como(app, MARIA)
    persona = _alta(rgc, "lucia@fie.undef.edu.ar").json()["usuario_id"]
    lucia = _como(app, persona)

    roles = []
    for rol in ("rgc", "director_carrera", "coordinador"):
        cambio = rgc.patch(f"{PERSONAS}/{persona}", json={"rol": rol})
        assert (cambio.status_code, cambio.json()["rol"]) == (200, rol), cambio.text
        roles.append(lucia.get("/api/yo").json()["rol"])
    # El cuarto, Administrador técnico, no lo otorga el RGC por la pantalla (R-02).
    cuarto = rgc.patch(f"{PERSONAS}/{persona}", json={"rol": "admin_tecnico"})

    assert roles == ["rgc", "director_carrera", "coordinador"]
    assert (cuarto.status_code, cuarto.json()["type"]) == (403, "/errores/permiso-insuficiente")


@pytest.mark.historia(149)
def test_un_patch_sin_cambio_real_no_audita(app):
    rgc = _como(app, MARIA)
    lucia = _alta(rgc, "lucia@fie.undef.edu.ar").json()["usuario_id"]
    registro = RegistroDeAuditoria(bd=app.state.bd)

    def cuantos():
        return [len(registro.listar(accion=a)) for a in
                (Accion.PERSONA_ROL, Accion.PERSONA_BAJA, Accion.PERSONA_REACTIVACION)]

    sin_cambio = [rgc.patch(f"{PERSONAS}/{lucia}", json=c).status_code
                  for c in ({"activo": True}, {"rol": "coordinador"})]
    antes_de_la_baja = cuantos()
    rgc.patch(f"{PERSONAS}/{lucia}", json={"activo": False})
    otra_baja = rgc.patch(f"{PERSONAS}/{lucia}", json={"activo": False})

    assert sin_cambio == [200, 200]
    assert antes_de_la_baja == [0, 0, 0]
    assert (otra_baja.status_code, otra_baja.json()["activo"]) == (200, False)
    assert cuantos() == [0, 1, 0]


@pytest.mark.historia(149)
def test_cambio_de_rol_y_baja_que_revoca_las_sesiones(app):
    rgc = _como(app, MARIA)
    nueva = _alta(rgc, "lucia@fie.undef.edu.ar").json()
    lucia = _como(app, nueva["usuario_id"])
    assert lucia.get("/api/yo").json()["rol"] == "coordinador"

    cambio = rgc.patch(f"{PERSONAS}/{nueva['usuario_id']}", json={"rol": "director_carrera"})
    con_el_rol_nuevo = lucia.get("/api/yo").json()["rol"]
    baja = rgc.patch(f"{PERSONAS}/{nueva['usuario_id']}", json={"activo": False})
    despues = lucia.get("/api/yo")

    assert (cambio.status_code, cambio.json()["rol"]) == (200, "director_carrera")
    assert con_el_rol_nuevo == "director_carrera"
    assert (baja.status_code, baja.json()["activo"]) == (200, False)
    assert despues.status_code == 401
    assert [p["email"] for p in rgc.get(PERSONAS, params={"activo": "false"}).json()["personas"]] == [
        "lucia@fie.undef.edu.ar"
    ]


@pytest.mark.historia(149)
@pytest.mark.parametrize(
    ("cambio", "estado", "tipo"),
    [
        ({}, 422, "/errores/pedido-invalido"),
        ({"rol": "director"}, 422, "/errores/pedido-invalido"),
        ({"activo": False}, 404, "/errores/recurso-no-encontrado"),
    ],
    ids=["sin_cambio", "rol_inexistente", "persona_inexistente"],
)
def test_patch_rechaza_un_pedido_sin_cambio_o_sobre_nadie(app, cambio, estado, tipo):
    respuesta = _como(app, MARIA).patch(f"{PERSONAS}/{uuid4()}", json=cambio)

    assert (respuesta.status_code, respuesta.json()["type"]) == (estado, tipo)


def _esperar_esperando(candado, cuantas):
    """Hasta que `cuantas` transacciones esperan detrás del candado, directa o indirectamente."""
    consulta = (
        "WITH RECURSIVE esperando(pid) AS ("
        " SELECT pid FROM pg_stat_activity WHERE %(candado)s = ANY(pg_blocking_pids(pid))"
        " UNION SELECT a.pid FROM pg_stat_activity a JOIN esperando e"
        " ON e.pid = ANY(pg_blocking_pids(a.pid)))"
        " SELECT count(*) FROM esperando"
    )
    limite = time.monotonic() + 10
    while candado.execute(consulta, {"candado": candado.info.backend_pid}).fetchone()[0] < cuantas:
        assert time.monotonic() < limite, "Las bajas no llegaron a esperar el candado."
        time.sleep(0.05)
        # El candado sigue en su transacción, que congela `pg_stat_activity` en la primera lectura.
        candado.execute("SELECT pg_stat_clear_snapshot()")


@pytest.mark.historia(150)
@pytest.mark.parametrize("cambio", [{"activo": False}, {"rol": "coordinador"}],
                         ids=["baja", "degradacion"])
def test_r03_dos_cambios_cruzados_a_la_vez_el_segundo_da_409_con_el_permiso(
    app, sin_rgc, url_bd, cambio
):
    # Nadie se cambia a sí mismo: por HTTP, el último titular solo se alcanza con dos cambios a
    # la vez. Dos técnicos interinos se dan de baja, o se degradan, uno al otro.
    tecnico = _como(app, ADMINISTRADOR_TECNICO)
    otro = _alta(tecnico, "tecnico2@fie.undef.edu.ar", rol="admin_tecnico").json()
    segundo = _como(app, otro["usuario_id"])

    with psycopg.connect(url_bd) as candado:
        candado.execute("SELECT 1 FROM acervo.usuario WHERE activo FOR UPDATE")
        with ThreadPoolExecutor(2) as hilos:
            bajas = [
                hilos.submit(tecnico.patch, f"{PERSONAS}/{otro['usuario_id']}", json=cambio),
                hilos.submit(segundo.patch, f"{PERSONAS}/{ADMINISTRADOR_TECNICO}", json=cambio),
            ]
            _esperar_esperando(candado, 2)
            candado.commit()
            respuestas = [baja.result() for baja in bajas]

    assert sorted(r.status_code for r in respuestas) == [200, 409]
    [rechazo] = [r.json() for r in respuestas if r.status_code == 409]
    assert rechazo["type"] == "/errores/ultimo-titular-de-permiso"
    assert rechazo["title"] == "No se puede dejar el permiso sin titular"
    assert rechazo["permiso"] == "GESTIONAR_PERSONAS"


def _eventos(cliente: TestClient, accion: str) -> dict[str, dict]:
    eventos = cliente.get("/api/admin/auditoria", params={"accion": accion}).json()["eventos"]
    return {e["objeto_id"]: e["detalle"]["despues"] for e in eventos}


@pytest.mark.historia(281)
def test_interinato_el_tecnico_gestiona_con_la_marca_interino_hasta_el_primer_rgc(app, sin_rgc):
    tecnico = _como(app, ADMINISTRADOR_TECNICO)

    coordinadora = _alta(tecnico, "lucia@fie.undef.edu.ar").json()
    cambio = tecnico.patch(f"{PERSONAS}/{coordinadora['usuario_id']}", json={"rol": "rgc"})
    # Antes del RGC, el técnico interino también otorga `admin_tecnico` por HTTP (R-02).
    otro_tecnico = _alta(tecnico, "tecnico2@fie.undef.edu.ar", rol="admin_tecnico")
    despues_del_rgc = tecnico.get(PERSONAS)
    altas = _eventos(tecnico, "persona.alta")
    roles = _eventos(tecnico, "persona.rol")

    assert cambio.status_code == 200
    assert roles[coordinadora["usuario_id"]] == {
        "rol": "rgc", "interino": True, "interinato_terminado": True
    }
    assert altas[coordinadora["usuario_id"]]["interino"] is True
    # El ascenso al primer RGC terminó el interinato: el pedido siguiente ya no puede.
    assert otro_tecnico.status_code == 403
    assert despues_del_rgc.status_code == 403


@pytest.mark.historia(281)
def test_el_alta_del_primer_rgc_por_http_termina_el_interinato(app, sin_rgc):
    tecnico = _como(app, ADMINISTRADOR_TECNICO)

    otro_tecnico = _alta(tecnico, "tecnico2@fie.undef.edu.ar", rol="admin_tecnico").json()
    rgc = _alta(tecnico, "rgc@fie.undef.edu.ar", rol="rgc").json()
    siguiente = tecnico.get(PERSONAS)
    altas = _eventos(tecnico, "persona.alta")

    assert (otro_tecnico["rol"], rgc["rol"]) == ("admin_tecnico", "rgc")
    assert altas[otro_tecnico["usuario_id"]]["interino"] is True
    assert altas[rgc["usuario_id"]]["interinato_terminado"] is True
    assert altas[rgc["usuario_id"]]["interino"] is True
    assert siguiente.status_code == 403
    # Ya con RGC, nadie otorga `admin_tecnico` por HTTP: la RGC no puede (R-02) y el técnico ya
    # no gestiona personas; queda el comando `alta-persona`.
    assert _alta(_como(app, rgc["usuario_id"]), "t3@fie.undef.edu.ar",
                 rol="admin_tecnico").status_code == 403


@pytest.mark.historia(148)
def test_el_primer_ingreso_deja_el_ultimo_acceso(app):
    rgc = _como(app, MARIA)
    nueva = _alta(rgc, "lucia@fie.undef.edu.ar").json()

    # `/auth/dev` atiende solo pedidos hechos en esta PC.
    ingreso = TestClient(app, headers=CABECERAS, client=("127.0.0.1", 50000)).post(
        "/auth/dev", json={"email": "lucia@fie.undef.edu.ar"}
    )
    [persona] = [p for p in rgc.get(PERSONAS, params={"rol": "coordinador"}).json()["personas"]
                 if p["usuario_id"] == nueva["usuario_id"]]

    assert ingreso.status_code < 400, ingreso.text
    assert nueva["ultimo_acceso_el"] is None
    assert persona["ultimo_acceso_el"] is not None
    assert persona["vinculada"] is False  # en `dev` no hay `sub` de Google
