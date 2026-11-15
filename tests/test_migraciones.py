"""El esquema versionado y los roles de base (historias 48 y 49, DEC-31, DEC-105).

Postgres real y sin superusuario: el bootstrap lo usa para crear cada base, y las migraciones
corren como `hv_migrador`. Los tests del runner arrancan de una base vacía (`base_vacia`); el del
bootstrap, de una recién creada (`base_nueva`); los del esquema trabajan en la base de la suite,
migrada, en una transacción que se revierte (`bd`).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from base_de_pruebas import CLAVE_APP, borrar_base, crear_base, preparar
from historia_viva.dominio.estados import TRANSICIONES, Estado
from historia_viva.migraciones import DIRECTORIO, Migracion, MigracionRechazada, aplicar
from validacion import DIRECTORA_DE_CARRERA, JORGE, MARIA


def _como(dsn: str, rol: str, clave: str) -> str:
    return make_conninfo(dsn, user=rol, password=clave)


@pytest.fixture(scope="session")
def base_nueva(url_superusuario) -> Iterator[str]:
    """Una base creada de cero con el bootstrap: `base_vacia` se reusa entre tests y ya trae lo
    que el bootstrap crea. Se borra al final de la sesión y sin esperar: borrar una base fuerza
    un checkpoint que, con la suite corriendo, tarda decenas de segundos."""
    base = f"hv_nueva_{uuid4().hex[:8]}"
    yield crear_base(url_superusuario, base)
    borrar_base(url_superusuario, base, esperar=False)


@pytest.mark.historia(49)
def test_el_bootstrap_deja_roles_base_y_extension(base_nueva, url_superusuario):
    base = psycopg.conninfo.conninfo_to_dict(base_nueva)["dbname"]
    # Idempotente: correrlo otra vez sobre lo mismo no falla ni cambia nada.
    preparar(url_superusuario, base)

    with psycopg.connect(base_nueva) as conexion:
        roles = conexion.execute(
            "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles"
            " WHERE rolname IN ('hv_migrador', 'hv_app') ORDER BY rolname"
        ).fetchall()
        dueno, conectan = conexion.execute(
            "SELECT pg_get_userbyid(datdba),"
            " (SELECT array_agg(a.grantee::regrole::text) FROM aclexplode(datacl) a"
            "  WHERE a.privilege_type = 'CONNECT')"
            " FROM pg_database WHERE datname = current_database()"
        ).fetchone()
        extensiones = conexion.execute("SELECT extname FROM pg_extension").fetchall()
    with psycopg.connect(_como(base_nueva, "hv_app", CLAVE_APP)) as conexion:
        search_path = conexion.execute("SHOW search_path").fetchone()
        inactiva = conexion.execute("SHOW idle_in_transaction_session_timeout").fetchone()

    # Ninguno es superusuario: las migraciones no lo necesitan.
    assert roles == [("hv_app", False, False, False), ("hv_migrador", False, False, False)]
    assert dueno == "hv_migrador"
    # Sin `CONNECT` para PUBLIC: solo los dos roles entran.
    assert sorted(conectan) == ["hv_app", "hv_migrador"]
    assert ("vector",) in extensiones
    assert search_path == ("acervo, public",)
    assert inactiva == ("1min",)


@pytest.mark.historia(49)
@pytest.mark.parametrize(
    ("clave_migrador", "clave_app"), [("", "otra"), ("una", ""), ("igual", "igual")],
    ids=["sin-clave-de-migrador", "sin-clave-de-app", "claves-iguales"],
)
def test_el_bootstrap_exige_dos_claves_distintas(base_vacia, url_superusuario,
                                                 clave_migrador, clave_app):
    base = psycopg.conninfo.conninfo_to_dict(base_vacia)["dbname"]

    with pytest.raises(psycopg.errors.RaiseException, match="clave"):
        preparar(url_superusuario, base, clave_migrador=clave_migrador, clave_app=clave_app)


def _escribir(directorio: Path, nombre: str, contenido: str) -> Path:
    directorio.mkdir(exist_ok=True)
    ruta = directorio / nombre
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


@pytest.mark.historia(49)
def test_aplicar_registra_cada_migracion_y_la_segunda_corrida_no_hace_nada(
    base_vacia, tmp_path
):
    _escribir(tmp_path, "0001_primera.sql", "CREATE TABLE acervo.uno (x int);")
    segunda = _escribir(tmp_path, "0002_segunda.sql", "CREATE TABLE acervo.dos (x int);")

    aplicadas = aplicar(base_vacia, directorio=tmp_path)
    de_nuevo = aplicar(base_vacia, directorio=tmp_path)

    with psycopg.connect(base_vacia) as conexion:
        registro = conexion.execute(
            "SELECT version, nombre, checksum, aplicada_el IS NOT NULL"
            " FROM acervo.schema_migracion ORDER BY version"
        ).fetchall()
    assert [m.version for m in aplicadas] == [1, 2]
    assert de_nuevo == []
    assert registro[1] == (2, "segunda", hashlib.sha256(segunda.read_bytes()).hexdigest(), True)
    assert [fila[:2] for fila in registro] == [(1, "primera"), (2, "segunda")]


@pytest.mark.historia(49)
def test_el_checksum_no_depende_del_fin_de_linea(tmp_path):
    # Con `core.autocrlf` el mismo archivo sale con CRLF en Windows y con LF en Linux: una base
    # migrada desde una PC no puede rechazar el `migrar` de un clon en el servidor.
    (tmp_path / "crlf").mkdir()
    (tmp_path / "lf").mkdir()
    crlf = tmp_path / "crlf" / "0001_tabla.sql"
    lf = tmp_path / "lf" / "0001_tabla.sql"
    crlf.write_bytes(b"CREATE TABLE acervo.uno (\r\n    x int\r\n);\r\n")
    lf.write_bytes(b"CREATE TABLE acervo.uno (\n    x int\n);\n")

    assert Migracion.leer(crlf).checksum == Migracion.leer(lf).checksum


def _versiones(dsn: str) -> list[int]:
    with psycopg.connect(dsn) as conexion:
        filas = conexion.execute("SELECT version FROM acervo.schema_migracion ORDER BY 1")
        return [version for (version,) in filas]


@pytest.mark.historia(49)
def test_un_checksum_cambiado_aborta_sin_aplicar_nada(base_vacia, tmp_path):
    primera = _escribir(tmp_path, "0001_primera.sql", "CREATE TABLE acervo.uno (x int);")
    aplicar(base_vacia, directorio=tmp_path)
    primera.write_text("CREATE TABLE acervo.uno (x bigint);", encoding="utf-8")
    _escribir(tmp_path, "0002_segunda.sql", "CREATE TABLE acervo.dos (x int);")

    with pytest.raises(MigracionRechazada, match="0001_primera.*checksum"):
        aplicar(base_vacia, directorio=tmp_path)
    assert _versiones(base_vacia) == [1]


@pytest.mark.historia(49)
def test_no_aplica_un_numero_menor_que_uno_ya_aplicado(base_vacia, tmp_path):
    _escribir(tmp_path, "0001_primera.sql", "CREATE TABLE acervo.uno (x int);")
    _escribir(tmp_path, "0003_tercera.sql", "CREATE TABLE acervo.tres (x int);")
    aplicar(base_vacia, directorio=tmp_path)
    # Llegó tarde: alguien la escribió con un número que ya quedó atrás.
    _escribir(tmp_path, "0002_tardia.sql", "CREATE TABLE acervo.dos (x int);")

    with pytest.raises(MigracionRechazada, match="0002_tardia.*0003"):
        aplicar(base_vacia, directorio=tmp_path)
    assert _versiones(base_vacia) == [1, 3]


@pytest.mark.historia(49)
def test_una_base_mas_nueva_que_el_codigo_no_se_migra(base_vacia, tmp_path):
    # Restaurar un respaldo hecho con un código más nuevo, o volver a un tag anterior.
    _escribir(tmp_path, "0001_primera.sql", "CREATE TABLE acervo.uno (x int);")
    nueva = _escribir(tmp_path, "0002_nueva.sql", "CREATE TABLE acervo.dos (x int);")
    aplicar(base_vacia, directorio=tmp_path)
    nueva.unlink()

    with pytest.raises(MigracionRechazada, match="0002.*más nueva que el código"):
        aplicar(base_vacia, directorio=tmp_path)
    assert _versiones(base_vacia) == [1, 2]


@pytest.mark.historia(49)
def test_dos_migraciones_con_el_mismo_numero_no_aplican_nada(base_vacia, tmp_path):
    _escribir(tmp_path, "0001_primera.sql", "CREATE TABLE acervo.uno (x int);")
    _escribir(tmp_path, "0002_de_un_ticket.sql", "CREATE TABLE acervo.dos (x int);")
    _escribir(tmp_path, "0002_de_otro_ticket.sql", "CREATE TABLE acervo.tres (x int);")

    with pytest.raises(MigracionRechazada, match="0002"):
        aplicar(base_vacia, directorio=tmp_path)
    with psycopg.connect(base_vacia) as conexion:
        uno = conexion.execute("SELECT to_regclass('acervo.uno')").fetchone()
    assert uno == (None,)


@pytest.mark.historia(49)
def test_una_migracion_que_falla_no_deja_nada_suyo(base_vacia, tmp_path):
    _escribir(tmp_path, "0001_primera.sql", "CREATE TABLE acervo.uno (x int);")
    _escribir(tmp_path, "0002_rota.sql", "CREATE TABLE acervo.dos (x int); SELECT 1 / 0;")

    with pytest.raises(psycopg.errors.DivisionByZero):
        aplicar(base_vacia, directorio=tmp_path)
    with psycopg.connect(base_vacia) as conexion:
        dos = conexion.execute("SELECT to_regclass('acervo.dos')").fetchone()
    assert _versiones(base_vacia) == [1]
    assert dos == (None,)


@pytest.mark.historia(49)
def test_una_migracion_marcada_corre_fuera_de_transaccion(base_vacia, tmp_path):
    _escribir(tmp_path, "0001_tabla.sql", "CREATE TABLE acervo.uno (x int);")
    # `CONCURRENTLY` falla dentro de una transacción.
    _escribir(
        tmp_path,
        "0002_indice.sql",
        "-- sin-transaccion\nCREATE INDEX CONCURRENTLY uno_x ON acervo.uno (x);\n",
    )

    aplicar(base_vacia, directorio=tmp_path)

    assert _versiones(base_vacia) == [1, 2]


@pytest.mark.historia(49)
def test_dos_migrar_simultaneos_no_duplican(base_vacia, tmp_path):
    # La espera deja a los dos `migrar` adentro a la vez si nada los ordena.
    _escribir(
        tmp_path, "0001_lenta.sql", "SELECT pg_sleep(1); CREATE TABLE acervo.uno (x int);"
    )

    with ThreadPoolExecutor(max_workers=2) as hilos:
        corridas = [hilos.submit(aplicar, base_vacia, directorio=tmp_path) for _ in range(2)]
        aplicadas = sorted(len(corrida.result()) for corrida in corridas)

    # Uno aplica; el otro espera el candado y encuentra todo hecho.
    assert aplicadas == [0, 1]
    assert _versiones(base_vacia) == [1]


# Las columnas de cada tabla del esquema base (spec, «Esquema y estados» §1; DEC-140).
ESQUEMA_BASE = {
    "schema_migracion": {"version", "nombre", "checksum", "aplicada_el"},
    "usuario": {
        "usuario_id", "email", "nombre", "sub_google", "rol", "activo", "creado_el",
        "ultimo_acceso_el",
    },
    "sesion": {
        "sesion_id", "usuario_id", "metodo", "creada_el", "ultima_actividad_el", "vence_el",
        "revocada_el",
    },
    "tipo_pieza": {"nombre", "orden", "activo", "provisorio"},
    "tipo_actividad": {"nombre", "orden", "es_actividad", "activo"},
    "carrera": {"carrera_id", "nombre", "orden", "activo"},
    "pieza": {
        "pieza_id", "estado", "tipo", "fecha_inicio", "fecha_fin", "precision_fecha",
        "rango_fechas", "origen", "resumen", "archivo_original_id", "archivo_original_url",
        "nombre_archivo", "tipo_mime", "tamano_bytes", "clase_original", "via_ingreso",
        "metadata_original", "huella_original", "validada_por", "validada_el", "creada_por",
        "trabajada_por", "creada_el", "actualizada_el", "alerta_inyeccion", "lote_id",
        "campos_heredados", "contexto_heredado", "tipo_actividad", "propietario",
        "modo_validacion", "borrada_el", "borrada_por", "borrada_motivo", "autor",
        "publicable", "campos_de_la_persona", "contexto_de_la_persona",
    },
    "pieza_carrera": {"pieza_id", "carrera_id", "asignada_el", "quitada_el"},
    "lote": {
        "lote_id", "nombre", "origen", "fecha_inicio", "fecha_fin", "precision_fecha", "evento",
        "lugar", "creado_por", "creado_el", "propietario",
    },
    "lote_carrera": {"lote_id", "carrera_id", "asignada_el", "quitada_el"},
    # 084: la vista de los conteos del Lote (0006 del spec, más `n_analisis_humano`).
    "v_lote_resumen": {
        "lote_id", "nombre", "n_piezas", "n_recibida", "n_pendiente", "n_incompleta",
        "n_validada", "n_analisis_humano",
    },
    # 148: las descripciones de imágenes embebidas cuyo trabajo sigue activo; la firma las espera.
    "v_descripcion_en_curso": {"pieza_id", "imagen"},
    "ficha": {
        "ficha_id", "pieza_id", "version", "contenido_markdown", "origen_version",
        "editada_por", "metodo_autenticacion", "configuracion_efectiva", "editada_el",
        "vigente", "informe_inyeccion", "aprobacion_id",
    },
    "trabajo_procesamiento": {
        "trabajo_id", "pieza_id", "tipo", "estado", "configuracion_efectiva", "generaciones",
        "tope_generaciones", "tiempo_maximo_ms", "tokens_entrada", "tokens_salida",
        "duracion_ms", "tomado_por", "arriendo_vence_el", "encolado_el", "iniciado_el",
        "terminado_el", "error", "ficha_id", "prioridad", "contexto_lote", "solicitado_por",
        "imagen",
    },
    "medicion_validacion": {
        "medicion_id", "pieza_id", "persona_id", "nombre_archivo", "abierta_el",
        "guardada_el", "duracion_s", "estado_final", "campos_modificados", "campos_faltantes",
        "cuerpo_editado", "caracteres_cuerpo_propuesta", "caracteres_cuerpo_guardado",
        "clase_original", "metodo_autenticacion", "modo_validacion",
    },
    "configuracion_capacidad": {
        "capacidad", "motor", "modelo", "instruccion_id", "parametros", "version",
        "actualizada_por", "actualizada_el",
    },
    "auditoria": {
        "id", "cuando", "actor_tipo", "actor_id", "actor_etiqueta", "accion", "objeto_tipo",
        "objeto_id", "antes", "despues", "motivo", "ip", "solicitud_id",
    },
    # La tabla de `PGVectorStore` (044): sus columnas son las que la biblioteca consulta.
    "data_fragmento": {"id", "node_id", "text", "metadata_", "embedding"},
    "evento_interruptor": {
        "evento_id", "clave", "habilitado", "motivo", "actor_tipo", "actor_etiqueta",
        "usuario_id", "en",
    },
    "agente_externo": {"agente_id", "nombre", "scopes", "creado_el", "creado_por"},
    "token_agente": {
        "token_id", "agente_id", "token_hash", "token_prefijo", "creado_el", "creado_por",
        "vence_el", "ultimo_uso_el", "revocado_el", "revocado_por",
    },
    "latido_trabajador": {"trabajador", "tipos", "latido_el"},
    "conversacion": {
        "conversacion_id", "persona_id", "titulo", "creada_el", "actualizada_el", "estado",
        "turno_desde",
    },
    "mensaje_conversacion": {
        "mensaje_id", "conversacion_id", "orden", "autor", "contenido", "filtros",
        "pregunta_reformulada", "estado", "modelo_embedding", "configuracion_efectiva",
        "citas_no_verificadas", "ms_hasta_fuentes", "ms_hasta_primer_token", "ms_hasta_respuesta",
        "creado_el",
    },
    "mensaje_cita": {
        "mensaje_id", "posicion", "pieza_id", "ficha_id", "fragmento_id", "ubicacion",
        "citada_en_respuesta",
    },
    "aporte": {
        "aporte_id", "agente_id", "idempotency_key", "cuerpo_huella", "pieza_id", "titulo",
        "cuerpo", "tipo_contenido", "canal", "fecha_publicacion", "url_publicacion",
        "recibido_el",
    },
    "aporte_fragmento_fuente": {"aporte_id", "fragmento_id"},
    "aporte_pieza_fuente": {"aporte_id", "pieza_id"},
    "efemeride": {
        "efemeride_id", "alcance", "fecha_objetivo", "difusion_desde", "difusion_hasta", "titulo",
        "cuerpo", "estado", "version", "plantilla", "alerta_inyeccion", "configuracion_efectiva",
        "origen_generacion", "generada_por", "generada_el", "editada_por", "editada_el",
        "aprobada_por", "aprobada_el", "descartada_por", "descartada_el", "descarte_motivo",
        "fuente_retirada_el", "metodo_autenticacion",
    },
    "efemeride_fuente": {"efemeride_id", "pieza_id", "ficha_id", "posicion", "alerta_inyeccion"},
    "relato": {
        "relato_id", "mensaje_id", "titulo", "cuerpo", "estado", "version", "alerta_inyeccion",
        "configuracion_efectiva", "propuesto_por", "propuesto_el", "editado_por", "editado_el",
        "aprobado_por", "aprobado_el", "descartado_por", "descartado_el", "descarte_motivo",
        "fuente_retirada_el", "metodo_autenticacion",
    },
    "relato_fuente": {"relato_id", "posicion", "pieza_id", "ficha_id", "alerta_inyeccion"},
    "programacion_efemerides": {
        "programacion_id", "habilitada", "periodicidad", "anticipacion_dias", "maximo_por_corrida",
        "alcances", "ultima_corrida_el", "pedido_manual_el", "pedido_manual_objetivo",
        "pedido_manual_por", "version", "actualizada_por", "actualizada_el",
        "ultima_corrida_resultado", "ultima_programada_el",
    },
    "informe": {
        "informe_id", "desde", "hasta", "carreras", "estado", "huella", "integridad",
        "generado_por", "generado_el", "reemplazado_por", "descartado_por", "descartado_el",
        "aprobado_el", "exportado_por", "exportado_el",
    },
    "informe_pieza": {
        "informe_id", "pieza_id", "ficha_id", "tipo_actividad", "fecha_inicio", "fecha_fin",
        "orden",
    },
    "informe_aprobacion": {
        "aprobacion_id", "informe_id", "rol", "aprobada_por", "aprobada_el",
        "metodo_autenticacion", "huella",
    },
    # 108: la medición del bloque (DEC-146 punto 14); `clase_original` es la clase de la previa.
    "aprobacion_en_bloque": {
        "aprobacion_id", "lote_id", "clase_original", "abierta_por", "abierta_el",
        "confirmada_el", "n_aprobadas", "n_rechazadas", "duracion_s", "vistas",
    },
    # 123: la réplica a Drive (spec §1.23), vacía mientras esté apagada.
    "replicacion_drive": {
        "pieza_id", "estado", "drive_file_id", "intentos", "proximo_intento", "ultimo_error",
        "arrendado_hasta", "replicado_el",
    },
    # 144: las Afirmaciones de la v1 de una foto, con su decisión (ADR-0007). 148: una fila por
    # imagen de la v1 (la foto entera o cada embebida sin texto) y la imagen de cada Afirmación.
    "descripcion_de_imagen": {
        "ficha_id", "tipo_imagen", "imagen", "numero", "pagina", "hoja", "estado"
    },
    "afirmacion": {
        "ficha_id", "orden", "texto", "estado", "decidida_por", "decidida_el", "respaldo",
        "imagen",
    },
    # 146: los resúmenes rehechos sin las Afirmaciones rechazadas, propuestas sin versión.
    # 148: con `imagen`, siempre 0, para colgar de la fila de la foto entera.
    "resumen_rehecho": {"ficha_id", "rehecho_el", "texto", "informe_inyeccion", "imagen"},
}


def _columnas(dsn: str) -> dict[str, set[str]]:
    with psycopg.connect(dsn) as conexion:
        filas = conexion.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'acervo'"
        ).fetchall()
    columnas: dict[str, set[str]] = {}
    for tabla, columna in filas:
        columnas.setdefault(tabla, set()).add(columna)
    return columnas


@pytest.mark.historia(49)
def test_la_base_vacia_queda_en_el_esquema_base_y_la_segunda_corrida_no_hace_nada(
    base_vacia,
):
    aplicar(base_vacia)
    de_nuevo = aplicar(base_vacia)

    with psycopg.connect(base_vacia) as conexion:
        tipos = conexion.execute("SELECT nombre FROM acervo.tipo_pieza ORDER BY orden")
        tipos_de_pieza = [nombre for (nombre,) in tipos]
    assert _columnas(base_vacia) == ESQUEMA_BASE
    assert tipos_de_pieza == [
        "fotografia", "acta", "resolucion", "convenio", "memoria", "nota", "plano", "planilla",
        "video",
    ]
    assert de_nuevo == []


@pytest.fixture
def bd(tx) -> psycopg.Connection:
    """Conexión de `hv_migrador` a la base de la suite, en una transacción que se revierte al
    terminar el test."""
    return tx


HUELLA = "a" * 64


def _insertar_pieza(
    bd: psycopg.Connection,
    *,
    estado: str = "Recibida",
    clase: str = "documento",
    via: str = "archivo",
    mime: str = "application/pdf",
    huella: str | None = None,
    creada_por: str | None = None,
) -> str:
    fila = bd.execute(
        "INSERT INTO acervo.pieza (estado, clase_original, via_ingreso, tipo_mime,"
        " nombre_archivo, tamano_bytes, huella_original, creada_por)"
        " VALUES (%s, %s, %s, %s, 'x', 1, %s, %s) RETURNING pieza_id::text",
        (estado, clase, via, mime, huella, creada_por),
    ).fetchone()
    assert fila is not None
    return str(fila[0])


@pytest.mark.historia(49)
@pytest.mark.parametrize(
    ("pieza", "restriccion"),
    [
        (dict(clase="video", mime="video/mp4"), "pieza_sin_plan_no_recibida"),
        (dict(clase="imagen", mime="application/pdf"), "pieza_clase_mime"),
        (dict(clase="documento", mime="image/png"), "pieza_clase_mime"),
        # Un Aporte no tiene plan: nunca está Recibida.
        (dict(clase="texto", via="aporte", mime="text/plain"), "pieza_sin_plan_no_recibida"),
        (dict(estado="Pendiente", via="aporte"), "pieza_aporte_coherente"),
        (
            dict(
                estado="Pendiente", clase="texto", via="aporte", mime="text/plain",
                creada_por=MARIA,
            ),
            "pieza_aporte_coherente",
        ),
    ],
    ids=[
        "video-recibida",
        "imagen-pdf",
        "documento-png",
        "aporte-recibido",
        "aporte-que-no-es-texto",
        "aporte-con-autor",
    ],
)
def test_la_clase_del_original_restringe_la_pieza(bd, pieza, restriccion):
    bd.execute(
        "INSERT INTO acervo.usuario (usuario_id, email, nombre, rol)"
        " VALUES (%s, 'm@fie', 'M', 'rgc')"
        " ON CONFLICT DO NOTHING",  # la base de la suite ya la tiene
        (MARIA,),
    )

    with pytest.raises(psycopg.errors.CheckViolation, match=restriccion):
        _insertar_pieza(bd, **pieza)


@pytest.mark.historia(49)
def test_la_clase_admite_lo_que_tiene_plan_y_el_aporte_de_texto(bd):
    _insertar_pieza(bd, clase="imagen", mime="image/jpeg")
    _insertar_pieza(bd, estado="Pendiente", clase="video", mime="video/webm")
    _insertar_pieza(
        bd, estado="Pendiente", clase="texto", via="aporte", mime="text/plain; charset=utf-8"
    )
    # El `.txt` que sube una persona tiene plan: se encola Recibido (decisión del usuario, 028).
    _insertar_pieza(bd, clase="texto", mime="text/plain; charset=windows-1252")

    vias = bd.execute("SELECT via_ingreso FROM acervo.pieza ORDER BY 1").fetchall()
    assert vias == [("aporte",), ("archivo",), ("archivo",), ("archivo",)]


@pytest.mark.historia(49)
def test_la_huella_del_original_no_se_repite(bd):
    _insertar_pieza(bd, huella=HUELLA)

    with pytest.raises(psycopg.errors.UniqueViolation, match="pieza_huella_unica"):
        _insertar_pieza(bd, huella=HUELLA)


NUEVE_OBLIGATORIOS = {
    "tipo": "acta",
    "tipo_actividad": "No es una actividad",
    "fecha_inicio": "1975-05-03",
    "fecha_fin": "1975-05-03",
    "precision_fecha": "dia",
    "origen": "Archivo de la SEU",
    "propietario": "SEU",
    "resumen": "Acta de 1975.",
    "archivo_original_id": "local://originales/x",
}
"""Las columnas de los nueve obligatorios para Validar (DEC-126), completas."""


def _pieza_con(bd: psycopg.Connection, estado: str, columnas: dict[str, str]) -> str:
    # Nace Pendiente, o Recibida si se pide: nada vuelve a Recibida (`pieza_transicion`).
    pieza_id = _insertar_pieza(bd, estado="Recibida" if estado == "Recibida" else "Pendiente")
    asignaciones = ", ".join(f"{columna} = %({columna})s" for columna in columnas)
    bd.execute(
        f"UPDATE acervo.pieza SET {asignaciones}, estado = %(estado)s, validada_por = %(firma)s,"
        " validada_el = now(),"
        " modo_validacion = CASE WHEN %(estado)s = 'Validada' THEN 'individual' END"
        " WHERE pieza_id = %(pieza_id)s",
        columnas | {"estado": estado, "firma": MARIA, "pieza_id": pieza_id},
    )
    return pieza_id


@pytest.mark.rf("RF-51", "RF-52")
@pytest.mark.historia(288)
@pytest.mark.parametrize("ausente", ["tipo_actividad", "propietario"])
def test_una_validada_sin_tipo_de_actividad_o_sin_propietario_no_se_guarda(bd, ausente):
    _pieza_con(bd, "Validada", NUEVE_OBLIGATORIOS)

    with pytest.raises(psycopg.errors.CheckViolation, match="pieza_validada_completa"):
        _pieza_con(
            bd, "Validada", {c: v for c, v in NUEVE_OBLIGATORIOS.items() if c != ausente}
        )


@pytest.mark.rf("RF-51", "RF-52")
@pytest.mark.historia(288)
@pytest.mark.parametrize("estado", ["Recibida", "Pendiente", "Incompleta"])
def test_sin_tipo_de_actividad_ni_propietario_la_pieza_no_validada_se_guarda(bd, estado):
    sin_ellos = {
        c: v for c, v in NUEVE_OBLIGATORIOS.items() if c not in {"tipo_actividad", "propietario"}
    }

    pieza_id = _pieza_con(bd, estado, sin_ellos)

    fila = bd.execute(
        "SELECT estado, tipo_actividad, propietario FROM acervo.pieza WHERE pieza_id = %s",
        (pieza_id,),
    ).fetchone()
    assert fila == (estado, None, None)


@pytest.mark.rf("RF-51", "RF-52")
@pytest.mark.parametrize(
    ("columna", "valor", "error"),
    [
        ("tipo_actividad", "Pasantía", psycopg.errors.ForeignKeyViolation),
        ("propietario", "  ", psycopg.errors.CheckViolation),
    ],
)
def test_el_tipo_de_actividad_es_del_catalogo_y_el_propietario_no_es_blanco(
    bd, columna, valor, error
):
    with pytest.raises(error):
        _pieza_con(bd, "Incompleta", {columna: valor})


@pytest.mark.historia(120)
@pytest.mark.parametrize(
    ("modo", "error", "restriccion"),
    [
        (None, psycopg.errors.CheckViolation, "pieza_validada_firmada"),
        ("a_ojo", psycopg.errors.CheckViolation, "pieza_modo_validacion_check"),
    ],
)
def test_una_validada_dice_en_que_modo_se_firmo(bd, modo, error, restriccion):
    pieza_id = _pieza_con(bd, "Validada", NUEVE_OBLIGATORIOS)
    bd.execute(
        "UPDATE acervo.pieza SET modo_validacion = 'bloque' WHERE pieza_id = %s", (pieza_id,)
    )

    with pytest.raises(error, match=restriccion):
        bd.execute(
            "UPDATE acervo.pieza SET modo_validacion = %s WHERE pieza_id = %s", (modo, pieza_id)
        )


@pytest.mark.historia(120)
@pytest.mark.parametrize("estado", ["Pendiente", "Incompleta"])
def test_solo_una_validada_tiene_modo_de_firma(bd, estado):
    pieza_id = _pieza_con(bd, estado, {"origen": "Archivo de la SEU"})

    with pytest.raises(psycopg.errors.CheckViolation, match="pieza_modo_solo_validada"):
        bd.execute(
            "UPDATE acervo.pieza SET modo_validacion = 'individual' WHERE pieza_id = %s",
            (pieza_id,),
        )


@pytest.mark.rf("RF-16")
def test_la_medicion_no_admite_el_modo_bloque(bd):
    # La firma en bloque no abre mediciones por Pieza (DEC-146 punto 14).
    pieza_id = _insertar_pieza(bd)

    with pytest.raises(psycopg.errors.CheckViolation, match="medicion_modo_individual"):
        bd.execute(
            "INSERT INTO acervo.medicion_validacion (pieza_id, persona_id, nombre_archivo,"
            " clase_original, caracteres_cuerpo_propuesta, modo_validacion)"
            " VALUES (%s, %s, 'x', 'documento', 0, 'bloque')",
            (pieza_id, MARIA),
        )


@pytest.mark.historia(128)
@pytest.mark.rf("RF-18")
def test_la_aprobacion_en_bloque_genera_su_duracion_al_confirmarse(bd):
    lote_id = bd.execute(
        "INSERT INTO acervo.lote (nombre, origen, fecha_inicio, fecha_fin, precision_fecha,"
        " creado_por) VALUES ('L', 'SEU', '1970-01-01', '1979-12-31', 'decada', %s)"
        " RETURNING lote_id",
        (MARIA,),
    ).fetchone()[0]
    aprobacion_id = bd.execute(
        "INSERT INTO acervo.aprobacion_en_bloque (lote_id, clase_original, abierta_por,"
        " abierta_el, vistas) VALUES (%s, 'documento', %s, '2026-10-17 10:00:00+00', '{}')"
        " RETURNING aprobacion_id",
        (lote_id, MARIA),
    ).fetchone()[0]

    abierta = bd.execute(
        "SELECT duracion_s FROM acervo.aprobacion_en_bloque WHERE aprobacion_id = %s",
        (aprobacion_id,),
    ).fetchone()
    confirmada = bd.execute(
        "UPDATE acervo.aprobacion_en_bloque SET confirmada_el = '2026-10-17 10:02:05+00',"
        " n_aprobadas = 7, n_rechazadas = 3 WHERE aprobacion_id = %s RETURNING duracion_s",
        (aprobacion_id,),
    ).fetchone()

    assert (abierta, confirmada) == ((None,), (125,))
    with pytest.raises(psycopg.errors.CheckViolation), bd.transaction():
        bd.execute(
            "UPDATE acervo.aprobacion_en_bloque SET n_aprobadas = NULL"
            " WHERE aprobacion_id = %s",
            (aprobacion_id,),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        bd.execute(
            "UPDATE acervo.aprobacion_en_bloque SET confirmada_el = '2026-10-17 09:00:00+00'"
            " WHERE aprobacion_id = %s",
            (aprobacion_id,),
        )


@pytest.mark.rf("RF-15")
@pytest.mark.parametrize(
    "columnas",
    [
        {"borrada_el": "now()"},
        {"borrada_el": "now()", "borrada_por": f"'{MARIA}'"},
        {"borrada_por": f"'{MARIA}'", "borrada_motivo": "'Duplicada'"},
    ],
    ids=["solo-cuando", "sin-motivo", "sin-cuando"],
)
def test_el_retiro_se_traza_entero_o_no_se_guarda(bd, columnas):
    pieza_id = _insertar_pieza(bd)
    asignaciones = ", ".join(f"{columna} = {valor}" for columna, valor in columnas.items())

    with pytest.raises(psycopg.errors.CheckViolation, match="pieza_borrado_trazado"):
        bd.execute(f"UPDATE acervo.pieza SET {asignaciones} WHERE pieza_id = %s", (pieza_id,))


@pytest.mark.rf("RF-15")
def test_un_original_retirado_se_puede_volver_a_subir(bd):
    retirada = _insertar_pieza(bd, huella=HUELLA)
    bd.execute(
        "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
        " borrada_motivo = 'Subida por error' WHERE pieza_id = %s",
        (MARIA, retirada),
    )

    _insertar_pieza(bd, huella=HUELLA)


@pytest.mark.rf("RF-15")
def test_los_indices_de_trabajo_dejan_afuera_a_las_retiradas(bd):
    indices = dict(
        bd.execute(
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = 'acervo' AND tablename = 'pieza'"
        ).fetchall()
    )

    parciales = {
        "pieza_cola_estado", "pieza_cola_usuario", "pieza_trabajada", "pieza_huella_unica",
        "pieza_rango_fechas",
    }
    assert {nombre for nombre in parciales if "borrada_el IS NULL" in indices[nombre]} == parciales
    assert "USING gist (rango_fechas)" in indices["pieza_rango_fechas"]


def _ficha(bd: psycopg.Connection, *, vigente: bool = True) -> str:
    pieza_id = _insertar_pieza(bd)
    fila = bd.execute(
        "INSERT INTO acervo.ficha (pieza_id, version, contenido_markdown, origen_version,"
        " configuracion_efectiva, informe_inyeccion, vigente)"
        " VALUES (%s, 1, '# Acta', 'modelo', '{}', '{}', %s)"
        " RETURNING ficha_id::text",
        (pieza_id, vigente),
    ).fetchone()
    assert fila is not None
    return str(fila[0])


@pytest.mark.rf("RF-10")
@pytest.mark.parametrize("origen", ["agente_externo", "sistema"])
def test_la_ficha_admite_las_versiones_del_agente_y_del_sistema(bd, origen):
    # DEC-12, DEC-122: la v1 de un Aporte y la de «Completar a mano» o un video.
    bd.execute(
        "INSERT INTO acervo.ficha (pieza_id, version, contenido_markdown, origen_version,"
        " informe_inyeccion) VALUES (%s, 1, '# Acta', %s, '{}')",
        (_insertar_pieza(bd), origen),
    )

    with pytest.raises(psycopg.errors.CheckViolation, match="origen_version"):
        bd.execute(
            "INSERT INTO acervo.ficha (pieza_id, version, contenido_markdown, origen_version,"
            " informe_inyeccion) VALUES (%s, 1, '# Acta', 'otro', '{}')",
            (_insertar_pieza(bd),),
        )


@pytest.mark.rf("RF-10")
def test_una_version_sin_informe_de_inyeccion_no_se_guarda(bd):
    # Sin default: un camino que olvidara `chequeo_inyeccion` falla en vez de guardar «sin
    # alerta» (DEC-26).
    with pytest.raises(psycopg.errors.NotNullViolation, match="informe_inyeccion"):
        bd.execute(
            "INSERT INTO acervo.ficha (pieza_id, version, contenido_markdown, origen_version,"
            " configuracion_efectiva) VALUES (%s, 1, '# Acta', 'modelo', '{}')",
            (_insertar_pieza(bd),),
        )


@pytest.mark.historia(49)
def test_una_ficha_vigente_puede_dejar_de_serlo(bd):
    ficha_id = _ficha(bd)

    bd.execute("UPDATE acervo.ficha SET vigente = false WHERE ficha_id = %s", (ficha_id,))

    assert bd.execute("SELECT vigente FROM acervo.ficha").fetchone() == (False,)


@pytest.mark.historia(49)
@pytest.mark.parametrize(
    ("vigente", "cambio"),
    [
        (True, "contenido_markdown = '# Otra'"),
        (True, "configuracion_efectiva = NULL"),
        (True, "editada_el = now() - interval '1 day'"),
        (True, "vigente = false, contenido_markdown = '# Otra'"),
        (True, "vigente = true"),
        (False, "vigente = true"),
        (False, "vigente = false"),
    ],
    ids=[
        "contenido", "configuracion", "fecha", "baja-y-contenido", "vigente-sin-cambio",
        "vuelve-a-vigente", "no-vigente-sin-cambio",
    ],
)
def test_el_trigger_rechaza_todo_otro_update_de_una_ficha(bd, vigente, cambio):
    ficha_id = _ficha(bd, vigente=vigente)

    with pytest.raises(psycopg.errors.RestrictViolation, match="ficha_inmutable"):
        bd.execute(f"UPDATE acervo.ficha SET {cambio} WHERE ficha_id = %s", (ficha_id,))



# 167: la base impone las transiciones del dominio (S-BD-11, ARQ-09).
@pytest.mark.historia(49)
def test_la_base_permite_exactamente_las_transiciones_del_dominio(bd):
    permitidas = set()
    for desde in Estado:
        for hacia in set(Estado) - {desde}:
            fila = bd.execute(
                "SELECT acervo.transicion_permitida(%s, %s)", (desde.value, hacia.value)
            ).fetchone()
            assert fila is not None
            if fila[0]:
                permitidas.add((desde, hacia))

    assert permitidas == TRANSICIONES


def _pieza_en(bd: psycopg.Connection, estado: str) -> str:
    if estado == "Validada":
        return _pieza_con(bd, "Validada", NUEVE_OBLIGATORIOS)
    return _insertar_pieza(bd, estado=estado)


@pytest.mark.historia(49)
@pytest.mark.parametrize(
    ("desde", "hacia"),
    [("Recibida", "Pendiente"), ("Pendiente", "Incompleta"), ("Validada", "Incompleta")],
)
def test_una_transicion_del_dominio_pasa_por_sql(bd, desde, hacia):
    pieza_id = _pieza_en(bd, desde)

    bd.execute(
        "UPDATE acervo.pieza SET estado = %s, modo_validacion = NULL WHERE pieza_id = %s",
        (hacia, pieza_id),
    )

    assert bd.execute(
        "SELECT estado FROM acervo.pieza WHERE pieza_id = %s", (pieza_id,)
    ).fetchone() == (hacia,)


@pytest.mark.historia(49)
@pytest.mark.parametrize(
    ("desde", "hacia"),
    [
        ("Validada", "Recibida"),
        ("Validada", "Pendiente"),
        ("Incompleta", "Pendiente"),
        ("Pendiente", "Recibida"),
        ("Recibida", "Validada"),
        ("Recibida", "Incompleta"),
    ],
)
def test_una_transicion_que_el_dominio_no_permite_la_rechaza_la_base(bd, desde, hacia):
    pieza_id = _pieza_en(bd, desde)

    with pytest.raises(psycopg.errors.RestrictViolation, match="pieza_transicion"):
        bd.execute(
            "UPDATE acervo.pieza SET estado = %s WHERE pieza_id = %s", (hacia, pieza_id)
        )


@pytest.mark.historia(49)
def test_retirar_y_restaurar_una_validada_no_es_una_transicion(bd):
    pieza_id = _pieza_en(bd, "Validada")

    bd.execute(
        "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
        " borrada_motivo = 'Retirada en la prueba' WHERE pieza_id = %s",
        (MARIA, pieza_id),
    )
    bd.execute(
        "UPDATE acervo.pieza SET estado = estado, borrada_el = NULL, borrada_por = NULL,"
        " borrada_motivo = NULL WHERE pieza_id = %s",
        (pieza_id,),
    )

    assert bd.execute(
        "SELECT estado, borrada_el FROM acervo.pieza WHERE pieza_id = %s", (pieza_id,)
    ).fetchone() == ("Validada", None)


# 029: la cola por tipo (spec §1.9). Los índices y el trigger cortan contra la base.
def _trabajo(
    bd: psycopg.Connection,
    pieza_id: str,
    *,
    tipo: str = "procesamiento",
    estado: str = "encolado",
    ficha_id: str | None = None,
) -> tuple[int, int]:
    fila = bd.execute(
        "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, estado, ficha_id,"
        " tiempo_maximo_ms, configuracion_efectiva, error)"
        " VALUES (%s, %s, %s, %s, 1000, '{}', %s) RETURNING prioridad, tope_generaciones",
        (pieza_id, tipo, estado, ficha_id, "x" if estado == "fallido" else None),
    ).fetchone()
    assert fila is not None
    return fila


def _pieza_de(bd: psycopg.Connection, ficha_id: str) -> str:
    fila = bd.execute(
        "SELECT pieza_id::text FROM acervo.ficha WHERE ficha_id = %s", (ficha_id,)
    ).fetchone()
    assert fila is not None
    return str(fila[0])


@pytest.mark.historia(232)
def test_un_trabajo_nace_con_prioridad_0_y_tope_de_dos_generaciones(bd):
    assert _trabajo(bd, _insertar_pieza(bd)) == (0, 2)


@pytest.mark.historia(215)
@pytest.mark.parametrize(
    "pieza",
    [
        dict(estado="Pendiente", clase="video", mime="video/mp4"),
        dict(estado="Pendiente", clase="texto", via="aporte", mime="text/plain"),
    ],
    ids=["video", "aporte"],
)
def test_lo_que_no_tiene_plan_no_entra_a_la_cola(bd, pieza):
    pieza_id = _insertar_pieza(bd, **pieza)

    with pytest.raises(psycopg.errors.CheckViolation, match="trabajo_sin_plan"):
        _trabajo(bd, pieza_id)


@pytest.mark.historia(215)
def test_el_txt_que_sube_una_persona_entra_a_la_cola(bd):
    # Decisión del usuario, 028: el `.txt` subido tiene plan (solo `resumir`).
    _trabajo(bd, _insertar_pieza(bd, clase="texto", mime="text/plain; charset=utf-8"))


@pytest.mark.historia(232)
@pytest.mark.parametrize("estado_previo", ["encolado", "en_curso", "completado"])
def test_un_solo_procesamiento_activo_o_completado_por_pieza(bd, estado_previo):
    pieza_id = _insertar_pieza(bd)
    _trabajo(bd, pieza_id, estado=estado_previo)

    with pytest.raises(psycopg.errors.UniqueViolation, match="trabajo_procesamiento_unico"):
        _trabajo(bd, pieza_id)


@pytest.mark.historia(232)
def test_un_procesamiento_fallido_deja_lugar_a_otro(bd):
    pieza_id = _insertar_pieza(bd)
    _trabajo(bd, pieza_id, estado="fallido")

    _trabajo(bd, pieza_id)


@pytest.mark.historia(215)
def test_una_sola_indexacion_activa_por_pieza_y_reindexar_una_completada(bd):
    ficha_id = _ficha(bd)
    pieza_id = _pieza_de(bd, ficha_id)
    _trabajo(bd, pieza_id, tipo="indexacion", estado="completado", ficha_id=ficha_id)
    # Reindexar es legítimo, y un procesamiento de la misma Pieza no cuenta.
    _trabajo(bd, pieza_id, tipo="indexacion", ficha_id=ficha_id)
    _trabajo(bd, pieza_id, estado="completado")

    with pytest.raises(psycopg.errors.UniqueViolation, match="trabajo_indexacion_activo"):
        _trabajo(bd, pieza_id, tipo="indexacion", ficha_id=ficha_id)


@pytest.mark.historia(215)
@pytest.mark.parametrize(("tipo", "con_ficha"), [("indexacion", False), ("procesamiento", True)])
def test_solo_la_indexacion_lleva_ficha(bd, tipo, con_ficha):
    ficha_id = _ficha(bd)

    with pytest.raises(psycopg.errors.CheckViolation, match="trabajo_ficha_solo_indexacion"):
        _trabajo(
            bd, _pieza_de(bd, ficha_id), tipo=tipo, ficha_id=ficha_id if con_ficha else None
        )


@pytest.mark.historia(215)
def test_la_cola_se_lee_por_tipo_prioridad_y_llegada(bd):
    indices = dict(
        bd.execute(
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = 'acervo' AND tablename = 'trabajo_procesamiento'"
        ).fetchall()
    )

    assert sorted(indices) == [
        "trabajo_arriendo",
        "trabajo_cola",
        "trabajo_descripcion_unica",
        "trabajo_indexacion_activo",
        "trabajo_procesamiento_pkey",
        "trabajo_procesamiento_unico",
        "trabajo_resumen_encolado",
    ]
    assert "(tipo, prioridad, encolado_el, trabajo_id) WHERE (estado = 'encolado'" in (
        indices["trabajo_cola"]
    )


@pytest.mark.historia(232)
def test_la_0008_sube_a_dos_el_tope_de_los_trabajos_activos(base_vacia, tmp_path):
    for ruta in sorted(DIRECTORIO.glob("000[1-7]_*.sql")):
        _escribir(tmp_path, ruta.name, ruta.read_text(encoding="utf-8"))
    aplicar(base_vacia, directorio=tmp_path)
    with psycopg.connect(base_vacia) as conexion:
        for estado in ("encolado", "en_curso", "fallido"):
            # Como nacía un trabajo antes de la 0008: con tope 1.
            conexion.execute(
                "INSERT INTO acervo.trabajo_procesamiento (pieza_id, estado, tipo,"
                " tiempo_maximo_ms, configuracion_efectiva, error)"
                " VALUES (%s, %s, 'procesamiento', 1000, '{}', 'x')",
                (_insertar_pieza(conexion), estado),
            )

    aplicar(base_vacia)

    with psycopg.connect(base_vacia) as conexion:
        topes = dict(
            conexion.execute(
                "SELECT estado, tope_generaciones FROM acervo.trabajo_procesamiento"
            ).fetchall()
        )
    assert topes == {"encolado": 2, "en_curso": 2, "fallido": 1}


# 123: `pieza.publicable` y `replicacion_drive` (spec §1.23, R-26). Las dos se activan con un
# tercero (C31 y C14/C34); el esquema ya está.
@pytest.mark.historia(243)
@pytest.mark.rf("RF-42")
def test_publicable_y_replicacion_drive_existen_tras_migrar(bd):
    pieza = _insertar_pieza(bd)
    [(publicable,)] = bd.execute(
        "SELECT publicable FROM acervo.pieza WHERE pieza_id = %s", (pieza,)
    ).fetchall()
    [(cola,)] = bd.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'replicacion_cola'"
    ).fetchall()
    for estado in ("pendiente", "subiendo", "fallido"):
        bd.execute(
            "INSERT INTO acervo.replicacion_drive (pieza_id, estado) VALUES (%s, %s)"
            " ON CONFLICT (pieza_id) DO UPDATE SET estado = EXCLUDED.estado",
            (pieza, estado),
        )
    bd.execute(
        "UPDATE acervo.replicacion_drive SET estado = 'replicado', drive_file_id = 'x'"
        " WHERE pieza_id = %s",
        (pieza,),
    )

    assert publicable is True
    assert cola.endswith(
        "(proximo_intento) WHERE (estado = ANY (ARRAY['pendiente'::text, 'fallido'::text]))"
    )
    for cambio, restriccion in [
        ("estado = 'borrado'", "replicacion_drive_estado_check"),
        ("drive_file_id = NULL", "replicacion_drive_replicado_con_archivo"),
        ("estado = 'fallido'", "replicacion_drive_replicado_con_archivo"),
        ("intentos = -1", "replicacion_drive_intentos_check"),
    ]:
        bd.execute("SAVEPOINT antes")
        with pytest.raises(psycopg.errors.CheckViolation, match=restriccion):
            bd.execute(
                f"UPDATE acervo.replicacion_drive SET {cambio} WHERE pieza_id = %s", (pieza,)
            )
        bd.execute("ROLLBACK TO SAVEPOINT antes")
    with pytest.raises(psycopg.errors.NotNullViolation, match="publicable"):
        bd.execute("UPDATE acervo.pieza SET publicable = NULL WHERE pieza_id = %s", (pieza,))


@pytest.mark.historia(243)
@pytest.mark.rf("RF-42")
def test_migrar_publicable_y_drive_sobre_una_base_con_datos_no_pierde_nada(base_vacia, tmp_path):
    rutas = sorted(DIRECTORIO.glob("*.sql"))
    [propia] = [r for r in rutas if r.name.endswith("_publicable_y_drive.sql")]
    for ruta in rutas[: rutas.index(propia)]:
        _escribir(tmp_path, ruta.name, ruta.read_text(encoding="utf-8"))
    aplicar(base_vacia, directorio=tmp_path)
    with psycopg.connect(base_vacia) as conexion:
        _insertar_pieza(conexion)
        _insertar_pieza(conexion, huella=HUELLA)
        antes = conexion.execute(
            "SELECT to_jsonb(p) FROM acervo.pieza p ORDER BY pieza_id"
        ).fetchall()

    aplicar(base_vacia)

    with psycopg.connect(base_vacia) as conexion:
        despues = conexion.execute(
            # 152 suma columnas a `pieza` después: no son de esta migración.
            "SELECT to_jsonb(p) - 'publicable' - 'campos_de_la_persona' - 'contexto_de_la_persona',"
            " publicable FROM acervo.pieza p ORDER BY pieza_id"
        ).fetchall()
    assert [fila for fila, _ in despues] == [fila for (fila,) in antes]
    assert {publicable for _, publicable in despues} == {True}


# 044: la tabla del índice (spec §1.8). Un fragmento sin las claves de los filtros no se guarda.
METADATA_COMPLETA = (
    '{"pieza_id": "p", "ficha_id": "f", "tipo": "acta", "tipo_actividad": "Transferencia",'
    ' "carreras": [], "fecha_inicio": "1970-01-01", "fecha_fin": "1979-12-31",'
    ' "modelo_embedding": "m@0123456789ab", "via_ingreso": "archivo"}'
)


def _fragmento(bd: psycopg.Connection, metadata: str, *, dimension: int = 1024) -> None:
    bd.execute(
        "INSERT INTO acervo.data_fragmento (node_id, text, metadata_, embedding)"
        " VALUES (%s, 'texto', %s::jsonb, %s::vector)",
        (str(uuid4()), metadata, "[" + ",".join(["0.5"] * dimension) + "]"),
    )


@pytest.mark.historia(213)
@pytest.mark.rnf("RNF-08")
def test_la_tabla_del_indice_guarda_un_fragmento_completo_de_1024(bd):
    _fragmento(bd, METADATA_COMPLETA)

    indices = bd.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'data_fragmento' ORDER BY 1"
    ).fetchall()
    # Búsqueda exacta: ningún índice vectorial (DEC-07).
    assert indices == [
        ("data_fragmento_node_id_key",), ("data_fragmento_pkey",), ("fragmento_carreras",),
        ("fragmento_modelo",), ("fragmento_pieza",),
    ]


@pytest.mark.rnf("RNF-10")
def test_el_filtro_de_carrera_tiene_su_gin(bd):
    """056: sin él, filtrar por Carrera recorre todo el índice (medido con 28 000 fragmentos)."""
    [(definicion,)] = bd.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'fragmento_carreras'"
    ).fetchall()
    assert "USING gin (((metadata_ -> 'carreras'::text)))" in definicion
    bd.execute("SET LOCAL enable_seqscan = off")  # con la tabla vacía el plan prefiere recorrerla
    plan = " ".join(fila for (fila,) in bd.execute(
        "EXPLAIN SELECT id FROM acervo.data_fragmento"
        " WHERE metadata_ -> 'carreras' ?| array['ing-informatica']"
    ).fetchall())
    assert "fragmento_carreras" in plan


@pytest.mark.historia(213)
@pytest.mark.rnf("RNF-08")
@pytest.mark.parametrize(
    ("metadata", "dimension", "error"),
    [
        (METADATA_COMPLETA.replace('"tipo_actividad"', '"actividad"'), 1024,
         psycopg.errors.CheckViolation),
        (METADATA_COMPLETA.replace('"carreras": []', '"carreras": "ing-informatica"'), 1024,
         psycopg.errors.CheckViolation),
        (METADATA_COMPLETA.replace('"archivo"', '"correo"'), 1024,
         psycopg.errors.CheckViolation),
        (METADATA_COMPLETA, 384, psycopg.errors.DataException),
    ],
    ids=["falta-una-clave", "carreras-no-es-lista", "via-de-ingreso-desconocida", "vector-de-384"],
)
def test_la_tabla_del_indice_rechaza_un_fragmento_incompleto(bd, metadata, dimension, error):
    with pytest.raises(error):
        _fragmento(bd, metadata, dimension=dimension)


# Permisos de `hv_app` por tabla (spec, «Roles y permisos»; DEC-105). Ninguna tiene TRUNCATE ni
# REFERENCES, y solo el índice tiene DELETE: es derivado y reconstruible (DEC-143). `ficha` además
# tiene UPDATE solo en la columna `vigente`.
LECTURA_Y_ESCRITURA = {"SELECT", "INSERT", "UPDATE"}
PERMISOS_DE_HV_APP = {
    "usuario": LECTURA_Y_ESCRITURA,
    "sesion": LECTURA_Y_ESCRITURA,
    "pieza": LECTURA_Y_ESCRITURA,
    "lote": LECTURA_Y_ESCRITURA,
    "trabajo_procesamiento": LECTURA_Y_ESCRITURA,
    "medicion_validacion": LECTURA_Y_ESCRITURA,
    "ficha": {"SELECT", "INSERT"},
    "configuracion_capacidad": {"SELECT", "UPDATE"},
    # Los catálogos los cambia una migración (DEC-105), salvo las Carreras, que carga
    # `catalogo carreras --cargar` (022, R-25) sin borrar.
    "tipo_pieza": {"SELECT"},
    "tipo_actividad": {"SELECT"},
    "carrera": LECTURA_Y_ESCRITURA,
    # Quitar una Carrera marca `quitada_el` (024, DEC-143): UPDATE solo en esa columna.
    "pieza_carrera": {"SELECT", "INSERT"},
    "lote_carrera": {"SELECT", "INSERT"},
    "v_lote_resumen": {"SELECT"},
    "v_descripcion_en_curso": {"SELECT"},
    "auditoria": {"SELECT", "INSERT"},
    "data_fragmento": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    # Append-only, como la auditoría: el historial del freno no lo reescribe ni la aplicación.
    "evento_interruptor": {"SELECT", "INSERT"},
    # Revocar es marcar (096, DEC-32): UPDATE solo en último uso y revocación.
    "agente_externo": {"SELECT", "INSERT"},
    "token_agente": {"SELECT", "INSERT"},
    # Cada trabajador reescribe su fila (035, DEC-106); nadie la borra.
    "latido_trabajador": LECTURA_Y_ESCRITURA,
    # La Conversación cambia de título, estado y turno en curso, nunca de dueña (UPDATE por
    # columna); sus mensajes y Citas son append-only y nadie borra ninguna de las tres (051).
    "conversacion": {"SELECT", "INSERT"},
    "mensaje_conversacion": {"SELECT", "INSERT"},
    "mensaje_cita": {"SELECT", "INSERT"},
    # El Aporte y sus fuentes no cambian después de recibidos (100, DEC-146 punto 8).
    "aporte": {"SELECT", "INSERT"},
    "aporte_fragmento_fuente": {"SELECT", "INSERT"},
    "aporte_pieza_fuente": {"SELECT", "INSERT"},
    # La efeméride cambia de texto y de estado y la programación es una fila que se edita (UPDATE
    # por columna); las fuentes son append-only y nadie borra ninguna de las tres (057, R-24).
    "efemeride": {"SELECT", "INSERT"},
    "efemeride_fuente": {"SELECT", "INSERT"},
    "programacion_efemerides": {"SELECT"},
    # El Relato cambia de texto y de estado (UPDATE por columna); sus fuentes son append-only y
    # nadie borra (064, R-24).
    "relato": {"SELECT", "INSERT"},
    "relato_fuente": {"SELECT", "INSERT"},
    # El informe cambia de estado y de marcas, nunca de contenido; lo congelado y las
    # aprobaciones son append-only y nadie borra (066, R-24).
    "informe": {"SELECT", "INSERT"},
    "informe_pieza": {"SELECT", "INSERT"},
    "informe_aprobacion": {"SELECT", "INSERT"},
    # La previa la abre y la confirmación la cierra; nadie la borra (108, DEC-146 punto 8).
    "aprobacion_en_bloque": LECTURA_Y_ESCRITURA,
    # La réplica avanza de estado y reintenta sobre su fila; nadie la borra (123, ADR-0004).
    "replicacion_drive": LECTURA_Y_ESCRITURA,
    # Lo propuesto no cambia; la decisión de cada Afirmación es UPDATE por columna (144).
    "descripcion_de_imagen": {"SELECT", "INSERT"},
    "afirmacion": {"SELECT", "INSERT"},
    # Cada resumen rehecho es una fila nueva: no se corrige ni se borra (146).
    "resumen_rehecho": {"SELECT", "INSERT"},
}


@pytest.mark.rf("RF-53")
def test_una_persona_tiene_uno_de_los_cuatro_roles_del_director(bd):
    for rol in ("rgc", "coordinador", "director_carrera", "admin_tecnico"):
        bd.execute(
            "INSERT INTO acervo.usuario (email, nombre, rol) VALUES (%s, 'X', %s)",
            (f"{rol}@fie", rol),
        )

    # Los dos del Prototipo ya no existen (DEC-124).
    with pytest.raises(psycopg.errors.CheckViolation, match="usuario_rol_check"):
        bd.execute(
            "INSERT INTO acervo.usuario (email, nombre, rol) VALUES ('a@fie', 'A', 'administrador')"
        )


@pytest.mark.rf("RF-53")
def test_una_persona_sin_rol_no_se_guarda(bd):
    # Sin valor por defecto: dar de alta obliga a elegir el rol.
    with pytest.raises(psycopg.errors.NotNullViolation, match="rol"):
        bd.execute("INSERT INTO acervo.usuario (email, nombre) VALUES ('s@fie', 'S')")


@pytest.mark.historia(48)
@pytest.mark.rf("RF-45")
def test_hv_app_tiene_solo_los_permisos_de_la_lista(bd):
    filas = bd.execute(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
        " WHERE table_schema = 'acervo' AND grantee = 'hv_app'"
    ).fetchall()
    permisos: dict[str, set[str]] = {}
    for tabla, privilegio in filas:
        permisos.setdefault(tabla, set()).add(privilegio)
    (
        columnas_de_ficha,
        columnas_de_pieza_carrera,
        columnas_de_lote_carrera,
        columnas_de_token,
        columnas_de_conversacion,
        columnas_de_efemeride,
        columnas_de_programacion,
        columnas_de_informe,
        columnas_de_relato,
    ) = (
        bd.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass"
            " AND attnum > 0 AND has_column_privilege('hv_app', attrelid, attnum, 'UPDATE')",
            (tabla,),
        ).fetchall()
        for tabla in (
            "acervo.ficha", "acervo.pieza_carrera", "acervo.lote_carrera", "acervo.token_agente",
            "acervo.conversacion", "acervo.efemeride", "acervo.programacion_efemerides",
            "acervo.informe", "acervo.relato",
        )
    )
    por_defecto = bd.execute("SELECT count(*) FROM pg_default_acl").fetchone()

    assert permisos == PERMISOS_DE_HV_APP
    assert columnas_de_ficha == [("vigente",)]
    assert columnas_de_pieza_carrera == [("quitada_el",)]
    assert columnas_de_lote_carrera == [("quitada_el",)]
    assert sorted(columnas_de_token) == [("revocado_el",), ("revocado_por",), ("ultimo_uso_el",)]
    assert sorted(columnas_de_conversacion) == [
        ("actualizada_el",), ("estado",), ("titulo",), ("turno_desde",)
    ]
    # Nunca el objetivo, la difusión, las fuentes ni la configuración con que se generó (057).
    assert {c for (c,) in columnas_de_efemeride} == {
        "titulo", "cuerpo", "estado", "version", "editada_por", "editada_el", "aprobada_por",
        "aprobada_el", "descartada_por", "descartada_el", "descarte_motivo", "fuente_retirada_el",
        "metodo_autenticacion",
    }
    # La fila se edita, pero nunca su id (057).
    assert {c for (c,) in columnas_de_programacion} == {
        "habilitada", "periodicidad", "anticipacion_dias", "maximo_por_corrida", "alcances",
        "ultima_corrida_el", "pedido_manual_el", "pedido_manual_objetivo", "pedido_manual_por",
        "version", "actualizada_por", "actualizada_el", "ultima_corrida_resultado",
        "ultima_programada_el",
    }
    # La integridad se recalcula al exportar (067); el contenido, nunca.
    assert sorted(columnas_de_informe) == [
        ("aprobado_el",), ("descartado_el",), ("descartado_por",), ("estado",),
        ("exportado_el",), ("exportado_por",), ("integridad",), ("reemplazado_por",),
    ]
    # Nunca la respuesta de origen, quién lo propuso ni la configuración (064).
    assert {c for (c,) in columnas_de_relato} == {
        "titulo", "cuerpo", "estado", "version", "editado_por", "editado_el", "aprobado_por",
        "aprobado_el", "descartado_por", "descartado_el", "descarte_motivo", "fuente_retirada_el",
        "metodo_autenticacion",  # la firma (065)
    }
    # Nada de ALTER DEFAULT PRIVILEGES: concedería en silencio lo que se olvidó revocar.
    assert por_defecto == (0,)


@pytest.mark.historia(48)
@pytest.mark.rf("RF-45")
def test_hv_app_registra_en_la_auditoria(url_app):
    with psycopg.connect(url_app) as conexion:
        fila = conexion.execute(
            "INSERT INTO auditoria (actor_tipo, actor_etiqueta, accion, objeto_tipo)"
            " VALUES ('cli', 'operador', 'persona.alta', 'usuario') RETURNING id"
        ).fetchone()
        conexion.rollback()

    assert fila is not None


def _migrar(*argumentos: str, **entorno: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "historia_viva.composicion", "migrar", *argumentos],
        env={**os.environ, **entorno},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.historia(49)
def test_quien_opera_migra_por_comando(base_vacia):
    migrada = _migrar(HV_BD_URL_MIGRADOR=base_vacia)
    de_nuevo = _migrar(HV_BD_URL_MIGRADOR=base_vacia)

    assert migrada.returncode == 0, migrada.stderr
    assert "0001_baseline_paridad" in migrada.stdout
    assert de_nuevo.returncode == 0, de_nuevo.stderr
    assert "0001" not in de_nuevo.stdout
    # Cada ticket suma su migración: se comparan las del disco, no una lista fija.
    assert _versiones(base_vacia) == sorted(
        Migracion.leer(ruta).version for ruta in DIRECTORIO.glob("*.sql")
    )


@pytest.mark.rf("RF-53")
def test_solo_en_desarrollo_migrar_siembra_una_persona_de_prueba_por_rol(base_vacia):
    produccion = _migrar(HV_BD_URL_MIGRADOR=base_vacia, HV_ENTORNO="")
    with psycopg.connect(base_vacia) as conexion:
        sin_personas = conexion.execute("SELECT count(*) FROM acervo.usuario").fetchone()
    _migrar(HV_BD_URL_MIGRADOR=base_vacia, HV_ENTORNO="desarrollo")
    de_nuevo = _migrar(HV_BD_URL_MIGRADOR=base_vacia, HV_ENTORNO="desarrollo")

    assert produccion.returncode == 0, produccion.stderr
    # Fuera de desarrollo la base nace sin personas (DEC-52).
    assert sin_personas == (0,)
    assert de_nuevo.returncode == 0, de_nuevo.stderr
    with psycopg.connect(base_vacia) as conexion:
        roles = conexion.execute(
            "SELECT rol, count(*) FROM acervo.usuario GROUP BY rol ORDER BY rol"
        ).fetchall()
        firma = conexion.execute(
            "SELECT rol FROM acervo.usuario WHERE usuario_id = %s", (MARIA,)
        ).fetchone()
    assert roles == [
        ("admin_tecnico", 1), ("coordinador", 1), ("director_carrera", 1), ("rgc", 1),
    ]
    # La identidad RGC de prueba con la que se firma en desarrollo (DEC-124).
    assert firma == ("rgc",)


@pytest.mark.historia(49)
def test_migrar_sin_su_dsn_falla_con_un_mensaje_claro():
    resultado = _migrar(HV_BD_URL_MIGRADOR="")

    assert resultado.returncode == 2
    assert "HV_BD_URL_MIGRADOR" in resultado.stderr


@pytest.mark.historia(49)
def test_la_ayuda_de_migrar_no_pide_el_dsn():
    resultado = _migrar("--help", HV_BD_URL_MIGRADOR="")

    assert resultado.returncode == 0, resultado.stderr
    assert "hv_migrador" in resultado.stdout


def _informe(bd: psycopg.Connection, huella: str = HUELLA) -> str:
    fila = bd.execute(
        "INSERT INTO acervo.informe (desde, hasta, huella, generado_por)"
        " VALUES ('2018-01-01', '2022-12-31', %s, %s) RETURNING informe_id::text",
        (huella, JORGE),
    ).fetchone()
    assert fila is not None
    return str(fila[0])


def _aprobar(bd: psycopg.Connection, informe: str, persona: str, rol: str, huella: str = HUELLA):
    bd.execute(
        "INSERT INTO acervo.informe_aprobacion"
        " (informe_id, rol, aprobada_por, metodo_autenticacion, huella)"
        " VALUES (%s, %s, %s, 'dev', %s)",
        (informe, rol, persona, huella),
    )


@pytest.mark.rf("RF-49")
@pytest.mark.parametrize(
    ("persona", "rol"), [(MARIA, "coordinador"), (DIRECTORA_DE_CARRERA, "rgc")],
    ids=["rol-ajeno", "sin-ese-rol"],
)
def test_una_aprobacion_lleva_el_rol_vigente_de_quien_aprueba(bd, persona, rol):
    informe = _informe(bd)

    with pytest.raises(psycopg.errors.RestrictViolation, match="informe_aprobacion_rol"):
        _aprobar(bd, informe, persona, rol)


@pytest.mark.rf("RF-49")
def test_un_informe_reemplazado_no_admite_aprobaciones(bd):
    viejo, nuevo = _informe(bd), _informe(bd)
    bd.execute(
        "UPDATE acervo.informe SET reemplazado_por = %s WHERE informe_id = %s", (nuevo, viejo)
    )

    with pytest.raises(psycopg.errors.RestrictViolation, match="informe_aprobacion_rol"):
        _aprobar(bd, viejo, MARIA, "rgc")


@pytest.mark.rf("RF-49")
@pytest.mark.parametrize(
    "aprobaciones",
    [
        [(JORGE, "coordinador", HUELLA)],
        [(JORGE, "coordinador", HUELLA), (MARIA, "rgc", "b" * 64)],
    ],
    ids=["una-sola", "otra-huella"],
)
def test_pasar_a_aprobado_exige_un_rgc_y_un_coordinador_sobre_la_misma_huella(
    bd, aprobaciones
):
    informe = _informe(bd)
    for persona, rol, huella in aprobaciones:
        _aprobar(bd, informe, persona, rol, huella)

    with pytest.raises(psycopg.errors.RestrictViolation, match="informe_aprobado_dos_firmas"):
        bd.execute(
            "UPDATE acervo.informe SET estado = 'aprobado', aprobado_el = now()"
            " WHERE informe_id = %s",
            (informe,),
        )


@pytest.mark.rf("RF-49")
def test_un_informe_aprobado_no_cambia_su_contenido(bd):
    informe = _informe(bd)
    _aprobar(bd, informe, JORGE, "coordinador")
    _aprobar(bd, informe, MARIA, "rgc")
    bd.execute(
        "UPDATE acervo.informe SET estado = 'aprobado', aprobado_el = now()"
        " WHERE informe_id = %s",
        (informe,),
    )

    with pytest.raises(psycopg.errors.RestrictViolation, match="informe_aprobado_dos_firmas"):
        bd.execute(
            "UPDATE acervo.informe SET hasta = '2023-12-31' WHERE informe_id = %s", (informe,)
        )


def _aprobado(bd: psycopg.Connection) -> str:
    informe = _informe(bd)
    _aprobar(bd, informe, JORGE, "coordinador")
    _aprobar(bd, informe, MARIA, "rgc")
    bd.execute(
        "UPDATE acervo.informe SET estado = 'aprobado', aprobado_el = now()"
        " WHERE informe_id = %s",
        (informe,),
    )
    return informe


@pytest.mark.rf("RF-49")
@pytest.mark.parametrize("firmas", [1, 2], ids=["con-una-firma", "aprobado"])
def test_lo_congelado_de_un_informe_firmado_no_suma_piezas(bd, firmas):
    if firmas == 2:
        informe = _aprobado(bd)
    else:
        informe = _informe(bd)
        _aprobar(bd, informe, JORGE, "coordinador")

    # El trigger rechaza antes que las claves foráneas: los ids no necesitan existir.
    with pytest.raises(psycopg.errors.RestrictViolation, match="informe_pieza_de_borrador"):
        bd.execute(
            "INSERT INTO acervo.informe_pieza (informe_id, pieza_id, ficha_id, tipo_actividad,"
            " fecha_inicio, fecha_fin, orden) VALUES (%s, gen_random_uuid(), gen_random_uuid(),"
            " 'Transferencia', '2019-01-01', '2019-01-01', 1)",
            (informe,),
        )


@pytest.mark.rf("RF-49")
def test_un_aprobado_no_cambia_cuando_se_aprobo(bd):
    informe = _aprobado(bd)

    with pytest.raises(psycopg.errors.RestrictViolation, match="informe_aprobado_dos_firmas"):
        bd.execute(
            "UPDATE acervo.informe SET aprobado_el = aprobado_el - interval '1 day'"
            " WHERE informe_id = %s",
            (informe,),
        )


@pytest.mark.rf("RF-49")
@pytest.mark.parametrize("columna", ["exportado_el", "exportado_por", "aprobado_el"])
def test_un_exportado_no_cambia_sus_marcas(bd, columna):
    informe = _aprobado(bd)
    bd.execute(
        "UPDATE acervo.informe SET estado = 'exportado', exportado_el = now(),"
        " exportado_por = %s WHERE informe_id = %s",
        (JORGE, informe),
    )
    nuevo = {"exportado_el": "now() + interval '1 day'", "exportado_por": f"'{MARIA}'",
             "aprobado_el": "now() + interval '1 day'"}[columna]

    with pytest.raises(psycopg.errors.RestrictViolation, match="informe_aprobado_dos_firmas"):
        bd.execute(
            f"UPDATE acervo.informe SET {columna} = {nuevo} WHERE informe_id = %s",  # type: ignore[arg-type]
            (informe,),
        )


@pytest.mark.rf("RF-49")
def test_un_informe_no_lleva_dos_aprobaciones_del_mismo_rol(bd):
    informe = _informe(bd)
    fila = bd.execute(
        "INSERT INTO acervo.usuario (email, nombre, rol)"
        " VALUES ('otro.coordinador@fie.undef.edu.ar', 'Otro', 'coordinador')"
        " RETURNING usuario_id::text"
    ).fetchone()
    assert fila is not None
    _aprobar(bd, informe, JORGE, "coordinador")

    with pytest.raises(psycopg.errors.UniqueViolation, match="informe_aprobacion_por_rol"):
        _aprobar(bd, informe, fila[0], "coordinador")


@pytest.mark.rf("RF-49")
def test_una_persona_no_aprueba_dos_veces_el_mismo_informe(bd):
    informe = _informe(bd)
    _aprobar(bd, informe, JORGE, "coordinador")
    # Le cambian el rol después de aprobar: pasa el trigger del rol y choca con la unicidad.
    bd.execute("UPDATE acervo.usuario SET rol = 'rgc' WHERE usuario_id = %s", (JORGE,))

    with pytest.raises(psycopg.errors.UniqueViolation, match="informe_aprobacion_por_persona"):
        _aprobar(bd, informe, JORGE, "rgc")
