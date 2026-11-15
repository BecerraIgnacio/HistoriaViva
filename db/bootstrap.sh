#!/bin/sh
# Bootstrap con superusuario (spec, «Roles y permisos»): los roles `hv_migrador` y `hv_app`, la
# base y la extensión `vector`. La imagen de Postgres lo corre una sola vez, al inicializar un
# volumen nuevo (`docker-entrypoint-initdb.d`). Sobre un volumen que ya existía, o para cambiar
# las claves, a mano:
#
#   docker compose exec postgres sh /docker-entrypoint-initdb.d/hv_bootstrap.sh
#
# Las claves viajan como ajustes de la sesión, no dentro del SQL. Son hexadecimales
# (`.env.example`): sin espacios ni barras, que `PGOPTIONS` interpretaría.
PGOPTIONS="-c hv.clave_migrador=$HV_CLAVE_MIGRADOR -c hv.clave_app=$HV_CLAVE_APP" psql \
    -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --file /hv/bootstrap.sql
