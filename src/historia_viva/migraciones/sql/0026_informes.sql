-- 066: el Informe de acreditación (spec §1.22, la 0021 del spec; DEC-131, R-22, R-24).
--
-- Se arma sin modelo y se congela al generar: `informe_pieza` apunta a versiones de Ficha, que
-- son inmutables, así que lo aprobado y lo exportado coinciden. Los faltantes no se guardan: se
-- calculan al leer (R-21). `hv_app` cambia el estado y las marcas del informe, nunca su
-- contenido; lo congelado y las aprobaciones son append-only, y nadie borra.

CREATE TABLE acervo.informe (
    informe_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    desde           date NOT NULL,
    hasta           date NOT NULL,
    -- El filtro tal como se pidió (`carrera_id`); vacío = todas. Sin FK.
    carreras        text[] NOT NULL DEFAULT '{}',
    estado          text NOT NULL DEFAULT 'borrador'
                        CHECK (estado IN ('borrador','aprobado','exportado')),
    -- SHA-256 del contenido canónico: las aprobaciones se firman sobre ella.
    huella          text NOT NULL CHECK (huella ~ '^[0-9a-f]{64}$'),
    integridad      numeric(5,4) CHECK (integridad BETWEEN 0 AND 1),
    generado_por    uuid NOT NULL REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    generado_el     timestamptz NOT NULL DEFAULT now(),
    reemplazado_por uuid REFERENCES acervo.informe (informe_id),
    descartado_por  uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    descartado_el   timestamptz,
    aprobado_el     timestamptz,
    exportado_por   uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    exportado_el    timestamptz,
    CONSTRAINT informe_periodo CHECK (hasta >= desde),
    CONSTRAINT informe_reemplazo_de_borrador
        CHECK (reemplazado_por IS NULL OR estado = 'borrador'),
    CONSTRAINT informe_descarte_trazado
        CHECK ((descartado_por IS NULL) = (descartado_el IS NULL)),
    CONSTRAINT informe_descarte_de_borrador
        CHECK (descartado_el IS NULL OR estado = 'borrador'),
    CONSTRAINT informe_aprobado_con_fecha CHECK ((estado = 'borrador') = (aprobado_el IS NULL)),
    CONSTRAINT informe_exportado_trazado CHECK (
        (estado = 'exportado') = (exportado_el IS NOT NULL AND exportado_por IS NOT NULL)
    )
);

CREATE INDEX informe_por_estado ON acervo.informe (estado, generado_el DESC)
    WHERE reemplazado_por IS NULL AND descartado_el IS NULL;

CREATE TABLE acervo.informe_pieza (
    informe_id     uuid NOT NULL REFERENCES acervo.informe (informe_id) ON DELETE RESTRICT,
    pieza_id       uuid NOT NULL REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    -- La versión cuyo resumen y Cita entran al informe.
    ficha_id       uuid NOT NULL REFERENCES acervo.ficha (ficha_id) ON DELETE RESTRICT,
    tipo_actividad text NOT NULL REFERENCES acervo.tipo_actividad (nombre)
                       CHECK (tipo_actividad <> 'No es una actividad'),
    fecha_inicio   date NOT NULL,
    fecha_fin      date NOT NULL,
    orden          integer NOT NULL CHECK (orden >= 1),
    PRIMARY KEY (informe_id, pieza_id),
    CONSTRAINT informe_pieza_orden_unico UNIQUE (informe_id, orden)
);

CREATE INDEX informe_pieza_por_pieza ON acervo.informe_pieza (pieza_id);

CREATE TABLE acervo.informe_aprobacion (
    aprobacion_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    informe_id           uuid NOT NULL REFERENCES acervo.informe (informe_id) ON DELETE RESTRICT,
    rol                  text NOT NULL CHECK (rol IN ('rgc','coordinador')),
    aprobada_por         uuid NOT NULL REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    aprobada_el          timestamptz NOT NULL DEFAULT now(),
    metodo_autenticacion text NOT NULL CHECK (metodo_autenticacion IN ('oidc','dev')),
    huella               text NOT NULL,
    CONSTRAINT informe_aprobacion_por_rol UNIQUE (informe_id, rol),
    CONSTRAINT informe_aprobacion_por_persona UNIQUE (informe_id, aprobada_por)
);

CREATE FUNCTION acervo.informe_aprobado_dos_firmas() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.estado <> 'borrador' AND (NEW.desde, NEW.hasta, NEW.carreras, NEW.huella,
        NEW.aprobado_el) IS DISTINCT FROM (OLD.desde, OLD.hasta, OLD.carreras, OLD.huella,
        OLD.aprobado_el) THEN
        RAISE EXCEPTION 'informe_aprobado_dos_firmas: el informe % ya no es Borrador: no cambia su contenido ni cuándo se aprobó',
            OLD.informe_id USING ERRCODE = 'restrict_violation';
    END IF;
    -- La primera exportación queda: las siguientes se auditan, no la reescriben (R-23).
    IF OLD.estado = 'exportado' AND (NEW.exportado_por, NEW.exportado_el)
        IS DISTINCT FROM (OLD.exportado_por, OLD.exportado_el) THEN
        RAISE EXCEPTION 'informe_aprobado_dos_firmas: el informe % ya se exportó y conserva su primera exportación',
            OLD.informe_id USING ERRCODE = 'restrict_violation';
    END IF;
    IF (OLD.estado, NEW.estado) NOT IN (('borrador','borrador'), ('borrador','aprobado'),
        ('aprobado','aprobado'), ('aprobado','exportado'), ('exportado','exportado')) THEN
        RAISE EXCEPTION 'informe_aprobado_dos_firmas: el informe % no pasa de % a %',
            OLD.informe_id, OLD.estado, NEW.estado USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.estado = 'borrador' AND NEW.estado = 'aprobado' AND (
        SELECT count(*) FILTER (WHERE rol = 'rgc') <> 1
            OR count(*) FILTER (WHERE rol = 'coordinador') <> 1
            OR count(*) <> 2
            OR count(DISTINCT aprobada_por) <> 2
        FROM acervo.informe_aprobacion
        WHERE informe_id = NEW.informe_id AND huella = NEW.huella
    ) THEN
        RAISE EXCEPTION 'informe_aprobado_dos_firmas: el informe % necesita la aprobación de un RGC y de un Coordinador distintos sobre su huella',
            NEW.informe_id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER informe_aprobado_dos_firmas BEFORE UPDATE ON acervo.informe
    FOR EACH ROW EXECUTE FUNCTION acervo.informe_aprobado_dos_firmas();

CREATE FUNCTION acervo.informe_aprobacion_rol() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM acervo.usuario
        WHERE usuario_id = NEW.aprobada_por AND rol = NEW.rol AND activo
    ) THEN
        RAISE EXCEPTION 'informe_aprobacion_rol: la persona % no tiene hoy el rol % activo',
            NEW.aprobada_por, NEW.rol USING ERRCODE = 'restrict_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM acervo.informe
        WHERE informe_id = NEW.informe_id AND estado = 'borrador'
          AND reemplazado_por IS NULL AND descartado_el IS NULL
    ) THEN
        RAISE EXCEPTION 'informe_aprobacion_rol: el informe % no es un Borrador vigente',
            NEW.informe_id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER informe_aprobacion_rol BEFORE INSERT ON acervo.informe_aprobacion
    FOR EACH ROW EXECUTE FUNCTION acervo.informe_aprobacion_rol();

-- Lo congelado se arma solo en un Borrador sin firmas: después cambiaría el contenido sin cambiar
-- la huella sobre la que se firmó.
CREATE FUNCTION acervo.informe_pieza_de_borrador() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM acervo.informe WHERE informe_id = NEW.informe_id AND estado = 'borrador'
    ) OR EXISTS (
        SELECT 1 FROM acervo.informe_aprobacion WHERE informe_id = NEW.informe_id
    ) THEN
        RAISE EXCEPTION 'informe_pieza_de_borrador: el informe % no es un Borrador sin aprobaciones',
            NEW.informe_id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER informe_pieza_de_borrador BEFORE INSERT ON acervo.informe_pieza
    FOR EACH ROW EXECUTE FUNCTION acervo.informe_pieza_de_borrador();

GRANT SELECT, INSERT ON acervo.informe TO hv_app;
GRANT UPDATE (estado, reemplazado_por, descartado_por, descartado_el, aprobado_el,
              exportado_por, exportado_el) ON acervo.informe TO hv_app;
GRANT SELECT, INSERT ON acervo.informe_pieza TO hv_app;
GRANT SELECT, INSERT ON acervo.informe_aprobacion TO hv_app;
