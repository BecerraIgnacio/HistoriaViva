-- 125: la supresión efectiva (runbook R14; historia 243, RF-42). Es lo único que borra datos del
-- Acervo y no es de la aplicación: la corre `hv_migrador` con el comando `suprimir`, por pedido de
-- la institución y con los controles de C31 encendidos. El retiro (DEC-29) sigue sin borrar nada.
--
-- Borra la Pieza, todas las versiones de su Ficha, sus fragmentos, sus Carreras, sus trabajos, sus
-- mediciones, su fila de réplica y, si es un Aporte, el Aporte con sus fuentes. Lo que la citaba
-- pierde la cita, no el texto: las efemérides y los Relatos no descartados quedan con «fuente
-- retirada» (R-12, como `Efemerides.marcar_fuente_retirada`), y los informes, las Conversaciones y
-- los Aportes que la usaban como fuente pierden esa fila. El original lo borra el comando después
-- del COMMIT. El registro, sin contenido, es el evento `pieza.supresion` de `auditoria`.
--
-- Devuelve lo que el comando necesita y registra: la huella y el identificador del original, el
-- archivo de Drive si se había replicado y cuántas filas borró de cada tabla. Una Pieza con un
-- trabajo `en_curso` no se suprime: el trabajador escribiría sobre filas borradas; retirarla antes
-- cancela sus trabajos.

CREATE FUNCTION acervo.suprimir_pieza(p_pieza_id uuid) RETURNS jsonb
LANGUAGE plpgsql
SET search_path = pg_catalog, acervo
AS $$
DECLARE
    v_pieza  record;
    v_aporte uuid;
    v_drive  text;
    v_filas  jsonb := '{}';
    v_n      bigint;
BEGIN
    SELECT pieza_id, huella_original, archivo_original_id, via_ingreso
      INTO v_pieza FROM acervo.pieza WHERE pieza_id = p_pieza_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'suprimir_pieza: la Pieza % no existe', p_pieza_id
            USING ERRCODE = 'no_data_found';
    END IF;
    IF EXISTS (SELECT 1 FROM acervo.trabajo_procesamiento
               WHERE pieza_id = p_pieza_id AND estado = 'en_curso') THEN
        RAISE EXCEPTION 'suprimir_pieza: la Pieza % tiene un trabajo en curso', p_pieza_id
            USING ERRCODE = 'object_in_use';
    END IF;
    SELECT drive_file_id INTO v_drive FROM acervo.replicacion_drive WHERE pieza_id = p_pieza_id;

    UPDATE acervo.efemeride SET fuente_retirada_el = now()
     WHERE fuente_retirada_el IS NULL AND estado <> 'descartada' AND efemeride_id IN
           (SELECT efemeride_id FROM acervo.efemeride_fuente WHERE pieza_id = p_pieza_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('efemerides_marcadas', v_n);
    UPDATE acervo.relato SET fuente_retirada_el = now()
     WHERE fuente_retirada_el IS NULL AND estado <> 'descartada' AND relato_id IN
           (SELECT relato_id FROM acervo.relato_fuente WHERE pieza_id = p_pieza_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('relatos_marcados', v_n);

    DELETE FROM acervo.data_fragmento WHERE metadata_->>'pieza_id' = p_pieza_id::text;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('data_fragmento', v_n);
    DELETE FROM acervo.mensaje_cita WHERE pieza_id = p_pieza_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('mensaje_cita', v_n);
    DELETE FROM acervo.efemeride_fuente WHERE pieza_id = p_pieza_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('efemeride_fuente', v_n);
    DELETE FROM acervo.relato_fuente WHERE pieza_id = p_pieza_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('relato_fuente', v_n);
    DELETE FROM acervo.informe_pieza WHERE pieza_id = p_pieza_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('informe_pieza', v_n);
    DELETE FROM acervo.aporte_pieza_fuente WHERE pieza_id = p_pieza_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('aporte_pieza_fuente', v_n);

    SELECT aporte_id INTO v_aporte FROM acervo.aporte WHERE pieza_id = p_pieza_id;
    IF v_aporte IS NOT NULL THEN
        DELETE FROM acervo.aporte_fragmento_fuente WHERE aporte_id = v_aporte;
        DELETE FROM acervo.aporte_pieza_fuente WHERE aporte_id = v_aporte;
        DELETE FROM acervo.aporte WHERE aporte_id = v_aporte;
        v_filas := v_filas || jsonb_build_object('aporte', 1);
    END IF;

    DELETE FROM acervo.trabajo_procesamiento WHERE pieza_id = p_pieza_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('trabajo_procesamiento', v_n);
    DELETE FROM acervo.medicion_validacion WHERE pieza_id = p_pieza_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('medicion_validacion', v_n);
    DELETE FROM acervo.replicacion_drive WHERE pieza_id = p_pieza_id;
    DELETE FROM acervo.pieza_carrera WHERE pieza_id = p_pieza_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('pieza_carrera', v_n);
    DELETE FROM acervo.ficha WHERE pieza_id = p_pieza_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_filas := v_filas || jsonb_build_object('ficha', v_n);
    DELETE FROM acervo.pieza WHERE pieza_id = p_pieza_id;

    RETURN jsonb_build_object(
        'huella_original', v_pieza.huella_original,
        'archivo_original', v_pieza.archivo_original_id,
        'via_ingreso', v_pieza.via_ingreso,
        'drive_file_id', v_drive,
        'filas', v_filas
    );
END;
$$;

COMMENT ON FUNCTION acervo.suprimir_pieza(uuid) IS
    'Supresión efectiva (runbook R14, 125): solo hv_migrador. No es el retiro (DEC-29).';

-- `hv_app` no la ejecuta; tampoco podría borrar (sin DELETE, ADR-0004 control 1).
REVOKE ALL ON FUNCTION acervo.suprimir_pieza(uuid) FROM PUBLIC;
