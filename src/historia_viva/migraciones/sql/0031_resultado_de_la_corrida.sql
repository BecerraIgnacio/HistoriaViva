-- 059: el resultado de la última corrida de efemérides, al lado de `ultima_corrida_el`, para que
-- P7f muestre qué dejó el «Generar ahora» que pidió (`pedido_manual_el`, generadas, omitidas con
-- su motivo y si se cortó). La auditoría guarda lo mismo en `efemeride.corrida`; esta columna es
-- la lectura de la pantalla y la escribe solo `efemerides`.

ALTER TABLE acervo.programacion_efemerides ADD COLUMN ultima_corrida_resultado jsonb;

GRANT UPDATE (ultima_corrida_resultado) ON acervo.programacion_efemerides TO hv_app;
