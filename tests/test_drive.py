"""La réplica de originales a Drive contra su doble (124; `07-drive.md` §3.8, DRV-04 a DRV-12).

El `Replicador` corre sobre la base real y el pool de la app; lo único sustituido es Drive, por
`DriveFalso` (un transporte de `httpx`): la suite no sale a la red ni a Google (historia 211).
La prueba contra la unidad real (`drive_real`) se saltea sin clave: la activa 126.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from aplicacion import entrar
from drive_falso import DriveFalso, clave_de_prueba
from espera import esperar_estado
from historia_viva import drive
from historia_viva.composicion import Entorno, EntornoInvalido, componer_replicador, main
from historia_viva.drive import (
    ClienteDrive,
    ConfiguracionDrive,
    Replicador,
    leer_clave,
)
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.originales import AlmacenDeOriginales
from test_busqueda import (  # noqa: F401 — fixtures de Piezas Validadas e indexadas
    LABORATORIO,
    bd,
    indexacion,
    trabajador,
    validada,
)
from test_conversacion import nueva, turno
from validacion import ADMINISTRADOR_TECNICO, MARIA, PDF

pytestmark = [
    pytest.mark.historia(240),
    pytest.mark.confirma("el replicador y la app confirman desde su pool"),
]


@pytest.fixture
def doble() -> DriveFalso:
    return DriveFalso()


@pytest.fixture
def replicador(cliente, doble, directorio_originales) -> Replicador:
    bd_ = cliente.app.state.bd
    return Replicador(
        bd=bd_,
        fichas=ModuloFichasPostgres(bd=bd_),
        originales=AlmacenDeOriginales(directorio_originales),
        cliente=doble.cliente(),
    )


def subir(cliente, contenido: bytes | None = None) -> str:
    entrar(cliente, MARIA)
    archivo = contenido or PDF + f"%{uuid4()}\n".encode()
    respuesta = cliente.post(
        "/api/piezas", files={"archivo": ("acta.pdf", archivo, "application/pdf")}
    )
    assert respuesta.status_code == 201, respuesta.text
    pieza_id: str = respuesta.json()["pieza_id"]
    return pieza_id


def fila(conexion: psycopg.Connection[Any], pieza_id: str) -> dict[str, Any]:
    resultado = conexion.cursor(row_factory=dict_row).execute(
        "SELECT *, proximo_intento - now() AS espera FROM acervo.replicacion_drive"
        " WHERE pieza_id = %s",
        (pieza_id,),
    ).fetchone()
    assert resultado is not None
    return resultado


def editar(conexion: psycopg.Connection[Any], pieza_id: str, cambio: str) -> None:
    """Como el Administrador técnico (o el reloj): una edición confirmada de la fila."""
    conexion.execute(
        f"UPDATE acervo.replicacion_drive SET {cambio} WHERE pieza_id = %s",  # type: ignore[arg-type]
        (pieza_id,),
    )


def replicar_todo(replicador: Replicador) -> int:
    vueltas = 0
    while replicador.ejecutar_una_vez():
        vueltas += 1
    return vueltas


def test_replicar_sube_el_original_con_sus_propiedades_y_no_cambia_la_pieza(
    cliente, pieza_pendiente, replicador, doble, _conexion_del_dueno
):
    assert replicador.ejecutar_una_vez() is True

    replicada = fila(_conexion_del_dueno, pieza_pendiente)
    assert replicada["estado"] == "replicado"
    assert (replicada["intentos"], replicada["ultimo_error"]) == (1, None)
    assert replicada["replicado_el"] is not None and replicada["arrendado_hasta"] is None
    [(file_id, archivo)] = doble.archivos.items()
    assert replicada["drive_file_id"] == file_id
    assert archivo["bytes"] == PDF
    assert archivo["name"] == f"{pieza_pendiente}-acta-fundacion.pdf"
    assert archivo["parents"] == ["carpeta-1"]
    assert archivo["appProperties"] == {
        "pieza_id": pieza_pendiente,
        "sha256": hashlib.sha256(PDF).hexdigest(),
    }
    # DRV-05, DRV-14: la Pieza sigue igual y el original sigue en disco.
    assert esperar_estado(cliente, pieza_pendiente, "Pendiente")
    original = cliente.get(f"/api/piezas/{pieza_pendiente}/original")
    assert (original.status_code, original.content) == (200, PDF)


def test_encender_la_replica_copia_el_atraso_sin_comando(
    cliente, replicador, doble, _conexion_del_dueno
):
    piezas = {subir(cliente) for _ in range(3)}

    assert replicar_todo(replicador) == 3

    assert {fila(_conexion_del_dueno, p)["estado"] for p in piezas} == {"replicado"}
    assert {a["appProperties"]["pieza_id"] for a in doble.archivos.values()} == piezas
    assert replicador.ejecutar_una_vez() is False


def test_un_error_transitorio_vuelve_a_pendiente_con_espera_creciente(
    cliente, replicador, doble, _conexion_del_dueno
):
    pieza = subir(cliente)
    doble.responder = [503]

    assert replicador.ejecutar_una_vez() is True
    primera = fila(_conexion_del_dueno, pieza)
    assert (primera["estado"], primera["intentos"]) == ("pendiente", 1)
    assert primera["ultimo_error"] == "Drive respondió 503."
    assert 110 < primera["espera"].total_seconds() <= 120  # 2^1 min
    # Antes de su turno no se toma.
    assert replicador.ejecutar_una_vez() is False

    editar(_conexion_del_dueno, pieza, "proximo_intento = now()")
    doble.caido = True
    assert replicador.ejecutar_una_vez() is True
    segunda = fila(_conexion_del_dueno, pieza)
    assert (segunda["estado"], segunda["intentos"]) == ("pendiente", 2)
    assert 230 < segunda["espera"].total_seconds() <= 240  # 2^2 min
    assert segunda["ultimo_error"] == "Drive no respondió (ConnectError)."

    editar(_conexion_del_dueno, pieza, "proximo_intento = now()")
    doble.caido = False
    assert replicador.ejecutar_una_vez() is True
    assert fila(_conexion_del_dueno, pieza)["estado"] == "replicado"


@pytest.mark.rnf("RNF-25")
def test_ocho_intentos_dejan_fallido_visible_en_la_salud(
    cliente, replicador, doble, _conexion_del_dueno
):
    pieza = subir(cliente)
    replicador.sincronizar()
    editar(_conexion_del_dueno, pieza, "intentos = 7")
    doble.responder = [503]

    assert replicador.ejecutar_una_vez() is True

    fallida = fila(_conexion_del_dueno, pieza)
    assert (fallida["estado"], fallida["intentos"]) == ("fallido", 8)
    # Un `fallido` no se retoma solo: lo devuelve a `pendiente` el Administrador técnico.
    assert replicador.ejecutar_una_vez() is False
    salud = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud").json()
    assert salud["drive"] == {"pendiente": 0, "subiendo": 0, "replicado": 0, "fallido": 1}


@pytest.mark.parametrize("codigo", [400, 403, 404])
def test_un_error_permanente_deja_fallido_en_el_primer_intento(
    codigo, cliente, replicador, doble, _conexion_del_dueno
):
    pieza = subir(cliente)
    doble.responder = [codigo]

    replicador.ejecutar_una_vez()

    fallida = fila(_conexion_del_dueno, pieza)
    assert (fallida["estado"], fallida["intentos"]) == ("fallido", 1)
    assert fallida["ultimo_error"] == f"Drive respondió {codigo}."


def test_un_403_de_cuota_es_transitorio(cliente, replicador, doble, _conexion_del_dueno):
    pieza = subir(cliente)
    doble.responder = [(403, "userRateLimitExceeded")]

    replicador.ejecutar_una_vez()

    assert fila(_conexion_del_dueno, pieza)["estado"] == "pendiente"


def test_un_original_que_no_coincide_con_su_huella_no_se_replica(
    pieza_pendiente, replicador, doble, directorio_originales, _conexion_del_dueno
):
    (directorio_originales / pieza_pendiente).write_bytes(PDF + b"%alterado\n")

    replicador.ejecutar_una_vez()

    fallida = fila(_conexion_del_dueno, pieza_pendiente)
    assert fallida["estado"] == "fallido"
    assert fallida["ultimo_error"] == "El original en disco no coincide con su huella."
    assert doble.subidas == 0


def test_sin_original_en_disco_queda_fallido(
    pieza_pendiente, replicador, directorio_originales, _conexion_del_dueno
):
    pieza = pieza_pendiente
    (directorio_originales / pieza).unlink()

    replicador.ejecutar_una_vez()

    assert fila(_conexion_del_dueno, pieza)["estado"] == "fallido"


def test_el_arriendo_se_renueva_con_cada_tramo(
    cliente, replicador, doble, monkeypatch, _conexion_del_dueno
):
    monkeypatch.setattr(drive, "TRAMO_BYTES", 16)
    pieza = subir(cliente)
    arriendos: list[object] = []
    doble.al_recibir_tramo = lambda: arriendos.append(
        fila(_conexion_del_dueno, pieza)["arrendado_hasta"]
    )

    replicador.ejecutar_una_vez()

    assert doble.tramos == -(-len(PDF + b"%" + b"0" * 36 + b"\n") // 16) == len(arriendos)
    assert arriendos == sorted(set(arriendos)), "cada tramo deja un arriendo nuevo y mayor"
    assert fila(_conexion_del_dueno, pieza)["estado"] == "replicado"


def test_un_308_que_recibio_menos_reenvia_lo_que_falta(
    cliente, replicador, doble, monkeypatch, _conexion_del_dueno
):
    monkeypatch.setattr(drive, "TRAMO_BYTES", 16)
    contenido = PDF + f"%{uuid4()}\n".encode()
    pieza = subir(cliente, contenido)
    doble.aceptar_hasta = 5

    replicador.ejecutar_una_vez()

    assert fila(_conexion_del_dueno, pieza)["estado"] == "replicado"
    [archivo] = doble.archivos.values()
    assert archivo["bytes"] == contenido


def test_un_308_sin_avance_es_transitorio_y_no_reenvia(
    cliente, replicador, doble, monkeypatch, _conexion_del_dueno
):
    monkeypatch.setattr(drive, "TRAMO_BYTES", 16)
    pieza = subir(cliente)
    doble.aceptar_hasta = 0  # Drive no recibió nada: 308 sin `Range`

    replicador.ejecutar_una_vez()

    devuelta = fila(_conexion_del_dueno, pieza)
    assert devuelta["estado"] == "pendiente"
    assert devuelta["ultimo_error"] == "Drive no avanzó la subida reanudable."
    assert doble.tramos == 1


def test_muchos_intentos_no_desbordan_la_espera(cliente, replicador, doble, _conexion_del_dueno):
    pieza = subir(cliente)
    replicador.sincronizar()
    editar(_conexion_del_dueno, pieza, "intentos = 60")
    doble.responder = [503]

    replicador.ejecutar_una_vez()

    assert fila(_conexion_del_dueno, pieza)["estado"] == "fallido"


def test_un_arriendo_vencido_sin_intentos_queda_fallido(
    cliente, replicador, doble, _conexion_del_dueno
):
    pieza = subir(cliente)
    replicador.sincronizar()
    editar(
        _conexion_del_dueno,
        pieza,
        "estado = 'subiendo', intentos = 8, arrendado_hasta = now() - interval '1 s'",
    )

    assert replicador.ejecutar_una_vez() is False

    fallida = fila(_conexion_del_dueno, pieza)
    assert (fallida["estado"], fallida["arrendado_hasta"]) == ("fallido", None)
    assert doble.subidas == 0


def test_otro_hilo_retoma_el_arriendo_vencido(cliente, replicador, _conexion_del_dueno):
    pieza = subir(cliente)
    replicador.sincronizar()
    editar(
        _conexion_del_dueno,
        pieza,
        "estado = 'subiendo', arrendado_hasta = now() + interval '1 min'",
    )
    # Es de otro hilo vivo: no se toca.
    assert replicador.ejecutar_una_vez() is False

    editar(_conexion_del_dueno, pieza, "arrendado_hasta = now() - interval '1 s'")
    assert replicador.ejecutar_una_vez() is True
    assert fila(_conexion_del_dueno, pieza)["estado"] == "replicado"


def test_quien_pierde_el_arriendo_a_mitad_deja_de_subir_y_no_toca_la_fila(
    cliente, replicador, doble, monkeypatch, _conexion_del_dueno
):
    monkeypatch.setattr(drive, "TRAMO_BYTES", 16)
    pieza = subir(cliente)

    def retomada_por_otro() -> None:
        editar(_conexion_del_dueno, pieza, "arrendado_hasta = '2100-01-01'")

    doble.al_recibir_tramo = retomada_por_otro

    replicador.ejecutar_una_vez()

    assert doble.tramos == 1 and doble.archivos == {}
    perdida = fila(_conexion_del_dueno, pieza)
    assert (perdida["estado"], str(perdida["arrendado_hasta"].year)) == ("subiendo", "2100")


def test_si_drive_ya_tiene_el_archivo_no_lo_sube_de_nuevo(
    cliente, replicador, doble, _conexion_del_dueno
):
    contenido = PDF + f"%{uuid4()}\n".encode()
    pieza = subir(cliente, contenido)
    doble.archivos["ya-estaba"] = {
        "appProperties": {"pieza_id": pieza, "sha256": hashlib.sha256(contenido).hexdigest()},
        "bytes": contenido,
        "md5": hashlib.md5(contenido).hexdigest(),
    }

    replicador.ejecutar_una_vez()

    assert fila(_conexion_del_dueno, pieza)["drive_file_id"] == "ya-estaba"
    assert doble.subidas == 0


def test_un_md5_distinto_deja_fallido(cliente, replicador, doble, _conexion_del_dueno):
    pieza = subir(cliente)
    doble.md5_corrupto = True

    replicador.ejecutar_una_vez()

    fallida = fila(_conexion_del_dueno, pieza)
    assert fallida["estado"] == "fallido"
    assert fallida["ultimo_error"] == "El MD5 de Drive no coincide con el original."


def test_un_corte_entre_crear_y_actualizar_la_fila_no_duplica(
    cliente, replicador, doble, _conexion_del_dueno
):
    pieza = subir(cliente)
    replicador.ejecutar_una_vez()
    [file_id] = doble.archivos
    # Drive creó el archivo y el proceso murió antes del UPDATE: la fila quedó `subiendo`.
    editar(
        _conexion_del_dueno,
        pieza,
        "estado = 'subiendo', drive_file_id = NULL, replicado_el = NULL,"
        " arrendado_hasta = now() - interval '1 s'",
    )

    assert replicador.ejecutar_una_vez() is True

    assert fila(_conexion_del_dueno, pieza)["drive_file_id"] == file_id
    assert (doble.subidas, len(doble.archivos)) == (1, 1)


def test_al_apagar_el_trabajador_la_subida_corta_y_la_fila_vuelve_a_pendiente(
    cliente, replicador, doble, monkeypatch, _conexion_del_dueno
):
    monkeypatch.setattr(drive, "TRAMO_BYTES", 16)
    pieza = subir(cliente)
    parar = threading.Event()
    doble.al_recibir_tramo = parar.set

    replicador.ejecutar_una_vez(parar)

    devuelta = fila(_conexion_del_dueno, pieza)
    assert (devuelta["estado"], devuelta["proximo_intento"]) == ("pendiente", None)
    assert doble.tramos == 1


# Encendido, clave y arranque (DRV-08, §3.9).


ENTORNO = {"HV_BD_URL": "postgresql://x", "HV_ORIGINALES": "originales"}


@pytest.mark.historia(211)
def test_drive_esta_apagado_por_defecto():
    assert Entorno.desde_variables(ENTORNO).drive is None
    assert Entorno.desde_variables(ENTORNO | {"HV_DRIVE": "off"}).drive is None
    with pytest.raises(EntornoInvalido, match="HV_DRIVE"):
        Entorno.desde_variables(ENTORNO | {"HV_DRIVE": "si"})


@pytest.mark.rnf("RNF-14")
@pytest.mark.parametrize("falta", ["HV_DRIVE_CLAVE", "HV_DRIVE_UNIDAD", "HV_DRIVE_CARPETA"])
def test_encendido_sin_su_configuracion_no_arranca(falta):
    variables = ENTORNO | {
        "HV_DRIVE": "on",
        "HV_DRIVE_CLAVE": "/run/secrets/drive_sa.json",
        "HV_DRIVE_UNIDAD": "u",
        "HV_DRIVE_CARPETA": "c",
    }
    del variables[falta]

    with pytest.raises(EntornoInvalido, match=falta):
        Entorno.desde_variables(variables)


@pytest.fixture
def archivo_de_clave(tmp_path: Path, doble: DriveFalso) -> Path:
    ruta = tmp_path / "drive_sa.json"
    ruta.write_text(json.dumps(clave_de_prueba(doble.clave)), encoding="utf-8")
    return ruta


@pytest.mark.rnf("RNF-14")
def test_la_clave_se_lee_del_archivo_montado_y_no_del_entorno(archivo_de_clave, tmp_path):
    contenido = archivo_de_clave.read_text(encoding="utf-8")

    assert leer_clave(archivo_de_clave)["client_email"].startswith("hv-originales@")
    # La clave pegada en la variable no es una ruta: no arranca, y el error no la repite.
    for valor in (contenido, str(tmp_path / "no-esta.json")):
        entorno = Entorno.desde_variables(
            ENTORNO
            | {
                "HV_DRIVE": "on",
                "HV_DRIVE_CLAVE": valor,
                "HV_DRIVE_UNIDAD": "u",
                "HV_DRIVE_CARPETA": "c",
            }
        )
        with pytest.raises(EntornoInvalido, match="HV_DRIVE_CLAVE") as error:
            componer_replicador(entorno, bd=None)  # type: ignore[arg-type]
        assert "PRIVATE KEY" not in str(error.value)


@pytest.mark.rnf("RNF-14")
@pytest.mark.parametrize("con_clave", [True, False])
def test_el_trabajador_encendido_arranca_la_replica_y_sin_clave_falla_al_arrancar(
    con_clave, archivo_de_clave, url_app, monkeypatch, capsys, tmp_path
):
    llegaron: list[str] = []

    async def trabajar(trabajador, bd, parar):
        llegaron.append("trabajador")
        bd.cerrar()

    async def replicar(replicador, parar):
        assert isinstance(parar, asyncio.Event)
        llegaron.append("replica")

    monkeypatch.setattr("historia_viva.composicion._trabajar", trabajar)
    monkeypatch.setattr("historia_viva.composicion._replicar", replicar)
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_ORIGINALES", str(tmp_path))
    monkeypatch.setenv("HV_MOTOR_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("HV_DRIVE", "on")
    monkeypatch.setenv("HV_DRIVE_UNIDAD", "u")
    monkeypatch.setenv("HV_DRIVE_CARPETA", "c")
    monkeypatch.setenv(
        "HV_DRIVE_CLAVE", str(archivo_de_clave if con_clave else tmp_path / "no-esta.json")
    )

    codigo = main(["trabajador", "--tipos", "procesamiento"])

    if con_clave:
        assert (codigo, llegaron) == (0, ["trabajador", "replica"])
    else:
        assert (codigo, llegaron) == (2, [])
        assert "HV_DRIVE_CLAVE" in capsys.readouterr().err


# Drive apagado o caído (ADR-0001, DRV-07).


@pytest.mark.historia(211)
@pytest.mark.xdist_group("indice")
@pytest.mark.parametrize("drive_", ["apagado", "caido"])
def test_corte_de_drive(
    drive_, cliente, validada, doble, directorio_originales, _conexion_del_dueno
):
    """Con Drive apagado o caído, ingesta, procesamiento, validación, Conversación y visor
    andan; el visor sirve del disco local."""
    parar = threading.Event()
    hilo = None
    if drive_ == "caido":
        doble.caido = True
        bd_ = cliente.app.state.bd
        replicador = Replicador(
            bd=bd_,
            fichas=ModuloFichasPostgres(bd=bd_),
            originales=AlmacenDeOriginales(directorio_originales),
            cliente=doble.cliente(),
        )
        hilo = threading.Thread(target=replicador.bucle, args=(parar, 0.05), daemon=True)
        hilo.start()
    try:
        # Ingesta, procesamiento, validación e indexación.
        pieza = validada(LABORATORIO)
        respuesta, eventos = turno(cliente, nueva(cliente), "¿Cuándo se creó el laboratorio?")
        assert respuesta.status_code == 200
        assert [f["pieza_id"] for f in dict(eventos)["fuentes"]["fichas"]] == [pieza]
        original = cliente.get(f"/api/piezas/{pieza}/original", headers={"Range": "bytes=0-3"})
        assert (original.status_code, original.content) == (206, b"%PDF")
        if hilo is not None:  # que el hilo haya intentado antes de mirar la fila
            limite = time.monotonic() + 5
            while time.monotonic() < limite and not _conexion_del_dueno.execute(
                "SELECT 1 FROM acervo.replicacion_drive WHERE estado = 'pendiente'"
            ).fetchall():
                time.sleep(0.05)
    finally:
        parar.set()
        if hilo is not None:
            hilo.join(5)
    replicas = _conexion_del_dueno.execute(
        "SELECT estado, ultimo_error FROM acervo.replicacion_drive"
    ).fetchall()
    if drive_ == "apagado":
        assert replicas == []
    else:
        assert replicas == [("pendiente", "Drive no respondió (ConnectError).")]
        assert esperar_estado(cliente, pieza, "Validada")


# Contra la unidad real (126): se saltea sin clave.


@pytest.mark.drive_real
@pytest.mark.historia(240)
@pytest.mark.skipif(
    not os.environ.get("HV_DRIVE_CLAVE_PRUEBA"),
    reason="sin HV_DRIVE_CLAVE_PRUEBA (y HV_DRIVE_UNIDAD_PRUEBA, HV_DRIVE_CARPETA_PRUEBA)",
)
def test_drive_real():
    configuracion = ConfiguracionDrive(
        clave=Path(os.environ["HV_DRIVE_CLAVE_PRUEBA"]),
        unidad=os.environ["HV_DRIVE_UNIDAD_PRUEBA"],
        carpeta=os.environ["HV_DRIVE_CARPETA_PRUEBA"],
    )
    cliente = ClienteDrive(configuracion, leer_clave(configuracion.clave))
    contenido = os.urandom(10 * 1024 * 1024)
    pieza_id, sha256 = uuid4(), hashlib.sha256(contenido).hexdigest()
    bloques = (contenido[i : i + 1024 * 1024] for i in range(0, len(contenido), 1024 * 1024))

    subido = cliente.subir(
        bloques,
        total=len(contenido),
        nombre="prueba-real.bin",
        tipo_mime="application/octet-stream",
        pieza_id=pieza_id,
        sha256=sha256,
        al_avanzar=lambda: None,
    )

    assert subido.md5 == hashlib.md5(contenido).hexdigest()
    assert cliente.buscar(pieza_id, sha256) == subido
