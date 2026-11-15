-- =====================================================================
-- Historia Viva -- MVP. Bootstrap con superusuario (spec, «Roles y
-- permisos»; DEC-105). Una sola vez por servidor e idempotente: lo corre
-- `bootstrap.sh`, y la suite con el mismo SQL.
--
-- Corre dentro de la base del MVP, ya creada (en el contenedor la crea la
-- imagen de Postgres con `POSTGRES_DB`). Las claves llegan como ajustes de
-- la sesión, `hv.clave_migrador` y `hv.clave_app`: no van en el repositorio.
-- Las migraciones no crean roles ni extensiones.
-- =====================================================================

DO $$
DECLARE
    clave_migrador text := coalesce(current_setting('hv.clave_migrador', true), '');
    clave_app      text := coalesce(current_setting('hv.clave_app', true), '');
BEGIN
    IF clave_migrador = '' OR clave_app = '' THEN
        RAISE EXCEPTION 'Falta la clave de hv_migrador o la de hv_app (HV_CLAVE_MIGRADOR, HV_CLAVE_APP).';
    END IF;
    IF clave_migrador = clave_app THEN
        RAISE EXCEPTION 'La clave de hv_migrador y la de hv_app tienen que ser distintas.';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hv_migrador') THEN
        CREATE ROLE hv_migrador LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hv_app') THEN
        CREATE ROLE hv_app LOGIN;
    END IF;
    EXECUTE format('ALTER ROLE hv_migrador LOGIN PASSWORD %L', clave_migrador);
    EXECUTE format('ALTER ROLE hv_app LOGIN PASSWORD %L', clave_app);
    -- `hv_migrador` es el único dueño: el schema `acervo` y todo lo que crean las migraciones.
    EXECUTE format('ALTER DATABASE %I OWNER TO hv_migrador', current_database());
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO hv_migrador, hv_app', current_database());
END
$$;

ALTER ROLE hv_migrador SET search_path = acervo, public;
ALTER ROLE hv_app SET search_path = acervo, public;
-- DEC-99: una transacción olvidada abierta no retiene candados para siempre.
ALTER ROLE hv_app SET idle_in_transaction_session_timeout = '60s';

-- `hv_migrador` no puede crear extensiones; `hv_app` necesita ver el tipo `vector`.
CREATE EXTENSION IF NOT EXISTS vector;
GRANT USAGE ON SCHEMA public TO hv_app;
