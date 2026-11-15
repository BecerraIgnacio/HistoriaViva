#!/usr/bin/env bash
# Arma el `.env` del servidor de la universidad (runbook R16, paso 3) desde `.env.example`, con
# secretos nuevos de `openssl rand`. No lee ningún otro `.env`: nada del entorno de la PC viaja
# al servidor (DEC-119, DEC-139). Deja `HV_ENTORNO` vacía y `HV_AUTH=oidc`; lo que entrega IT
# (`HV_OIDC_*`, el certificado) y las rutas del servidor se completan a mano después.
#
#   ./generar-env-servidor.sh /srv/historia-viva/.env historiaviva.fie.undef.edu.ar
#
# No pisa un `.env` que ya existe: cambiar las claves de Postgres con el volumen creado no las
# cambia en la base (ver `db/bootstrap.sh`).
set -euo pipefail

destino=${1:?Uso: generar-env-servidor.sh DESTINO DOMINIO}
dominio=${2:?Uso: generar-env-servidor.sh DESTINO DOMINIO}
ejemplo="$(dirname "$0")/.env.example"

if [[ -e "$destino" ]]; then
    echo "Ya existe $destino: no se pisa. Borralo a mano si de verdad hay que empezar de cero." >&2
    exit 1
fi

secreto() { openssl rand -hex "$1"; }

# Cada secreto en su variable: dentro de `declare` una falla de `openssl` no corta `set -e`.
clave_postgres=$(secreto 24)
clave_migrador=$(secreto 24)
clave_app=$(secreto 24)
clave_cookie=$(secreto 32)
for clave in "$clave_postgres" "$clave_migrador" "$clave_app" "$clave_cookie"; do
    if [[ ! $clave =~ ^[0-9a-f]{48}([0-9a-f]{16})?$ ]]; then
        echo "openssl no generó una clave: no se escribe nada." >&2
        exit 1
    fi
done

declare -A valores=(
    [POSTGRES_PASSWORD]="$clave_postgres"
    [HV_CLAVE_MIGRADOR]="$clave_migrador"
    [HV_CLAVE_APP]="$clave_app"
    [HV_CLAVE_COOKIE_PREVIA]="$clave_cookie"
    [HV_DESPLIEGUE]=servidor
    [HV_AUTH]=oidc
    [HV_ENTORNO]=
    [HV_DOMINIO]="$dominio"
    [HV_URL_PUBLICA]="https://$dominio"
    [HV_ORIGEN]="https://$dominio"
)

# Solo quien despliega lee el `.env`: lleva las claves de la base. `noclobber`: si otro lo creó
# después del chequeo de arriba, `>` falla en vez de pisarlo.
umask 077
set -o noclobber
while IFS= read -r linea || [[ -n "$linea" ]]; do
    nombre=${linea%%=*}
    if [[ $linea != \#* && $linea == *=* && -v "valores[$nombre]" ]]; then
        printf '%s=%s\n' "$nombre" "${valores[$nombre]}"
    else
        printf '%s\n' "$linea"
    fi
done < "$ejemplo" > "$destino"

echo "Listo: $destino. Faltan HV_OIDC_CLIENTE_ID y HV_OIDC_SECRETO (IT, C18), HV_TLS,"
echo "HV_CERTIFICADOS, HV_ORIGINALES, HV_VOLCADOS y lo de restic (runbook R16, paso 3)."
