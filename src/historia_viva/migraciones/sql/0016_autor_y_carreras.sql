-- 024: Autor y Carreras, opcionales (DEC-126). Del spec: la parte de la 0018 que es de este ticket;
-- `lote.propietario` y `lote_carrera` son de 082.
--
-- Las dos son proyección del frontmatter de la Ficha vigente (DEC-143): las escribe solo `fichas`.

ALTER TABLE acervo.pieza ADD COLUMN autor text;

COMMENT ON COLUMN acervo.pieza.autor IS
    'Opcional; admite el valor literal «Desconocido» (DEC-126). Nulo = no declarado.';

-- Sin `DELETE` (ADR-0004 enmendado): quitar una Carrera marca `quitada_el` y volver a ponerla
-- limpia la marca. La Carrera es un filtro, no un permiso (DEC-125): nada restringe filas por ella.
CREATE TABLE acervo.pieza_carrera (
    pieza_id    uuid NOT NULL REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    carrera_id  text NOT NULL REFERENCES acervo.carrera (carrera_id),
    asignada_el timestamptz NOT NULL DEFAULT now(),
    quitada_el  timestamptz CHECK (quitada_el IS NULL OR quitada_el >= asignada_el),
    PRIMARY KEY (pieza_id, carrera_id)
);

COMMENT ON COLUMN acervo.pieza_carrera.quitada_el IS 'Vigente = nula.';

-- El filtro por Carrera en listados y en el informe.
CREATE INDEX pieza_carrera_por_carrera ON acervo.pieza_carrera (carrera_id, pieza_id)
    WHERE quitada_el IS NULL;

GRANT SELECT, INSERT ON acervo.pieza_carrera TO hv_app;
GRANT UPDATE (quitada_el) ON acervo.pieza_carrera TO hv_app;
