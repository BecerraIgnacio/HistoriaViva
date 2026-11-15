-- 044: la tabla del índice, la de LlamaIndex `PGVectorStore` (spec §1.8; DEC-01, DEC-07, DEC-84).
--
-- La crea esta migración y no la biblioteca: `hv_app` no tiene DDL y `indice` arma la tienda con
-- `perform_setup=False`. El nombre es el que la biblioteca deriva de `table_name="fragmento"`
-- (`data_` + nombre) y las columnas son las que consulta; el spike de 044 lo confirmó.
-- Búsqueda exacta: sin índice vectorial (DEC-07; HNSW queda fuera del MVP).

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'Falta la extensión vector: corré db/bootstrap.sql como superusuario.';
    END IF;
END
$$;

CREATE TABLE acervo.data_fragmento (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- = fragmento_id (UUIDv5, DEC-04).
    node_id   varchar NOT NULL UNIQUE,
    text      varchar NOT NULL,
    metadata_ jsonb NOT NULL,
    -- Un modelo de dimensión menor llega relleno con ceros (DEC-03); lo rellena `indice`.
    embedding vector(1024) NOT NULL,
    -- Las claves de los filtros previos y del borrado por Pieza; sin ellas el fragmento sería
    -- invisible a un filtro o imposible de desindexar.
    CONSTRAINT fragmento_claves CHECK (metadata_ ?& ARRAY[
        'pieza_id', 'ficha_id', 'tipo', 'tipo_actividad', 'carreras', 'fecha_inicio', 'fecha_fin',
        'modelo_embedding', 'via_ingreso'
    ]),
    CONSTRAINT fragmento_carreras_lista CHECK (jsonb_typeof(metadata_->'carreras') = 'array'),
    CONSTRAINT fragmento_via_ingreso CHECK (metadata_->>'via_ingreso' IN ('archivo', 'aporte'))
);

-- Borrado y control por Pieza; filtro y conteo por modelo (`indice.estado`).
CREATE INDEX fragmento_pieza ON acervo.data_fragmento ((metadata_->>'pieza_id'));
CREATE INDEX fragmento_modelo ON acervo.data_fragmento ((metadata_->>'modelo_embedding'));

-- La única tabla donde `hv_app` borra: el índice es derivado y reconstruible (DEC-143, RNF-08).
GRANT SELECT, INSERT, UPDATE, DELETE ON acervo.data_fragmento TO hv_app;
REVOKE TRUNCATE, REFERENCES ON acervo.data_fragmento FROM hv_app;
