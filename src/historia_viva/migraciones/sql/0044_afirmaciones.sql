-- 144 (ADR-0007): la descripción de una foto llega como Afirmaciones que una persona acepta o rechaza
-- de a una. Se guardan con la v1 que las propuso, cada una con su estado y con quién la decidió; al
-- firmar, las aceptadas se escriben en el cuerpo de la Ficha, que sigue siendo la fuente de verdad
-- (ADR-0001). Las Fichas anteriores no tienen fila acá y se validan como hasta ahora.
--
-- `descripcion_de_imagen` es lo que dejó `describir_imagen` para esa v1: la clase de 139, o NULL si
-- la respuesta no cumplió el esquema (la Pieza quedó sin Afirmaciones y con el aviso). La supresión
-- efectiva (`suprimir_pieza`) borra la Ficha y estas filas se van con ella.
CREATE TABLE acervo.descripcion_de_imagen (
    ficha_id    uuid PRIMARY KEY REFERENCES acervo.ficha (ficha_id) ON DELETE CASCADE,
    tipo_imagen text
);

COMMENT ON TABLE acervo.descripcion_de_imagen IS
    'Lo que propuso describir_imagen para la v1 de una foto (144). tipo_imagen NULL: falló el esquema.';

CREATE TABLE acervo.afirmacion (
    ficha_id     uuid NOT NULL
                 REFERENCES acervo.descripcion_de_imagen (ficha_id) ON DELETE CASCADE,
    orden        smallint NOT NULL CHECK (orden >= 1),
    -- El tope es `entradas.LARGO_MAXIMO_DE_AFIRMACION`: una oración corta, no prosa.
    texto        text NOT NULL CHECK (
                     btrim(texto) <> '' AND position(E'\n' IN texto) = 0 AND length(texto) <= 300
                 ),
    estado       text NOT NULL DEFAULT 'sin_decidir'
                 CHECK (estado IN ('sin_decidir', 'aceptada', 'rechazada')),
    decidida_por uuid REFERENCES acervo.usuario (usuario_id),
    decidida_el  timestamptz,
    PRIMARY KEY (ficha_id, orden),
    -- Toda decisión tiene persona y momento; una sin decidir no tiene ninguno (ADR-0002).
    CONSTRAINT afirmacion_decidida_por_alguien CHECK (
        (estado = 'sin_decidir') = (decidida_por IS NULL)
        AND (decidida_por IS NULL) = (decidida_el IS NULL)
    )
);

COMMENT ON TABLE acervo.afirmacion IS
    'Las Afirmaciones de la v1 de una foto, con su decisión y quién la tomó (ADR-0007, 144).';

GRANT SELECT, INSERT ON acervo.descripcion_de_imagen TO hv_app;
GRANT SELECT, INSERT ON acervo.afirmacion TO hv_app;
GRANT UPDATE (estado, decidida_por, decidida_el) ON acervo.afirmacion TO hv_app;

-- La instrucción de 139 pasa a la de Afirmaciones, con esquema (144). Solo si la fila todavía nombra
-- la de 139: si el Administrador técnico eligió otra, manda lo suyo. Las de prosa siguen en el
-- código (v1 viejas, línea de base del banco). Sube la versión, como 0041.
UPDATE acervo.configuracion_capacidad
SET instruccion_id = 'describir_imagen_en_afirmaciones', version = version + 1,
    actualizada_por = NULL, actualizada_el = now()
WHERE capacidad = 'describir_imagen' AND instruccion_id = 'describir_imagen_por_clase';
