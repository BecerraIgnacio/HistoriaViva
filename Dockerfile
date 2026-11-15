# Imagen de la aplicación del MVP: un solo programa con varios comandos (`web`, `trabajador`,
# `migrar`, `modelos`; los elige `command:` en el compose). Incluye Tesseract con el idioma `spa`
# para la transcripción por OCR (issue 12) y `ffmpeg`, cuyo `ffprobe` lee los datos técnicos de un
# video (092); sin él, la subida de video responde 503. `util-linux` trae `prlimit`, que le pone a
# `ffprobe` los topes de memoria y CPU (093); la imagen base ya lo trae, se declara igual. `tzdata`
# hace valer el `TZ` del compose: la ventana de efemérides cuenta los días de Argentina (058).
# LibreOffice sin interfaz (Writer y Calc) convierte el `.doc` y el `.xls` en el procesamiento
# (153, `conversion`); suma unos 350 MB a la imagen. Sin él, esos formatos siguen dando 415 con qué
# hacer. El HEIC lo convierte pillow-heif, que viene con las dependencias de Python. `util-linux`
# trae también `unshare`, que le quita la red a la conversión donde el sistema lo deja (el seccomp
# por defecto de Docker no: ahí la cortan el proxy muerto del perfil y los sockets anulados).
# Fijada por digest (SEG-04): la imagen que se construye en el servidor es la que se probó.
ARG IMAGEN_PYTHON=python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
FROM ${IMAGEN_PYTHON}

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa ffmpeg util-linux \
       tzdata libreoffice-writer-nogui libreoffice-calc-nogui \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv==0.13.4"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencias primero, en su propia capa: cambiar el código no las reinstala.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Usuario sin privilegios. Los originales llegan por bind mount de `HV_ORIGINALES`: el directorio
# del host tiene que dejar escribir al uid 1000; el de la imagen sirve si no se monta nada.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/originales \
    && chown -R app:app /data
USER app

ENV HV_ORIGINALES=/data/originales \
    HV_TESSERACT_CMD=/usr/bin/tesseract

EXPOSE 8000

# El esquema lo deja al día el servicio `migrar` antes de que arranque `web`. `0.0.0.0` dentro
# del contenedor, para que llegue Caddy; el compose no publica el puerto.
ENTRYPOINT ["python", "-m", "historia_viva.composicion"]
CMD ["web", "--host", "0.0.0.0", "--puerto", "8000"]
