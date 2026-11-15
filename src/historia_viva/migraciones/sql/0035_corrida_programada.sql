-- 060: cuándo empezó la última corrida programada (no un «Generar ahora»). De ella y de la
-- periodicidad sale cuándo toca la siguiente; una corrida cortada no la anota, así la vuelta
-- siguiente la retoma.

ALTER TABLE acervo.programacion_efemerides ADD COLUMN ultima_programada_el timestamptz;

GRANT UPDATE (ultima_programada_el) ON acervo.programacion_efemerides TO hv_app;
