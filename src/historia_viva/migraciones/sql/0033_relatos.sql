-- 064: los Relatos (spec §1.21b, la 0023 del spec; DEC-147, R-12, R-24).
--
-- Un Relato nace de una respuesta de la Conversación («Proponer como relato») y no se redacta: el
-- cuerpo es el texto de la respuesta. `relato_fuente` suma `posicion`, el `[n]` que la Cita tenía
-- en la respuesta (como `efemeride_fuente`); con `relato.mensaje_id` es la clave de su
-- `mensaje_cita`. La PK es por posición: dos `[n]` pueden ser de la misma Pieza.

CREATE TABLE acervo.relato (
    relato_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    mensaje_id             uuid NOT NULL
                               REFERENCES acervo.mensaje_conversacion (mensaje_id) ON DELETE RESTRICT,
    titulo                 text NOT NULL CHECK (btrim(titulo) <> ''),
    cuerpo                 text NOT NULL CHECK (length(cuerpo) > 0),
    estado                 text NOT NULL DEFAULT 'propuesta'
                               CHECK (estado IN ('propuesta','aprobada','descartada')),
    version                integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    alerta_inyeccion       boolean NOT NULL DEFAULT false,
    configuracion_efectiva jsonb NOT NULL,
    propuesto_por          uuid NOT NULL REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    propuesto_el           timestamptz NOT NULL DEFAULT now(),
    editado_por            uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    editado_el             timestamptz,
    aprobado_por           uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    aprobado_el            timestamptz,
    descartado_por         uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    descartado_el          timestamptz,
    descarte_motivo        text CHECK (length(descarte_motivo) BETWEEN 5 AND 500),
    fuente_retirada_el     timestamptz,
    CONSTRAINT relato_edicion_trazada CHECK ((editado_por IS NULL) = (editado_el IS NULL)),
    CONSTRAINT relato_aprobado_firmado CHECK (
        (estado = 'aprobada') = (aprobado_por IS NOT NULL AND aprobado_el IS NOT NULL)
    ),
    CONSTRAINT relato_descartado_trazado CHECK (
        (estado = 'descartada') = (descartado_por IS NOT NULL AND descartado_el IS NOT NULL)
    ),
    CONSTRAINT relato_descarte_con_motivo
        CHECK ((estado = 'descartada') = (descarte_motivo IS NOT NULL))
);

CREATE INDEX relato_por_estado ON acervo.relato (estado, propuesto_el DESC);
CREATE INDEX relato_aprobado ON acervo.relato (aprobado_el)
    WHERE estado = 'aprobada' AND fuente_retirada_el IS NULL;
-- Una respuesta no genera dos Relatos vigentes; descartado, se puede proponer otra vez.
CREATE UNIQUE INDEX relato_unico_por_mensaje ON acervo.relato (mensaje_id)
    WHERE estado <> 'descartada';

-- R-12: un Relato Aprobado o Descartado es inmutable; lo único que cambia es la marca «fuente
-- retirada», de nula a una fecha, una vez (como `efemeride_cerrada`).
CREATE FUNCTION acervo.relato_cerrado() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
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

CREATE TRIGGER relato_cerrado BEFORE UPDATE ON acervo.relato
    FOR EACH ROW EXECUTE FUNCTION acervo.relato_cerrado();

CREATE TABLE acervo.relato_fuente (
    relato_id        uuid NOT NULL REFERENCES acervo.relato (relato_id) ON DELETE RESTRICT,
    posicion         integer NOT NULL CHECK (posicion >= 1),
    pieza_id         uuid NOT NULL REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    -- La versión citada: el fragmento cambia en cada edición (DEC-146 punto 9).
    ficha_id         uuid NOT NULL REFERENCES acervo.ficha (ficha_id) ON DELETE RESTRICT,
    alerta_inyeccion boolean NOT NULL DEFAULT false,
    PRIMARY KEY (relato_id, posicion)
);

CREATE INDEX relato_fuente_por_pieza ON acervo.relato_fuente (pieza_id);

-- Sin DELETE (ADR-0004, control 1). Las fuentes son append-only (R-24).
GRANT SELECT, INSERT ON acervo.relato TO hv_app;
GRANT UPDATE (titulo, cuerpo, estado, version, editado_por, editado_el, aprobado_por,
              aprobado_el, descartado_por, descartado_el, descarte_motivo, fuente_retirada_el)
    ON acervo.relato TO hv_app;
GRANT SELECT, INSERT ON acervo.relato_fuente TO hv_app;
