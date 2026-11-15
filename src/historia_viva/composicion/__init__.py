"""Dónde se eligen las clases concretas y los comandos de la imagen (spec, módulo `composicion`).

    python -m historia_viva.composicion migrar   # hv_migrador; en desarrollo, personas de prueba
    python -m historia_viva.composicion web [--host H] [--puerto P]  # como hv_app
    python -m historia_viva.composicion trabajador --tipos procesamiento   # o indexacion
    python -m historia_viva.composicion trabajador --tipos efemeride   # el planificador (R-08)
    python -m historia_viva.composicion reindexar   # encola la indexación de cada Validada
    python -m historia_viva.composicion modelos   # baja los modelos de la configuración vigente
    python -m historia_viva.composicion interruptor detener procesamiento_habilitado --motivo M
    python -m historia_viva.composicion catalogo carreras --cargar carreras.csv   # como hv_app
    python -m historia_viva.composicion alta-persona --email E --nombre N --rol admin_tecnico
    python -m historia_viva.composicion emitir-token "Agente 1" --alcances fragmentos:leer
    python -m historia_viva.composicion revocar-token hv_0123abcd
    python -m historia_viva.composicion estado-vacio   # runbook R16, paso 9: el servidor vacío
    python -m historia_viva.composicion reprocesar --fallidos [--lote ID] [--tipo indexacion]
    python -m historia_viva.composicion banco cargar-referencia   # valida HV_REFERENCIA
    python -m historia_viva.composicion banco m1 --conjunto ajuste|prueba [--salida DIR]
    python -m historia_viva.composicion banco m2 [--informe ID] [--salida DIR]
    python -m historia_viva.composicion banco m3 --condicion A|B|C [--foto ARCHIVO] [--cpu TEXTO]
        [--conjunto ajuste|prueba] [--consultas N] [--salida DIR]   # sin guardar nada
    python -m historia_viva.composicion banco m4 [--plantilla [--evaluador NOMBRE]...]
        [--evaluaciones ARCHIVO] [--salida DIR]
    python -m historia_viva.composicion banco comparar
        --capacidad resumir|describir_imagen|embeber|responder [--modelo M]... [--repeticiones 3]
        [--salida DIR]   # RF-28: ganador y mejor no chino
    python -m historia_viva.composicion banco ingesta --corpus DIR [--modelo M]...
        [--instruccion ID] [--hardware TEXTO] [--salida DIR]   # invenciones, OCR y tiempo (137)
    python -m historia_viva.composicion matriz [--salida DIR] [--corridas DIR]   # de desarrollo

Es el único módulo que construye un `MotorOllama` y el único que importa `migraciones`: el
resto recibe el mapa `{id → motor}` que nombra la columna `motor` de `configuracion_capacidad`.
Nada se compone al importar: cada comando, y cada test, arma su propio grafo (historia 64).
Las variables las lee `Entorno` (spec, «Variables de entorno»).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import mimetypes
import os
import platform
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache, partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import UUID

import uvicorn

from historia_viva import banco, conversion, video
from historia_viva.banco.aceptacion import Aceptacion
from historia_viva.banco.comparacion import Banco, eleccion_del_banco
from historia_viva.agentes import (
    AgenteExistente,
    Credenciales,
    Scope,
    TokenInexistente,
    contar_tokens,
    es_prefijo,
)
from historia_viva.api_agentes import RETRY_AFTER_INTERRUPTOR_S, Cupos, TopesDeCupo
from historia_viva.api.app import Grafo, crear_app
from historia_viva.auditoria import Actor, RegistroDeAuditoria, TipoDeActor
from historia_viva.bd import BaseNoDisponible, Bd, Tx
from historia_viva.busqueda import (
    Busqueda,
    BuscadorNoDisponible,
    Consulta,
    IndiceNoDisponible,
    UMBRAL_DE_SIMILITUD,
    consultas_de_referencia,
)
from historia_viva.conversacion import FRAGMENTOS_DEL_MODELO, Conversaciones
from historia_viva.efemerides import Efemerides
from historia_viva.efemerides import contar_firmas_de_ensayo as firmas_de_ensayo_en_efemerides
from historia_viva.capa_modelos.configuracion import CapaPara, ConfiguracionDeCapacidad, validar
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_IMAGEN,
    BloqueBinario,
    Capacidad,
    CapaModelos,
)
from historia_viva.capa_modelos.local import capa_para
from historia_viva.capa_modelos.ollama import MotorOllama
from historia_viva.catalogos import CargaRechazada, cargar_carreras, leer_carreras
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.errores import (
    ConfiguracionInvalida,
    DescripcionFueraDeEsquema,
    LimiteExcedido,
    MotorNoDisponible,
    PiezaNoEncontrada,
)
from historia_viva.dominio.tipos import MetodoAutenticacion
from historia_viva.drive import (
    ClaveDeDriveInvalida,
    ClienteDrive,
    ConfiguracionDrive,
    Replicador,
    leer_clave,
)
from historia_viva.dominio.originales import Contenedor
from historia_viva.dominio.permisos import Rol
from historia_viva.fichas.postgres import (
    ModuloFichasPostgres,
    contar_piezas,
    etiquetas_de_control,
)
from historia_viva.fichas.postgres import contar_firmas_de_ensayo as firmas_de_ensayo_en_fichas
from historia_viva.identidad import (
    Padron,
    PersonaNoEncontrada,
    RechazoDelPadron,
    Sesiones,
    contar_personas,
)
from historia_viva.identidad.oidc import ConfiguracionOIDC, IngresoOIDC
from historia_viva.composicion.registro import configurar_registro
from historia_viva.indexacion import Indexacion
from historia_viva.informes import InformeNoEncontrado, Informes
from historia_viva.informes import contar_firmas_de_ensayo as firmas_de_ensayo_en_informes
from historia_viva.ingesta import (
    AnalisisDeVideoNoDisponible,
    AnalizarVideo,
    DatosTecnicos,
    Ingesta,
)
from historia_viva.instrumentacion import RegistroMediciones
from historia_viva.interruptor import (
    MOTIVO_MINIMO,
    Freno,
    MotivoRequerido,
    ProcesamientoDetenido,
    cambiar,
    estado,
    frenar_todo,
    habilitado_en,
)
from historia_viva.lotes import Lotes
from historia_viva.migraciones import MigracionRechazada, aplicar, sembrar_desarrollo
from historia_viva.originales import AlmacenDeOriginales, ArchivoIlegible, Conversor
from historia_viva.procesamiento.cola import (
    CAPACIDADES_DE_DOCUMENTO,
    CAPACIDADES_DE_IMAGEN,
    CAPACIDADES_DE_TEXTO,
    REPROCESABLES,
    TOPE_DE_REPROCESOS,
    Cola,
    TipoDeTrabajo,
)
from historia_viva.procesamiento.manejador import (
    ManejadorDeDescripcion,
    ManejadorDeProcesamiento,
    ManejadorDeResumen,
)
from historia_viva.procesamiento.planificador import TIPO as TIPO_DEL_PLANIFICADOR
from historia_viva.procesamiento.planificador import Planificador
from historia_viva.procesamiento.trabajador import (
    _ESPERA_AL_DETENER_S,
    Cortacircuito,
    Manejador,
    Trabajador,
)
from historia_viva.retiro import Retiro
from historia_viva.supresion import (
    MotivoDeSupresionInvalido,
    PiezaEnProceso,
    Supresion,
    SupresionDeshabilitada,
)
from historia_viva.validacion import Validacion

if TYPE_CHECKING:
    from historia_viva.indice import Indice

__all__ = [
    "Entorno",
    "EntornoInvalido",
    "EstadoDelServidor",
    "armar_grafo",
    "armar_indexacion",
    "armar_planificador",
    "armar_trabajador",
    "componer_banco",
    "componer_planificador",
    "componer_replicador",
    "componer_trabajador",
    "componer_web",
    "estado_del_servidor",
    "main",
    "motores",
]

_MODOS_DE_INGRESO = ("oidc", "dev")

TIPOS_DE_TRABAJO = ("procesamiento", "indexacion")
"""Los de la cola: `web` espera un trabajador vivo de cada uno (035)."""

EFEMERIDE = TIPO_DEL_PLANIFICADOR
"""`trabajador --tipos efemeride` arma el planificador, no un tipo de la cola (R-08). Late con
este tipo (059)."""

CAPACIDADES_POR_TIPO: Mapping[TipoDeTrabajo, frozenset[Capacidad]] = {
    TipoDeTrabajo.PROCESAMIENTO: frozenset(
        (*CAPACIDADES_DE_IMAGEN, *CAPACIDADES_DE_DOCUMENTO, *CAPACIDADES_DE_TEXTO)
    ),
    TipoDeTrabajo.INDEXACION: frozenset((Capacidad.EMBEBER,)),
}
"""Las que invoca cada tipo: su cortacircuito mira solo sus motores (DEC-86). Con el Ollama de
generación caído, la indexación sigue."""

_TAMANO_POOL_WEB = 10
"""Los pedidos que atiende `web` a la vez (ajustable)."""

_TAMANO_POOL_TRABAJADOR = 2
"""El trabajo en curso más la toma siguiente: el trabajador procesa de a uno."""

Validar = Callable[[Mapping[Capacidad, ConfiguracionDeCapacidad]], None]


_DOMINIOS_POR_DEFECTO = ("fie.undef.edu.ar",)
BUSQUEDAS_POR_MINUTO = 30
"""`HV_CUPO_BUSQUEDAS_MIN` vacía: las búsquedas con texto por minuto y por persona (SEG-09)."""
"""Los de `HV_DOMINIOS_PERMITIDOS` vacía: el de la FIE."""


class EntornoInvalido(Exception):
    """Falta una variable que el comando necesita o tiene un valor que no se admite."""


@dataclass(frozen=True)
class Entorno:
    """Las variables de la aplicación (spec, «Variables de entorno»), leídas una sola vez."""

    dsn_app: str | None
    """`HV_BD_URL`: `hv_app`, con el que conectan `web` y los trabajadores."""
    dsn_migrador: str | None
    """`HV_BD_URL_MIGRADOR`: `hv_migrador`, solo para `migrar`."""
    originales: Path | None
    """`HV_ORIGINALES`: raíz del almacén de originales."""
    motor_url: str
    """`HV_MOTOR_URL`: el Ollama de generación (id `ollama`)."""
    motor_embeddings_url: str
    """`HV_MOTOR_EMBEDDINGS_URL`: el Ollama de embeddings (id `ollama_embeddings`, DEC-06)."""
    auth: str
    """`HV_AUTH`: `oidc` o `dev`."""
    entorno: str | None
    """`HV_ENTORNO`: `desarrollo` o ausente (producción)."""
    url_publica: str
    """`HV_URL_PUBLICA`: base de `url_original` y de los enlaces de las Citas."""
    origen: str
    """`HV_ORIGEN`: el `Origin` que tiene que traer toda mutación (CSRF, DEC-37)."""
    proxy: str | None
    """`HV_PROXY`: el host de Caddy en el compose; lo que llega de su socket cuenta como hecho
    en la PC para `/auth/dev`, porque Caddy se publica solo en `127.0.0.1:80`."""
    retry_after_s: int
    """`HV_RETRY_AFTER_S`: el `Retry-After` del 503 de `/v1` por el freno (DEC-78)."""
    topes_de_cupo: TopesDeCupo = TopesDeCupo()
    """`HV_CUPO_CONSULTAS_MIN`, `HV_CUPO_APORTES_MIN`, `HV_CUPO_BUSQUEDAS_EN_VUELO` (DEC-113)."""
    busquedas_min: int = BUSQUEDAS_POR_MINUTO
    """`HV_CUPO_BUSQUEDAS_MIN`: el cupo por persona de `/api/busqueda` con texto (165, DEC-113)."""
    umbral_de_similitud: float = UMBRAL_DE_SIMILITUD
    """`HV_UMBRAL_SIMILITUD` (nueva, 133): la similitud coseno mínima de una Fuente, entre 0 y 1."""
    dominios_permitidos: tuple[str, ...] = _DOMINIOS_POR_DEFECTO
    """`HV_DOMINIOS_PERMITIDOS`, separados por comas: los del email de un alta (y de OIDC)."""
    oidc: ConfiguracionOIDC | None = None
    """El cliente OIDC (`HV_OIDC_*`); sin `HV_OIDC_CLIENTE_ID`, no hay ingreso con Google."""
    bloque_habilitado: bool = False
    """`HV_BLOQUE_HABILITADO`: la aprobación en bloque, apagada hasta que la SEU acepte C29
    (R-28)."""
    datos_personales_habilitados: bool = False
    """`HV_DATOS_PERSONALES_HABILITADOS`: `publicable` desde Validar, el aviso de DNI, CUIL o CBU
    y la supresión efectiva, apagados hasta que decidan la SEU y la asesoría jurídica (C31, 125)."""
    referencia: Path | None = None
    """`HV_REFERENCIA`: el conjunto de referencia del banco, fuera de git (DEC-97, DEC-120)."""
    drive: ConfiguracionDrive | None = None
    """`HV_DRIVE=on` (124): la réplica de originales en Drive, en el trabajador que los monta."""
    despliegue: str | None = None
    """`HV_DESPLIEGUE` (nueva, 121): `servidor` es el de la universidad (runbook R16), sin modo
    de ensayo ni personas de prueba, y `web` solo con OIDC completo por https. `humo` es lo mismo
    con el proveedor OIDC falso por http: lo pone solo `compose.humo.yml`, en la VM."""

    @property
    def servidor(self) -> bool:
        return self.despliegue is not None

    @classmethod
    def desde_variables(cls, variables: Mapping[str, str]) -> Entorno:
        # Vacía es ausente: es lo que deja `${VARIABLE}` sin definir en el compose.
        def leer(variable: str) -> str | None:
            return variables.get(variable) or None

        auth = leer("HV_AUTH") or "oidc"
        if auth not in _MODOS_DE_INGRESO:
            raise EntornoInvalido(f"HV_AUTH tiene que ser oidc o dev, no «{auth}».")
        despliegue = leer("HV_DESPLIEGUE")
        if despliegue not in (None, "servidor", "humo"):
            raise EntornoInvalido(
                f"HV_DESPLIEGUE tiene que ser servidor, humo o quedar vacía, no «{despliegue}»."
            )
        # Un `.env` equivocado no deja entrar por el proveedor falso de la prueba de humo: en el
        # servidor el emisor es https; `humo` solo lo pone `compose.humo.yml`.
        emisor = leer("HV_OIDC_EMISOR")
        if despliegue == "servidor" and emisor and not emisor.startswith("https://"):
            raise EntornoInvalido(f"En el servidor HV_OIDC_EMISOR es https, no «{emisor}».")
        # El servidor de la universidad arranca vacío y sin ensayo (DEC-119, DEC-139, runbook
        # R16): con `HV_ENTORNO`, `migrar` sembraría personas de prueba y `dev` firmaría.
        if despliegue and auth != "oidc":
            raise EntornoInvalido("En el servidor HV_AUTH es oidc.")
        if despliegue and leer("HV_ENTORNO"):
            raise EntornoInvalido(
                "En el servidor HV_ENTORNO va vacía: con ella `migrar` siembra personas de prueba."
            )
        # El modo ensayo no queda abierto fuera de la PC (historia 228, DEC-85).
        if auth == "dev" and leer("HV_ENTORNO") != "desarrollo":
            raise EntornoInvalido("HV_AUTH=dev exige HV_ENTORNO=desarrollo.")
        if auth == "dev" and (leer("HV_OIDC_CLIENTE_ID") or leer("HV_OIDC_SECRETO")):
            raise EntornoInvalido("HV_AUTH=dev no arranca con Google configurado (HV_OIDC_*).")
        originales = leer("HV_ORIGINALES")
        referencia = leer("HV_REFERENCIA")
        dominios = tuple(
            d.strip().lower() for d in (leer("HV_DOMINIOS_PERMITIDOS") or "").split(",")
            if d.strip()
        ) or _DOMINIOS_POR_DEFECTO
        url_publica = leer("HV_URL_PUBLICA") or "http://localhost"

        def entero_positivo(variable: str, defecto: int) -> int:
            valor = leer(variable) or str(defecto)
            if not valor.isdigit() or int(valor) < 1:
                raise EntornoInvalido(f"{variable} tiene que ser un entero positivo, no «{valor}».")
            return int(valor)

        retry_after_s = entero_positivo("HV_RETRY_AFTER_S", RETRY_AFTER_INTERRUPTOR_S)
        topes = TopesDeCupo()
        topes_de_cupo = TopesDeCupo(
            consultas_min=entero_positivo("HV_CUPO_CONSULTAS_MIN", topes.consultas_min),
            aportes_min=entero_positivo("HV_CUPO_APORTES_MIN", topes.aportes_min),
            busquedas_en_vuelo=entero_positivo(
                "HV_CUPO_BUSQUEDAS_EN_VUELO", topes.busquedas_en_vuelo
            ),
        )
        busquedas_min = entero_positivo("HV_CUPO_BUSQUEDAS_MIN", BUSQUEDAS_POR_MINUTO)
        umbral = leer("HV_UMBRAL_SIMILITUD") or str(UMBRAL_DE_SIMILITUD)
        try:
            umbral_de_similitud = float(umbral)
        except ValueError:
            umbral_de_similitud = -1.0
        if not 0 <= umbral_de_similitud < 1:
            raise EntornoInvalido(f"HV_UMBRAL_SIMILITUD va de 0 a 1 (sin el 1), no «{umbral}».")
        bloque = (leer("HV_BLOQUE_HABILITADO") or "false").lower()
        if bloque not in ("true", "false"):
            raise EntornoInvalido(f"HV_BLOQUE_HABILITADO tiene que ser true o false, no «{bloque}».")
        drive_ = (leer("HV_DRIVE") or "off").lower()
        if drive_ not in ("on", "off"):
            raise EntornoInvalido(f"HV_DRIVE tiene que ser on u off, no «{drive_}».")
        drive = None
        if drive_ == "on":
            drive = ConfiguracionDrive(
                clave=Path(_exigida(leer("HV_DRIVE_CLAVE"), "HV_DRIVE_CLAVE")),
                unidad=_exigida(leer("HV_DRIVE_UNIDAD"), "HV_DRIVE_UNIDAD"),
                carpeta=_exigida(leer("HV_DRIVE_CARPETA"), "HV_DRIVE_CARPETA"),
            )
        datos_personales = (leer("HV_DATOS_PERSONALES_HABILITADOS") or "false").lower()
        if datos_personales not in ("true", "false"):
            raise EntornoInvalido(
                "HV_DATOS_PERSONALES_HABILITADOS tiene que ser true o false,"
                f" no «{datos_personales}»."
            )
        oidc = None
        if cliente_id := leer("HV_OIDC_CLIENTE_ID"):
            oidc = ConfiguracionOIDC(
                # `HV_OIDC_EMISOR` (nueva, 036): la prueba de humo apunta al proveedor falso.
                emisor=leer("HV_OIDC_EMISOR") or "https://accounts.google.com",
                cliente_id=cliente_id,
                secreto=_exigida(leer("HV_OIDC_SECRETO"), "HV_OIDC_SECRETO"),
                redireccion=f"{url_publica}/auth/callback",
                clave_del_intento=_exigida(
                    leer("HV_CLAVE_COOKIE_PREVIA"), "HV_CLAVE_COOKIE_PREVIA"
                ),
                dominios=dominios,
            )
        return cls(
            dsn_app=leer("HV_BD_URL"),
            dsn_migrador=leer("HV_BD_URL_MIGRADOR"),
            originales=Path(originales) if originales else None,
            motor_url=leer("HV_MOTOR_URL") or "http://ollama:11434",
            motor_embeddings_url=leer("HV_MOTOR_EMBEDDINGS_URL")
            or "http://ollama-embeddings:11434",
            auth=auth,
            entorno=leer("HV_ENTORNO"),
            url_publica=url_publica,
            origen=leer("HV_ORIGEN") or "http://localhost",
            proxy=leer("HV_PROXY"),
            dominios_permitidos=dominios,
            oidc=oidc,
            retry_after_s=retry_after_s,
            topes_de_cupo=topes_de_cupo,
            busquedas_min=busquedas_min,
            umbral_de_similitud=umbral_de_similitud,
            bloque_habilitado=bloque == "true",
            datos_personales_habilitados=datos_personales == "true",
            referencia=Path(referencia) if referencia else None,
            drive=drive,
            despliegue=despliegue,
        )


def _exigida[T](valor: T | None, variable: str) -> T:
    if valor is None:
        raise EntornoInvalido(f"Falta la variable de entorno {variable}.")
    return valor


def motores(entorno: Entorno) -> dict[str, MotorOllama]:
    """Los ids que puede nombrar la columna `motor`: dos Ollama, uno para generar y otro para
    `embeber`, que no espera detrás de la visión ni la descarga (DEC-06)."""
    return {
        "ollama": MotorOllama(url=entorno.motor_url),
        "ollama_embeddings": MotorOllama(url=entorno.motor_embeddings_url),
    }


def armar_grafo(
    *,
    bd: Bd,
    capa_para: CapaPara,
    validar: Validar,
    directorio_originales: Path,
    avisar: Callable[[], None],
    modo_de_ingreso: MetodoAutenticacion,
    origen: str,
    analizar_video: AnalizarVideo | None,
    conversor: Conversor | None = None,
    reloj: Callable[[], datetime] | None = None,
    proxy: str | None = None,
    tipos_esperados: tuple[str, ...] = (),
    oidc: ConfiguracionOIDC | None = None,
    url_publica: str = "http://localhost",
    retry_after_s: int = RETRY_AFTER_INTERRUPTOR_S,
    dominios_permitidos: tuple[str, ...] = _DOMINIOS_POR_DEFECTO,
    topes_de_cupo: TopesDeCupo = TopesDeCupo(),
    busquedas_min: int = BUSQUEDAS_POR_MINUTO,
    bloque_habilitado: bool = False,
    datos_personales_habilitados: bool = False,
    corridas_del_banco: Path | None = None,
    # Sin corte: el valor calibrado es del modelo real y lo pone `Entorno`, no la capa falsa.
    umbral_de_similitud: float = 0.0,
) -> Grafo:
    """El grafo de `web` sobre una capa cualquiera: la suite le pasa la falsa y, para los
    vencimientos de la sesión, un reloj. `analizar_video` es `video.analizar` o un doble;
    `None` si no hay `ffprobe`. `conversor`, el de `.doc`, `.xls` y HEIC (153); sin él, 415.
    `corridas_del_banco`, donde `banco comparar` deja sus tablas."""
    originales = AlmacenDeOriginales(directorio_originales, conversor=conversor)
    fichas = ModuloFichasPostgres(bd=bd)
    cola = Cola(bd=bd)
    auditoria = RegistroDeAuditoria(bd=bd)
    configuracion = ConfiguracionDeModelos(bd=bd, validar=validar, auditoria=auditoria)
    sesiones = Sesiones(bd=bd, auditoria=auditoria, reloj=reloj)

    def capa_vigente() -> CapaModelos:
        # La instantánea valida el conjunto entero: sin una fila de R1, `web` no arranca (DEC-87).
        return capa_para(configuracion.instantanea().por_capacidad)

    indice = _indice_perezoso(bd)
    indexacion = armar_indexacion(bd=bd, capa_para=capa_para, validar=validar, indice=indice)
    busqueda = _armar_busqueda(
        capa=capa_vigente, configuracion=configuracion, fichas=fichas,
        indice=indice, indexacion=indexacion, umbral=umbral_de_similitud,
    )
    lotes = Lotes(fichas=fichas, cola=cola, indexacion=indexacion, auditoria=auditoria)
    efemerides = Efemerides(
        bd=bd,
        fichas=fichas,
        configuracion=configuracion,
        capa_para=capa_para,
        auditoria=auditoria,
    )
    return Grafo(
        bd=bd,
        originales=originales,
        fichas=fichas,
        mediciones=RegistroMediciones(bd=bd, reloj=reloj),
        validacion=Validacion(
            bd=bd,
            fichas=fichas,
            indexador=indexacion,
            lotes=lotes,
            auditoria=auditoria,
            marcar_fuente_retirada=efemerides.marcar_fuente_retirada,
            rehacer_resumen=lambda tx, pieza_id: cola.encolar_resumen(
                tx,
                pieza_id,
                configuracion_efectiva=configuracion.instantanea(tx, (Capacidad.RESUMIR,)),
            ),
            avisar=avisar,
            bloque_habilitado=bloque_habilitado,
            datos_personales_habilitados=datos_personales_habilitados,
            reloj=reloj,
        ),
        ingesta=Ingesta(
            bd=bd,
            originales=originales,
            fichas=fichas,
            cola=cola,
            configuracion=configuracion,
            auditoria=auditoria,
            lotes=lotes,
            avisar=avisar,
            analizar_video=(
                None if analizar_video is None else partial(_analizar_video, analizar_video)
            ),
            # Lo de `chequeo_inyeccion` llega por `fichas`, su único importador (DEC-133).
            etiquetas_de_control=etiquetas_de_control,
        ),
        cola=cola,
        lotes=lotes,
        capa_vigente=capa_vigente,
        configuracion=configuracion,
        capa_para=capa_para,
        eleccion_del_banco=(
            (lambda _: None) if corridas_del_banco is None
            else partial(eleccion_del_banco, corridas_del_banco)
        ),
        busqueda=busqueda,
        conversaciones=Conversaciones(
            bd=bd,
            busqueda=busqueda,
            fichas=fichas,
            auditoria=auditoria,
            configuracion=configuracion,
            capa_para=capa_para,
            efemerides=efemerides,  # «Proponer como relato» (DEC-147)
        ),
        efemerides=efemerides,
        # Sin capa de modelos: el informe no la usa (DEC-131).
        informes=Informes(
            fichas=fichas,
            auditoria=auditoria,
            url_publica=url_publica.rstrip("/"),
            integridad=partial(
                banco.integridad, fichas=fichas, configuracion=configuracion
            ),
        ),
        indexacion=indexacion,
        retiro=Retiro(
            bd=bd,
            fichas=fichas,
            indexacion=indexacion,
            cola=cola,
            lotes=lotes,
            configuracion=configuracion,
            auditoria=auditoria,
            marcar_fuente_retirada=efemerides.marcar_fuente_retirada,
        ),
        sesiones=sesiones,
        auditoria=auditoria,
        credenciales=Credenciales(bd=bd, auditoria=auditoria, reloj=reloj),
        padron=Padron(
            auditoria=auditoria, sesiones=sesiones, dominios_permitidos=dominios_permitidos
        ),
        modo_de_ingreso=modo_de_ingreso,
        origen=origen,
        proxy=proxy,
        url_publica=url_publica.rstrip("/"),
        retry_after_s=retry_after_s,
        cupos=Cupos(topes_de_cupo),
        cupos_de_busqueda=Cupos(TopesDeCupo(consultas_min=busquedas_min)),
        tipos_esperados=tipos_esperados,
        oidc=None if oidc is None else IngresoOIDC(oidc, sesiones=sesiones, reloj=reloj),
    )


def _analizar_video(
    analizar: AnalizarVideo, ruta: Path, contenedor: Contenedor
) -> DatosTecnicos:
    """`analizar` con los errores que entiende `ingesta`, que no depende de `video` (DEC-94)."""
    try:
        return analizar(ruta, contenedor)
    except video.FfprobeNoDisponible as error:
        raise AnalisisDeVideoNoDisponible() from error
    except video.VideoIlegible as error:
        raise ArchivoIlegible(
            f"No se pudo leer el video: {error} Exportalo de nuevo como MP4 y subilo.",
            motivo="sin-video",
        ) from error


def _armar_busqueda(
    *,
    capa: Callable[[], CapaModelos],
    configuracion: ConfiguracionDeModelos,
    fichas: ModuloFichasPostgres,
    indice: Callable[[], Indice],
    indexacion: Indexacion,
    umbral: float,
) -> Busqueda:
    """La de `web`, que es también la que mide M1 (DEC-89)."""
    return Busqueda(
        indice=indice,
        capa=capa,
        fichas=fichas,
        # Sin preguntarle el digest al motor: la búsqueda sin texto no lo necesita.
        modelo_vigente=lambda: indice().modelo_mayoritario(
            f"{configuracion.vigente()[Capacidad.EMBEBER].modelo}@"
        ),
        # Fragmentos de otro modelo, Validadas sin fragmentos o indexaciones fallidas.
        indice_incompleto=lambda modelo: indexacion.progreso(modelo).incompleto,
        umbral=umbral,
    )


def _indice_perezoso(bd: Bd) -> Callable[[], Indice]:
    """Un solo `Indice` por grafo, armado al primer uso: armarlo carga llama-index
    (ticket 130)."""

    @cache
    def indice() -> Indice:
        from historia_viva.indice import Indice

        return Indice(bd=bd, dsn=bd.dsn)

    return indice


def armar_indexacion(
    *,
    bd: Bd,
    capa_para: CapaPara,
    validar: Validar,
    indice: Callable[[], Indice] | None = None,
) -> Indexacion:
    """`indexacion` con su índice, que se arma recién al usarlo: armar el `Indice` carga
    llama-index, y lo paga solo quien guarda o lee fragmentos (ticket 130). `web` le pasa el
    `Indice` que comparte con la búsqueda."""
    return Indexacion(
        bd=bd,
        indice=indice or _indice_perezoso(bd),
        capa_para=capa_para,
        fichas=ModuloFichasPostgres(bd=bd),
        cola=Cola(bd=bd),
        configuracion=ConfiguracionDeModelos(bd=bd, validar=validar),
        auditoria=RegistroDeAuditoria(bd=bd),
    )


def armar_trabajador(
    *,
    bd: Bd,
    capa_para: CapaPara,
    directorio_originales: Path,
    tipos: Collection[TipoDeTrabajo] = (TipoDeTrabajo.PROCESAMIENTO,),
    reloj: Callable[[], float] = time.monotonic,
    conversor: Conversor | None = None,
) -> Trabajador:
    """El trabajador de `tipos` sobre una capa cualquiera: el mapa tipo → manejador (DEC-08)
    y la habilitación, interruptor ∧ motor vivo de sus capacidades (DEC-86, DEC-88)."""
    manejadores: dict[TipoDeTrabajo, Manejador[Any]] = {}
    if TipoDeTrabajo.PROCESAMIENTO in tipos:
        originales = AlmacenDeOriginales(directorio_originales, conversor=conversor)
        manejadores[TipoDeTrabajo.PROCESAMIENTO] = ManejadorDeProcesamiento(
            capa_para=capa_para,
            fichas=ModuloFichasPostgres(bd=bd),
            originales=originales,
            cola=Cola(bd=bd),
        )
        # La descripción de las imágenes embebidas (148) usa el mismo modelo de visión: la toma
        # el trabajador del procesamiento, de a una, y no compite con él por la CPU.
        manejadores[TipoDeTrabajo.DESCRIPCION] = ManejadorDeDescripcion(
            capa_para=capa_para, fichas=ModuloFichasPostgres(bd=bd), originales=originales
        )
        # El resumen rehecho usa el mismo motor y la misma CPU: lo toma este trabajador (146).
        manejadores[TipoDeTrabajo.RESUMEN] = ManejadorDeResumen(
            capa_para=capa_para, fichas=ModuloFichasPostgres(bd=bd), cola=Cola(bd=bd)
        )
    if TipoDeTrabajo.INDEXACION in tipos:
        # Congela solo al reencolar por cambio de modelo (el gancho), sin validar: la fila la
        # valida `capa_para` al ejecutar el trabajo nuevo.
        manejadores[TipoDeTrabajo.INDEXACION] = armar_indexacion(
            bd=bd, capa_para=capa_para, validar=lambda _: None
        )
    # El trabajador no congela instantáneas: solo lee las filas vigentes y `capa_para` las valida.
    configuracion = ConfiguracionDeModelos(bd=bd, validar=lambda _: None)
    capacidades = frozenset().union(*(CAPACIDADES_POR_TIPO[tipo] for tipo in tipos))
    motor_vivo = Cortacircuito(
        lambda: capa_para(configuracion.vigente()).verificar_salud(capacidades), reloj=reloj
    )
    return Trabajador(
        bd=bd,
        cola=Cola(bd=bd),
        manejadores=manejadores,
        # El interruptor primero y sin caché (DEC-27): frenado, ni se le pregunta al motor.
        habilitado=lambda: habilitado_en(bd, Freno.PROCESAMIENTO) and motor_vivo(),
        reloj=reloj,
    )


def armar_planificador(
    *,
    bd: Bd,
    capa_para: CapaPara,
    validar: Validar,
    ahora: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    reloj: Callable[[], float] = time.monotonic,
) -> Planificador:
    """El planificador de efemérides (R-08) con la misma habilitación que la cola: interruptor ∧
    el motor de `redactar_efemeride` vivo (DEC-86, DEC-136)."""
    efemerides = Efemerides(
        bd=bd,
        fichas=ModuloFichasPostgres(bd=bd),
        configuracion=ConfiguracionDeModelos(bd=bd, validar=validar),
        capa_para=capa_para,
        auditoria=RegistroDeAuditoria(bd=bd),
    )
    configuracion = ConfiguracionDeModelos(bd=bd, validar=lambda _: None)
    motor_vivo = Cortacircuito(
        lambda: capa_para(configuracion.vigente()).verificar_salud(
            (Capacidad.REDACTAR_EFEMERIDE,)
        ),
        reloj=reloj,
    )
    return Planificador(
        bd=bd,
        tareas={
            "efemerides": lambda habilitado: efemerides.correr_programadas(
                habilitado, ahora=ahora()
            )
        },
        habilitado=lambda: habilitado_en(bd, Freno.PROCESAMIENTO) and motor_vivo(),
    )


def componer_web(entorno: Entorno) -> Grafo:
    """El grafo de `web`, cuya base cierra el ciclo de vida de la aplicación.

    Raises:
        ConfiguracionInvalida: Si una fila de `configuracion_capacidad` no valida: `web` no
            arranca (DEC-87) y el comando lo dice antes de levantar uvicorn.
    """
    if entorno.servidor:
        _exigir_ingreso_del_servidor(entorno)
    directorio_originales = _exigida(entorno.originales, "HV_ORIGINALES")
    motores_ = motores(entorno)
    if entorno.auth == "dev":
        print("ADVERTENCIA: autenticación de desarrollo activa", file=sys.stderr, flush=True)
    grafo = armar_grafo(
        bd=Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=_TAMANO_POOL_WEB),
        capa_para=partial(capa_para, motores=motores_),
        validar=partial(validar, motores=motores_),
        directorio_originales=directorio_originales,
        # El trabajador es otro proceso y se entera por sondeo (DEC-106).
        avisar=lambda: None,
        modo_de_ingreso=MetodoAutenticacion(entorno.auth),
        origen=entorno.origen,
        proxy=entorno.proxy,
        analizar_video=video.analizar if video.disponible() else None,
        # Admite lo que el trabajador sabe convertir: corren sobre la misma imagen (153).
        conversor=conversion.ConversorAislado(),
        # Un trabajador de cada tipo que se puede lanzar, también el planificador (059); el que
        # no late, caído (035).
        tipos_esperados=(*TIPOS_DE_TRABAJO, EFEMERIDE),
        oidc=entorno.oidc,
        url_publica=entorno.url_publica,
        retry_after_s=entorno.retry_after_s,
        dominios_permitidos=entorno.dominios_permitidos,
        topes_de_cupo=entorno.topes_de_cupo,
        busquedas_min=entorno.busquedas_min,
        bloque_habilitado=entorno.bloque_habilitado,
        datos_personales_habilitados=entorno.datos_personales_habilitados,
        # El directorio por defecto de `banco comparar` (076).
        corridas_del_banco=entorno.referencia and entorno.referencia / "corridas",
        umbral_de_similitud=entorno.umbral_de_similitud,
    )
    try:
        # Con la base caída arranca igual y responde 503 hasta que vuelva.
        with suppress(BaseNoDisponible):
            grafo.capa_vigente()
    except ConfiguracionInvalida:
        grafo.bd.cerrar()
        raise
    return grafo


def _exigir_ingreso_del_servidor(entorno: Entorno) -> None:
    """En el servidor `web` no arranca sin por dónde entrar: OIDC con su cliente y la URL https
    de la FIE como único origen (runbook R16, paso 3)."""
    if entorno.oidc is None:
        raise EntornoInvalido(
            "En el servidor HV_AUTH=oidc exige HV_OIDC_CLIENTE_ID, HV_OIDC_SECRETO y"
            " HV_CLAVE_COOKIE_PREVIA: sin ellas nadie puede entrar."
        )
    if not entorno.url_publica.startswith("https://") or entorno.origen != entorno.url_publica:
        raise EntornoInvalido(
            "En el servidor HV_URL_PUBLICA es la URL https de la FIE y HV_ORIGEN es la misma."
        )


def componer_trabajador(entorno: Entorno, tipos: Sequence[str]) -> tuple[Trabajador, Bd]:
    """El trabajador de `tipos` y su base, que cierra quien lo apaga.

    Raises:
        ConfiguracionInvalida: Si una fila de `configuracion_capacidad` no valida: el
            trabajador no arranca, igual que `web` (DEC-87).
    """
    directorio_originales = _exigida(entorno.originales, "HV_ORIGINALES")
    motores_ = motores(entorno)
    # La réplica a Drive transacciona desde su propio hilo: una conexión más.
    tamano_pool = _TAMANO_POOL_TRABAJADOR + (entorno.drive is not None)
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=tamano_pool)
    configuracion = ConfiguracionDeModelos(bd=bd, validar=partial(validar, motores=motores_))
    try:
        # Con la base caída arranca igual: la cola la sondea hasta que vuelva.
        with suppress(BaseNoDisponible):
            configuracion.instantanea()
    except ConfiguracionInvalida:
        bd.cerrar()
        raise
    trabajador = armar_trabajador(
        bd=bd,
        capa_para=partial(capa_para, motores=motores_),
        directorio_originales=directorio_originales,
        tipos=[TipoDeTrabajo(tipo) for tipo in tipos],
        conversor=conversion.ConversorAislado(),
    )
    return trabajador, bd


def componer_replicador(entorno: Entorno, bd: Bd) -> Replicador | None:
    """La réplica a Drive del trabajador, si `HV_DRIVE=on` (DRV-04, DRV-12).

    Raises:
        EntornoInvalido: Si la clave de la cuenta de servicio falta o no se entiende: encendida,
            falla al arrancar y no en la primera subida (§3.9).
    """
    if entorno.drive is None:
        return None
    try:
        clave = leer_clave(entorno.drive.clave)
    except ClaveDeDriveInvalida as error:
        raise EntornoInvalido(str(error)) from error
    return Replicador(
        bd=bd,
        fichas=ModuloFichasPostgres(bd=bd),
        originales=AlmacenDeOriginales(_exigida(entorno.originales, "HV_ORIGINALES")),
        cliente=ClienteDrive(entorno.drive, clave),
    )


def componer_planificador(entorno: Entorno) -> tuple[Planificador, Bd]:
    """El planificador de efemérides y su base; con una fila inválida no arranca (DEC-87)."""
    motores_ = motores(entorno)
    # La sesión del candado y la transacción de la tarea: nunca más de dos a la vez.
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=_TAMANO_POOL_TRABAJADOR)
    validar_ = partial(validar, motores=motores_)
    try:
        with suppress(BaseNoDisponible):
            ConfiguracionDeModelos(bd=bd, validar=validar_).instantanea()
    except ConfiguracionInvalida:
        bd.cerrar()
        raise
    planificador = armar_planificador(
        bd=bd, capa_para=partial(capa_para, motores=motores_), validar=validar_
    )
    return planificador, bd


class ComponentesDelBanco(NamedTuple):
    bd: Bd
    fichas: ModuloFichasPostgres
    aceptacion: Aceptacion
    informes: Informes
    """Solo para leer las Piezas de un informe (`banco m2 --informe`)."""
    configuracion: ConfiguracionDeModelos
    comparacion: Banco
    busqueda: Busqueda
    """La de M1, también para `consultas-de-referencia`."""


def componer_banco(entorno: Entorno) -> ComponentesDelBanco:
    """Lo que usa el comando `banco`, como `hv_app`. Sin interruptor: el banco no encola nada y
    corre con la cola frenada (DEC-96)."""
    # Dos conexiones: la transacción del comando y la de la búsqueda de M1, que usa la suya.
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=2)
    fichas = ModuloFichasPostgres(bd=bd)
    # Sin validar: el banco informa la configuración tal como está, no la juzga.
    configuracion = ConfiguracionDeModelos(bd=bd, validar=lambda _: None)
    motores_ = motores(entorno)
    capa_para_ = partial(capa_para, motores=motores_)
    indice = _indice_perezoso(bd)
    indexacion = armar_indexacion(
        bd=bd, capa_para=capa_para_, validar=lambda _: None, indice=indice
    )
    integridad = partial(banco.integridad, fichas=fichas, configuracion=configuracion)
    auditoria = RegistroDeAuditoria(bd=bd)

    def capa_vigente() -> CapaModelos:
        return capa_para_(configuracion.instantanea().por_capacidad)

    busqueda = _armar_busqueda(
        capa=capa_vigente, configuracion=configuracion, fichas=fichas,
        indice=indice, indexacion=indexacion,
        # El banco mide la búsqueda de `web`, con su mismo corte.
        umbral=entorno.umbral_de_similitud,
    )
    return ComponentesDelBanco(
        bd=bd,
        fichas=fichas,
        aceptacion=Aceptacion(
            fichas=fichas, configuracion=configuracion, pendientes=indexacion.pendientes,
            busqueda=busqueda,
            # M3 mide la Conversación de `web`, con turnos que no se guardan.
            conversaciones=Conversaciones(
                bd=bd, busqueda=busqueda, fichas=fichas, auditoria=auditoria,
                configuracion=configuracion, capa_para=capa_para_,
                efemerides=Efemerides(
                    bd=bd, fichas=fichas, configuracion=configuracion, capa_para=capa_para_,
                    auditoria=auditoria,
                ),
            ),
            descargar=partial(_sacar_de_memoria, entorno),
            describir_foto=partial(_describir_foto, capa_vigente),
        ),
        informes=Informes(
            fichas=fichas,
            auditoria=RegistroDeAuditoria(bd=bd),
            url_publica=entorno.url_publica.rstrip("/"),
            integridad=integridad,
        ),
        configuracion=configuracion,
        # El candidato pasa por el mismo `validar` y la misma `capa_para` que `web` (RF-28).
        comparacion=Banco(
            validar=partial(validar, motores=motores_), fabrica_capa=capa_para_,
            # `responder` (078): los fragmentos que vería el modelo en la Conversación.
            recuperar=lambda consulta: [
                f.texto for f in busqueda.recuperar(
                    Consulta(texto=consulta.texto, filtro=consulta.filtro),
                    max_fragmentos=FRAGMENTOS_DEL_MODELO,
                ).fragmentos
            ],
        ),
        busqueda=busqueda,
    )


def _sacar_de_memoria(entorno: Entorno) -> None:
    """La condición en frío de M3 (DEC-90): los dos Ollama sin modelos cargados."""
    for motor in motores(entorno).values():
        motor.sacar_de_memoria()


def _describir_foto(capa: Callable[[], CapaModelos], foto: Path) -> Callable[[], object]:
    """Una descripción de `foto` por llamada, con la fila vigente de `describir_imagen`: la
    carga de la condición C de M3, en el mismo Ollama que la Conversación."""
    imagen = BloqueBinario(
        etiqueta=ETIQUETA_IMAGEN,
        contenido=foto.read_bytes(),
        tipo_mime=mimetypes.guess_type(foto.name)[0] or "image/jpeg",
        nombre_archivo=foto.name,
    )
    capa_ = capa()

    def describir() -> object:
        # M3 mide tiempos bajo carga: una respuesta fuera del esquema (144) ya ocupó al motor
        # entero, así que es una vuelta de carga, no un error.
        try:
            return capa_.describir_imagen(imagen=imagen)
        except DescripcionFueraDeEsquema:
            return None

    return describir


def _cpu() -> str:
    """El modelo de CPU y los hilos, como los dice el sistema: no ve la GPU."""
    return f"{platform.processor() or platform.machine()}, {os.cpu_count()} hilos lógicos"


def _m3(entorno: Entorno, referencia: Path, argumentos: argparse.Namespace) -> int:
    """`banco m3` (historia 204). La transacción dura lo que tarda leer la referencia y la
    configuración: los turnos llaman a modelos y no corren con una abierta (DEC-99)."""
    from historia_viva.banco.m3 import CargaNoArranca, escribir_m3, tabla_m3
    from historia_viva.banco.referencia import ReferenciaInvalida, cargar_referencia

    if argumentos.condicion == "C" and argumentos.foto is None:
        print("La condición C necesita --foto.", file=sys.stderr)
        return 2
    cpu = argumentos.cpu or _cpu()
    banco_ = componer_banco(entorno)
    try:
        with banco_.bd.transaccion() as tx:
            conjunto = cargar_referencia(tx, referencia, fichas=banco_.fichas)
            configuracion = banco_.aceptacion.configuracion_efectiva(tx)
        m3 = banco_.aceptacion.medir_m3(
            conjunto, argumentos.conjunto, argumentos.condicion, cpu=cpu, foto=argumentos.foto,
            consultas=argumentos.consultas,
        )
    except ReferenciaInvalida as error:
        print(f"La referencia no sirve:\n{error}", file=sys.stderr)
        return 1
    except (
        ValueError, CargaNoArranca, BuscadorNoDisponible, IndiceNoDisponible, LimiteExcedido,
        MotorNoDisponible, OSError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        banco_.bd.cerrar()
    rutas = escribir_m3(
        argumentos.salida or referencia / "corridas", m3,
        hash_referencia=conjunto.hash_referencia, configuracion_efectiva=configuracion,
        momento=datetime.now().astimezone(),
    )
    print(tabla_m3(m3, hash_referencia=conjunto.hash_referencia))
    print("Escrito:", *rutas, sep="\n  ")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Los comandos de la imagen: los servicios (`web`, `trabajador`, ...) y los de operación."""
    parser = argparse.ArgumentParser(
        prog="historia_viva.composicion", description="Los comandos de Historia Viva."
    )
    comandos = parser.add_subparsers(dest="comando", required=True)
    web = comandos.add_parser("web", help="la aplicación web, como hv_app")
    # En el contenedor, `0.0.0.0` para que llegue Caddy; en la PC, solo loopback (DEC-119).
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--puerto", type=int, default=8000)
    trabajador = comandos.add_parser("trabajador", help="consume la cola, como hv_app")
    trabajador.add_argument(
        "--tipos", nargs="+", choices=(*TIPOS_DE_TRABAJO, EFEMERIDE), required=True
    )
    comandos.add_parser(
        "migrar",
        help="aplica las migraciones que falten, como hv_migrador",
        description="Aplica como `hv_migrador` (HV_BD_URL_MIGRADOR) las migraciones que"
        " falten. Los roles y la extensión `vector` los deja antes, una sola vez, el bootstrap"
        " con superusuario (`db/bootstrap.sh`).",
    )
    comandos.add_parser("modelos", help="baja los modelos de la configuración vigente")
    comandos.add_parser(
        "estado-vacio",
        help="verifica que el servidor arrancó vacío, como hv_app (runbook R16, paso 9)",
        description="0 Piezas, 0 tokens de Agente, 0 firmas de ensayo y la API de Agentes"
        " apagada (DEC-119, DEC-139). Sale con 1 si algo no está vacío. No copia ni borra nada:"
        " no hay comando de traspaso de datos (R-34).",
    )
    comandos.add_parser(
        "reindexar",
        help="encola la indexación de cada Pieza Validada, como hv_app",
        description="Reconstruye el índice desde las Fichas Validadas no retiradas (RNF-08):"
        " encola un trabajo de indexación por Pieza, o reusa el que ya tiene. Lo corre el"
        " trabajador `--tipos indexacion`.",
    )
    _agregar_interruptor(comandos)
    catalogo = comandos.add_parser("catalogo", help="carga un catálogo, como hv_app")
    carreras = catalogo.add_subparsers(dest="catalogo", required=True).add_parser(
        "carreras",
        help="carga la lista de Carreras desde un CSV",
        description="Deja la lista de Carreras del CSV (cabecera `carrera_id,nombre`, en el"
        " orden en que se ofrecen): inserta las nuevas, actualiza las demás y desactiva las"
        " ausentes, sin borrar. Rige hasta que la SEU permita sembrarlas por migración (C45).",
        epilog="Cambiar el carrera_id de una Carrera son dos cargas: la primera le da otro nombre"
        " a la anterior y suma la nueva con el nombre de siempre; la segunda saca la anterior.",
    )
    carreras.add_argument("--cargar", type=Path, required=True, metavar="CSV")
    _agregar_alta_persona(comandos)
    _agregar_tokens(comandos)
    _agregar_reprocesar(comandos)
    _agregar_banco(comandos)
    _agregar_suprimir(comandos)
    _agregar_matriz(comandos)
    consultas = comandos.add_parser(
        "consultas-de-referencia",
        help="las 3 consultas de referencia por la búsqueda, como hv_app",
        description="Corre las 3 consultas de referencia (spec §2.3) con la búsqueda y escribe"
        " sus Piezas en JSON. La usan la restauración probada (runbook R9) y la reconstrucción"
        " del índice (runbook R13).",
    )
    consultas.add_argument(
        "--comparar", type=Path, metavar="JSON",
        help="sale con 1 si no dan lo mismo que este archivo: la salida anterior del comando o"
        " el .referencia.json de un volcado",
    )
    argumentos = parser.parse_args(argv)
    configurar_registro()
    try:
        entorno = Entorno.desde_variables(os.environ)
        if argumentos.comando == "migrar":
            return _migrar(
                _exigida(entorno.dsn_migrador, "HV_BD_URL_MIGRADOR"),
                desarrollo=entorno.entorno == "desarrollo",
            )
        if argumentos.comando == "modelos":
            return _bajar_modelos(entorno)
        if argumentos.comando == "estado-vacio":
            return _estado_vacio(entorno)
        if argumentos.comando == "reindexar":
            return _reindexar(entorno)
        if argumentos.comando == "interruptor":
            return _interruptor(entorno, argumentos)
        if argumentos.comando == "catalogo":
            return _cargar_carreras(entorno, argumentos.cargar)
        if argumentos.comando == "alta-persona":
            return _alta_persona(entorno, argumentos)
        if argumentos.comando == "emitir-token":
            return _emitir_token(entorno, argumentos.agente, argumentos.alcances)
        if argumentos.comando == "revocar-token":
            return _revocar_token(entorno, argumentos.prefijo)
        if argumentos.comando == "reprocesar":
            return _reprocesar_fallidos(entorno, argumentos)
        if argumentos.comando == "banco":
            return _banco(entorno, argumentos)
        if argumentos.comando == "suprimir":
            return _suprimir(entorno, argumentos)
        if argumentos.comando == "matriz":
            return _matriz(entorno, argumentos)
        if argumentos.comando == "consultas-de-referencia":
            return _consultas_de_referencia(entorno, argumentos.comparar)
        if argumentos.comando == "web":
            grafo = componer_web(entorno)
            # Carga llama-index antes de atender: la primera edición de una Validada desindexa
            # con la Pieza bloqueada y no tiene que pagar el import (ticket 130).
            from historia_viva.indice import precargar

            precargar()

            # Un solo proceso (sin `workers`): los cupos de `/v1` viven en su memoria (098).
            uvicorn.run(
                crear_app(grafo),
                host=argumentos.host,
                port=argumentos.puerto,
                # `request.client` es el socket real: con `X-Forwarded-For` cualquiera se haría
                # pasar por la PC ante `/auth/dev`. Nada usa la IP ni el esquema del cliente.
                proxy_headers=False,
                # Ninguna respuesta dice qué servidor la escribe (spec, encabezados).
                server_header=False,
                # Sin el registro propio de uvicorn: el suyo escribe la consulta (`?q=`).
                log_config=None,
            )
            return 0
        cola = [tipo for tipo in argumentos.tipos if tipo != EFEMERIDE]
        procesos: list[Callable[..., Awaitable[None]]] = []
        if cola:
            if TipoDeTrabajo.INDEXACION.value in cola:
                from historia_viva.indice import precargar

                precargar()  # el primer `guardar` no paga el import con la Ficha bloqueada
            trabajador_, bd_ = componer_trabajador(entorno, cola)
            procesos.append(partial(_trabajar, trabajador_, bd_))
            try:
                replicador = componer_replicador(entorno, bd_)
            except BaseException:
                bd_.cerrar()
                raise
            if replicador is not None:
                procesos.append(partial(_replicar, replicador))
        if EFEMERIDE in argumentos.tipos:
            try:
                procesos.append(partial(_planificar, *componer_planificador(entorno)))
            except BaseException:
                if cola:
                    bd_.cerrar()
                raise
        with suppress(KeyboardInterrupt):
            asyncio.run(_correr(procesos))
        return 0
    except (EntornoInvalido, MotivoRequerido) as error:
        print(error, file=sys.stderr)
        return 2
    except (ConfiguracionInvalida, BaseNoDisponible) as error:
        print(error, file=sys.stderr)
        return 1


def _migrar(dsn_migrador: str, *, desarrollo: bool) -> int:
    try:
        aplicadas = aplicar(dsn_migrador)
    except MigracionRechazada as error:
        print(error, file=sys.stderr)
        return 1
    for migracion in aplicadas:
        print(f"  {migracion.version:04d}_{migracion.nombre}")
    print(f"Base al día: {len(aplicadas)} migración(es) aplicada(s).")
    if desarrollo:
        sembrar_desarrollo(dsn_migrador)
        print("Personas de prueba sembradas (desarrollo).")
    return 0


@dataclass(frozen=True)
class EstadoDelServidor:
    """Lo que el paso 9 del runbook R16 verifica en el servidor real (`test_seg_traspaso_vacio`):
    no viajaron Piezas, tokens ni firmas de ensayo, y `/v1` nace apagada (DEC-119, DEC-139)."""

    piezas: int
    tokens: int
    firmas_de_ensayo: int
    api_agentes_habilitada: bool
    personas: int
    """Solo se informa: el paso 6 ya dio de alta al primer Administrador técnico."""

    @property
    def vacio(self) -> bool:
        return (self.piezas, self.tokens, self.firmas_de_ensayo) == (0, 0, 0) and not (
            self.api_agentes_habilitada
        )


def estado_del_servidor(tx: Tx) -> EstadoDelServidor:
    """Las consultas de control, cada una en su dueño y en la misma transacción. La API de
    Agentes sale del último evento del interruptor y no de `habilitado`, que ante un error de
    lectura diría «apagada»: acá un error sale como error."""
    ultimo = estado(tx).get(Freno.API_AGENTES)
    return EstadoDelServidor(
        piezas=contar_piezas(tx),
        tokens=contar_tokens(tx),
        firmas_de_ensayo=firmas_de_ensayo_en_fichas(tx)
        + firmas_de_ensayo_en_informes(tx)
        + firmas_de_ensayo_en_efemerides(tx),
        api_agentes_habilitada=ultimo is not None and ultimo.habilitado,
        personas=contar_personas(tx),
    )


def _estado_vacio(entorno: Entorno) -> int:
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=1)
    try:
        with bd.transaccion() as tx:
            actual = estado_del_servidor(tx)
    finally:
        bd.cerrar()
    print(f"Piezas: {actual.piezas}")
    print(f"Tokens de Agente: {actual.tokens}")
    print(f"Firmas de ensayo: {actual.firmas_de_ensayo}")
    print(f"API de Agentes: {'encendida' if actual.api_agentes_habilitada else 'apagada'}")
    print(f"Personas: {actual.personas}")
    # Con `humo`, `web` deja entrar por el proveedor falso: eso no es el servidor real.
    vacio = actual.vacio and entorno.despliegue != "humo"
    if entorno.despliegue == "humo":
        print("Ingreso: el proveedor OIDC falso de la prueba de humo (compose.humo.yml).")
    print("El servidor está vacío." if vacio else "El servidor NO está vacío.")
    return 0 if vacio else 1


def _agregar_interruptor(comandos: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    interruptor = comandos.add_parser(
        "interruptor", help="los frenos de ADR-0004: estado, detener, reanudar, frenar-todo"
    )
    acciones = interruptor.add_subparsers(dest="accion", required=True)
    acciones.add_parser("estado", help="el estado de los dos frenos")
    claves = [freno.value for freno in Freno]
    detener = acciones.add_parser("detener", help="detiene una clave; pide motivo")
    detener.add_argument("clave", choices=claves)
    detener.add_argument("--motivo", default="", help=f"por qué, {MOTIVO_MINIMO} caracteres o más")
    reanudar = acciones.add_parser("reanudar", help="reanuda una clave")
    reanudar.add_argument("clave", choices=claves)
    reanudar.add_argument("--motivo", default="")
    frenar = acciones.add_parser("frenar-todo", help="detiene las dos claves; pide motivo")
    frenar.add_argument("--motivo", default="", help=f"por qué, {MOTIVO_MINIMO} caracteres o más")


def _interruptor(entorno: Entorno, argumentos: argparse.Namespace) -> int:
    """Como `hv_app`; el evento queda con actor `cli` y el usuario del sistema operativo."""
    por = Actor(TipoDeActor.CLI, getpass.getuser())
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=1)
    try:
        with bd.transaccion() as tx:
            if argumentos.accion == "frenar-todo":
                frenar_todo(tx, por=por, motivo=argumentos.motivo)
            elif argumentos.accion != "estado":
                cambiar(
                    tx,
                    por=por,
                    cambios={Freno(argumentos.clave): argumentos.accion == "reanudar"},
                    motivo=argumentos.motivo,
                )
            vigente = estado(tx)
    finally:
        bd.cerrar()
    for freno in Freno:
        if (evento := vigente.get(freno)) is None:
            print(f"{freno}: detenido (sin eventos)")  # falla cerrado, como `habilitado`
            continue
        como = "habilitado" if evento.habilitado else "detenido"
        print(
            f"{freno}: {como} desde {evento.en:%Y-%m-%d %H:%M}"
            f" ({evento.actor.tipo} {evento.actor.etiqueta}): {evento.motivo}"
        )
    return 0


def _bajar_modelos(entorno: Entorno) -> int:
    """Lo que hace el servicio `modelos`: los nombres salen de `configuracion_capacidad`."""
    motores_ = motores(entorno)
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=1)
    try:
        configuracion = ConfiguracionDeModelos(bd=bd, validar=partial(validar, motores=motores_))
        # Valida como `web` y el trabajador: con una fila inválida no arrancarían igual (DEC-87).
        vigente = configuracion.instantanea().por_capacidad
    finally:
        bd.cerrar()
    modelos = {c.modelo for c in vigente.values() if c.motor in motores_ and c.modelo}
    # Los dos Ollama comparten `hvmvp_modelos` y el de embeddings solo lo lee: todo se baja
    # por el de generación.
    generacion = motores_["ollama"]
    faltantes = generacion.faltantes(modelos)
    if faltantes is None:
        print(f"El motor `ollama` ({generacion.url}) no responde.", file=sys.stderr)
        return 1
    try:
        for modelo in sorted(faltantes):
            print(f"Bajando {modelo}…", flush=True)
            generacion.bajar(modelo)
    except MotorNoDisponible as error:
        print(error, file=sys.stderr)
        return 1
    print("Modelos listos.")
    return 0


def _reindexar(entorno: Entorno) -> int:
    """`reindexar`, del Administrador técnico (CU-21); los trabajos los corre el trabajador
    `--tipos indexacion`."""
    motores_ = motores(entorno)
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=1)
    try:
        indexacion = armar_indexacion(
            bd=bd,
            capa_para=partial(capa_para, motores=motores_),
            validar=partial(validar, motores=motores_),
        )
        with bd.transaccion() as tx:
            encoladas = indexacion.reconstruir(tx, actor=Actor(TipoDeActor.CLI, getpass.getuser()))
    except ProcesamientoDetenido as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        bd.cerrar()
    print(f"Indexación encolada para {encoladas} Pieza(s) Validada(s).")
    return 0


def _cargar_carreras(entorno: Entorno, ruta: Path) -> int:
    """`catalogo carreras --cargar`, del Administrador técnico (R-25). Todo o nada (R-42)."""
    try:
        with ruta.open("rb") as origen:
            filas = leer_carreras(origen, ruta.name)
        bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=1)
        try:
            with bd.transaccion() as tx:
                resultado = cargar_carreras(
                    tx,
                    filas,
                    por=Actor(TipoDeActor.CLI, getpass.getuser()),
                    auditoria=RegistroDeAuditoria(bd=bd),
                )
        finally:
            bd.cerrar()
    except (CargaRechazada, OSError) as error:
        print(f"No se cargó ninguna Carrera: {error}", file=sys.stderr)
        return 1
    print(
        f"Carreras: {len(resultado.insertadas)} nueva(s), {len(resultado.actualizadas)}"
        f" actualizada(s), {len(resultado.desactivadas)} desactivada(s)."
    )
    return 0


def _agregar_alta_persona(comandos: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    alta = comandos.add_parser(
        "alta-persona",
        help="da de alta una persona, le cambia el rol o la da de baja, como hv_app",
        description="Con --nombre y --rol da de alta; con --rol solo, cambia el rol; con"
        " --inactiva da de baja (no borra: conserva lo firmado y revoca sus sesiones) y con"
        " --activa la reactiva (DEC-113). Es la única vía de crear al primer Administrador"
        " técnico (R-02).",
    )
    alta.add_argument("--email", required=True)
    alta.add_argument("--nombre")
    estado_ = alta.add_mutually_exclusive_group()
    estado_.add_argument("--rol", choices=[rol.value for rol in Rol])
    estado_.add_argument("--inactiva", action="store_true")
    estado_.add_argument("--activa", action="store_true")


def _alta_persona(entorno: Entorno, argumentos: argparse.Namespace) -> int:
    """Actor `cli` con el usuario del sistema operativo: el comando puede todo (R-02)."""
    if argumentos.nombre is not None and argumentos.rol is None:
        raise EntornoInvalido("Un alta lleva --nombre y --rol.")
    if not (argumentos.rol or argumentos.inactiva or argumentos.activa):
        raise EntornoInvalido("Falta qué hacer: --rol, --inactiva o --activa.")
    por = Actor(TipoDeActor.CLI, getpass.getuser())
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=1)
    auditoria = RegistroDeAuditoria(bd=bd)
    padron = Padron(
        auditoria=auditoria,
        sesiones=Sesiones(bd=bd, auditoria=auditoria),
        dominios_permitidos=entorno.dominios_permitidos,
    )
    try:
        with bd.transaccion() as tx:
            if argumentos.nombre is not None:
                padron.alta(tx, argumentos.email, argumentos.nombre, Rol(argumentos.rol), por=por)
                print("Habilitada, todavía no ingresó.")
                return 0
            persona = padron.buscar(tx, argumentos.email)
            if persona is None:
                raise PersonaNoEncontrada
            if argumentos.rol:
                padron.cambiar_rol(tx, persona.usuario_id, Rol(argumentos.rol), por=por)
            elif argumentos.inactiva:
                padron.desactivar(tx, persona.usuario_id, por=por)
            else:
                padron.reactivar(tx, persona.usuario_id, por=por)
    except RechazoDelPadron as error:
        print(f"{error.codigo}: {error}", file=sys.stderr)
        return 1
    finally:
        bd.cerrar()
    print("Listo.")
    return 0


def _agregar_tokens(comandos: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`emitir-token` y `revocar-token`, del Administrador técnico (spec, módulo `agentes`)."""
    emitir = comandos.add_parser(
        "emitir-token",
        help="emite el token de un Agente externo, como hv_app",
        description="Emite un token del Agente y lo imprime una sola vez; si el Agente no existe,"
        " lo da de alta con --alcances. No enciende la API de Agentes: eso es"
        " `interruptor reanudar api_agentes_habilitada` (DEC-136).",
    )
    emitir.add_argument("agente", help="nombre del Agente, p. ej. «Agente 1»")
    emitir.add_argument(
        "--alcances", nargs="+", choices=[scope.value for scope in Scope], default=[]
    )
    revocar = comandos.add_parser(
        "revocar-token", help="revoca un token de Agente; rige en el pedido siguiente"
    )
    revocar.add_argument("prefijo", help="el id público del token, `hv_` y 8 hex")


def _emitir_token(entorno: Entorno, agente: str, alcances: Sequence[str]) -> int:
    """Como `hv_app`, con actor `cli`: el alta y la emisión quedan auditadas con el prefijo."""
    por = Actor(TipoDeActor.CLI, getpass.getuser())
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=1)
    credenciales = Credenciales(bd=bd, auditoria=RegistroDeAuditoria(bd=bd))
    try:
        with bd.transaccion() as tx:
            agente_id = credenciales.agente_id(tx, agente)
            if agente_id is not None and alcances:
                print(
                    f"«{agente}» ya existe: sus alcances no se cambian por comando.",
                    file=sys.stderr,
                )
                return 2
            if agente_id is None:
                if not alcances:
                    print(f"«{agente}» es nuevo: decí sus --alcances.", file=sys.stderr)
                    return 2
                agente_id = credenciales.crear_agente(
                    tx, nombre=agente, scopes=frozenset(map(Scope, alcances)), por=por
                )
            emitido = credenciales.emitir(tx, agente_id=agente_id, por=por)
    except AgenteExistente:
        # Otro `emitir-token` dio de alta el mismo nombre a la vez; esta transacción no dejó nada.
        print(
            f"«{agente}» se dio de alta a la vez desde otro comando: volvé a correrlo.",
            file=sys.stderr,
        )
        return 1
    finally:
        bd.cerrar()
    print(f"Token de «{agente}» (se muestra una sola vez; entregalo fuera de banda):")
    print(emitido.token)
    print(f"Prefijo {emitido.prefijo}, vence el {emitido.vence_el:%Y-%m-%d}.")
    print("La API de Agentes sigue como estaba: la enciende `interruptor reanudar`.")
    return 0


def _revocar_token(entorno: Entorno, prefijo: str) -> int:
    """Como `hv_app`, con actor `cli`; rige en el pedido siguiente. Lo que no tiene forma de
    prefijo no se repite: puede ser el token entero pegado por error."""
    if not es_prefijo(prefijo):
        print("Eso no es un prefijo de token (`hv_` y 8 hex): no se revocó nada.", file=sys.stderr)
        return 2
    por = Actor(TipoDeActor.CLI, getpass.getuser())
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=1)
    credenciales = Credenciales(bd=bd, auditoria=RegistroDeAuditoria(bd=bd))
    try:
        with bd.transaccion() as tx:
            credenciales.revocar_token(tx, credenciales.token_id(tx, prefijo), por=por)
    except TokenInexistente as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        bd.cerrar()
    print(f"Token {prefijo} revocado: el pedido siguiente da 401.")
    return 0


def _agregar_matriz(comandos: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    matriz = comandos.add_parser(
        "matriz",
        help="la matriz requisito → test, de desarrollo (historia 221)",
        description="Recolecta la suite, cruza sus marcadores con la lista de requisitos y escribe"
        " matriz.md, matriz.csv y matriz-inversa.csv con el último resultado de cada test (de"
        " .resultados/junit-*.xml) y el último número de M1 a M4. Sale con 1 si un requisito"
        " que entra no tiene test o si un marcador no apunta a ningún requisito. Necesita"
        " pytest y la carpeta tests: no corre en la imagen.",
    )
    matriz.add_argument("--salida", type=Path, help="dónde escribir; por defecto, .resultados")
    matriz.add_argument(
        "--corridas", type=Path,
        help="las corridas del banco; por defecto, HV_REFERENCIA/corridas",
    )


def _matriz(entorno: Entorno, argumentos: argparse.Namespace) -> int:
    from historia_viva import matriz

    try:
        tests = matriz.recolectar()
    except matriz.RecoleccionFallida as error:
        print(error, file=sys.stderr)
        return 1
    corridas = argumentos.corridas or (entorno.referencia and entorno.referencia / "corridas")
    resultado = matriz.armar(
        matriz.leer_requisitos(), tests, matriz.leer_resultados(),
        matriz.ultimas_mediciones(corridas),
    )
    rutas = matriz.escribir(resultado, argumentos.salida or matriz.RAIZ / ".resultados")
    for nodeid, marcadores in resultado.inexistentes.items():
        print(f"Marcador sin requisito: {nodeid} ({', '.join(marcadores)})", file=sys.stderr)
    if resultado.huecos:
        print("Huecos (entran y no tienen test):", *resultado.huecos, file=sys.stderr)
    print("Escrito:", *rutas, sep="\n  ")
    return 1 if resultado.falla else 0


def _agregar_suprimir(comandos: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    suprimir = comandos.add_parser(
        "suprimir",
        help="supresión efectiva de una Pieza (runbook R14), como hv_migrador",
        description="Borra la Pieza, todas las versiones de su Ficha, sus fragmentos y su"
        " original, y registra la supresión sin contenido (`pieza.supresion`). No es el retiro:"
        " no se puede restaurar. Se habilita con HV_DATOS_PERSONALES_HABILITADOS=true (C31)."
        " Antes, el volcado previo del runbook R14.",
    )
    suprimir.add_argument("pieza_id", type=UUID)
    suprimir.add_argument(
        "--motivo", required=True, help="la referencia del pedido, sin nombres ni números personales"
    )
    suprimir.add_argument(
        "--previa", action="store_true", help="dice qué borraría y lo revierte: no borra nada"
    )


def _suprimir(entorno: Entorno, argumentos: argparse.Namespace) -> int:
    """Como `hv_migrador`, con actor `cli` y el usuario del sistema operativo (runbook R14)."""
    bd = Bd(_exigida(entorno.dsn_migrador, "HV_BD_URL_MIGRADOR"), tamano_pool=1)
    supresion = Supresion(
        bd=bd,
        fichas=ModuloFichasPostgres(bd=bd),
        originales=AlmacenDeOriginales(_exigida(entorno.originales, "HV_ORIGINALES")),
        auditoria=RegistroDeAuditoria(bd=bd),
        habilitada=entorno.datos_personales_habilitados,
    )
    try:
        hecho = supresion.suprimir(
            argumentos.pieza_id,
            motivo=argumentos.motivo,
            por=Actor(TipoDeActor.CLI, getpass.getuser()),
            previa=argumentos.previa,
        )
    except (SupresionDeshabilitada, MotivoDeSupresionInvalido) as error:
        print(error, file=sys.stderr)
        return 2
    except PiezaNoEncontrada:
        print(f"No hay una Pieza {argumentos.pieza_id}: no se borró nada.", file=sys.stderr)
        return 1
    except PiezaEnProceso as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        bd.cerrar()
    filas = ", ".join(f"{tabla} {n}" for tabla, n in hecho.filas.items() if n)
    if argumentos.previa:
        print(f"Previa, no se borró nada. Se borrarían: {filas}.")
    else:
        print(f"Pieza {hecho.pieza_id} suprimida el {hecho.suprimida_el:%Y-%m-%d %H:%M}: {filas}.")
        print(f"SHA-256 del original: {hecho.huella_original}.")
    otras = [str(otra) for otra in hecho.otras_con_la_misma_huella if otra != hecho.pieza_id]
    if otras:
        print(f"Otras Piezas con el mismo original: {', '.join(otras)}.")
    if hecho.drive_file_id:
        print(f"Tenía réplica en Drive: borrá a mano el archivo {hecho.drive_file_id}.")
    if hecho.error_del_original:
        print(f"{hecho.error_del_original}. Borralo a mano.", file=sys.stderr)
        return 1
    if not argumentos.previa:
        print("Original borrado." if hecho.original_borrado else "No tenía original en disco.")
        print("Falta avisar a Juan (Agente 1). Los backups la conservan hasta vencer (R-DP2).")
    return 0


def _agregar_reprocesar(comandos: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    reprocesar = comandos.add_parser(
        "reprocesar",
        help="reencola los trabajos fallidos, como hv_app",
        description="Encola un trabajo nuevo por cada Pieza cuyo último trabajo terminó"
        " fallido, de un Lote o de un tipo (DEC-86). Las Piezas que ya fallaron 6 veces se"
        " saltean y se listan; --forzar las encola igual y queda auditado (DEC-137).",
    )
    reprocesar.add_argument("--fallidos", action="store_true", required=True)
    reprocesar.add_argument("--lote", type=UUID, metavar="ID")
    reprocesar.add_argument("--tipo", choices=[tipo.value for tipo in REPROCESABLES])
    reprocesar.add_argument("--forzar", action="store_true")


def _reprocesar_fallidos(entorno: Entorno, argumentos: argparse.Namespace) -> int:
    """Actor `cli` con el usuario del sistema operativo; el trabajador los toma por sondeo."""
    motores_ = motores(entorno)
    bd = Bd(_exigida(entorno.dsn_app, "HV_BD_URL"), tamano_pool=1)
    try:
        encolados, salteadas = armar_grafo(
            bd=bd,
            capa_para=partial(capa_para, motores=motores_),
            validar=partial(validar, motores=motores_),
            directorio_originales=_exigida(entorno.originales, "HV_ORIGINALES"),
            avisar=lambda: None,
            analizar_video=None,  # el comando no recibe videos
            modo_de_ingreso=MetodoAutenticacion(entorno.auth),
            origen=entorno.origen,
        ).ingesta.reprocesar_fallidos(
            por=Actor(TipoDeActor.CLI, getpass.getuser()),
            lote_id=argumentos.lote,
            tipo=None if argumentos.tipo is None else TipoDeTrabajo(argumentos.tipo),
            forzar=argumentos.forzar,
        )
    finally:
        bd.cerrar()
    print(f"{encolados} encolado(s).")
    if salteadas:
        print(f"Salteadas por el tope de {TOPE_DE_REPROCESOS} reprocesos (--forzar las encola):")
        for pieza_id in salteadas:
            print(f"  {pieza_id}")
    return 0


def _agregar_banco(comandos: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    medidas = comandos.add_parser(
        "banco",
        help="el banco de pruebas, como hv_app",
        description="Valida el conjunto de referencia de HV_REFERENCIA, mide M1, M2 y M4 y compara"
        " modelos candidatos. Sin tablas:"
        " cada corrida escribe un JSON, un CSV y una tabla markdown (DEC-97). Corre con el"
        " procesamiento detenido: no encola nada (DEC-96).",
    ).add_subparsers(dest="medida", required=True)
    medidas.add_parser(
        "cargar-referencia",
        help="valida piezas.csv y consultas.csv sin escribir en la base",
    )
    m1 = medidas.add_parser(
        "m1",
        help="M1: recall@10 sobre las 10 primeras Piezas de la búsqueda, con IC de Wilson",
    )
    m1.add_argument(
        "--conjunto", choices=("ajuste", "prueba"), required=True,
        help="prueba se corre oficialmente una sola vez por huella (DEC-54)",
    )
    m1.add_argument(
        "--salida", type=Path, metavar="DIR",
        help="dónde escribir; por defecto, HV_REFERENCIA/corridas",
    )
    m2 = medidas.add_parser(
        "m2",
        help="M2 sobre las Validadas (se niega con indexaciones pendientes) o sobre un informe",
    )
    m2.add_argument(
        "--informe", type=UUID, metavar="ID", help="mide sobre las Piezas del informe (R-21)"
    )
    m2.add_argument(
        "--salida", type=Path, metavar="DIR",
        help="dónde escribir; por defecto, HV_REFERENCIA/corridas",
    )
    m3 = medidas.add_parser(
        "m3",
        help="M3: tiempo hasta las fuentes de la Conversación (A caliente, B frío, C con una foto"
        " describiéndose), sin guardar nada",
    )
    m3.add_argument("--condicion", choices=("A", "B", "C"), required=True)
    m3.add_argument(
        "--conjunto", choices=("ajuste", "prueba"), default="prueba",
        help="de dónde salen las consultas (las que tienen Pieza esperada)",
    )
    m3.add_argument(
        "--cpu", metavar="TEXTO",
        help="modelo de CPU e hilos, para las notas; por defecto, lo que diga el sistema",
    )
    m3.add_argument(
        "--foto", type=Path, metavar="ARCHIVO", help="con C, la foto que se describe en bucle"
    )
    m3.add_argument(
        "--consultas", type=int, metavar="N",
        help="las de primer turno; por defecto, 30 (A y C) y 5 (B)",
    )
    m3.add_argument(
        "--salida", type=Path, metavar="DIR",
        help="dónde escribir; por defecto, HV_REFERENCIA/corridas",
    )
    m4 = medidas.add_parser(
        "m4",
        help="M4: la checklist de la SEU de efemérides y Relatos, por CSV (R-13)",
    )
    m4.add_argument(
        "--plantilla", action="store_true",
        help="escribe el CSV que completa la SEU, con todas las efemérides y todos los Relatos",
    )
    m4.add_argument(
        "--evaluador", action="append", default=[], metavar="NOMBRE",
        help="con --plantilla, una tanda de filas por evaluador (se repite)",
    )
    m4.add_argument(
        "--evaluaciones", type=Path, metavar="ARCHIVO",
        help="el CSV completado; por defecto, HV_REFERENCIA/evaluaciones.csv",
    )
    m4.add_argument(
        "--salida", type=Path, metavar="DIR",
        help="dónde escribir; por defecto, HV_REFERENCIA/corridas",
    )
    comparar = medidas.add_parser(
        "comparar",
        help="RF-28: compara los candidatos de candidatos.csv; informa el ganador y el mejor"
        " no chino",
    )
    comparar.add_argument(
        "--capacidad", choices=("resumir", "describir_imagen", "embeber", "responder"),
        required=True,
        help="embeber mide recall@10 con las consultas de ajuste; no usa --repeticiones",
    )
    comparar.add_argument(
        "--modelo", action="append", default=[], metavar="M",
        help="un candidato de candidatos.csv (se repite); sin él, todos los de la capacidad",
    )
    comparar.add_argument("--repeticiones", type=int, default=3, metavar="N")
    comparar.add_argument(
        "--salida", type=Path, metavar="DIR",
        help="dónde escribir; por defecto, HV_REFERENCIA/corridas",
    )
    ingesta = medidas.add_parser(
        "ingesta",
        help="invenciones, cobertura, error del OCR y tiempo por Pieza de un corpus etiquetado"
        " (formato de etiquetas.json en historia_viva.banco.ingesta); no usa HV_REFERENCIA",
    )
    ingesta.add_argument(
        "--corpus", type=Path, metavar="DIR", required=True,
        help="los originales y etiquetas.json; su candidatos.csv, si tiene, manda",
    )
    ingesta.add_argument(
        "--modelo", action="append", default=[], metavar="M",
        help="un candidato de describir_imagen de candidatos.csv (se repite); sin él, el"
        " modelo de la fila vigente",
    )
    ingesta.add_argument(
        "--instruccion", metavar="ID",
        help="la instrucción de describir_imagen con que se mide, p. ej. describir_fotografia"
        " para la línea de base (139); por defecto, la de la fila vigente",
    )
    ingesta.add_argument(
        "--hardware", metavar="TEXTO",
        help="la máquina, para el informe (p. ej. «sin GPU»); por defecto, la CPU que diga el"
        " sistema",
    )
    ingesta.add_argument(
        "--salida", type=Path, metavar="DIR", help="dónde escribir; por defecto, DIR/corridas"
    )


def _banco(entorno: Entorno, argumentos: argparse.Namespace) -> int:
    """`banco`, del Administrador técnico (historia 168). No consulta el interruptor (DEC-96)."""
    from historia_viva.banco.aceptacion import (
        EsperadasNoValidadas,
        IndexacionPendiente,
        es_oficial,
        escribir_m1,
        escribir_m2,
        tabla_m1,
        tabla_m2,
    )
    from historia_viva.banco.comparacion import CandidatoInvalido
    from historia_viva.banco.m4 import EvaluacionesInvalidas, escribir_m4, tabla_m4
    from historia_viva.banco.referencia import ReferenciaInvalida, cargar_referencia, huella

    if argumentos.medida == "ingesta":
        return _ingesta(entorno, argumentos)
    referencia = _exigida(entorno.referencia, "HV_REFERENCIA")
    if argumentos.medida == "m3":
        return _m3(entorno, referencia, argumentos)
    banco_ = componer_banco(entorno)
    try:
        if argumentos.medida == "comparar":
            return _comparar(banco_, referencia, argumentos)
        with banco_.bd.transaccion() as tx:
            if argumentos.medida == "cargar-referencia":
                conjunto = cargar_referencia(tx, referencia, fichas=banco_.fichas)
                prueba = sum(c.conjunto == "prueba" for c in conjunto.consultas)
                print(
                    f"Referencia válida: {len(conjunto.piezas)} Pieza(s) y"
                    f" {len(conjunto.consultas)} consulta(s), {prueba} de prueba."
                    f" hash_referencia: {conjunto.hash_referencia}"
                )
                return 0
            if argumentos.medida == "m1":
                conjunto = cargar_referencia(tx, referencia, fichas=banco_.fichas)
                m1 = banco_.aceptacion.medir_m1(tx, conjunto, argumentos.conjunto)
                salida = argumentos.salida or referencia / "corridas"
                oficial = es_oficial(salida, m1.conjunto, conjunto.hash_referencia)
                rutas = escribir_m1(
                    salida, m1, hash_referencia=conjunto.hash_referencia,
                    configuracion_efectiva=banco_.aceptacion.configuracion_efectiva(tx),
                    oficial=oficial, momento=datetime.now().astimezone(),
                )
                print(tabla_m1(m1, hash_referencia=conjunto.hash_referencia, oficial=oficial))
                print("Escrito:", *rutas, sep="\n  ")
                return 0
            if argumentos.medida == "m4":
                salida = argumentos.salida or referencia / "corridas"
                momento = datetime.now().astimezone()
                if argumentos.plantilla:
                    rutas = banco_.aceptacion.plantilla_m4(
                        tx, salida, evaluadores=argumentos.evaluador, momento=momento
                    )
                    print("Plantilla de M4: completala y guardala como"
                          f" {referencia / 'evaluaciones.csv'}.")
                    print("Escrito:", *rutas, sep="\n  ")
                    return 0
                archivo = argumentos.evaluaciones or referencia / "evaluaciones.csv"
                m4 = banco_.aceptacion.medir_m4(tx, archivo)
                rutas = escribir_m4(salida, m4, momento=momento)
                print(tabla_m4(m4))
                print("Escrito:", *rutas, sep="\n  ")
                return 0
            hash_referencia = huella(referencia)
            piezas = (
                None if argumentos.informe is None
                else banco_.informes.piezas(tx, argumentos.informe)
            )
            resultado = banco_.aceptacion.medir_m2(tx, piezas)
            configuracion = banco_.aceptacion.configuracion_efectiva(tx)
    except ReferenciaInvalida as error:
        print(f"La referencia no sirve:\n{error}", file=sys.stderr)
        return 1
    except CandidatoInvalido as error:
        print(error, file=sys.stderr)
        return 1
    except EvaluacionesInvalidas as error:
        print(f"Las evaluaciones no sirven:\n{error}", file=sys.stderr)
        return 1
    except FileNotFoundError as error:
        print(f"No está el archivo de evaluaciones: {error.filename}.", file=sys.stderr)
        return 1
    except InformeNoEncontrado:
        print(f"No existe el informe {argumentos.informe}.", file=sys.stderr)
        return 1
    except (
        IndexacionPendiente, EsperadasNoValidadas, BuscadorNoDisponible, IndiceNoDisponible,
        LimiteExcedido,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        banco_.bd.cerrar()
    rutas = escribir_m2(
        argumentos.salida or referencia / "corridas",
        resultado,
        hash_referencia=hash_referencia,
        configuracion_efectiva=configuracion,
        informe_id=argumentos.informe,
        momento=datetime.now().astimezone(),
    )
    print(tabla_m2(resultado, hash_referencia=hash_referencia, informe_id=argumentos.informe))
    print("Escrito:", *rutas, sep="\n  ")
    return 0


def _comparar(
    banco_: ComponentesDelBanco, referencia: Path, argumentos: argparse.Namespace
) -> int:
    """`banco comparar` (RF-28, DEC-132). La transacción dura lo que tarda leer la referencia y
    la fila vigente: la corrida, que puede llevar horas, no la tiene abierta."""
    from historia_viva.banco.comparacion import escribir_comparacion, leer_candidatos
    from historia_viva.banco.embeber import (
        BancoDeEmbeber,
        escribir_comparacion_embeber,
        leer_corpus,
    )
    from historia_viva.banco.referencia import cargar_referencia

    capacidad = Capacidad(argumentos.capacidad)
    de_la_capacidad = [c for c in leer_candidatos(referencia) if c.capacidad is capacidad]
    if faltan := set(argumentos.modelo) - {c.modelo for c in de_la_capacidad}:
        print(f"No están en candidatos.csv para {capacidad.value}: {', '.join(sorted(faltan))}.",
              file=sys.stderr)
        return 1
    candidatos = [
        c for c in de_la_capacidad if c.modelo in argumentos.modelo or not argumentos.modelo
    ]
    if not candidatos:
        print(f"candidatos.csv no tiene candidatos para {capacidad.value}.", file=sys.stderr)
        return 1
    with banco_.bd.transaccion() as tx:
        conjunto = cargar_referencia(tx, referencia, fichas=banco_.fichas)
        vigente = banco_.configuracion.vigente(tx)
        if capacidad is Capacidad.EMBEBER:
            corpus = leer_corpus(tx, conjunto, fichas=banco_.fichas)
    momento = datetime.now().astimezone()
    salida = argumentos.salida or referencia / "corridas"
    if capacidad is Capacidad.EMBEBER:
        rutas, tabla = escribir_comparacion_embeber(
            salida,
            BancoDeEmbeber(banco_.comparacion).comparar(
                vigente, corpus, candidatos, momento=momento,
                progreso=lambda modelo, texto: print(f"{modelo} · {texto}", file=sys.stderr,
                                                     flush=True),
            ),
            vigente=vigente, hash_referencia=conjunto.hash_referencia, momento=momento,
        )
        print(tabla)
        print("Escrito:", *rutas, sep="\n  ")
        return 0
    evaluaciones = banco_.comparacion.comparar(
        vigente, conjunto, referencia, candidatos, repeticiones=argumentos.repeticiones,
        momento=momento,
        progreso=lambda modelo, m: print(
            f"{modelo} · {m.referencia_id} · repetición {m.repeticion}:"
            f" {m.tiempo_ms / 1000:.1f} s" + (f" · {m.error}" if m.error else ""),
            file=sys.stderr, flush=True,
        ),
    )
    rutas, tabla = escribir_comparacion(
        salida, capacidad, evaluaciones,
        hash_referencia=conjunto.hash_referencia, momento=momento,
    )
    print(tabla)
    print("Escrito:", *rutas, sep="\n  ")
    return 0


def _ingesta(entorno: Entorno, argumentos: argparse.Namespace) -> int:
    """`banco ingesta` (137). La transacción dura lo que tarda leer la fila vigente: la corrida
    llama a los modelos sin una abierta."""
    from historia_viva.banco.comparacion import CandidatoInvalido, leer_candidatos
    from historia_viva.banco.ingesta import (
        MedicionDeIngesta,
        escribir_ingesta,
        leer_corpus,
        medir_ingesta,
    )
    from historia_viva.banco.referencia import ReferenciaInvalida
    from historia_viva.capa_modelos.configuracion import ConfiguracionEfectiva

    try:
        corpus = leer_corpus(argumentos.corpus)
        de_la_capacidad = [
            c for c in leer_candidatos(argumentos.corpus)
            if c.capacidad is Capacidad.DESCRIBIR_IMAGEN
        ]
    except ReferenciaInvalida as error:
        print(f"El corpus no sirve:\n{error}", file=sys.stderr)
        return 1
    banco_ = componer_banco(entorno)
    try:
        with banco_.bd.transaccion() as tx:
            vigente = banco_.configuracion.vigente(tx)
        if argumentos.instruccion:  # `preparar` la valida contra la capacidad
            vigente = {**vigente, Capacidad.DESCRIBIR_IMAGEN: replace(
                vigente[Capacidad.DESCRIBIR_IMAGEN], instruccion_id=argumentos.instruccion)}
        pedidos = argumentos.modelo or [vigente[Capacidad.DESCRIBIR_IMAGEN].modelo]
        if faltan := set(pedidos) - {c.modelo for c in de_la_capacidad}:
            print("No están en candidatos.csv para describir_imagen:"
                  f" {', '.join(sorted(faltan))}.", file=sys.stderr)
            return 1
        transcribir = vigente[Capacidad.TRANSCRIBIR]
        hardware = argumentos.hardware or _cpu()
        corridas = []

        def avisar(modelo: str, m: MedicionDeIngesta) -> None:
            print(f"{modelo} · {m.archivo}: {m.tiempo_ms / 1000:.1f} s"
                  + (f" · {m.error}" if m.error else ""), file=sys.stderr, flush=True)

        for candidato in (c for c in de_la_capacidad if c.modelo in pedidos):
            capa, efectiva = banco_.comparacion.preparar(vigente, candidato)
            corridas.append(medir_ingesta(
                capa, candidato, efectiva, corpus, argumentos.corpus,
                tope_transcribir_s=transcribir.parametros.get("timeout_s"),
                progreso=partial(avisar, candidato.modelo),
            ))
    except CandidatoInvalido as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        banco_.bd.cerrar()
    rutas, tabla = escribir_ingesta(
        argumentos.salida or argumentos.corpus / "corridas", corridas,
        corpus=argumentos.corpus, hardware=hardware,
        transcribir=ConfiguracionEfectiva({Capacidad.TRANSCRIBIR: transcribir}).a_json(),
        momento=datetime.now().astimezone(),
    )
    print(tabla)
    print("Escrito:", *rutas, sep="\n  ")
    return 0


def _consultas_de_referencia(entorno: Entorno, comparar: Path | None) -> int:
    """`consultas-de-referencia`: sin `comparar`, el JSON en la última línea de la salida; con
    él, las diferencias."""
    banco_ = componer_banco(entorno)
    try:
        obtenidas = consultas_de_referencia(banco_.busqueda)
    except (BuscadorNoDisponible, IndiceNoDisponible, LimiteExcedido) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        banco_.bd.cerrar()
    if comparar is None:
        print(json.dumps(obtenidas, ensure_ascii=False))
        return 0
    # La última línea, como la lee el respaldo: antes puede haber líneas del registro, y
    # `Set-Content -Encoding utf8` de PowerShell 5.1 escribe un BOM.
    esperadas = json.loads(comparar.read_text(encoding="utf-8-sig").strip().splitlines()[-1])
    # También el `.referencia.json` de un volcado (runbook R9).
    esperadas = esperadas.get("consultas", esperadas)
    distintas = [n for n in sorted(esperadas.keys() | obtenidas.keys())
                 if esperadas.get(n) != obtenidas.get(n)]  # fmt: skip
    for nombre in distintas:
        print(f"{nombre}: esperadas {esperadas.get(nombre)}, obtenidas {obtenidas.get(nombre)}")
    if distintas:
        return 1
    print("Las 3 consultas de referencia dan lo esperado.")
    return 0


def _al_sigterm() -> asyncio.Event:
    """Se enciende con el SIGTERM de `docker stop`."""
    parar = asyncio.Event()
    with suppress(NotImplementedError):  # Windows no tiene señales en el bucle de eventos
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, parar.set)
    return parar


async def _correr(procesos: Sequence[Callable[..., Awaitable[None]]]) -> None:
    """`trabajador --tipos`: la cola y el planificador en el mismo proceso, con un solo SIGTERM
    para los dos (un segundo `add_signal_handler` reemplazaría al primero)."""
    parar = _al_sigterm()
    await asyncio.gather(*(proceso(parar=parar) for proceso in procesos))


async def _trabajar(trabajador: Trabajador, bd: Bd, parar: asyncio.Event | None = None) -> None:
    """Hasta Ctrl+C o `parar` (el SIGTERM): deja terminar el trabajo en curso y cierra la
    base."""
    parar = parar or _al_sigterm()
    try:
        await trabajador.iniciar()
        print("Trabajador en marcha.", flush=True)
        await parar.wait()
    finally:
        await trabajador.detener()
        bd.cerrar()


async def _replicar(replicador: Replicador, parar: asyncio.Event | None = None) -> None:
    """La réplica a Drive en su hilo: al parar, la subida en curso corta en el tramo siguiente
    y su fila vuelve a `pendiente`. La base la cierra `_trabajar`."""
    parar = parar or _al_sigterm()
    detener = threading.Event()
    hilo = asyncio.create_task(asyncio.to_thread(replicador.bucle, detener))
    try:
        print("Réplica a Drive en marcha.", flush=True)
        await parar.wait()
    finally:
        detener.set()
        await asyncio.wait({hilo}, timeout=_ESPERA_AL_DETENER_S)


async def _planificar(
    planificador: Planificador, bd: Bd, parar: asyncio.Event | None = None
) -> None:
    """Como `_trabajar`: al parar, la corrida corta antes del objetivo siguiente y el pedido
    queda marcado para el próximo arranque."""
    parar = parar or _al_sigterm()
    detener = threading.Event()
    hilo = asyncio.create_task(asyncio.to_thread(planificador.bucle, detener))
    try:
        print("Planificador de efemérides en marcha.", flush=True)
        await parar.wait()
    finally:
        detener.set()
        await asyncio.wait({hilo}, timeout=_ESPERA_AL_DETENER_S)
        bd.cerrar()
