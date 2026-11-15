-- 022: `catalogo carreras --cargar <csv>` escribe `carrera` como `hv_app` (R-25) hasta que C45
-- permita sembrar la lista oficial por migración. Sin `DELETE`: una Carrera que sale de la lista
-- se desactiva y las Fichas que la tienen siguen válidas (DEC-127).
GRANT INSERT, UPDATE ON acervo.carrera TO hv_app;

-- La carga valida los nombres sobre la lista final, no fila por fila: así dos Carreras pueden
-- intercambiar sus nombres. `catalogos.cargar_carreras` la difiere dentro de su transacción.
ALTER TABLE acervo.carrera
    DROP CONSTRAINT carrera_nombre_key,
    ADD CONSTRAINT carrera_nombre_key UNIQUE (nombre) DEFERRABLE INITIALLY IMMEDIATE;
