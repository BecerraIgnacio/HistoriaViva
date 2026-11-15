-- 063: la marca «fuente retirada» (R-12) no se quita ni cambia, tampoco en una Propuesta: se
-- puede aprobar o descartar, pero la marca queda. En una Aprobada o una Descartada sigue siendo
-- lo único que cambia, de nula a una fecha, una vez.

CREATE OR REPLACE FUNCTION acervo.efemeride_cerrada() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.fuente_retirada_el IS NOT NULL
       AND NEW.fuente_retirada_el IS DISTINCT FROM OLD.fuente_retirada_el THEN
        RAISE EXCEPTION 'efemeride_cerrada: la marca de fuente retirada de % no se quita',
            OLD.efemeride_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.estado = 'propuesta'
       OR (OLD.fuente_retirada_el IS NULL AND NEW.fuente_retirada_el IS NOT NULL
           AND to_jsonb(NEW) - 'fuente_retirada_el' = to_jsonb(OLD) - 'fuente_retirada_el') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'efemeride_cerrada: la efeméride % está %; solo admite la marca de fuente retirada',
        OLD.efemeride_id, OLD.estado
        USING ERRCODE = 'restrict_violation';
END;
$$;
