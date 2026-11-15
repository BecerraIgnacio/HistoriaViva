-- 025: firma y retiro (spec §1.6, la 0007 del spec sin `alerta_inyeccion`, que ya está en la
-- 0005). `modo_validacion` es proyección del frontmatter de la Ficha vigente y la escribe solo
-- `fichas`; las columnas `borrada_*` son el retiro del glosario (DEC-146 punto 22), que llega con
-- R7: desde ya todo listado y todo índice de trabajo deja afuera a las retiradas.

ALTER TABLE acervo.pieza
    ADD COLUMN modo_validacion text CHECK (modo_validacion IN ('individual','bloque')),
    ADD COLUMN borrada_el timestamptz,
    ADD COLUMN borrada_por uuid REFERENCES acervo.usuario (usuario_id),
    ADD COLUMN borrada_motivo text
        CHECK (borrada_motivo IS NULL OR char_length(borrada_motivo) BETWEEN 5 AND 500),
    ADD CONSTRAINT pieza_borrado_trazado CHECK (
        (borrada_el IS NULL) = (borrada_por IS NULL)
        AND (borrada_el IS NULL) = (borrada_motivo IS NULL)
    );

-- Hasta acá toda firma fue individual: el frontmatter ya lo decía (se derivaba del estado).
UPDATE acervo.pieza SET modo_validacion = 'individual' WHERE estado = 'Validada';

-- Invariante 4 ampliada: la firma dice también en qué modo se hizo (historia 120).
ALTER TABLE acervo.pieza
    DROP CONSTRAINT pieza_validada_firmada,
    ADD CONSTRAINT pieza_validada_firmada CHECK (
        estado <> 'Validada'
        OR (validada_por IS NOT NULL AND validada_el IS NOT NULL AND modo_validacion IS NOT NULL)
    ),
    -- Y al revés: sin firma no hay modo (reabrir una Validada lo anula, spec §5).
    ADD CONSTRAINT pieza_modo_solo_validada
        CHECK (estado = 'Validada' OR modo_validacion IS NULL);

COMMENT ON COLUMN acervo.pieza.modo_validacion IS
    'individual o bloque (la aprobación en bloque se activa con C29). Nulo si no está Validada.';
COMMENT ON COLUMN acervo.pieza.borrada_el IS
    'Retiro (borrado lógico): la Pieza sale de listados, búsqueda y /v1. Se restaura.';
COMMENT ON COLUMN acervo.pieza.borrada_motivo IS 'Entre 5 y 500 caracteres.';

DROP INDEX acervo.pieza_cola_estado;
CREATE INDEX pieza_cola_estado ON acervo.pieza (estado, creada_el DESC)
    WHERE borrada_el IS NULL;

DROP INDEX acervo.pieza_cola_usuario;
CREATE INDEX pieza_cola_usuario ON acervo.pieza (creada_por, estado)
    WHERE borrada_el IS NULL AND estado IN ('Pendiente','Incompleta');

DROP INDEX acervo.pieza_trabajada;
CREATE INDEX pieza_trabajada ON acervo.pieza (trabajada_por, estado)
    WHERE borrada_el IS NULL AND estado = 'Incompleta';

-- Un original retirado se puede volver a subir (DEC-20: duplicado en el Acervo no retirado).
DROP INDEX acervo.pieza_huella_unica;
CREATE UNIQUE INDEX pieza_huella_unica ON acervo.pieza (huella_original)
    WHERE borrada_el IS NULL AND via_ingreso = 'archivo' AND huella_original IS NOT NULL;

-- Solapamiento de Fechas en los listados.
CREATE INDEX pieza_rango_fechas ON acervo.pieza USING gist (rango_fechas)
    WHERE borrada_el IS NULL;
