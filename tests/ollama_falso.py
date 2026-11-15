"""Un Ollama de mentira en el borde HTTP de la capa de modelos real.

Es un `httpx.MockTransport`: todo lo de este lado del borde —plantillas, delimitación, topes,
normalización de la imagen— es el código real. Contesta `POST /api/chat` en *streaming*,
como el Ollama de verdad (un JSON por línea, el último con `done: true`), `POST /api/embed`
con un vector de `dimension` por texto, `GET /api/tags` con los modelos que el test diga que
están descargados (y su digest) y `POST /api/pull`, que los descarga.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import replace

import httpx

from historia_viva.capa_modelos.configuracion import ConfiguracionDeCapacidad, validar
from historia_viva.capa_modelos.contrato import CapaModelos, Capacidad, Plantilla
from historia_viva.capa_modelos.local import capa_para
from historia_viva.capa_modelos.ollama import MotorOllama

MODELO_VISION = "vision-de-prueba"
MODELO_TEXTO = "texto-de-prueba"
MODELO_EMBEDDINGS = "embeddings-de-prueba"
MOTORES = ("ollama", "ollama_embeddings")
"""Los ids del mapa de motores de `composicion`."""

CONFIGURACION_DE_PRUEBA: Mapping[Capacidad, ConfiguracionDeCapacidad] = {
    Capacidad.DESCRIBIR_IMAGEN: ConfiguracionDeCapacidad(
        motor="ollama",
        modelo=MODELO_VISION,
        instruccion_id=Plantilla.DESCRIBIR_FOTOGRAFIA,
        parametros={
            "temperatura": 0.2,
            "num_ctx": 5632,
            "max_tokens_salida": 512,
            "timeout_s": 180,
        },
    ),
    Capacidad.TRANSCRIBIR: ConfiguracionDeCapacidad(
        motor="local", modelo=None, instruccion_id=None, parametros={"timeout_s": 300}
    ),
    Capacidad.RESUMIR: ConfiguracionDeCapacidad(
        motor="ollama",
        modelo=MODELO_TEXTO,
        instruccion_id=Plantilla.RESUMIR_FICHA,
        parametros={
            "temperatura": 0.2,
            "num_ctx": 9324,
            "max_tokens_entrada": 8000,
            "max_tokens_salida": 300,
            "timeout_s": 90,
        },
    ),
}
"""Las filas de la semilla con modelos de prueba: los topes son los de la semilla."""

FILA_EMBEBER = ConfiguracionDeCapacidad(
    motor="ollama_embeddings",
    modelo=MODELO_EMBEDDINGS,
    instruccion_id=None,
    parametros={
        "dimension": 1024,
        "prefijo_fragmento": "",
        "prefijo_consulta": "",
        "timeout_s": 120,
    },
)
CONFIGURACION_CON_EMBEBER: Mapping[Capacidad, ConfiguracionDeCapacidad] = {
    **CONFIGURACION_DE_PRUEBA,
    Capacidad.EMBEBER: FILA_EMBEBER,
}
"""Con la fila de `embeber` (045), que va al segundo Ollama."""

FILA_RESPONDER = ConfiguracionDeCapacidad(
    motor="ollama",
    modelo=MODELO_TEXTO,
    instruccion_id=Plantilla.RESPONDER_EN_CONVERSACION,
    parametros={
        "temperatura": 0.2,
        "num_ctx": 9324,
        "max_tokens_entrada": 7700,
        "max_tokens_salida": 600,
        "timeout_s": 180,
    },
)
CONFIGURACION_CON_RESPONDER: Mapping[Capacidad, ConfiguracionDeCapacidad] = {
    **CONFIGURACION_CON_EMBEBER,
    Capacidad.RESPONDER: FILA_RESPONDER,
}
"""Con la fila de `responder` (052): el modelo de `resumir`, con su `num_ctx`."""

CONFIGURACION_CON_EFEMERIDES: Mapping[Capacidad, ConfiguracionDeCapacidad] = {
    **CONFIGURACION_CON_RESPONDER,
    Capacidad.REDACTAR_EFEMERIDE: replace(
        FILA_RESPONDER, instruccion_id=Plantilla.REDACTAR_EFEMERIDE_DIA
    ),
}
"""Con la fila de `redactar_efemeride` (057): la de `responder` con la variante de día."""

ERROR_A_MITAD = "model runner has unexpectedly stopped"
"""La línea `{"error": …}` con la que Ollama corta una respuesta ya empezada."""


def digest_de(modelo: str) -> str:
    """El digest que `/api/tags` informa de `modelo` (64 hexadecimales, como Ollama)."""
    return hashlib.sha256(modelo.encode()).hexdigest()


class _Cuerpo(httpx.SyncByteStream):
    """El cuerpo en streaming que anota cuándo el cliente cierra la conexión."""

    def __init__(self, partes: Iterator[bytes], motor: OllamaFalso) -> None:
        self._partes = partes
        self._motor = motor

    def __iter__(self) -> Iterator[bytes]:
        return self._partes

    def close(self) -> None:
        self._motor.cerradas += 1


class OllamaFalso:
    """Registra cada petición y contesta lo que el test le diga. Se puede apagar."""

    def __init__(self) -> None:
        self.peticiones: list[httpx.Request] = []
        self.texto = "Una respuesta del motor."
        self.done_reason = "stop"
        self.prendido = True
        self.falla: Exception | None = None
        self.modelos_descargados = [MODELO_VISION, MODELO_TEXTO, MODELO_EMBEDDINGS]
        self.dimension = 1024
        self.trozos: list[str] | None = None
        """Los fragmentos de `/api/chat`, en orden; sin ellos, `texto` partido en dos."""
        self.corte: str | None = None
        """Cómo se corta la respuesta después de los trozos: `sin_cierre` (sin la línea
        `done`), `error` (una línea `{"error": …}`) o `conexion` (se cae a mitad)."""
        self.cerradas = 0
        """Las respuestas de `/api/chat` que el cliente cerró: con eso el motor deja de generar."""

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        self.peticiones.append(peticion)
        if not self.prendido:
            raise httpx.ConnectError("conexión rechazada", request=peticion)
        if self.falla is not None:
            raise self.falla
        if peticion.url.path == "/api/pull":
            self.modelos_descargados.append(json.loads(peticion.content)["model"])
            return httpx.Response(200, json={"status": "success"})
        if peticion.url.path == "/api/tags":
            modelos = [{"name": m, "digest": digest_de(m)} for m in self.modelos_descargados]
            return httpx.Response(200, json={"models": modelos})
        if peticion.url.path == "/api/embed":
            pedido = json.loads(peticion.content)
            vectores = [[0.1] * self.dimension for _ in pedido["input"]]
            return httpx.Response(200, json={"model": pedido["model"], "embeddings": vectores})
        # El texto en fragmentos, como llega de verdad, y el cierre con las cuentas.
        mitad = len(self.texto) // 2
        trozos = self.trozos or [self.texto[:mitad], self.texto[mitad:]]
        lineas = [
            {"message": {"role": "assistant", "content": trozo}, "done": False} for trozo in trozos
        ]
        if self.corte is None:
            lineas.append(
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": self.done_reason,
                    "prompt_eval_count": 120,
                    "eval_count": 30,
                }
            )
        elif self.corte == "error":
            lineas.append({"error": ERROR_A_MITAD})
        corte = self.corte

        def cuerpo() -> Iterator[bytes]:
            for linea in lineas:
                yield (json.dumps(linea) + "\n").encode()
            if corte == "conexion":
                raise httpx.ReadError("se cortó la conexión", request=peticion)

        return httpx.Response(200, stream=_Cuerpo(cuerpo(), self))

    def chats(self) -> list[dict]:
        return [
            json.loads(peticion.content)
            for peticion in self.peticiones
            if peticion.url.path == "/api/chat"
        ]

    def ultimo_chat(self) -> dict:
        return self.chats()[-1]

    def embeds(self) -> list[dict]:
        return [
            json.loads(peticion.content)
            for peticion in self.peticiones
            if peticion.url.path == "/api/embed"
        ]

    def motor(self, url: str = "http://motor") -> MotorOllama:
        return MotorOllama(url=url, cliente=httpx.Client(transport=httpx.MockTransport(self)))

    def motores(self) -> dict[str, MotorOllama]:
        """Los dos ids del mapa de `composicion` (DEC-06), los dos contra este mismo doble."""
        return {
            "ollama": self.motor(),
            "ollama_embeddings": self.motor("http://motor-embeddings"),
        }

    def validar(self, configuracion: Mapping[Capacidad, ConfiguracionDeCapacidad]) -> None:
        validar(configuracion, MOTORES)  # los ids de `motores()`, sin abrir otro cliente

    def capa(
        self, configuracion: Mapping[Capacidad, ConfiguracionDeCapacidad] = CONFIGURACION_DE_PRUEBA
    ) -> CapaModelos:
        return capa_para(configuracion, self.motores())
