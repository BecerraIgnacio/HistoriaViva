-- 082: el Lote declara propietario y Carreras, opcionales, y sus Piezas los heredan (DEC-126).
-- Del spec: la parte de la 0018 que es de este ticket. `lote_carrera` la escribe solo `lotes` (R-25).

ALTER TABLE acervo.lote
    ADD COLUMN propietario text CHECK (propietario IS NULL OR btrim(propietario) <> '');

COMMENT ON COLUMN acervo.lote.propietario IS 'Opcional en el Lote (DEC-126); las Piezas lo heredan.';

-- Sin `DELETE` (ADR-0004 enmendado), como `pieza_carrera`: quitar una Carrera marca `quitada_el`.
CREATE TABLE acervo.lote_carrera (
    lote_id     uuid NOT NULL REFERENCES acervo.lote (lote_id) ON DELETE RESTRICT,
    carrera_id  text NOT NULL REFERENCES acervo.carrera (carrera_id),
    asignada_el timestamptz NOT NULL DEFAULT now(),
    quitada_el  timestamptz CHECK (quitada_el IS NULL OR quitada_el >= asignada_el),
    PRIMARY KEY (lote_id, carrera_id)
);

COMMENT ON COLUMN acervo.lote_carrera.quitada_el IS 'Vigente = nula.';

CREATE INDEX lote_carrera_vigente ON acervo.lote_carrera (lote_id) WHERE quitada_el IS NULL;

GRANT SELECT, INSERT ON acervo.lote_carrera TO hv_app;
GRANT UPDATE (quitada_el) ON acervo.lote_carrera TO hv_app;
