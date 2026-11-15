-- 035: el latido de cada proceso trabajador (spec §1.18, de la 0005 del spec; DEC-106, RNF-25).
--
-- Una fila por proceso, con el mismo id que escribe en `trabajo_procesamiento.tomado_por`: el
-- trabajador la reescribe cada 15 s y la salud da por caído un tipo sin latido de menos de 60 s.
-- Un reinicio estrena id y la fila vieja queda: sin DELETE, nadie la borra.

CREATE TABLE acervo.latido_trabajador (
    trabajador text PRIMARY KEY,
    tipos      text[] NOT NULL,
    latido_el  timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON acervo.latido_trabajador TO hv_app;
