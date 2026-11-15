"""Controles de datos personales (125; historia 243, RF-42, R-DP2; auditoría, decisiones 15 y 16).

Tres piezas, las tres apagadas hasta C31 (`HV_DATOS_PERSONALES_HABILITADOS`): el aviso de DNI,
CUIL o CBU al validar (función pura de `dominio` que consulta `validacion`; avisa, no bloquea, no
se guarda), `publicable` desde Validar (solo el RGC, con una versión nueva que reindexa) y la
supresión efectiva con `hv_migrador` (runbook R14), que no es el retiro. HTTP y comando contra
Postgres real; el único doble es la capa.
"""

from __future__ import annotations

import ast
import json
import warnings
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from aplicacion import en_marcha, entrar
from historia_viva.auditoria import Accion, RegistroDeAuditoria
from historia_viva.composicion import Entorno, EntornoInvalido, main
from historia_viva.dominio.datos_personales import DatoPersonal, detectar_datos_personales
from historia_viva.dominio.permisos import Rol, puede_marcar_publicable
from historia_viva.procesamiento.cola import TipoDeTrabajo
from test_api_agentes import (  # noqa: F401 — fixtures de /v1 y de lo que cita
    agente,
    efemeride,
    emitir,
    encender,
    relato,
)
from test_busqueda import (  # noqa: F401 — fixtures de Piezas Validadas e indexadas
    LABORATORIO,
    bd,
    buscar,
    ids,
    indexacion,
    trabajador,
    validada,
)
from test_efemerides import sembrar  # noqa: F401 — Piezas Validadas sembradas
from validacion import EDICION_COMPLETA, JORGE, MARIA

pytestmark = [pytest.mark.rf("RF-42"), pytest.mark.historia(243)]

TESTS = Path(__file__).resolve().parent
MOTIVO = "Pedido de supresión, nota SEU 14/2026."
CON_DNI = (
    "Acta de la sesión del Consejo. Asistió el señor Pérez, DNI 12.345.678,"
    " con CUIL 20-12345678-6, y cobró por la cuenta 2850590940090418135201."
)


# ── El aviso: función pura de `dominio` ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("texto", "esperados"),
    [
        ("Firmó el DNI 30111222 al pie.", [(DatoPersonal.DNI, "30111222")]),
        ("D.N.I. N° 7.654.321", [(DatoPersonal.DNI, "7.654.321")]),
        ("Asistió Juan (12.345.678).", [(DatoPersonal.DNI, "12.345.678")]),
        ("CUIL 20-12345678-6", [(DatoPersonal.CUIL, "20-12345678-6")]),
        ("cuit 20123456786", [(DatoPersonal.CUIL, "20123456786")]),
        ("CBU 2850590940090418135201", [(DatoPersonal.CBU, "2850590940090418135201")]),
        # Con el dígito verificador mal no es un CUIL ni un CBU: se calla.
        ("CUIL 20-12345678-7", []),
        ("CBU 2850590940090418135202", []),
        # Un importe con puntos, una fecha, un número suelto o un hash no avisan.
        ("Se pagaron $ 12.345.678 de obra.", []),
        ("El 20240315 se firmó el legajo 12345678.", []),
        ("sha256 ab20123456786cd0285059094009041813520199", []),
    ],
)
def test_el_detector_avisa_dni_cuil_y_cbu_y_calla_lo_que_no_lo_es(texto, esperados):
    hallados = [(h.tipo, texto[h.inicio : h.fin]) for h in detectar_datos_personales(texto)]

    assert hallados == esperados


def test_el_corpus_de_prueba_no_da_falsos_positivos():
    """El corpus de prueba es todo texto de la suite y del código (cuerpos, transcripciones de la
    capa falsa, semilla): ninguno lleva un DNI, un CUIL ni un CBU, así que el detector no avisa en
    ninguno. Este archivo no cuenta: sus ejemplos son los positivos."""
    rutas = [*TESTS.glob("*.py"), *(TESTS.parent / "src").rglob("*.py")]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # escapes viejos de otros archivos
        arboles = [
            ast.parse(ruta.read_text(encoding="utf-8"))
            for ruta in rutas
            if ruta.name not in ("test_datos_personales.py", "datos_personales.py")
        ]
    textos = [
        nodo.value
        for arbol in arboles
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str)
    ]
    semilla = TESTS.parent / "src" / "historia_viva" / "migraciones" / "semilla_desarrollo.sql"
    textos.append(semilla.read_text(encoding="utf-8"))

    assert len(textos) > 10_000
    assert [t for t in textos if detectar_datos_personales(t)] == []


def test_los_controles_arrancan_apagados_y_otro_valor_no_arranca():
    assert Entorno.desde_variables({}).datos_personales_habilitados is False
    assert Entorno.desde_variables(
        {"HV_DATOS_PERSONALES_HABILITADOS": "true"}
    ).datos_personales_habilitados is True
    with pytest.raises(EntornoInvalido, match="HV_DATOS_PERSONALES_HABILITADOS"):
        Entorno.desde_variables({"HV_DATOS_PERSONALES_HABILITADOS": "si"})


def test_publicable_lo_cambia_solo_el_rgc():
    assert [rol for rol in Rol if puede_marcar_publicable(rol)] == [Rol.RGC]


# ── En la pantalla partida ────────────────────────────────────────────────────────────────────


@pytest.fixture
def habilitados() -> bool:
    return True


@pytest.fixture
def cliente(url_app, capa_modelos, directorio_originales, habilitados):
    with en_marcha(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        datos_personales_habilitados=habilitados,
    ) as cliente:
        try:
            yield cliente
        finally:
            capa_modelos.liberar()


def ficha(cliente, pieza_id: str, persona: str = MARIA) -> dict[str, Any]:
    entrar(cliente, persona)
    respuesta = cliente.get(f"/api/piezas/{pieza_id}/ficha")
    assert respuesta.status_code == 200, respuesta.text
    cuerpo: dict[str, Any] = respuesta.json()
    return cuerpo


def guardar(cliente, pieza_id: str, persona: str = MARIA, **cambios: Any):
    entrar(cliente, persona)
    version = ficha(cliente, pieza_id, persona)["version"]
    return cliente.put(
        f"/api/piezas/{pieza_id}/ficha",
        json={**EDICION_COMPLETA, "cuerpo": LABORATORIO, "version_base": version, **cambios},
    )


def columna_publicable(url_bd: str, pieza_id: str) -> bool:
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT publicable FROM acervo.pieza WHERE pieza_id = %s", (pieza_id,)
        ).fetchone()
    assert fila is not None
    return bool(fila[0])


@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
@pytest.mark.parametrize("habilitados", [False])
def test_apagados_no_hay_aviso_ni_campo_y_publicable_da_409(cliente, validada, url_bd):
    pieza_id = validada(CON_DNI)
    leida = ficha(cliente, pieza_id)

    respuesta = guardar(cliente, pieza_id, cuerpo=CON_DNI, publicable=False)

    assert not {"datos_personales", "puede_marcar_publicable", "publicable"} & set(leida)
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["type"] == "/errores/datos-personales-deshabilitados"
    assert ficha(cliente, pieza_id)["version"] == leida["version"]
    assert columna_publicable(url_bd, pieza_id) is True
    guardada = guardar(cliente, pieza_id, cuerpo=CON_DNI)
    assert guardada.status_code == 200, guardada.text
    assert "datos_personales" not in guardada.json()


@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
def test_encendidos_avisan_al_validar_sin_bloquear_ni_guardar_el_aviso(cliente, validada, url_bd):
    pieza_id = validada(LABORATORIO)

    respuesta = guardar(cliente, pieza_id, cuerpo=CON_DNI)

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "Validada"  # avisa, no bloquea
    avisos = respuesta.json()["datos_personales"]
    assert [(a["campo"], a["tipo"]) for a in avisos] == [
        ("cuerpo", "dni"), ("cuerpo", "cuil"), ("cuerpo", "cbu")
    ]
    cuerpo = ficha(cliente, pieza_id)["cuerpo"]
    assert [cuerpo[a["inicio"] : a["fin"]] for a in ficha(cliente, pieza_id)["datos_personales"]] == [
        "12.345.678", "20-12345678-6", "2850590940090418135201"
    ]
    with psycopg.connect(url_bd) as conexion:
        columnas = conexion.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'acervo' AND (column_name ~ 'dni|cuil|cbu|datos_personales')"
        ).fetchall()
    assert columnas == []


@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
@pytest.mark.xdist_group("indice")
def test_el_rgc_marca_no_publicable_con_una_version_nueva_que_reindexa_y_sale_de_v1(
    cliente, validada, trabajador, agente, url_bd
):
    pieza_id = validada(LABORATORIO)
    antes = ficha(cliente, pieza_id)
    assert (antes["publicable"], antes["puede_marcar_publicable"]) == (True, True)
    assert ficha(cliente, pieza_id, JORGE)["puede_marcar_publicable"] is False

    respuesta = guardar(cliente, pieza_id, publicable=False)

    assert respuesta.status_code == 200, respuesta.text
    despues = ficha(cliente, pieza_id)
    assert despues["version"] == antes["version"] + 1
    assert (despues["frontmatter"]["publicable"], despues["publicable"]) == (False, False)
    assert columna_publicable(url_bd, pieza_id) is False
    with psycopg.connect(url_bd) as conexion:
        (encolados,) = conexion.execute(
            "SELECT count(*) FROM acervo.trabajo_procesamiento WHERE pieza_id = %s"
            " AND tipo = %s AND estado = 'encolado'",
            (pieza_id, TipoDeTrabajo.INDEXACION.value),
        ).fetchone() or (0,)
    assert encolados == 1
    assert trabajador.procesar_siguiente() is True
    v1 = cliente.get("/v1/fragmentos", params={"desde": "1900-01-01"}, headers=agente)
    assert v1.status_code == 200, v1.text
    assert v1.json()["fragmentos"] == []
    assert ids(buscar(cliente, q="laboratorio de ensayos de materiales")) == [pieza_id]
    # El Coordinador valida pero no decide qué sale al Agente 1; si edita, queda como estaba.
    assert guardar(cliente, pieza_id, JORGE, publicable=True).status_code == 403
    assert guardar(cliente, pieza_id, JORGE, resumen="Otro resumen.").status_code == 200
    assert columna_publicable(url_bd, pieza_id) is False


# ── La supresión efectiva (runbook R14) ───────────────────────────────────────────────────────


def filas_de_la_pieza(url_bd: str, pieza_id: str) -> dict[str, int]:
    """Cada tabla de `acervo` con columna `pieza_id`, y los fragmentos del índice."""
    with psycopg.connect(url_bd) as conexion:
        tablas = [
            tabla
            for (tabla,) in conexion.execute(
                "SELECT table_name FROM information_schema.columns WHERE table_schema = 'acervo'"
                " AND column_name = 'pieza_id' AND table_name IN (SELECT table_name"
                " FROM information_schema.tables WHERE table_type = 'BASE TABLE')"
            ).fetchall()
        ]
        consultas: dict[str, sql.SQL | sql.Composed] = {
            tabla: sql.SQL("SELECT count(*) FROM acervo.{} WHERE pieza_id = %s").format(
                sql.Identifier(tabla)
            )
            for tabla in tablas
        }
        consultas["data_fragmento"] = sql.SQL(
            "SELECT count(*) FROM acervo.data_fragmento WHERE metadata_->>'pieza_id' = %s"
        )
        cuentas = {
            tabla: (conexion.execute(consulta, (pieza_id,)).fetchone() or (0,))[0]
            for tabla, consulta in consultas.items()
        }
    return {tabla: n for tabla, n in cuentas.items() if n}


def bytes_del_original(directorio: Path, pieza_id: str) -> int:
    return sum(r.stat().st_size for r in directorio.rglob("*") if r.is_file() and pieza_id in r.name)


@pytest.fixture
def suprimir(monkeypatch, url_bd, directorio_originales, capsys):
    """`suprimir` por comando, como en el runbook R14: `hv_migrador` y los originales de la app."""
    monkeypatch.setenv("HV_BD_URL_MIGRADOR", url_bd)
    monkeypatch.setenv("HV_ORIGINALES", str(directorio_originales))
    monkeypatch.setenv("HV_DATOS_PERSONALES_HABILITADOS", "true")

    def correr(*argumentos: str) -> tuple[int, str, str]:
        codigo = main(["suprimir", *argumentos])
        salida = capsys.readouterr()
        return codigo, salida.out, salida.err

    return correr


@pytest.mark.confirma("la app, el trabajador y el comando confirman desde sus propios pools")
@pytest.mark.xdist_group("indice")
def test_la_supresion_borra_pieza_fichas_fragmentos_y_original_y_registra_sin_contenido(
    cliente, validada, trabajador, suprimir, url_bd, url_app, directorio_originales
):
    pieza_id = validada(CON_DNI)
    assert guardar(cliente, pieza_id, cuerpo=CON_DNI, resumen="Otra versión.").status_code == 200
    assert trabajador.procesar_siguiente() is True  # la reindexación de la versión nueva
    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "INSERT INTO acervo.replicacion_drive (pieza_id, estado, drive_file_id)"
            " VALUES (%s, 'replicado', 'archivo-en-drive')",
            (pieza_id,),
        )
        (huella,) = conexion.execute(
            "SELECT huella_original FROM acervo.pieza WHERE pieza_id = %s", (pieza_id,)
        ).fetchone() or (None,)
    antes = filas_de_la_pieza(url_bd, pieza_id)
    assert antes["ficha"] >= 3 and antes["data_fragmento"] > 0
    assert bytes_del_original(directorio_originales, pieza_id) > 0
    # El derivado que deja la conversión (el JPEG de un HEIC, 153) se va con el original.
    derivado = directorio_originales / ".convertidos" / pieza_id
    derivado.parent.mkdir()
    derivado.write_bytes(b"\xff\xd8\xff jpeg convertido")
    with psycopg.connect(url_app) as conexion, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conexion.execute("SELECT acervo.suprimir_pieza(%s)", (pieza_id,))

    previa = suprimir(pieza_id, "--motivo", MOTIVO, "--previa")
    assert previa[0] == 0, previa
    assert "no se borró nada" in previa[1]
    assert filas_de_la_pieza(url_bd, pieza_id) == antes
    con_dni = suprimir(pieza_id, "--motivo", "Lo pidió el titular, DNI 30111222.")
    assert con_dni[0] == 2 and "DNI" in con_dni[2]

    codigo, salida, error = suprimir(pieza_id, "--motivo", MOTIVO)

    assert (codigo, error) == (0, ""), salida
    assert filas_de_la_pieza(url_bd, pieza_id) == {}
    assert bytes_del_original(directorio_originales, pieza_id) == 0
    assert not derivado.exists()
    assert "archivo-en-drive" in salida and "Juan" in salida
    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.PIEZA_SUPRESION)
    assert (evento.objeto.id, evento.motivo, evento.actor.tipo) == (pieza_id, MOTIVO, "cli")
    assert evento.despues is not None and evento.despues["huella_original"] == huella
    assert set(evento.despues) == {"huella_original", "via_ingreso", "filas"}
    registro = json.dumps(evento.despues, ensure_ascii=False)
    assert [t for t in ("pieza.pdf", "Pérez", "12.345.678", "Consejo") if t in registro] == []
    assert suprimir(pieza_id, "--motivo", MOTIVO)[0] == 1  # ya no existe


@pytest.mark.confirma("las Piezas y lo que las cita se siembran confirmados")
def test_lo_que_citaba_la_pieza_pierde_la_cita_y_queda_con_fuente_retirada(
    sembrar, efemeride, relato, suprimir, url_bd, directorio_originales
):
    pieza, otra = sembrar("1975-05-03"), sembrar("1980-05-03")
    efemeride_id = efemeride("2026-05-03", [pieza, otra])
    relato_id = relato([(1, pieza)])
    # Sembrada sin original: se le pone uno en disco, donde lo deja `originales`.
    directorio_originales.mkdir(parents=True, exist_ok=True)
    (directorio_originales / pieza).write_bytes(b"%PDF-1.4 acta")
    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET archivo_original_id = %s WHERE pieza_id = %s",
            (f"local://originales/{pieza}", pieza),
        )

    codigo, salida, error = suprimir(pieza, "--motivo", MOTIVO)

    assert (codigo, error) == (0, "")
    assert "Original borrado." in salida
    assert bytes_del_original(directorio_originales, pieza) == 0
    assert filas_de_la_pieza(url_bd, pieza) == {}
    assert filas_de_la_pieza(url_bd, otra) == {"pieza": 1, "ficha": 1, "efemeride_fuente": 1}
    with psycopg.connect(url_bd) as conexion:
        marcas = conexion.execute(
            "SELECT (SELECT fuente_retirada_el IS NOT NULL FROM acervo.efemeride"
            " WHERE efemeride_id = %s), (SELECT fuente_retirada_el IS NOT NULL FROM acervo.relato"
            " WHERE relato_id = %s)",
            (efemeride_id, relato_id),
        ).fetchone()
    assert marcas == (True, True)


@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
@pytest.mark.parametrize("habilitados", [False])
def test_apagada_la_supresion_no_borra_nada(cliente, validada, suprimir, monkeypatch, url_bd):
    pieza_id = validada(LABORATORIO)
    antes = filas_de_la_pieza(url_bd, pieza_id)
    monkeypatch.setenv("HV_DATOS_PERSONALES_HABILITADOS", "")

    codigo, _, error = suprimir(pieza_id, "--motivo", MOTIVO)

    assert codigo == 2 and "C31" in error
    assert filas_de_la_pieza(url_bd, pieza_id) == antes
    assert suprimir(str(uuid4()), "--motivo", MOTIVO)[0] == 2


@pytest.mark.rf("RF-29")
@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
def test_un_retiro_no_suprime(cliente, validada, url_bd, directorio_originales):
    pieza_id = validada(LABORATORIO)
    antes = filas_de_la_pieza(url_bd, pieza_id)
    entrar(cliente, MARIA)

    respuesta = cliente.post(f"/api/admin/piezas/{pieza_id}/retiro", json={"motivo": MOTIVO})

    assert respuesta.status_code == 200, respuesta.text
    despues = filas_de_la_pieza(url_bd, pieza_id)
    assert despues["pieza"] == 1 and despues["ficha"] == antes["ficha"]
    assert bytes_del_original(directorio_originales, pieza_id) > 0
