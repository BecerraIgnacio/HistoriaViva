-- 056: GIN sobre las Carreras del fragmento (spec §5, índices de `data_fragmento`; RNF-10).
--
-- El filtro previo `metadata_->'carreras' ?| …` (`indice._filtrar`) recorría la tabla entera:
-- con 28 000 fragmentos sintéticos (el volumen al cierre, §7) tardó segundos sin el índice y
-- décimas con él, en la PC del alumno. Sigue sin índice vectorial (DEC-07). Sin `CONCURRENTLY`:
-- al volumen de hoy se crea en un instante.

CREATE INDEX fragmento_carreras ON acervo.data_fragmento USING gin ((metadata_->'carreras'));
