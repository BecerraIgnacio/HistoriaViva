"""Convertir un `.doc`, un `.xls` o una foto HEIC en un proceso aparte (153, DEC-41 revisada).

Es el único módulo que corre LibreOffice y abre un HEIC. Lo que entra es contenido hostil, así
que corre en un proceso hijo: sin shell, con la entrada cerrada, en un directorio temporal
propio con un perfil de LibreOffice que se descarta, con tope de tiempo de reloj y, en POSIX,
de memoria, CPU y bytes escritos (`prlimit`). Sin red: en un espacio de red vacío
(`unshare --net`) si el sistema lo deja y, siempre, con el proxy apuntando a un puerto muerto y
los sockets del hijo de Python anulados. Sin vínculos: el perfil no los actualiza al abrir (un
`file://` no pasa por el proxy). Sin macros: el perfil las apaga, y un `.doc` o un
`.xls` con proyecto VBA ni siquiera se admite (`originales`).

Lo convertido no se cree: lo mide la guarda de `originales` (`_admitir_zip`, el tope de
píxeles, el de bytes) antes de leerlo. Lo arma `composicion` y lo recibe `originales`.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from historia_viva.dominio.originales import DOC, HEIC, TOPE_BYTES, XLS
from historia_viva.originales import ConversionFallida

__all__ = ["ConversorAislado"]

_log = logging.getLogger(__name__)

_FILTROS = {DOC: "docx:MS Word 2007 XML", XLS: "xlsx:Calc MS Excel 2007 XML"}
"""El formato de salida de `--convert-to` de LibreOffice para cada formato viejo."""

_PRLIMIT: str | None = None if sys.platform == "win32" else "prlimit"
_MEMORIA_MAXIMA = 3 * 1024**3
"""Espacio de direcciones: LibreOffice reserva mucho más de lo que usa."""
_CPU_MAXIMA_S = 60
_PROCESOS_MAXIMOS = 256
"""`RLIMIT_NPROC` cuenta los hilos de todo el usuario, también los de la aplicación: alcanza
para LibreOffice y corta una bomba de procesos."""
_ESCRITO_MAXIMO = 2 * TOPE_BYTES
"""Ningún archivo del hijo pasa de esto: el tope de la clase lo mide después `originales`."""

_PROXY_MUERTO = "http://127.0.0.1:9"
"""El puerto de descarte: todo pedido HTTP o FTP del hijo se corta ahí."""

_SIN_RED_EN_PYTHON = """
import socket
def _sin_red(*_, **__):
    raise OSError("la conversion corre sin red")
socket.socket = socket.create_connection = socket.getaddrinfo = _sin_red
"""

_HEIC_A_JPEG = (
    _SIN_RED_EN_PYTHON
    + """
import sys, warnings
from PIL import Image, ImageOps
import pillow_heif
pillow_heif.register_heif_opener()
# Pillow mide los píxeles al abrir, antes de decodificar: una bomba no llega a ocupar memoria.
warnings.simplefilter("error", Image.DecompressionBombWarning)
with Image.open(sys.argv[1]) as foto:
    # Sin EXIF: la ubicación del teléfono no pasa al JPEG que muestra el visor.
    ImageOps.exif_transpose(foto).convert("RGB").save(sys.argv[2], "JPEG", quality=90)
"""
)

_PERFIL = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
 xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
{items}
</oor:items>
"""
_AJUSTES = (
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableMacrosExecution", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "MacroSecurityLevel", "3"),
    ("/org.openoffice.Office.Common/Security/Scripting", "BlockUntrustedRefererLinks", "true"),
    # Los vínculos no se actualizan al abrir: un INCLUDETEXT o una referencia externa a
    # `file:///proc/1/environ` meterían el entorno del trabajador en la Ficha. Los valores son de
    # officecfg (Writer.xcs, Calc.xcs): «nunca» es 2 en Writer y 1 en Calc.
    ("/org.openoffice.Office.Writer/Content/Update", "Link", "2"),
    ("/org.openoffice.Office.Writer/Content/Update", "Field", "false"),
    ("/org.openoffice.Office.Calc/Content/Update", "Link", "1"),
    ("/org.openoffice.Inet/Settings", "ooInetProxyType", "2"),
    ("/org.openoffice.Inet/Settings", "ooInetNoProxy", ""),
    *(
        ("/org.openoffice.Inet/Settings", f"ooInet{protocolo}Proxy{campo}", valor)
        for protocolo in ("HTTP", "HTTPS", "FTP")
        for campo, valor in (("Name", "127.0.0.1"), ("Port", "9"))
    ),
)
"""Macros apagadas sin preguntar, vínculos sin actualizar y todo lo que LibreOffice busque por
red, al proxy muerto."""


class ConversorAislado:
    """El conversor de producción: LibreOffice para `.doc` y `.xls`, pillow-heif para HEIC.

    `formatos` dice qué hay instalado en este servidor; lo que falta sigue dando 415.
    """

    def __init__(self, *, timeout_s: float = 120) -> None:
        self._timeout_s = timeout_s
        self._soffice = shutil.which("soffice") or shutil.which("libreoffice")
        formatos = set()
        if self._soffice is not None:
            formatos |= {DOC, XLS}
        if importlib.util.find_spec("pillow_heif") is not None:
            formatos.add(HEIC)
        if _PRLIMIT is not None and shutil.which(_PRLIMIT) is None:
            _log.warning("Sin prlimit no se convierte: el hijo correría sin topes.")
            formatos.clear()
        self.formatos = frozenset(formatos)
        self._sin_red = _aislar_la_red()
        if self.formatos and not self._sin_red:
            _log.warning(
                "La conversión corre sin espacio de red propio: la cortan el proxy muerto y los"
                " sockets anulados, no el sistema."
            )

    def convertir(self, origen: Path, tipo_mime: str, destino: Path) -> None:
        """Escribe en `destino` el convertido de `origen`, ambos en un directorio propio.

        Raises:
            ConversionFallida: Si el formato no está instalado, el hijo falla o se pasa de su
                tope de tiempo.
        """
        if tipo_mime not in self.formatos:
            raise ConversionFallida(f"Este servidor no convierte {tipo_mime}.")
        aparte = destino.parent
        if tipo_mime == HEIC:
            comando = [sys.executable, "-I", "-c", _HEIC_A_JPEG, str(origen), str(destino)]
        else:
            assert self._soffice is not None
            perfil = aparte / "perfil"
            (perfil / "user").mkdir(parents=True)
            (perfil / "user" / "registrymodifications.xcu").write_text(
                perfil_de_libreoffice(), encoding="utf-8"
            )
            salida = aparte / "salida"
            comando = [
                self._soffice, "--headless", "--norestore", "--nolockcheck", "--nodefault",
                "--nologo", f"-env:UserInstallation={perfil.resolve().as_uri()}",
                "--convert-to", _FILTROS[tipo_mime], "--outdir", str(salida), str(origen),
            ]  # fmt: skip
        correr_aislado(comando, aparte, timeout_s=self._timeout_s, sin_red=self._sin_red)
        if tipo_mime != HEIC:
            convertido = aparte / "salida" / f"{origen.stem}{destino.suffix}"
            if not convertido.is_file():
                raise ConversionFallida(
                    "No se pudo convertir el archivo: si tiene contraseña, quitásela y subilo"
                    " de nuevo."
                )
            os.replace(convertido, destino)


def perfil_de_libreoffice() -> str:
    """El `registrymodifications.xcu` del perfil temporal de cada conversión."""
    return _PERFIL.format(
        items="\n".join(
            f'<item oor:path="{ruta}"><prop oor:name="{nombre}" oor:op="fuse">'
            f"<value>{valor}</value></prop></item>"
            for ruta, nombre, valor in _AJUSTES
        )
    )


def correr_aislado(
    comando: list[str], aparte: Path, *, timeout_s: float, sin_red: bool = False
) -> None:
    """Corre `comando` en `aparte` con sus topes y espera a que termine.

    Raises:
        ConversionFallida: Si no se puede ejecutar, sale con error o vence `timeout_s`.
    """
    prefijo = [] if not sin_red else ["unshare", "--net", "--map-root-user", "--"]
    if _PRLIMIT is not None:
        prefijo += [
            _PRLIMIT, f"--as={_MEMORIA_MAXIMA}", f"--cpu={_CPU_MAXIMA_S}",
            f"--fsize={_ESCRITO_MAXIMO}", f"--nproc={_PROCESOS_MAXIMOS}", "--",
        ]  # fmt: skip
    entorno = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(aparte),
        "TMPDIR": str(aparte),
        "LANG": "C.UTF-8",
        **{variable: _PROXY_MUERTO for variable in ("http_proxy", "https_proxy", "ftp_proxy")},
        "no_proxy": "",
    }
    if "SYSTEMROOT" in os.environ:  # Windows no arranca un proceso sin ella
        entorno["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    try:
        proceso = subprocess.Popen(
            [*prefijo, *comando],
            cwd=aparte,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=entorno,
            # Su propio grupo: LibreOffice lanza otro proceso y el tope de tiempo mata a los dos.
            start_new_session=True,
        )
    except OSError as error:
        raise ConversionFallida(f"No se pudo ejecutar el conversor: {error.strerror}.") from error
    try:
        codigo = proceso.wait(timeout_s)
    except subprocess.TimeoutExpired:
        _matar(proceso)
        raise ConversionFallida(
            f"La conversión no terminó en {timeout_s:g} s: guardalo en el formato nuevo y"
            " subilo de nuevo."
        ) from None
    if codigo != 0:
        raise ConversionFallida("El conversor no pudo leer el archivo.")


def _matar(proceso: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        # shortcut: en Windows muere solo el hijo directo; el despliegue es Linux.
        proceso.kill()
    else:
        os.killpg(proceso.pid, signal.SIGKILL)
    proceso.wait()


def _aislar_la_red() -> bool:
    """Si este sistema deja abrir un espacio de red vacío sin privilegios (Linux con espacios
    de usuario; el seccomp por defecto de Docker no lo deja)."""
    if sys.platform == "win32" or shutil.which("unshare") is None:
        return False
    try:
        return (
            subprocess.run(
                ["unshare", "--net", "--map-root-user", "true"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
