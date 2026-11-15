-- 021: los catálogos de la Ficha (DEC-127). Del spec: los catálogos de la 0017 (su `CHECK` de rol
-- lo hizo 016).
--
-- Los tres los lee `catalogos` y `hv_app` solo los lee (DEC-105). La SEU corrige tipos de
-- actividad y de Pieza con una migración como `hv_migrador`; dar de baja es `activo = false`, que
-- no invalida Fichas.

-- Nace vacía: la lista oficial la entrega la SEU (C45). En desarrollo la siembra
-- `semilla_desarrollo.sql`.
CREATE TABLE acervo.carrera (
    carrera_id text PRIMARY KEY CHECK (carrera_id ~ '^[a-z0-9-]+$'),
    nombre     text NOT NULL UNIQUE CHECK (btrim(nombre) <> ''),
    orden      integer NOT NULL,
    activo     boolean NOT NULL DEFAULT true
);

COMMENT ON COLUMN acervo.carrera.carrera_id IS
    'Slug estable: el valor que viaja en metadata_.carreras y en los filtros.';

CREATE TABLE acervo.tipo_actividad (
    nombre       text PRIMARY KEY CHECK (btrim(nombre) <> ''),
    orden        integer NOT NULL,
    es_actividad boolean NOT NULL DEFAULT true,
    activo       boolean NOT NULL DEFAULT true
);

COMMENT ON COLUMN acervo.tipo_actividad.es_actividad IS
    'false solo en «No es una actividad»: el informe de acreditación la excluye (DEC-131).';

-- Los siete del Director (DIR-23).
INSERT INTO acervo.tipo_actividad (nombre, orden, es_actividad) VALUES
    ('Transferencia',              1, true),
    ('Consultoría',                2, true),
    ('Asistencia Técnica',         3, true),
    ('Proyecto UNDEX',             4, true),
    ('Evento Socio-Cultural',      5, true),
    ('Programa/Curso/Diplomatura', 6, true),
    ('Evento Académico',           7, true),
    ('No es una actividad',        8, false);

ALTER TABLE acervo.tipo_pieza ADD COLUMN provisorio boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN acervo.tipo_pieza.provisorio IS
    'true mientras la SEU no confirme el tipo (C4). Confirmarlo o darlo de baja es una migración.';

INSERT INTO acervo.tipo_pieza (nombre, orden, provisorio) VALUES
    ('planilla', 8, true),
    ('video',    9, true);

-- `INSERT` y `UPDATE` sobre `carrera` los suma 022 «cargar Carreras desde un CSV».
GRANT SELECT ON acervo.carrera, acervo.tipo_actividad TO hv_app;
