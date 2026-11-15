-- 146 (ADR-0007, punto 3): el resumen de una foto sale de las Afirmaciones que nadie rechazó. Al
-- rechazar una (o al volver atrás un rechazo), la cola rehace el resumen en segundo plano con un
-- trabajo de tipo `resumen`: uno solo encolado por Pieza, que al correr lee las decisiones de ese
-- momento, así varios rechazos seguidos no encolan varios y el último gana.
ALTER TABLE acervo.trabajo_procesamiento
    DROP CONSTRAINT trabajo_procesamiento_tipo_check,
    ADD CONSTRAINT trabajo_procesamiento_tipo_check
        CHECK (tipo IN ('procesamiento', 'indexacion', 'resumen'));

CREATE UNIQUE INDEX trabajo_resumen_encolado ON acervo.trabajo_procesamiento (pieza_id)
    WHERE tipo = 'resumen' AND estado = 'encolado';

-- Cada resumen rehecho queda como propuesta, sin crear versión de la Ficha: la pantalla lo pone en
-- el campo y la persona firma. Se guardan todos, no solo el último: como la Procedencia sale de
-- comparar con la v1 (ADR-0001), un resumen guardado que es igual a uno de estos lo propuso un
-- modelo, no la persona. Cuelgan de la v1 de la foto, como las Afirmaciones: la supresión efectiva
-- borra la Ficha y estas filas se van con ella.
CREATE TABLE acervo.resumen_rehecho (
    ficha_id          uuid NOT NULL
                      REFERENCES acervo.descripcion_de_imagen (ficha_id) ON DELETE CASCADE,
    rehecho_el        timestamptz NOT NULL DEFAULT clock_timestamp(),
    texto             text NOT NULL CHECK (btrim(texto) <> ''),
    -- La capa 2 de ADR-0003 sobre el resumen nuevo, como sobre el de cada versión.
    informe_inyeccion jsonb NOT NULL,
    PRIMARY KEY (ficha_id, rehecho_el)
);

COMMENT ON TABLE acervo.resumen_rehecho IS
    'Resúmenes rehechos con las Afirmaciones no rechazadas (146); el último es la propuesta.';

GRANT SELECT, INSERT ON acervo.resumen_rehecho TO hv_app;
