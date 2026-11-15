-- =====================================================================
-- Historia Viva -- MVP. Migración 0001: baseline de paridad.
--
-- La forma final de las cinco migraciones del Prototipo (db/001 a 005),
-- consolidada: no las reaplica una por una. Más la clase y la huella del
-- original en `pieza` (la 0004 del spec) y lo que fija la
-- especificación de paridad: `usuario.sub_google` y `usuario.rol`,
-- `sesion`, `ficha.metodo_autenticacion`, `ficha.configuracion_efectiva`
-- y el trigger `ficha_inmutable`, `trabajo_procesamiento` sin
-- `tope_tokens` (el tope es por llamada; acá se miden los tokens),
-- `configuracion_capacidad` y `auditoria`.
--
-- La base del MVP nace limpia (DEC-52): ninguna migración siembra
-- personas. El schema `acervo` lo crea el runner antes de esta migración.
-- =====================================================================

COMMENT ON SCHEMA acervo IS
    'Acervo histórico de la FIE. Detalle interno del módulo de Fichas: nada fuera de ese módulo conoce estas tablas (RNF-09).';


-- ------------------------------------------------------------ usuario ---
CREATE TABLE acervo.usuario (
    usuario_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email            text NOT NULL UNIQUE
                         CHECK (email = lower(email) AND email LIKE '%@%'),
    nombre           text NOT NULL,
    sub_google       text UNIQUE,
    -- Los dos roles del Prototipo; los cuatro del Director llegan en su
    -- propia migración, que reemplaza este CHECK (DEC-124).
    rol              text NOT NULL DEFAULT 'usuario'
                         CHECK (rol IN ('usuario','administrador')),
    activo           boolean NOT NULL DEFAULT true,
    creado_el        timestamptz NOT NULL DEFAULT now(),
    ultimo_acceso_el timestamptz
);

COMMENT ON TABLE acervo.usuario IS
    'Persona de la SEU. Sin columna de contraseña ni de hash (RNF-13): se identifica por Google Workspace o, en desarrollo, por la sesión dev.';
COMMENT ON COLUMN acervo.usuario.sub_google IS
    'Identificador estable de Google. Nulo hasta el primer ingreso.';
COMMENT ON COLUMN acervo.usuario.activo IS
    'Baja lógica. Una persona dada de baja sigue siendo legible en las Fichas que firmó.';


-- ------------------------------------------------------------- sesion ---
CREATE TABLE acervo.sesion (
    sesion_id           text PRIMARY KEY,
    usuario_id          uuid NOT NULL REFERENCES acervo.usuario (usuario_id),
    metodo              text NOT NULL CHECK (metodo IN ('oidc','dev')),
    creada_el           timestamptz NOT NULL DEFAULT now(),
    ultima_actividad_el timestamptz NOT NULL DEFAULT now(),
    vence_el            timestamptz NOT NULL,
    revocada_el         timestamptz,

    CONSTRAINT sesion_vence_despues CHECK (vence_el > creada_el)
);

COMMENT ON TABLE acervo.sesion IS
    'Sesión de una persona. Cerrar o revocar es marcar `revocada_el`, sin DELETE; activa, vencida o revocada es derivado.';
COMMENT ON COLUMN acervo.sesion.sesion_id IS
    'sha256 hexadecimal del id de la cookie. Nunca el id.';

CREATE INDEX sesion_viva ON acervo.sesion (usuario_id) WHERE revocada_el IS NULL;


-- --------------------------------------------------------- tipo_pieza ---
-- Una tabla y no un CHECK ni una constante: la taxonomía la define la SEU
-- (consulta C4) y se corrige con un INSERT de `hv_migrador`, sin desplegar.
-- No hay FK desde `pieza.tipo`: dar de baja un tipo no invalida Fichas.
CREATE TABLE acervo.tipo_pieza (
    nombre text PRIMARY KEY CHECK (nombre = lower(nombre) AND nombre <> ''),
    orden  integer NOT NULL,
    activo boolean NOT NULL DEFAULT true
);

COMMENT ON TABLE acervo.tipo_pieza IS
    'Valores admitidos de pieza.tipo. Lista configurable, no enum del código (consulta C4).';

INSERT INTO acervo.tipo_pieza (nombre, orden) VALUES
    ('fotografia', 1),
    ('acta',       2),
    ('resolucion', 3),
    ('convenio',   4),
    ('memoria',    5),
    ('nota',       6),
    ('plano',      7);


-- -------------------------------------------------------------- pieza ---
CREATE TABLE acervo.pieza (
    pieza_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    estado               text NOT NULL DEFAULT 'Recibida'
                             CHECK (estado IN ('Recibida','Pendiente','Incompleta','Validada')),
    tipo                 text,   -- se valida contra tipo_pieza en `fichas`, sin FK
    fecha_inicio         date,
    fecha_fin            date,
    precision_fecha      text CHECK (precision_fecha IN ('dia','mes','anio','decada','rango')),
    rango_fechas         daterange
                             GENERATED ALWAYS AS (daterange(fecha_inicio, fecha_fin, '[]')) STORED,
    origen               text,
    resumen              text,
    archivo_original_id  text,
    archivo_original_url text,
    nombre_archivo       text NOT NULL,
    tipo_mime            text NOT NULL,
    tamano_bytes         bigint NOT NULL CHECK (tamano_bytes > 0),
    clase_original       text NOT NULL
                             CHECK (clase_original IN ('imagen','documento','planilla','video','texto')),
    -- Nace con este nombre: la base del MVP arranca limpia y no hay
    -- `procedencia` que renombrar (DEC-140).
    via_ingreso          text NOT NULL DEFAULT 'archivo'
                             CHECK (via_ingreso IN ('archivo','aporte')),
    metadata_original    jsonb
                             CHECK (metadata_original IS NULL
                                    OR jsonb_typeof(metadata_original) = 'object'),
    huella_original      text CHECK (huella_original ~ '^[0-9a-f]{64}$'),
    validada_por         uuid REFERENCES acervo.usuario (usuario_id),
    validada_el          timestamptz,
    creada_por           uuid REFERENCES acervo.usuario (usuario_id),
    trabajada_por        uuid REFERENCES acervo.usuario (usuario_id),
    creada_el            timestamptz NOT NULL DEFAULT now(),
    actualizada_el       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pieza_fechas_ordenadas
        CHECK (fecha_inicio IS NULL OR fecha_fin IS NULL OR fecha_fin >= fecha_inicio),

    -- Invariante 12, con la quinta rama `rango` del Prototipo (D-P1): las
    -- otras cuatro siguen siendo estrictas.
    CONSTRAINT pieza_precision_coherente CHECK (
        precision_fecha IS NULL OR fecha_inicio IS NULL OR fecha_fin IS NULL
        OR (precision_fecha = 'dia'
            AND fecha_inicio = fecha_fin)
        OR (precision_fecha = 'mes'
            AND date_part('day', fecha_inicio) = 1
            AND date_part('year', fecha_inicio) = date_part('year', fecha_fin)
            AND date_part('month', fecha_inicio) = date_part('month', fecha_fin)
            AND date_part('month', fecha_fin + 1) <> date_part('month', fecha_fin))
        OR (precision_fecha = 'anio'
            AND date_part('month', fecha_inicio) = 1 AND date_part('day', fecha_inicio) = 1
            AND date_part('month', fecha_fin) = 12  AND date_part('day', fecha_fin) = 31
            AND date_part('year', fecha_inicio) = date_part('year', fecha_fin))
        OR (precision_fecha = 'decada'
            AND date_part('month', fecha_inicio) = 1 AND date_part('day', fecha_inicio) = 1
            AND date_part('month', fecha_fin) = 12  AND date_part('day', fecha_fin) = 31
            AND date_part('year', fecha_inicio)::int % 10 = 0
            AND date_part('year', fecha_fin)::int = date_part('year', fecha_inicio)::int + 9)
        OR (precision_fecha = 'rango'
            AND fecha_fin >= fecha_inicio)
    ),

    -- Invariante 4: toda Pieza Validada está firmada (RNF-07, ADR-0002).
    CONSTRAINT pieza_validada_firmada CHECK (
        estado <> 'Validada'
        OR (validada_por IS NOT NULL AND validada_el IS NOT NULL)
    ),

    -- Invariante 5: es la diferencia entre Validada e Incompleta (RF-14).
    CONSTRAINT pieza_validada_completa CHECK (
        estado <> 'Validada'
        OR (tipo IS NOT NULL AND origen IS NOT NULL AND resumen IS NOT NULL
            AND fecha_inicio IS NOT NULL AND fecha_fin IS NOT NULL
            AND precision_fecha IS NOT NULL AND archivo_original_id IS NOT NULL)
    ),

    -- Agregar un formato es una línea acá, a propósito.
    CONSTRAINT pieza_clase_mime CHECK (CASE clase_original
        WHEN 'imagen' THEN tipo_mime LIKE 'image/%'
        WHEN 'documento' THEN tipo_mime IN (
            'application/pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        WHEN 'planilla' THEN split_part(tipo_mime, ';', 1) IN (
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'text/csv')
        WHEN 'video' THEN tipo_mime IN ('video/mp4','video/quicktime','video/webm')
        WHEN 'texto' THEN split_part(tipo_mime, ';', 1) = 'text/plain'
    END),

    -- Lo que no tiene plan de modelos nace Pendiente (DEC-10).
    CONSTRAINT pieza_sin_plan_no_recibida
        CHECK (clase_original NOT IN ('video','texto') OR estado <> 'Recibida'),

    -- Un Aporte es texto y no lo sube una persona.
    CONSTRAINT pieza_aporte_coherente CHECK (
        (clase_original = 'texto') = (via_ingreso = 'aporte')
        AND (via_ingreso <> 'aporte' OR creada_por IS NULL)
    )
);

COMMENT ON TABLE acervo.pieza IS
    'Unidad del acervo. Proyección consultable del frontmatter de la Ficha vigente; la Ficha markdown es la fuente de verdad (ADR-0001).';
COMMENT ON COLUMN acervo.pieza.estado IS
    'Recibida, Pendiente, Incompleta o Validada: el mismo valor que viaja al frontmatter.';
COMMENT ON COLUMN acervo.pieza.precision_fecha IS
    'Qué se sabe de la Fecha: dia, mes, anio, decada o rango. Sin esta columna una década es indistinguible de un evento que duró diez años.';
COMMENT ON COLUMN acervo.pieza.rango_fechas IS
    'Columna generada [fecha_inicio, fecha_fin] para indexar solapamiento; no se edita.';
COMMENT ON COLUMN acervo.pieza.archivo_original_id IS
    'local://originales/{pieza_id}: opaco y permanente.';
COMMENT ON COLUMN acervo.pieza.nombre_archivo IS
    'Nombre con el que la persona subió el original; solo se muestra.';
COMMENT ON COLUMN acervo.pieza.tipo_mime IS
    'MIME detectado del contenido, no el declarado por el navegador. Es el que se devuelve al servir el original.';
COMMENT ON COLUMN acervo.pieza.clase_original IS
    'imagen, documento, planilla, video o texto: decide el plan de modelos y el visor.';
COMMENT ON COLUMN acervo.pieza.via_ingreso IS
    'archivo (lo subió una persona) o aporte (lo mandó un Agente externo). En el código, ViaDeIngreso (DEC-140).';
COMMENT ON COLUMN acervo.pieza.metadata_original IS
    'Lo que se lee del original sin modelos (hojas de una planilla, ffprobe de un video). Inmutable.';
COMMENT ON COLUMN acervo.pieza.huella_original IS
    'SHA-256 hexadecimal del original, calculado al vuelo en la subida.';
COMMENT ON COLUMN acervo.pieza.trabajada_por IS
    'Última persona que guardó la Ficha. Alimenta Mis Piezas (DEC-23).';

CREATE INDEX pieza_cola_estado ON acervo.pieza (estado, creada_el DESC);

CREATE INDEX pieza_cola_usuario ON acervo.pieza (creada_por, estado)
    WHERE estado IN ('Pendiente','Incompleta');

-- Mis Piezas: las Incompletas que trabajé (DEC-23).
CREATE INDEX pieza_trabajada ON acervo.pieza (trabajada_por, estado)
    WHERE estado = 'Incompleta';

-- Duplicado por SHA-256 en todo el Acervo (DEC-20): respaldo de la
-- comprobación de la subida. Un Aporte no tiene archivo que comparar.
CREATE UNIQUE INDEX pieza_huella_unica ON acervo.pieza (huella_original)
    WHERE via_ingreso = 'archivo' AND huella_original IS NOT NULL;


-- -------------------------------------------------------------- ficha ---
CREATE TABLE acervo.ficha (
    ficha_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pieza_id               uuid NOT NULL REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    version                integer NOT NULL CHECK (version >= 1),
    contenido_markdown     text NOT NULL CHECK (length(contenido_markdown) > 0),
    origen_version         text NOT NULL CHECK (origen_version IN ('modelo','persona')),
    editada_por            uuid REFERENCES acervo.usuario (usuario_id),
    metodo_autenticacion   text CHECK (metodo_autenticacion IN ('oidc','dev')),
    configuracion_efectiva jsonb,
    editada_el             timestamptz NOT NULL DEFAULT now(),
    vigente                boolean NOT NULL DEFAULT true,

    CONSTRAINT ficha_version_unica UNIQUE (pieza_id, version),

    -- Invariante 11: lo que firma una persona tiene persona (ADR-0002).
    CONSTRAINT ficha_persona_identificada
        CHECK (origen_version <> 'persona' OR editada_por IS NOT NULL),
    -- Separa las firmas de ensayo (dev) de las reales (DEC-37).
    CONSTRAINT ficha_metodo_de_persona
        CHECK ((origen_version = 'persona') = (metodo_autenticacion IS NOT NULL)),
    CONSTRAINT ficha_configuracion_de_modelo
        CHECK (origen_version = 'modelo' OR configuracion_efectiva IS NULL)
);

COMMENT ON TABLE acervo.ficha IS
    'El markdown por Pieza, versionado. Filas apiladas e inmutables: responde quién cambió qué y cuándo (RNF-07).';
COMMENT ON COLUMN acervo.ficha.ficha_id IS
    'Identifica una versión, no la Ficha conceptual.';
COMMENT ON COLUMN acervo.ficha.origen_version IS
    'modelo: propuesta, sin firmar. persona: firmada (ADR-0002).';
COMMENT ON COLUMN acervo.ficha.metodo_autenticacion IS
    'Con qué sesión firmó la persona: oidc o dev. Solo en versiones persona.';
COMMENT ON COLUMN acervo.ficha.configuracion_efectiva IS
    'Con qué modelo, digest, instrucción y topes se generó. Solo en versiones modelo.';

-- Invariante 10: una sola Ficha vigente por Pieza.
CREATE UNIQUE INDEX ficha_vigente_unica ON acervo.ficha (pieza_id) WHERE vigente;

-- RNF-07 contra un UPDATE hecho desde psql: una versión no cambia; lo único
-- que se admite es que deje de estar vigente. Nada vuelve a vigente.
CREATE FUNCTION acervo.ficha_inmutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.vigente AND NOT NEW.vigente
       AND to_jsonb(NEW) - 'vigente' = to_jsonb(OLD) - 'vigente' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'ficha_inmutable: la versión % de la Ficha % no se modifica; solo puede dejar de estar vigente',
        OLD.version, OLD.pieza_id
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER ficha_inmutable BEFORE UPDATE ON acervo.ficha
    FOR EACH ROW EXECUTE FUNCTION acervo.ficha_inmutable();


-- ---------------------------------------------- trabajo_procesamiento ---
-- La cola es una tabla, sin broker (consulta C2); la toma se hace con
-- SELECT ... FOR UPDATE SKIP LOCKED.
CREATE TABLE acervo.trabajo_procesamiento (
    trabajo_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pieza_id               uuid NOT NULL REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    tipo                   text NOT NULL CHECK (tipo IN ('procesamiento','indexacion')),
    estado                 text NOT NULL DEFAULT 'encolado'
                               CHECK (estado IN ('encolado','en_curso','completado','fallido','cancelado')),
    configuracion_efectiva jsonb NOT NULL,
    generaciones           integer NOT NULL DEFAULT 0 CHECK (generaciones >= 0),
    tope_generaciones      integer NOT NULL DEFAULT 1 CHECK (tope_generaciones BETWEEN 1 AND 3),
    tiempo_maximo_ms       integer NOT NULL CHECK (tiempo_maximo_ms BETWEEN 1000 AND 1800000),
    tokens_entrada         integer NOT NULL DEFAULT 0 CHECK (tokens_entrada >= 0),
    tokens_salida          integer NOT NULL DEFAULT 0 CHECK (tokens_salida >= 0),
    duracion_ms            integer CHECK (duracion_ms >= 0),
    tomado_por             text,
    arriendo_vence_el      timestamptz,
    encolado_el            timestamptz NOT NULL DEFAULT now(),
    iniciado_el            timestamptz,
    terminado_el           timestamptz,
    error                  text,

    -- Invariante 9 (ADR-0004): el bucle de generación se corta contra la base.
    CONSTRAINT trabajo_tope_generaciones CHECK (generaciones <= tope_generaciones),
    CONSTRAINT trabajo_fallido_explicado CHECK (estado <> 'fallido' OR error IS NOT NULL)
);

COMMENT ON TABLE acervo.trabajo_procesamiento IS
    'La cola de procesamiento. Un fallo del trabajador no es un estado de la Pieza: la Pieza sigue Recibida y el fallo vive acá.';
COMMENT ON COLUMN acervo.trabajo_procesamiento.configuracion_efectiva IS
    'Instantánea de la configuración congelada al encolar: un cambio de fila rige para los trabajos nuevos.';
COMMENT ON COLUMN acervo.trabajo_procesamiento.tokens_entrada IS
    'Tokens medidos, sin tope: el tope de tokens es por llamada, en la capa de modelos.';
COMMENT ON COLUMN acervo.trabajo_procesamiento.arriendo_vence_el IS
    'Un proceso caído libera su trabajo por vencimiento, no por intervención.';

-- Invariante 8 (RNF-05): solo un trabajo fallido o cancelado deja lugar a otro.
CREATE UNIQUE INDEX trabajo_idempotente_por_pieza
    ON acervo.trabajo_procesamiento (pieza_id, tipo)
    WHERE estado IN ('encolado','en_curso','completado');

CREATE INDEX trabajo_cola ON acervo.trabajo_procesamiento (encolado_el)
    WHERE estado = 'encolado';


-- ------------------------------------------------ medicion_validacion ---
CREATE TABLE acervo.medicion_validacion (
    medicion_id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pieza_id                    uuid NOT NULL REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    persona_id                  uuid NOT NULL REFERENCES acervo.usuario (usuario_id),
    -- Copiado al abrir: la instrumentación no consulta `acervo.pieza` (RNF-09).
    nombre_archivo              text NOT NULL,
    abierta_el                  timestamptz NOT NULL DEFAULT now(),
    guardada_el                 timestamptz,
    duracion_s                  integer
                                    GENERATED ALWAYS AS
                                    (date_part('epoch', guardada_el - abierta_el)::integer) STORED,
    estado_final                text CHECK (estado_final IN ('Validada','Incompleta')),
    campos_modificados          text[] NOT NULL DEFAULT '{}',
    campos_faltantes            text[] NOT NULL DEFAULT '{}',
    cuerpo_editado              boolean NOT NULL DEFAULT false,
    caracteres_cuerpo_propuesta integer NOT NULL CHECK (caracteres_cuerpo_propuesta >= 0),
    caracteres_cuerpo_guardado  integer CHECK (caracteres_cuerpo_guardado >= 0),

    CONSTRAINT medicion_cierre_coherente
        CHECK ((guardada_el IS NULL) = (estado_final IS NULL)),
    CONSTRAINT medicion_orden_temporal
        CHECK (guardada_el IS NULL OR guardada_el >= abierta_el)
);

COMMENT ON TABLE acervo.medicion_validacion IS
    'Una fila por apertura de una Ficha en la pantalla partida. Reabrir genera una fila nueva: nunca se pisa la anterior.';

CREATE INDEX medicion_por_pieza ON acervo.medicion_validacion (pieza_id, abierta_el);


-- -------------------------------------------- configuracion_capacidad ---
-- Una fila por capacidad. La siembra va con el código que la usa; el
-- contenido de `parametros` lo valida `capa_modelos`, la base solo exige
-- que sea un objeto.
CREATE TABLE acervo.configuracion_capacidad (
    capacidad       text PRIMARY KEY
                        CHECK (capacidad IN ('describir_imagen','transcribir','resumir',
                                             'embeber','responder','redactar_efemeride')),
    motor           text NOT NULL,
    modelo          text,
    instruccion_id  text,
    parametros      jsonb NOT NULL CHECK (jsonb_typeof(parametros) = 'object'),
    version         integer NOT NULL DEFAULT 1,
    actualizada_por uuid REFERENCES acervo.usuario (usuario_id),
    actualizada_el  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE acervo.configuracion_capacidad IS
    'Con qué motor, modelo e instrucción corre cada capacidad. El historial de cambios es auditoria (modelo.cambio).';
COMMENT ON COLUMN acervo.configuracion_capacidad.version IS
    'Sube en cada cambio: es la version_base de la edición optimista.';


-- ---------------------------------------------------------- auditoria ---
-- Append-only: se escribe en la misma transacción que el cambio. El dominio
-- de `accion` vive en el código, no en un CHECK, para no migrar por acción.
CREATE TABLE acervo.auditoria (
    id             bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    cuando         timestamptz NOT NULL DEFAULT now(),
    actor_tipo     text NOT NULL CHECK (actor_tipo IN ('persona','cli','agente','anonimo','sistema')),
    actor_id       uuid,   -- sin FK: un ingreso rechazado no tiene usuario
    actor_etiqueta text NOT NULL,
    accion         text NOT NULL,
    objeto_tipo    text NOT NULL,
    objeto_id      text,
    antes          jsonb,
    despues        jsonb,
    motivo         text,
    ip             inet,
    solicitud_id   text
);

COMMENT ON TABLE acervo.auditoria IS
    'Accesos y acciones administrativas. hv_app solo inserta y consulta (RF-45).';

CREATE INDEX auditoria_por_objeto ON acervo.auditoria (objeto_tipo, objeto_id, cuando DESC);
CREATE INDEX auditoria_por_fecha ON acervo.auditoria (cuando DESC);
