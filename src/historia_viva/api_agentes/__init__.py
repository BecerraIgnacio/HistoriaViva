"""La superficie `/v1` del contrato con el Agente 1 (spec, módulo `api_agentes`; DEC-33).

Su formato de error, el freno `api_agentes_habilitada` (037) y el enrutador `/v1` (097), con el
freno cableado a todas sus rutas. No usa el RFC 9457 de `api`: el contrato fija
`{codigo, mensaje, solicitud_id}`. Solo traduce: la búsqueda es de `busqueda` y el token, de
`agentes`. No importa `psycopg`, `identidad` ni `api` (reglas de `test_arquitectura`).

Toda ruta de `/v1` (las de 099, 100 y 103 también) va en `router`, cuyas rutas son `_RutaV1`:
cualquier excepción que no sea `ErrorV1` se vuelve un `Error` del contrato (503, con su rastro en
el log) antes de llegar a los manejadores de `api`, así nada sale de `/v1` en RFC 9457. Lo que
tenga que ser 4xx se traduce a `ErrorV1` en la ruta.

Los cupos por token (`Cupos`, 098) se toman después del alcance (401 → 503 → 403 → 429 → 400 →
404, DEC-28) y toda respuesta con un token válido informa el de su ventana (`RateLimit-*`).

Sin `from __future__ import annotations`: FastAPI resuelve las anotaciones de la dependencia que
arma `con_api_agentes_habilitada`, y una anotación en texto no ve el `autenticar` local.
"""

import json
import logging
import math
import re
import secrets
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import (
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Send
from starlette.types import Scope as ScopeAsgi

from historia_viva import catalogos
from historia_viva.agentes import PrincipalAgente, Scope, TokenInvalido
from historia_viva.auditoria import Actor, TipoDeActor
from historia_viva.busqueda import LIMITE_MAXIMO, Consulta
from historia_viva.dominio.errores import ConsultaInvalida
from historia_viva.dominio.tipos import EtiquetaHallada, FiltroDeBusqueda, PiezaBuscable
from historia_viva.ingesta import (
    AporteRechazado,
    AporteRecibido,
    ClaveReutilizada,
    FuenteInexistente,
    normalizar_texto,
    normalizar_titulo,
)
from historia_viva.interruptor import Freno, habilitado_en

if TYPE_CHECKING:
    from historia_viva.efemerides import EfemerideEntregable, RelatoEntregable
    from historia_viva.indice import Fragmento

__all__ = [
    "RETRY_AFTER_INTERRUPTOR_S",
    "Cupos",
    "ErrorV1",
    "TopesDeCupo",
    "autenticar",
    "con_api_agentes_habilitada",
    "presentar_fragmento",
    "registrar_manejadores",
    "router",
]

RETRY_AFTER_INTERRUPTOR_S = 300
"""`Retry-After` del 503 por interruptor si `HV_RETRY_AFTER_S` no dice otra cosa (DEC-78). Es el
único lugar del valor por defecto: lo toman `Entorno` y, para la suite, `armar_grafo`."""
RETRY_AFTER_CAIDA_S = 30
"""`Retry-After` del 503 por base o motor caídos, o falla no prevista (spec §1.5)."""
_NO_DISPONIBLE = "Historia Viva no está disponible en este momento."
"""El mismo para el freno y para una caída: sin motivo en el cuerpo (DEC-78)."""
_TOKEN = re.compile(r"(hv_[0-9a-f]{8})_\S+")
_log = logging.getLogger(__name__)


class ErrorV1(Exception):
    """Un error de `/v1` con un código del contrato (DEC-67)."""

    def __init__(
        self, estado: int, codigo: str, mensaje: str, cabeceras: dict[str, str] | None = None
    ) -> None:
        super().__init__(mensaje)
        self.estado = estado
        self.codigo = codigo
        self.mensaje = mensaje
        self.cabeceras = cabeceras or {}


def _no_disponible(retry_after_s: int) -> ErrorV1:
    return ErrorV1(
        503, "servicio_no_disponible", _NO_DISPONIBLE, {"Retry-After": str(retry_after_s)}
    )


@dataclass(frozen=True)
class TopesDeCupo:
    """Los cupos por token de `/v1` (DEC-33, propuesta para C15); `HV_CUPO_CONSULTAS_MIN`,
    `HV_CUPO_APORTES_MIN` y `HV_CUPO_BUSQUEDAS_EN_VUELO` los cambian sin tocar código (DEC-113)."""

    consultas_min: int = 60
    aportes_min: int = 10
    busquedas_en_vuelo: int = 2


_VENTANA_S = 60


class Cupos:
    """El limitador de `/v1`, por token: una ventana fija de 60 s que empieza con el primer pedido
    (consultas `GET` y aportes `POST` por separado) y un tope de búsquedas en vuelo.

    Vive en la memoria de un único proceso: `web` es un solo uvicorn sin `workers`; con dos, cada
    uno contaría su propio cupo. Las rutas síncronas corren en hilos: todo pasa bajo un candado."""

    def __init__(
        self, topes: TopesDeCupo = TopesDeCupo(), *,  # noqa: B008 — dataclass congelada
        reloj: Callable[[], float] = time.monotonic
    ) -> None:
        self.topes = topes
        self._reloj = reloj
        self._candado = threading.Lock()
        self._ventanas: dict[tuple[UUID, bool], tuple[float, int]] = {}
        self._en_vuelo: Counter[UUID] = Counter()

    def _tope(self, aportes: bool) -> int:
        return self.topes.aportes_min if aportes else self.topes.consultas_min

    def _vigente(self, clave: tuple[UUID, bool], ahora: float) -> tuple[float, int]:
        inicio, usados = self._ventanas.get(clave, (ahora, 0))
        return (ahora, 0) if ahora >= inicio + _VENTANA_S else (inicio, usados)

    def _terna(
        self, aportes: bool, inicio: float, usados: int, ahora: float
    ) -> tuple[int, int, int]:
        tope = self._tope(aportes)
        return tope, max(tope - usados, 0), math.ceil(inicio + _VENTANA_S - ahora)

    def estado(self, token_id: UUID, metodo: str) -> tuple[int, int, int]:
        """`(cupo, restantes, segundos hasta el reinicio)` de la ventana vigente, sin gastar."""
        clave = (token_id, metodo == "POST")
        with self._candado:
            ahora = self._reloj()
            inicio, usados = self._vigente(clave, ahora)
        return self._terna(clave[1], inicio, usados, ahora)

    def tomar(self, token_id: UUID, metodo: str) -> tuple[tuple[int, int, int], bool]:
        """Gasta un pedido si queda cupo: `(estado, admitido)` en un solo paso, así las
        cabeceras y el `Retry-After` son los de este pedido aunque la ventana venza después."""
        clave = (token_id, metodo == "POST")
        with self._candado:
            ahora = self._reloj()
            inicio, usados = self._vigente(clave, ahora)
            admitido = usados < self._tope(clave[1])
            if admitido:
                usados += 1
                self._ventanas[clave] = (inicio, usados)
        return self._terna(clave[1], inicio, usados, ahora), admitido

    @contextmanager
    def buscando(self, token_id: UUID) -> Iterator[None]:
        """Una búsqueda en vuelo del token; pasado el tope, 429 sin esperar."""
        with self._candado:
            if self._en_vuelo[token_id] >= self.topes.busquedas_en_vuelo:
                # Se libera cuando termina otra búsqueda: no hay un reinicio que anunciar.
                raise _limite_superado("búsquedas en vuelo", 1)
            self._en_vuelo[token_id] += 1
        try:
            yield
        finally:
            with self._candado:
                self._en_vuelo[token_id] -= 1


def _limite_superado(que: str, retry_after_s: int) -> ErrorV1:
    return ErrorV1(
        429,
        "limite_superado",
        f"Se superó el límite de {que} para este token.",
        {"Retry-After": str(max(retry_after_s, 1))},
    )


def _respuesta(error: ErrorV1, solicitud_id: str) -> JSONResponse:
    return JSONResponse(
        {"codigo": error.codigo, "mensaje": error.mensaje, "solicitud_id": solicitud_id},
        status_code=error.estado,
        headers=error.cabeceras,
    )


async def _manejar_error_v1(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ErrorV1)
    return _respuesta(error, request.state.solicitud_id)


class _MiddlewareSolicitud:
    """Cada pedido a `/v1` recibe su `solicitud_id` (`X-Solicitud-Id`, el mismo del cuerpo de
    un error), `Cache-Control: no-store` y una línea de log JSON a stdout con el agente, el
    prefijo del token, la ruta plantilla, el estado y la duración; nunca el token (R-41). Una
    falla no prevista que escape de las rutas es 503, no 500 (contrato, códigos de error). Con
    un token válido, `RateLimit-*` de su ventana; el 401 no los lleva (`V1NoAutenticado`)."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: ScopeAsgi, receive: Receive, send: Send) -> None:
        ruta = scope["path"] if scope["type"] == "http" else ""
        if ruta != "/v1" and not ruta.startswith("/v1/"):
            await self._app(scope, receive, send)
            return
        solicitud_id = f"req_{secrets.token_hex(6)}"
        scope.setdefault("state", {})["solicitud_id"] = solicitud_id
        comienzo = time.monotonic()
        estado = 0

        async def enviar(mensaje: Message) -> None:
            nonlocal estado
            if mensaje["type"] == "http.response.start":
                estado = mensaje["status"]
                cabeceras = MutableHeaders(scope=mensaje)
                cabeceras["X-Solicitud-Id"] = solicitud_id
                cabeceras["Cache-Control"] = "no-store"
                # La terna que dejaron `autenticar` o `_tomar_cupo`, no una recalculada ahora.
                if terna := scope["state"].get("cupo"):
                    cupo, restantes, reinicio = terna
                    cabeceras["RateLimit-Limit"] = str(cupo)
                    cabeceras["RateLimit-Remaining"] = str(restantes)
                    cabeceras["RateLimit-Reset"] = str(reinicio)
            await send(mensaje)

        try:
            await self._app(scope, receive, enviar)
        except Exception:
            if estado:  # la respuesta ya salió: no hay otra que mandar
                raise
            _log.exception("Falla no prevista en /v1 (%s).", solicitud_id)
            caida = _no_disponible(RETRY_AFTER_CAIDA_S)
            await _respuesta(caida, solicitud_id)(scope, receive, enviar)
        finally:
            agente: PrincipalAgente | None = scope["state"].get("agente")
            plantilla = scope.get("route")
            linea = json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "solicitud_id": solicitud_id,
                    "agente": agente and agente.nombre,
                    "token_prefijo": agente and agente.token_prefijo,
                    "metodo": scope["method"],
                    "ruta": getattr(plantilla, "path", ruta),
                    "estado": estado,
                    "duracion_ms": int((time.monotonic() - comienzo) * 1000),
                },
                ensure_ascii=False,
            )
            print(_TOKEN.sub(r"\1_…", linea), flush=True)


def registrar_manejadores(app: FastAPI, *, retry_after_s: int, cupos: Cupos) -> None:
    """El manejador de `ErrorV1`, el middleware de la solicitud, el `Retry-After` del freno
    (`HV_RETRY_AFTER_S`) y los cupos por token."""
    app.state.retry_after_s = retry_after_s
    app.state.cupos = cupos
    app.add_exception_handler(ErrorV1, _manejar_error_v1)
    app.add_middleware(_MiddlewareSolicitud)


def con_api_agentes_habilitada(autenticar: Callable[..., Any]) -> Callable[..., Any]:
    """Dependencia que autentica con `autenticar` y después mira el freno: con
    `api_agentes_habilitada` apagada, 503 `servicio_no_disponible` con `Retry-After`. El orden
    lo da la dependencia (401 antes que 503): un anónimo no averigua el estado del sistema
    (DEC-109). Se lee sin caché y falla cerrado. Devuelve lo que devolvió `autenticar`."""

    def habilitada(request: Request, agente: Annotated[Any, Depends(autenticar)]) -> Any:
        if not habilitado_en(request.app.state.bd, Freno.API_AGENTES):
            raise _no_disponible(request.app.state.retry_after_s)
        return agente

    return habilitada


def autenticar(request: Request, authorization: Annotated[str, Header()] = "") -> PrincipalAgente:
    """Solo autentica: el alcance se mira después del freno (401 → 503 → 403, DEC-28). Ausente,
    malformado, vencido y revocado dan el mismo 401; una cookie no cuenta."""
    esquema, _, token = authorization.partition(" ")
    try:
        if esquema.lower() != "bearer":
            raise TokenInvalido
        agente: PrincipalAgente = request.app.state.credenciales.autenticar(token.strip())
    except TokenInvalido:
        raise ErrorV1(401, "token_invalido", "Token ausente, vencido o revocado.") from None
    request.state.agente = agente  # para la línea de log
    # El 503 del freno y el 403 informan el cupo sin gastarlo; `_tomar_cupo` lo reemplaza.
    cupos: Cupos = request.app.state.cupos
    request.state.cupo = cupos.estado(agente.token_id, request.method)
    return agente


_habilitada = con_api_agentes_habilitada(autenticar)


def _tomar_cupo(request: Request, agente: PrincipalAgente) -> PrincipalAgente:
    cupos: Cupos = request.app.state.cupos
    request.state.cupo, admitido = cupos.tomar(agente.token_id, request.method)
    if not admitido:
        que = "aportes" if request.method == "POST" else "consultas"
        raise _limite_superado(que, request.state.cupo[2])
    return agente


def exigir(alcance: Scope) -> Callable[..., PrincipalAgente]:
    """La dependencia de una ruta de `/v1`: token (401), freno (503), alcance (403
    `operacion_no_permitida`) y cupo (429), en el orden de DEC-28."""

    def con_alcance(
        request: Request, agente: Annotated[PrincipalAgente, Depends(_habilitada)]
    ) -> PrincipalAgente:
        if not agente.puede(alcance):
            raise ErrorV1(
                403, "operacion_no_permitida", "El token no tiene permiso para esta operación."
            )
        return _tomar_cupo(request, agente)

    return con_alcance


class _RutaV1(APIRoute):
    """Una ruta de `/v1`: lo que no sea `ErrorV1` no llega a los manejadores RFC 9457 de `api`.
    Una falla no prevista (base caída incluida) es 503 con `Retry-After: 30` y deja su rastro."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        atender = super().get_route_handler()

        async def atender_v1(request: Request) -> Response:
            try:
                respuesta: Response = await atender(request)
            except ErrorV1:
                raise
            except RequestValidationError:
                raise _parametros_invalidos("Parámetros inválidos.") from None
            except Exception:  # noqa: BLE001
                _log.exception("Falla no prevista en /v1 (%s).", request.state.solicitud_id)
                raise _no_disponible(RETRY_AFTER_CAIDA_S) from None
            return respuesta

        return atender_v1


router = APIRouter(prefix="/v1", tags=["Agentes"], route_class=_RutaV1)


class _ConsultaDeFragmentos(BaseModel):
    """Los parámetros de `GET /v1/fragmentos`; uno desconocido es 400 (contrato, P-06)."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(None, min_length=3, max_length=500)
    desde: date | None = None
    hasta: date | None = None
    tipo: str | None = Field(None, min_length=1)
    limite: int = Field(10, ge=1, le=LIMITE_MAXIMO)


def _parametros_invalidos(mensaje: str) -> ErrorV1:
    return ErrorV1(400, "parametros_invalidos", mensaje)


def _forma_invalida(rechazo: ValidationError) -> ErrorV1:
    """El 400 que nombra el primer parámetro o campo que no cumple el contrato."""
    primero = rechazo.errors()[0]
    campo = ".".join(str(parte) for parte in primero["loc"])
    if primero["type"] == "json_invalid":
        return _parametros_invalidos("El cuerpo no es un JSON válido.")
    if primero["type"] == "model_type":
        return _parametros_invalidos("El cuerpo tiene que ser un objeto JSON.")
    if primero["type"] == "extra_forbidden":
        return _parametros_invalidos(f"Parámetro desconocido: {campo}.")
    return _parametros_invalidos(f"Parámetro inválido: {campo}.")


@router.get("/fragmentos")
def buscar_fragmentos(
    request: Request,
    agente: Annotated[PrincipalAgente, Depends(exigir(Scope.FRAGMENTOS_LEER))],
) -> dict[str, Any]:
    """Fragmentos de Piezas Validadas no retiradas con su Cita. Sin coincidencias es 200 con la
    lista vacía; si no se pudo buscar, 503 y nunca un 200 vacío (DEC-69)."""
    try:
        pedido = _ConsultaDeFragmentos.model_validate(dict(request.query_params))
        consulta = Consulta(
            texto=pedido.q,
            filtro=FiltroDeBusqueda(
                desde=pedido.desde, hasta=pedido.hasta, tipos=(pedido.tipo,) if pedido.tipo else ()
            ),
            limite=pedido.limite,
        )
        if pedido.tipo is not None:
            with request.app.state.bd.transaccion() as tx:
                catalogos.validar_tipo_de_pieza(tx, pedido.tipo, conserva=pedido.tipo)
    except ValidationError as rechazo:
        raise _forma_invalida(rechazo) from None
    except catalogos.ValorFueraDeCatalogo:
        raise _parametros_invalidos(f"Tipo de Pieza desconocido: {pedido.tipo}.") from None
    except ConsultaInvalida as rechazo:
        raise _parametros_invalidos(str(rechazo)) from None
    # Buscador, índice, base o el tope de tiempo: 503 por `_RutaV1`, nunca un `200` vacío.
    cupos: Cupos = request.app.state.cupos
    with cupos.buscando(agente.token_id):
        hallados = request.app.state.busqueda.buscar_fragmentos(consulta, pedido.limite)
    url_publica: str = request.app.state.url_publica
    fragmentos = [presentar_fragmento(h, pieza, url_publica) for h, pieza in hallados]
    return {"total": len(fragmentos), "limite": pedido.limite, "fragmentos": fragmentos}


_ID_PIEZA = re.compile(r"pza_[A-Za-z0-9._-]{1,60}")
"""El patrón del id de Pieza del contrato 1.0.0 (`IdPiezaV1`; DEC-146 punto 18, R-29)."""
_PIEZA_NO_DISPONIBLE = "No hay una Pieza Validada con ese identificador."


def _decodificar(prefijo: str, id_publico: str) -> UUID | None:
    """El UUID de un id opaco; `None` si no es `<prefijo><uuid>` en su forma canónica (minúscula
    y con guiones), así cada Pieza tiene un solo id que la nombra."""
    try:
        uuid = UUID(id_publico.removeprefix(prefijo))
    except ValueError:
        return None
    return uuid if f"{prefijo}{uuid}" == id_publico else None


@router.get("/piezas/{pieza_id}")
def obtener_pieza(
    request: Request,
    pieza_id: str,
    _: Annotated[PrincipalAgente, Depends(exigir(Scope.PIEZAS_LEER))],
) -> dict[str, Any]:
    """La Ficha Validada completa con todos sus fragmentos (RF-35). Fuera del patrón del
    contrato, 400; con el patrón pero sin un UUID, inexistente, no Validada o retirada, el mismo
    404 (contrato, `V1NoEncontrada`; DEC-146 punto 18)."""
    if not _ID_PIEZA.fullmatch(pieza_id):
        raise _parametros_invalidos("El identificador de la Pieza no tiene la forma pza_<uuid>.")
    uuid = _decodificar("pza_", pieza_id)
    ampliada = None if uuid is None else request.app.state.busqueda.ampliar(uuid)
    if ampliada is None:
        raise ErrorV1(404, "pieza_no_disponible", _PIEZA_NO_DISPONIBLE)
    pieza, fragmentos = ampliada
    lote_nombre = None
    if pieza.lote_id is not None:
        with request.app.state.bd.transaccion() as tx:
            lote_nombre = request.app.state.lotes.obtener(tx, pieza.lote_id).nombre
    url_publica: str = request.app.state.url_publica
    return {
        "pieza_id": f"pza_{pieza.pieza_id}",
        "tipo": pieza.tipo,
        "estado": "Validada",
        "resumen": pieza.resumen,
        "texto": pieza.cuerpo,
        "fecha_inicio": pieza.fecha_inicio.isoformat(),
        "fecha_fin": pieza.fecha_fin.isoformat(),
        "precision_fecha": pieza.precision_fecha.value,
        "origen": pieza.origen,
        "lote_id": None if pieza.lote_id is None else f"lot_{pieza.lote_id}",
        "lote_nombre": lote_nombre,
        "url_original": _url_original(url_publica, pieza),
        "validada_por": pieza.validada_por,
        "validada_el": _momento(pieza.validada_el),
        "modo_validacion": pieza.modo_validacion,
        "fragmentos": [presentar_fragmento(f, pieza, url_publica) for f in fragmentos],
    }


_TOPE_APORTE_BYTES = 256 * 1024
_CLAVE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_Normalizado = BeforeValidator(lambda v: normalizar_texto(v) if isinstance(v, str) else v)
"""Antes de medir y de calcular la huella (R-36): un cuerpo solo de controles queda vacío."""
_EnUnaLinea = BeforeValidator(lambda v: normalizar_titulo(v) if isinstance(v, str) else v)
_URI: TypeAdapter[AnyUrl] = TypeAdapter(AnyUrl)


class _Aporte(BaseModel):
    """El `Aporte` del contrato: un campo desconocido es 400 (`additionalProperties: false`)."""

    model_config = ConfigDict(extra="forbid")

    titulo: Annotated[str, _EnUnaLinea, Field(min_length=1, max_length=300)]
    cuerpo: Annotated[str, _Normalizado, Field(min_length=1, max_length=20000)]
    tipo_contenido: Literal["gacetilla", "post", "comunicado", "newsletter"]
    canal: Literal["instagram", "linkedin", "web", "correo"] | None = None
    piezas_fuente: list[Annotated[str, Field(pattern=r"^pza_[A-Za-z0-9._-]{1,60}$")]] = Field(
        max_length=50
    )
    fragmentos_fuente: list[Annotated[str, Field(pattern=r"^frg_[A-Za-z0-9._-]{1,60}$")]] = (
        Field(default_factory=list, max_length=200)
    )
    # Estricta: solo un string `YYYY-MM-DD` (`format: date`); ni `0` ni un datetime.
    fecha_publicacion: Annotated[date | None, Field(strict=True)] = None
    url_publicacion: str | None = None

    @field_validator("url_publicacion")
    @classmethod
    def _es_uri(cls, valor: str | None) -> str | None:
        if valor is not None:
            _URI.validate_python(valor)  # se guarda como llegó: `AnyUrl` la reescribiría
        return valor


async def _cuerpo_del_aporte(
    request: Request,
    agente: Annotated[PrincipalAgente, Depends(exigir(Scope.APORTES_ESCRIBIR))],
) -> tuple[PrincipalAgente, bytes]:
    """El cuerpo, leído recién después de token, freno, alcance y cupo (DEC-28); pasado el
    tope se corta sin leer el resto (en `/v1` es 400, no 413)."""
    cuerpo = bytearray()
    async for trozo in request.stream():
        cuerpo += trozo
        if len(cuerpo) > _TOPE_APORTE_BYTES:
            raise _parametros_invalidos("El cuerpo supera los 256 KiB admitidos.")
    return agente, bytes(cuerpo)


@router.post("/aportes", status_code=201)
def registrar_aporte(
    request: Request,
    response: Response,
    recibido: Annotated[tuple[PrincipalAgente, bytes], Depends(_cuerpo_del_aporte)],
) -> dict[str, Any]:
    """El Aporte entra como Pieza `texto` `Pendiente` (RF-25, ADR-0002): 201 con `Location` a
    la Pieza, que da 404 hasta que una persona la valide. La misma clave con el mismo cuerpo es
    200 con el acuse original; con otro, 400 `idempotency_key_reutilizada`. Con etiquetas de
    control, 422 `aporte_rechazado` después de la forma y antes de la clave y las fuentes
    (DEC-133): no se crea nada y la clave queda libre."""
    agente, cuerpo = recibido
    clave = request.headers.get("Idempotency-Key")
    if clave is None:
        raise _parametros_invalidos("Falta la cabecera Idempotency-Key.")
    if not _CLAVE.fullmatch(clave):
        raise _parametros_invalidos(
            "La cabecera Idempotency-Key tiene que tener de 8 a 128 caracteres A-Z, a-z, 0-9,"
            " punto, guion bajo, dos puntos o guion."
        )
    try:
        pedido = _Aporte.model_validate_json(cuerpo)
    except ValidationError as rechazo:
        raise _forma_invalida(rechazo) from None
    piezas, piezas_sin_uuid = _decodificar_todos("pza_", pedido.piezas_fuente)
    fragmentos, sin_uuid = _decodificar_todos("frg_", pedido.fragmentos_fuente)
    if sin_uuid:
        raise _parametros_invalidos("Parámetro inválido: fragmentos_fuente.")
    aporte = AporteRecibido(
        titulo=pedido.titulo,
        cuerpo=pedido.cuerpo,
        tipo_contenido=pedido.tipo_contenido,
        canal=pedido.canal,
        piezas_fuente=piezas,
        fragmentos_fuente=fragmentos,
        fecha_publicacion=pedido.fecha_publicacion,
        url_publicacion=pedido.url_publicacion,
    )
    por = Actor(TipoDeActor.AGENTE, agente.token_prefijo, solicitud_id=request.state.solicitud_id)
    ingesta = request.app.state.ingesta
    try:
        if piezas_sin_uuid:
            # Con el patrón pero sin un UUID: no existe, como en `/v1/piezas/{id}` (DEC-146 p.
            # 18). El contenido se mira antes (DEC-28) y el mensaje lista también las que tienen
            # UUID y no valen como fuente.
            ingesta.rechazar_etiquetas_de_control(
                agente_id=agente.agente_id, aporte=aporte, por=por
            )
            no_validadas = ingesta.fuentes_no_validadas(piezas)
            raise _fuentes_rechazadas([*piezas_sin_uuid, *(f"pza_{p}" for p in no_validadas)])
        acuse = ingesta.recibir_aporte(
            agente_id=agente.agente_id, clave=clave, aporte=aporte, por=por
        )
    except AporteRechazado as rechazo:
        raise _aporte_rechazado(rechazo.etiquetas) from None
    except ClaveReutilizada:
        raise ErrorV1(
            400,
            "idempotency_key_reutilizada",
            "La clave de idempotencia ya fue usada con un cuerpo distinto. Usá una clave nueva.",
        ) from None
    except FuenteInexistente as rechazo:
        raise _fuentes_rechazadas([f"pza_{p}" for p in rechazo.piezas]) from None
    pieza_id = f"pza_{acuse.pieza_id}"
    if acuse.repetido:
        response.status_code = 200
    else:
        response.headers["Location"] = f"/v1/piezas/{pieza_id}"
    return {
        "aporte_id": f"apt_{acuse.aporte_id}",
        "pieza_id": pieza_id,
        "estado": "Pendiente",
        "recibido_el": _momento(acuse.recibido_el),
        "idempotency_key": clave,
    }


def _decodificar_todos(prefijo: str, ids: list[str]) -> tuple[tuple[UUID, ...], list[str]]:
    """Los UUID de `ids` sin repetir (una fuente repetida cuenta una vez) y los que no lo son."""
    uuids = {i: _decodificar(prefijo, i) for i in ids}
    return (
        tuple(dict.fromkeys(u for u in uuids.values() if u is not None)),
        [i for i, u in uuids.items() if u is None],
    )


_LUGARES_EN_EL_MENSAJE = 5


def _aporte_rechazado(etiquetas: tuple[EtiquetaHallada, ...]) -> ErrorV1:
    """El 422 nombra la etiqueta de la lista y dónde está, nunca lo que escribió el agente."""
    lugares = "; ".join(
        f"{e.etiqueta} en {e.seccion}, línea {e.linea}"
        for e in etiquetas[:_LUGARES_EN_EL_MENSAJE]
    )
    if len(etiquetas) > _LUGARES_EN_EL_MENSAJE:
        lugares += f"; y {len(etiquetas) - _LUGARES_EN_EL_MENSAJE} más"
    return ErrorV1(
        422,
        "aporte_rechazado",
        f"El contenido del aporte trae etiquetas de control ({lugares}) y no se acepta."
        " Quitalas y reenviá.",
    )


def _fuentes_rechazadas(ids: list[str]) -> ErrorV1:
    """Inexistente, no Validada o retirada, el mismo mensaje: solo lista los ids."""
    return ErrorV1(400, "fuente_inexistente", f"Fuentes rechazadas: {', '.join(ids)}.")


def _url_original(url_publica: str, pieza: PiezaBuscable) -> str:
    """La URL de la Pieza en Historia Viva (DEC-146 punto 10): la abre una persona con sesión."""
    return f"{url_publica}/validar.html?pieza={pieza.pieza_id}"


def _momento(momento: datetime) -> str:
    return momento.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def presentar_fragmento(
    hallado: "Fragmento", pieza: PiezaBuscable, url_publica: str
) -> dict[str, Any]:
    """El `Fragmento` del contrato: ids opacos `frg_`/`pza_`, lo de la Pieza leído de `fichas`
    (DEC-72) y nada más; ni `alerta_inyeccion` (C33) ni campos internos."""
    ubicacion = hallado.ubicacion
    return {
        "fragmento_id": f"frg_{hallado.fragmento_id}",
        "pieza_id": f"pza_{pieza.pieza_id}",
        "texto": hallado.texto,
        "fecha_inicio": pieza.fecha_inicio.isoformat(),
        "fecha_fin": pieza.fecha_fin.isoformat(),
        "precision_fecha": pieza.precision_fecha.value,
        "tipo": pieza.tipo,
        "resumen_pieza": pieza.resumen,
        "url_original": _url_original(url_publica, pieza),
        "validada_el": _momento(pieza.validada_el),
        "pagina": ubicacion.get("pagina"),
        # Planilla: el nombre de la hoja; video: la marca `HH:MM:SS` (spec, `ubicacion`).
        "seccion": ubicacion.get("hoja") or ubicacion.get("marca"),
    }


_MAXIMO_DE_DIAS = 92
"""El período más largo de `/v1/efemerides` y `/v1/relatos`, contando los dos días (R-11)."""


class _Periodo(BaseModel):
    """`desde` y `hasta`, obligatorios; uno desconocido es 400 (contrato, R-11)."""

    model_config = ConfigDict(extra="forbid")

    desde: date
    hasta: date


def _periodo(request: Request, que: str) -> tuple[date, date]:
    try:
        periodo = _Periodo.model_validate(dict(request.query_params))
    except ValidationError as rechazo:
        if any(e["type"] == "missing" for e in rechazo.errors()):
            raise _parametros_invalidos(
                f"La consulta de {que} requiere los parámetros desde y hasta."
            ) from None
        raise _forma_invalida(rechazo) from None
    if periodo.hasta < periodo.desde:
        raise _parametros_invalidos("hasta no puede ser anterior a desde.")
    if (periodo.hasta - periodo.desde).days + 1 > _MAXIMO_DE_DIAS:
        raise _parametros_invalidos(f"El período no puede pasar de {_MAXIMO_DE_DIAS} días.")
    return periodo.desde, periodo.hasta


def _parte_local(email: str) -> str:
    """Quien firmó, como `validada_por`: la parte local del email institucional."""
    return email.partition("@")[0]


@router.get("/efemerides")
def listar_efemerides(
    request: Request,
    _: Annotated[PrincipalAgente, Depends(exigir(Scope.EFEMERIDES_LEER))],
) -> dict[str, Any]:
    """Las Aprobadas cuya difusión se solapa con el período, sin las de fuente retirada o no
    publicable (DEC-130, R-11, R-12). Sin ninguna, 200 con la lista vacía."""
    desde, hasta = _periodo(request, "efemérides")
    with request.app.state.bd.transaccion() as tx:
        aprobadas = request.app.state.efemerides.aprobadas(tx, desde, hasta)
    efemerides = [_presentar_efemeride(e) for e in aprobadas]
    return {"total": len(efemerides), "efemerides": efemerides}


def _presentar_efemeride(efemeride: "EfemerideEntregable") -> dict[str, Any]:
    """La `EfemerideAgente` del contrato; sin la alerta de inyección (C33)."""
    de_dia = efemeride.alcance == "dia"
    return {
        "efemeride_id": f"efe_{efemeride.efemeride_id}",
        "tipo": efemeride.alcance.value,
        "dia": efemeride.fecha_objetivo.day if de_dia else None,
        "mes": efemeride.fecha_objetivo.month,
        "difusion_desde": efemeride.difusion_desde.isoformat(),
        "difusion_hasta": efemeride.difusion_hasta.isoformat(),
        "titulo": efemeride.titulo,
        "texto": efemeride.cuerpo,
        "piezas_fuente": [f"pza_{p}" for p in efemeride.piezas_fuente],
        # La efeméride cita Fichas, no fragmentos: no hay ninguno que nombrar.
        "fragmentos_fuente": [],
        "aprobada_por": _parte_local(efemeride.aprobada_por),
        "aprobada_el": _momento(efemeride.aprobada_el),
    }


@router.get("/relatos")
def listar_relatos(
    request: Request,
    _: Annotated[PrincipalAgente, Depends(exigir(Scope.EFEMERIDES_LEER))],
) -> dict[str, Any]:
    """Los Aprobados en el período, por día de aprobación, sin los de fuente retirada o no
    publicable (DEC-147, R-12). Sin ninguno, 200 con la lista vacía."""
    desde, hasta = _periodo(request, "relatos")
    with request.app.state.bd.transaccion() as tx:
        aprobados = request.app.state.efemerides.relatos_aprobados(tx, desde, hasta)
    relatos = [_presentar_relato(r) for r in aprobados]
    return {"total": len(relatos), "relatos": relatos}


def _presentar_relato(relato: "RelatoEntregable") -> dict[str, Any]:
    """El `RelatoAgente` del contrato, sin la alerta (C33): una Pieza citada en dos `[n]` va una
    sola vez, en el orden de su primera Cita."""
    return {
        "relato_id": f"rel_{relato.relato_id}",
        "titulo": relato.titulo,
        "texto": relato.cuerpo,
        "piezas_fuente": [f"pza_{p}" for p in dict.fromkeys(f.pieza_id for f in relato.fuentes)],
        # Las Citas de la respuesta guardan la Ficha, no el fragmento.
        "fragmentos_fuente": [],
        "aprobado_por": _parte_local(relato.aprobado_por),
        "aprobado_el": _momento(relato.aprobado_el),
    }


_METODOS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


# Va última: toda ruta nueva de `/v1` se declara antes que esta.
@router.api_route("", methods=_METODOS, include_in_schema=False)
@router.api_route("/{resto:path}", methods=_METODOS, include_in_schema=False)
def ruta_inexistente(
    request: Request, agente: Annotated[PrincipalAgente, Depends(_habilitada)]
) -> None:
    _tomar_cupo(request, agente)  # el 429 va antes que el 404 (DEC-28)
    raise ErrorV1(404, "ruta_inexistente", "No existe esa ruta en la API de agentes.")
