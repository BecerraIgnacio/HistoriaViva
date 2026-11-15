-- 065: la revisión de los Relatos, igual que la de la efeméride.
--
-- La firma guarda con qué se entró, como la 0032: lo aprobado con `dev` es de ensayo (DEC-139).
-- La marca «fuente retirada» no se quita ni cambia, tampoco en un Propuesto, como la 0034: se puede
-- aprobar o descartar y la marca queda.

ALTER TABLE acervo.relato
    ADD COLUMN metodo_autenticacion text CHECK (metodo_autenticacion IN ('oidc','dev')),
    ADD CONSTRAINT relato_firma_con_metodo
        CHECK ((estado = 'aprobada') = (metodo_autenticacion IS NOT NULL));

GRANT UPDATE (metodo_autenticacion) ON acervo.relato TO hv_app;

CREATE OR REPLACE FUNCTION acervo.relato_cerrado() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.fuente_retirada_el IS NOT NULL
       AND NEW.fuente_retirada_el IS DISTINCT FROM OLD.fuente_retirada_el THEN
        RAISE EXCEPTION 'relato_cerrado: la marca de fuente retirada de % no se quita',
            OLD.relato_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.estado = 'propuesta'
       OR (OLD.fuente_retirada_el IS NULL AND NEW.fuente_retirada_el IS NOT NULL
           AND to_jsonb(NEW) - 'fuente_retirada_el' = to_jsonb(OLD) - 'fuente_retirada_el') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'relato_cerrado: el relato % está %; solo admite la marca de fuente retirada',
        OLD.relato_id, OLD.estado
        USING ERRCODE = 'restrict_violation';
END;
$$;
