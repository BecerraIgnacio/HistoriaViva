-- 057: las efemérides (spec §1.21, la 0020 del spec; DEC-130, R-08 a R-12, R-24) y la fila de
-- la sexta capacidad, `redactar_efemeride` (R-10).
--
-- Las columnas siguen el esquema y no la tarjeta del módulo (auditoría de tickets, decisión 17):
-- `alcance`, `titulo`, `cuerpo`, `alerta_inyeccion`, `descarte_motivo`. `efemeride_fuente` suma
-- `posicion`: el `[n]` del texto es la fuente `n`, en el orden en que la vio el modelo (como
-- `mensaje_cita.posicion`). Nadie borra: una Descartada queda, y la que no se generó no existe.

CREATE TABLE acervo.efemeride (
    efemeride_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alcance                text NOT NULL CHECK (alcance IN ('dia','mes')),
    fecha_objetivo         date NOT NULL,
    difusion_desde         date NOT NULL,
    difusion_hasta         date NOT NULL,
    titulo                 text NOT NULL CHECK (btrim(titulo) <> ''),
    cuerpo                 text NOT NULL CHECK (length(cuerpo) > 0),
    estado                 text NOT NULL DEFAULT 'propuesta'
                               CHECK (estado IN ('propuesta','aprobada','descartada')),
    version                integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    plantilla              text NOT NULL DEFAULT 'provisoria'
                               CHECK (plantilla IN ('provisoria','institucional')),
    alerta_inyeccion       boolean NOT NULL DEFAULT false,
    configuracion_efectiva jsonb NOT NULL,
    origen_generacion      text NOT NULL CHECK (origen_generacion IN ('programada','manual')),
    generada_por           uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    generada_el            timestamptz NOT NULL DEFAULT now(),
    editada_por            uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    editada_el             timestamptz,
    aprobada_por           uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    aprobada_el            timestamptz,
    descartada_por         uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    descartada_el          timestamptz,
    descarte_motivo        text CHECK (length(descarte_motivo) BETWEEN 5 AND 500),
    fuente_retirada_el     timestamptz,
    CONSTRAINT efemeride_mes_en_el_dia_1
        CHECK (alcance = 'dia' OR extract(day FROM fecha_objetivo) = 1),
    CONSTRAINT efemeride_difusion_ordenada CHECK (difusion_hasta >= difusion_desde),
    CONSTRAINT efemeride_dia_de_un_dia
        CHECK (alcance = 'mes' OR difusion_desde = difusion_hasta),
    CONSTRAINT efemeride_generada_por_coherente
        CHECK ((origen_generacion = 'manual') = (generada_por IS NOT NULL)),
    CONSTRAINT efemeride_edicion_trazada CHECK ((editada_por IS NULL) = (editada_el IS NULL)),
    CONSTRAINT efemeride_aprobada_firmada CHECK (
        (estado = 'aprobada') = (aprobada_por IS NOT NULL AND aprobada_el IS NOT NULL)
    ),
    CONSTRAINT efemeride_descartada_trazada CHECK (
        (estado = 'descartada') = (descartada_por IS NOT NULL AND descartada_el IS NOT NULL)
    ),
    CONSTRAINT efemeride_descarte_con_motivo
        CHECK ((estado = 'descartada') = (descarte_motivo IS NOT NULL))
);

CREATE INDEX efemeride_por_estado ON acervo.efemeride (estado, fecha_objetivo);
CREATE INDEX efemeride_aprobada ON acervo.efemeride (difusion_desde, difusion_hasta)
    WHERE estado = 'aprobada' AND fuente_retirada_el IS NULL;
-- La programación no genera dos veces la misma fecha; una Descartada deja generar otra a mano.
CREATE UNIQUE INDEX efemeride_unica_vigente ON acervo.efemeride (alcance, fecha_objetivo)
    WHERE estado <> 'descartada';

-- R-12: una Aprobada o una Descartada es inmutable; lo único que cambia es la marca «fuente
-- retirada», de nula a una fecha, una vez.
CREATE FUNCTION acervo.efemeride_cerrada() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
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

CREATE TRIGGER efemeride_cerrada BEFORE UPDATE ON acervo.efemeride
    FOR EACH ROW EXECUTE FUNCTION acervo.efemeride_cerrada();

CREATE TABLE acervo.efemeride_fuente (
    efemeride_id     uuid NOT NULL REFERENCES acervo.efemeride (efemeride_id) ON DELETE RESTRICT,
    pieza_id         uuid NOT NULL REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    -- La versión citada: el fragmento cambia en cada edición (DEC-146 punto 9).
    ficha_id         uuid NOT NULL REFERENCES acervo.ficha (ficha_id) ON DELETE RESTRICT,
    posicion         integer NOT NULL CHECK (posicion >= 1),
    alerta_inyeccion boolean NOT NULL DEFAULT false,
    PRIMARY KEY (efemeride_id, pieza_id),
    CONSTRAINT efemeride_fuente_posicion_unica UNIQUE (efemeride_id, posicion)
);

CREATE INDEX efemeride_fuente_por_pieza ON acervo.efemeride_fuente (pieza_id);

CREATE TABLE acervo.programacion_efemerides (
    programacion_id        smallint PRIMARY KEY DEFAULT 1 CHECK (programacion_id = 1),
    habilitada             boolean NOT NULL DEFAULT false,
    periodicidad           text NOT NULL DEFAULT 'semanal'
                               CHECK (periodicidad IN ('diaria','semanal','mensual')),
    anticipacion_dias      integer NOT NULL DEFAULT 14 CHECK (anticipacion_dias BETWEEN 0 AND 365),
    maximo_por_corrida     integer NOT NULL DEFAULT 50 CHECK (maximo_por_corrida BETWEEN 1 AND 500),
    alcances               text[] NOT NULL DEFAULT '{dia,mes}' CHECK (
        cardinality(alcances) >= 1 AND alcances <@ ARRAY['dia','mes']
    ),
    ultima_corrida_el      timestamptz,
    pedido_manual_el       timestamptz,
    pedido_manual_objetivo jsonb CHECK (
        pedido_manual_objetivo IS NULL OR (
            pedido_manual_el IS NOT NULL AND pedido_manual_objetivo->>'tipo' IN ('dia','mes')
        )
    ),
    version                integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    actualizada_por        uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    actualizada_el         timestamptz NOT NULL DEFAULT now()
);

INSERT INTO acervo.programacion_efemerides DEFAULT VALUES;

-- R-10: provisorio hasta que decida el banco, el modelo de `responder` con sus topes (y su
-- `num_ctx`, para que Ollama no lo recargue). La fila nombra la variante de día; el módulo
-- elige `_DIA` o `_MES` según el objetivo.
INSERT INTO acervo.configuracion_capacidad (capacidad, motor, modelo, instruccion_id, parametros)
SELECT 'redactar_efemeride', motor, modelo, 'redactar_efemeride_dia', parametros
FROM acervo.configuracion_capacidad WHERE capacidad = 'responder';

-- Sin DELETE en ninguna (ADR-0004, control 1). Las fuentes son append-only (R-24).
GRANT SELECT, INSERT ON acervo.efemeride TO hv_app;
GRANT UPDATE (titulo, cuerpo, estado, version, editada_por, editada_el, aprobada_por,
              aprobada_el, descartada_por, descartada_el, descarte_motivo, fuente_retirada_el)
    ON acervo.efemeride TO hv_app;
GRANT SELECT, INSERT ON acervo.efemeride_fuente TO hv_app;
GRANT SELECT ON acervo.programacion_efemerides TO hv_app;
GRANT UPDATE (habilitada, periodicidad, anticipacion_dias, maximo_por_corrida, alcances,
              ultima_corrida_el, pedido_manual_el, pedido_manual_objetivo, version,
              actualizada_por, actualizada_el)
    ON acervo.programacion_efemerides TO hv_app;
