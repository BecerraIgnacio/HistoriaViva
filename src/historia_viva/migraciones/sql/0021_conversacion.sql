-- 051: la Conversación (spec §1.20, la 0019 del spec; DEC-129, R-24).
--
-- Las columnas siguen el esquema y no la tarjeta del módulo (auditoría de tickets, decisión 17):
-- `persona_id`, `autor`, `contenido`, `posicion`. Un turno son dos filas de mensaje, la pregunta
-- y la respuesta, insertadas una vez cada una: `hv_app` no reescribe mensajes ni Citas, y nadie
-- borra (las Conversaciones son un registro auditado, R-19). `titulo` admite nulo: la
-- Conversación nace sin cuerpo (`POST /api/conversaciones` responde `titulo: null`) y lo toma de
-- la primera pregunta.

CREATE TABLE acervo.conversacion (
    conversacion_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    persona_id      uuid NOT NULL REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    titulo          text CHECK (btrim(titulo) <> ''),
    creada_el       timestamptz NOT NULL DEFAULT now(),
    actualizada_el  timestamptz NOT NULL DEFAULT now(),
    estado          text NOT NULL DEFAULT 'activa' CHECK (estado IN ('activa','respondiendo')),
    turno_desde     timestamptz,
    CONSTRAINT conversacion_turno_con_fecha
        CHECK ((estado = 'respondiendo') = (turno_desde IS NOT NULL))
);

CREATE INDEX conversacion_por_persona ON acervo.conversacion (persona_id, actualizada_el DESC);

CREATE TABLE acervo.mensaje_conversacion (
    mensaje_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversacion_id        uuid NOT NULL
                               REFERENCES acervo.conversacion (conversacion_id) ON DELETE RESTRICT,
    orden                  integer NOT NULL CHECK (orden >= 1),
    autor                  text NOT NULL CHECK (autor IN ('persona','historia_viva')),
    contenido              text NOT NULL,
    filtros                jsonb,
    pregunta_reformulada   text,
    estado                 text CHECK (
        estado IN ('completa','sin_evidencia','interrumpida','error','sin_semantica')
    ),
    modelo_embedding       text,
    configuracion_efectiva jsonb,
    citas_no_verificadas   integer CHECK (citas_no_verificadas >= 0),
    ms_hasta_fuentes       integer CHECK (ms_hasta_fuentes >= 0),
    ms_hasta_primer_token  integer CHECK (ms_hasta_primer_token >= 0),
    ms_hasta_respuesta     integer CHECK (ms_hasta_respuesta >= 0),
    creado_el              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT mensaje_orden_unico UNIQUE (conversacion_id, orden),
    CONSTRAINT mensaje_filtros_de_la_persona CHECK (autor = 'persona' OR filtros IS NULL),
    CONSTRAINT mensaje_estado_de_la_respuesta
        CHECK ((autor = 'historia_viva') = (estado IS NOT NULL)),
    CONSTRAINT mensaje_datos_de_la_respuesta CHECK (
        autor = 'historia_viva' OR (
            pregunta_reformulada IS NULL AND modelo_embedding IS NULL
            AND configuracion_efectiva IS NULL AND citas_no_verificadas IS NULL
            AND ms_hasta_fuentes IS NULL AND ms_hasta_primer_token IS NULL
            AND ms_hasta_respuesta IS NULL
        )
    )
);

-- El `UNIQUE (conversacion_id, orden)` ya es el índice `mensaje_por_conversacion` del spec.

CREATE TABLE acervo.mensaje_cita (
    mensaje_id          uuid NOT NULL
                            REFERENCES acervo.mensaje_conversacion (mensaje_id) ON DELETE RESTRICT,
    posicion            integer NOT NULL CHECK (posicion >= 1),
    pieza_id            uuid NOT NULL REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    ficha_id            uuid NOT NULL REFERENCES acervo.ficha (ficha_id) ON DELETE RESTRICT,
    -- Sin FK: el id del fragmento cambia en cada edición (DEC-146 punto 9).
    fragmento_id        uuid,
    ubicacion           jsonb,
    citada_en_respuesta boolean NOT NULL DEFAULT false,
    PRIMARY KEY (mensaje_id, posicion)
);

CREATE INDEX mensaje_cita_por_pieza ON acervo.mensaje_cita (pieza_id);

GRANT SELECT, INSERT ON acervo.conversacion TO hv_app;
-- Nunca `persona_id`: una Conversación no cambia de dueña (R-31).
GRANT UPDATE (titulo, actualizada_el, estado, turno_desde) ON acervo.conversacion TO hv_app;
GRANT SELECT, INSERT ON acervo.mensaje_conversacion TO hv_app;
GRANT SELECT, INSERT ON acervo.mensaje_cita TO hv_app;
