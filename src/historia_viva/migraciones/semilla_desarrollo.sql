-- Personas de prueba, una por rol del Director (DEC-124), y Carreras de prueba. Solo en
-- desarrollo: fuera de él la base nace sin personas (DEC-52) ni Carreras (C45). No es una
-- migración: la corre `migrar` con HV_ENTORNO=desarrollo, cada vez, sin pisar lo que ya esté.
--
-- María es la identidad RGC de prueba con la que se firma sobre el corpus de prueba; lo que firma
-- con la sesión `dev` es una firma de ensayo (DEC-139).

INSERT INTO acervo.usuario (usuario_id, email, nombre, rol) VALUES
    ('11111111-1111-4111-8111-111111111111', 'mrossi@fie.undef.edu.ar',
     'Maria Fernanda Rossi', 'rgc'),
    ('22222222-2222-4222-8222-222222222222', 'jperalta@fie.undef.edu.ar',
     'Jorge Alberto Peralta', 'coordinador'),
    ('33333333-3333-4333-8333-333333333333', 'saguirre@fie.undef.edu.ar',
     'Silvina Beatriz Aguirre', 'director_carrera'),
    ('44444444-4444-4444-8444-444444444444', 'tibarra@fie.undef.edu.ar',
     'Tomás Esteban Ibarra', 'admin_tecnico')
ON CONFLICT DO NOTHING;

-- Carreras de prueba, hasta que la SEU entregue la lista oficial (C45). Sin `ON CONFLICT`: el
-- `UNIQUE (nombre)` es diferible (022) y no puede ser árbitro.
INSERT INTO acervo.carrera (carrera_id, nombre, orden)
SELECT * FROM (VALUES
    ('ing-civil',       'Ingeniería Civil',          1),
    ('ing-electronica', 'Ingeniería Electrónica',    2),
    ('ing-informatica', 'Ingeniería en Informática', 3),
    ('ing-mecanica',    'Ingeniería Mecánica',       4)
) AS prueba (carrera_id, nombre, orden)
WHERE NOT EXISTS (
    SELECT FROM acervo.carrera
    WHERE carrera.carrera_id = prueba.carrera_id OR carrera.nombre = prueba.nombre
);
