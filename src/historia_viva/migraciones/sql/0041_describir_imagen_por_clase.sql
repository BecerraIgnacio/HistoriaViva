-- 139: `describir_imagen` pasa a la instrucción que clasifica la imagen antes de describirla y no
-- pide personas, ropa ni lugar como pauta fija (ante el cartel de Cyber.AR los inventaba).
--
-- Solo cambia la fila si todavía nombra la primera instrucción: si el Administrador técnico ya la
-- cambió, manda lo suyo. `describir_fotografia` sigue en el código: las v1 que la registraron
-- la siguen nombrando (014) y el banco de ingesta la usa como línea de base (`--instruccion`).
-- Sube la versión para que la pantalla de Modelos no pise el cambio con una versión vieja.
UPDATE acervo.configuracion_capacidad
SET instruccion_id = 'describir_imagen_por_clase', version = version + 1, actualizada_por = NULL,
    actualizada_el = now()
WHERE capacidad = 'describir_imagen' AND instruccion_id = 'describir_fotografia';
