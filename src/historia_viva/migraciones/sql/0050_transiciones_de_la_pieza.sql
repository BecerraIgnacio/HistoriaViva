-- 167 (S-BD-11 de la matriz del laboratorio, ARQ-09): la base rechaza el cambio de estado de una
-- Pieza que el dominio no permite, aunque llegue por SQL con `hv_app`. Las transiciones son las de
-- `dominio.estados.TRANSICIONES`; `test_migraciones` compara las dos tablas, así que cambiar una
-- sin la otra rompe la suite.
--
-- Solo mira el estado: el retiro y la restauración (DEC-29, DEC-138) mueven `borrada_*` y no el
-- estado; el reproceso deja la Pieza `Recibida`; la supresión (0038) borra la fila y un DELETE no
-- pasa por este trigger. Nacer en un estado (`Recibida`, o `Pendiente` el video y el Aporte) es un
-- INSERT: lo restringen los CHECK de la tabla.

CREATE FUNCTION acervo.transicion_permitida(desde text, hacia text) RETURNS boolean
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog
AS $$
    SELECT (desde, hacia) IN (
        ('Recibida', 'Pendiente'),
        ('Pendiente', 'Incompleta'),
        ('Pendiente', 'Validada'),
        ('Incompleta', 'Validada'),
        ('Validada', 'Incompleta')
    )
$$;

CREATE FUNCTION acervo.pieza_transicion() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, acervo
AS $$
BEGIN
    IF NOT acervo.transicion_permitida(OLD.estado, NEW.estado) THEN
        RAISE EXCEPTION 'pieza_transicion: la Pieza % no pasa de % a %',
            OLD.pieza_id, OLD.estado, NEW.estado
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER pieza_transicion BEFORE UPDATE OF estado ON acervo.pieza
    FOR EACH ROW WHEN (OLD.estado IS DISTINCT FROM NEW.estado)
    EXECUTE FUNCTION acervo.pieza_transicion();
