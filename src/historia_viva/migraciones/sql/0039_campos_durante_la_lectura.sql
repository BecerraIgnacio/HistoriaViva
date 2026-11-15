-- 152: lo que una persona completa mientras la Pieza está Recibida (decisión del 15-sep). Los campos
-- duros van a sus columnas, como los que copia el Lote; acá queda cuáles escribió y su Contexto, que
-- la v1 toma en vez del heredado. Así la procedencia de la v1 dice «persona» y no «modelo» (ADR-0001).
ALTER TABLE acervo.pieza
    ADD COLUMN campos_de_la_persona text[] NOT NULL DEFAULT '{}',
    ADD COLUMN contexto_de_la_persona text;

COMMENT ON COLUMN acervo.pieza.campos_de_la_persona IS
    'Cuáles campos duros escribió una persona con la Pieza Recibida, antes de la v1 (152).';
COMMENT ON COLUMN acervo.pieza.contexto_de_la_persona IS
    'Contexto escrito con la Pieza Recibida; la v1 lo usa en lugar de contexto_heredado (152).';
