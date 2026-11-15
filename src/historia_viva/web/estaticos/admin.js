"use strict";
const CSRF = { "X-HV": "1" };
const $ = (id) => document.getElementById(id);
const NO_SE_PUDO = "No se pudo cambiar el interruptor. Sigue como estaba.";

// Cada pestaña existe solo para quien tiene su permiso; lo decide el servidor (`/api/yo`).
// Modelos, personas, Retiradas y programación de efemérides se suman acá (DEC-118).
const PESTANAS = [
  { id: "modelos", nombre: "Modelos", permiso: "OPERAR_SISTEMA", cargar: () => cargarModelos() },
  { id: "interruptor", nombre: "Interruptor", permiso: "OPERAR_SISTEMA", cargar: cargarInterruptor },
  { id: "tokens", nombre: "Tokens", permiso: "OPERAR_SISTEMA", cargar: cargarTokens },
  { id: "efemerides", nombre: "Programación de efemérides", permiso: "OPERAR_SISTEMA", cargar: cargarEfemerides },
  { id: "personas", nombre: "Personas", permiso: "GESTIONAR_PERSONAS", cargar: cargarPersonas },
  { id: "retiradas", nombre: "Retiradas", permiso: "RETIRAR_PIEZAS", cargar: () => cargarRetiradas() },
];
let yo = null;

const NOMBRE_DEL_FRENO = {
  procesamiento_habilitado: "Procesamiento",
  api_agentes_habilitada: "API de Agentes",
};
const BOTONES = ["cambiar-procesamiento", "cambiar-api", "frenar-todo"];

function mostrarFranja(sistema) {
  $("franja-interruptor").hidden = !sistema || sistema.procesamiento_habilitado !== false;
}

async function mostrarIndice() {
  const respuesta = await fetch("/api/salud");
  const indice = respuesta.ok ? (await respuesta.json()).indice : null;
  if (!indice || !indice.incompleto) return;
  const n = indice.afectadas;
  const cuantas = n === 1
    ? "1 Pieza Validada todavía no es buscable"
    : `${n} Piezas Validadas todavía no son buscables`;
  $("franja-indice").textContent = `Índice incompleto: ${cuantas} (${indice.pendientes} en cola,`
    + ` ${indice.fallidas} con la indexación fallida, ${indice.de_otro_modelo} fragmentos de`
    + " otro modelo). La búsqueda responde con lo ya indexado.";
  $("franja-indice").hidden = false;
}

function mostrarError(texto) {
  $("error-interruptor").textContent = texto;
  $("error-interruptor").hidden = !texto;
}

function elegir(pestana) {
  for (const otra of PESTANAS) {
    const boton = $(`pestana-${otra.id}`);
    if (boton) boton.setAttribute("aria-selected", String(otra === pestana));
    const panel = $(`panel-${otra.id}`);
    if (panel) panel.hidden = otra !== pestana;
  }
  pestana.cargar();
}

const hora = (iso) => new Date(iso).toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit", hour12: false });

function pintarFreno(prefijo, clave, panel) {
  const habilitado = panel[clave];
  const ultimo = panel.historial.find((evento) => evento.clave === clave);
  const estado = $(`estado-${prefijo}`);
  estado.textContent = (habilitado ? "Funcionando" : "Detenido")
    + (ultimo ? ` desde las ${hora(ultimo.en)} por ${ultimo.persona}` : "");
  estado.className = `estado ${habilitado ? "estado-andando" : "estado-detenido"}`;
  $(`cambiar-${prefijo}`).textContent = habilitado ? "Detener" : "Reanudar";
}

function pintar(panel) {
  mostrarFranja({ procesamiento_habilitado: panel.procesamiento_habilitado });
  pintarFreno("procesamiento", "procesamiento_habilitado", panel);
  pintarFreno("api", "api_agentes_habilitada", panel);
  $("historial-vacio").hidden = panel.historial.length > 0;
  $("historial").replaceChildren(...panel.historial.map((evento) => {
    const fila = document.createElement("tr");
    const celdas = [
      new Date(evento.en).toLocaleString("es-AR"),
      NOMBRE_DEL_FRENO[evento.clave] || evento.clave,
      evento.habilitado ? "Funcionando" : "Detenido",
      evento.persona,
      evento.motivo,
    ];
    for (const [indice, texto] of celdas.entries()) {
      const celda = document.createElement("td");
      celda.textContent = texto;
      if (indice === 4) celda.className = "motivo";
      fila.append(celda);
    }
    return fila;
  }));
}

async function leerPanel() {
  const respuesta = await fetch("/api/admin/interruptor");
  if (!respuesta.ok) throw new Error(String(respuesta.status));
  return respuesta.json();
}

async function cargarInterruptor() {
  $("cargando-interruptor").hidden = false;
  try {
    pintar(await leerPanel());
    mostrarError("");
  } catch {
    mostrarError("No se pudo leer el estado del interruptor. Recargá la página para probar de nuevo.");
  } finally {
    $("cargando-interruptor").hidden = true;
    // Con el estado leído o sin él, los botones tienen texto; sin estado no hay a qué cambiar.
    BOTONES.forEach((id) => { $(id).disabled = !$("estado-procesamiento").textContent; });
  }
}

// `cambio(vigente)` arma el pedido sobre el estado leído justo antes: si otro freno cambió
// por comando con la página abierta, no se lo pisa (DEC-136).
async function enviar(cambio, foco) {
  mostrarError("");
  BOTONES.forEach((id) => { $(id).disabled = true; });
  try {
    const vigente = await leerPanel();
    const pedido = {
      procesamiento_habilitado: vigente.procesamiento_habilitado,
      api_agentes_habilitada: vigente.api_agentes_habilitada,
      ...cambio(vigente),
    };
    const detiene = (pedido.procesamiento_habilitado === false && vigente.procesamiento_habilitado)
      || (pedido.api_agentes_habilitada === false && vigente.api_agentes_habilitada);
    if (detiene) pedido.motivo = $("motivo").value;
    const respuesta = await fetch("/api/admin/interruptor", {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify(pedido),
    });
    const cuerpo = await respuesta.json();
    if (!respuesta.ok) {
      if (cuerpo.campo === "motivo") {
        mostrarError(cuerpo.detail);
        foco = "motivo";
      } else {
        mostrarError(NO_SE_PUDO);
      }
      return;
    }
    if (detiene) $("motivo").value = "";
    pintar(cuerpo);
  } catch {
    mostrarError(NO_SE_PUDO);
  } finally {
    BOTONES.forEach((id) => { $(id).disabled = false; });
    $(foco).focus();
  }
}

$("cambiar-procesamiento").addEventListener("click", () => enviar(
  (v) => ({ procesamiento_habilitado: !v.procesamiento_habilitado }), "cambiar-procesamiento"
));
$("cambiar-api").addEventListener("click", () => enviar(
  (v) => ({ api_agentes_habilitada: !v.api_agentes_habilitada }), "cambiar-api"
));
$("frenar-todo").addEventListener("click", () => {
  $("confirmar-frenar-todo").returnValue = "";
  $("confirmar-frenar-todo").showModal();
  $("cancelar-frenar-todo").focus();
});
$("confirmar-frenar-todo").addEventListener("close", () => {
  if ($("confirmar-frenar-todo").returnValue === "frenar") {
    enviar(() => ({ procesamiento_habilitado: false, api_agentes_habilitada: false }), "frenar-todo");
  } else {
    $("frenar-todo").focus();
  }
});

// ── P7c Tokens ──────────────────────────────────────────────────────────────────────────
// El token en claro solo vive en el campo del modal: no va a la URL, a variables globales ni
// a la consola, y al cerrar se borra del campo.
const fecha = (iso) => iso ? new Date(iso).toLocaleString("es-AR", { dateStyle: "medium", timeStyle: "short" }) : "—";

function errorTokens(texto) {
  $("error-tokens").textContent = texto;
  $("error-tokens").hidden = !texto;
}

function errorEmitir(texto) {
  $("error-emitir").textContent = texto;
  $("error-emitir").hidden = !texto;
}

function textoDeEstado(token) {
  if (token.estado === "vencido") return "Vencido";
  if (token.estado === "revocado") return "Revocado";
  return token.revocado_el ? `Activo hasta ${fecha(token.revocado_el)} (rotado)` : "Activo";
}

function celda(texto, clase) {
  const td = document.createElement("td");
  td.textContent = texto;
  if (clase) td.className = clase;
  return td;
}

function boton(texto, alHacerClic) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = texto;
  b.addEventListener("click", () => alHacerClic(b));
  return b;
}

function pintarTokens(agentes) {
  const filas = [];
  for (const agente of agentes) {
    agente.tokens.forEach((token, indice) => {
      const fila = document.createElement("tr");
      if (indice === 0) {
        const nombre = document.createElement("td");
        nombre.rowSpan = agente.tokens.length;
        const alcances = document.createElement("div");
        alcances.className = "alcances";
        alcances.textContent = agente.alcances.join(", ");
        nombre.append(agente.nombre, alcances);
        fila.append(nombre);
      }
      const prefijo = document.createElement("td");
      const codigo = document.createElement("code");
      codigo.textContent = `${token.prefijo}_…`;
      prefijo.append(codigo);
      fila.append(prefijo, celda(fecha(token.vence_el)),
        celda(token.ultimo_uso_el ? fecha(token.ultimo_uso_el) : "Nunca"),
        celda(textoDeEstado(token), `estado-${token.estado}`));
      const acciones = document.createElement("div");
      acciones.className = "acciones";
      if (indice === 0) acciones.append(boton("Rotar…", (b) => pedirRotar(agente, b)));
      if (token.estado === "activo") acciones.append(boton("Revocar…", (b) => pedirRevocar(agente, token, b)));
      const accion = document.createElement("td");
      accion.append(acciones);
      fila.append(accion);
      filas.push(fila);
    });
  }
  $("tokens").replaceChildren(...filas);
  $("tabla-tokens").hidden = filas.length === 0;
  $("tokens-vacio").hidden = filas.length > 0;
}

async function cargarTokens() {
  $("cargando-tokens").hidden = false;
  try {
    const respuesta = await fetch("/api/admin/agentes");
    if (!respuesta.ok) throw new Error(String(respuesta.status));
    pintarTokens((await respuesta.json()).agentes);
    errorTokens("");
  } catch {
    errorTokens("No se pudieron leer los tokens. Recargá la página para probar de nuevo.");
  } finally {
    $("cargando-tokens").hidden = true;
  }
}

async function postear(ruta, cuerpo) {
  const respuesta = await fetch(ruta, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...CSRF },
    body: JSON.stringify(cuerpo || {}),
  });
  return { respuesta, cuerpo: await respuesta.json() };
}

let focoAlCerrarToken = null;

function mostrarToken(emitido, foco) {
  focoAlCerrarToken = foco;
  $("titulo-token").textContent = `Token de «${emitido.nombre}»`;
  $("token-en-claro").value = emitido.token;
  $("aviso-copia").textContent = "";
  $("ya-lo-guarde").checked = false;
  $("cerrar-token").disabled = true;
  $("token-emitido").showModal();
  $("token-en-claro").focus();
  $("token-en-claro").select();
}

$("ya-lo-guarde").addEventListener("change", () => { $("cerrar-token").disabled = !$("ya-lo-guarde").checked; });
// Escape no lo cierra mientras «Ya lo guardé» no esté marcado.
$("token-emitido").addEventListener("cancel", (evento) => { if (!$("ya-lo-guarde").checked) evento.preventDefault(); });
$("cerrar-token").addEventListener("click", () => $("token-emitido").close());
$("token-emitido").addEventListener("close", () => {
  // Chrome deja cerrar con Escape aunque se cancele el evento si no hubo un gesto del usuario
  // desde que se abrió: sin «Ya lo guardé», el modal vuelve a abrirse con el token.
  if (!$("ya-lo-guarde").checked) {
    $("token-emitido").showModal();
    return;
  }
  $("token-en-claro").value = "";
  $("aviso-copia").textContent = "";
  if (focoAlCerrarToken && document.contains(focoAlCerrarToken)) focoAlCerrarToken.focus();
});
$("copiar-token").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("token-en-claro").value);
    $("aviso-copia").textContent = "Copiado";
  } catch {
    $("token-en-claro").focus();
    $("token-en-claro").select();
    $("aviso-copia").textContent = "No se pudo copiar solo. Copialo con Ctrl+C.";
  }
});

$("emitir-token").addEventListener("submit", async (evento) => {
  evento.preventDefault();
  errorEmitir("");
  const alcances = [...document.querySelectorAll('input[name="alcance"]:checked')].map((c) => c.value);
  if (!alcances.length) {
    errorEmitir("Elegí al menos un alcance.");
    document.querySelector('input[name="alcance"]').focus();
    return;
  }
  $("boton-emitir").disabled = true;
  try {
    const { respuesta, cuerpo } = await postear("/api/admin/agentes", { nombre: $("nombre-agente").value.trim(), alcances });
    if (!respuesta.ok) {
      errorEmitir(cuerpo.campo === "nombre" ? cuerpo.detail : "No se pudo emitir el token.");
      if (cuerpo.campo === "nombre") $("nombre-agente").focus();
      return;
    }
    $("emitir-token").reset();
    await cargarTokens();
    mostrarToken(cuerpo, $("nombre-agente"));
  } catch {
    errorEmitir("No se pudo emitir el token.");
  } finally {
    $("boton-emitir").disabled = false;
  }
});

// La confirmación pendiente: qué hacer si el diálogo se cierra con «revocar» o «rotar».
let pendiente = null;

function pedirRevocar(agente, token, foco) {
  $("titulo-revocar").textContent = `¿Revocar el token ${token.prefijo}?`;
  $("efecto-revocar").textContent = `«${agente.nombre}» pierde el acceso con este token ya. Los demás no se afectan.`;
  pendiente = { agente, token, foco };
  $("confirmar-revocar").returnValue = "";
  $("confirmar-revocar").showModal();
  $("cancelar-revocar").focus();
}

$("confirmar-revocar").addEventListener("close", async () => {
  const { agente, token, foco } = pendiente;
  if ($("confirmar-revocar").returnValue !== "revocar") { foco.focus(); return; }
  errorTokens("");
  try {
    const { respuesta } = await postear(`/api/admin/agentes/${agente.agente_id}/tokens/${token.token_id}/revocacion`);
    // 409: ya estaba revocado (por comando u otra pestaña); la lista recargada lo muestra.
    if (!respuesta.ok && respuesta.status !== 409) errorTokens("No se pudo revocar el token. Sigue como estaba.");
  } catch {
    errorTokens("No se pudo revocar el token. Sigue como estaba.");
  }
  await cargarTokens();
  $("pestana-tokens").focus();
});

// `datetime-local` habla en la hora de la PC; al servidor va en UTC.
const local = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

function pedirRotar(agente, foco) {
  $("titulo-rotar").textContent = `¿Rotar el token de «${agente.nombre}»?`;
  $("efecto-rotar").textContent = `Se emite un token nuevo para «${agente.nombre}». Los actuales siguen valiendo hasta la fecha que elijas, para que el Agente cambie al nuevo sin cortes; después quedan revocados. Los demás Agentes no se afectan.`;
  const ahora = new Date();
  // El minuto siguiente: el minuto en curso ya puede haber pasado al llegar al servidor.
  $("vencen-anteriores").min = local(new Date(ahora.getTime() + 60000));
  $("vencen-anteriores").max = local(new Date(ahora.getTime() + 7 * 864e5));
  $("vencen-anteriores").value = local(new Date(ahora.getTime() + 864e5));
  pendiente = { agente, foco };
  $("confirmar-rotar").returnValue = "";
  $("confirmar-rotar").showModal();
  $("cancelar-rotar").focus();
}

$("confirmar-rotar").addEventListener("close", async () => {
  const { agente, foco } = pendiente;
  if ($("confirmar-rotar").returnValue !== "rotar") { foco.focus(); return; }
  errorTokens("");
  try {
    const vencen = new Date($("vencen-anteriores").value).toISOString();
    const { respuesta, cuerpo } = await postear(`/api/admin/agentes/${agente.agente_id}/tokens`, { vencen_anteriores_el: vencen });
    if (!respuesta.ok) {
      errorTokens(cuerpo.campo === "vencen_anteriores_el" ? cuerpo.detail : "No se pudo rotar el token. Sigue como estaba.");
      foco.focus();
      return;
    }
    await cargarTokens();
    mostrarToken(cuerpo, $("pestana-tokens"));
  } catch {
    errorTokens("No se pudo rotar el token. Sigue como estaba.");
    foco.focus();
  }
});

// ── P7b Personas ────────────────────────────────────────────────────────────────────────
// Las reglas las decide el servidor (`identidad.Padron`); la pantalla solo no ofrece lo que
// va a rechazar: el rol `admin_tecnico` lo otorga o lo quita solo un Administrador técnico
// (R-02), y nadie se cambia el rol ni se da de baja a sí mismo.
const ROLES = {
  rgc: { nombre: "RGC", puede: "Carga Piezas y crea Lotes, valida Fichas, da de alta a las personas (menos al Administrador técnico), retira y restaura Piezas, revisa efemérides y relatos y aprueba el Informe de acreditación." },
  coordinador: { nombre: "Coordinador de Extensión", puede: "Carga y valida como el RGC, revisa efemérides y relatos, genera el Informe de acreditación y lo aprueba junto con el RGC." },
  director_carrera: { nombre: "Director de Carrera", puede: "Conversa con Historia Viva, abre los originales y la Ficha en solo lectura y lee lo ya aprobado. No carga ni valida." },
  admin_tecnico: { nombre: "Administrador técnico", puede: "Opera el sistema: modelos, interruptor, tokens de Agentes y comandos. No firma Fichas." },
};
const otorgables = () => Object.keys(ROLES).filter((rol) => rol !== "admin_tecnico" || yo.rol === "admin_tecnico");
const tocable = (persona) => persona.usuario_id !== yo.usuario_id && otorgables().includes(persona.rol);

function mensajeDePersonas(cuerpo) {
  switch (cuerpo && cuerpo.type) {
    case "/errores/persona-existente": return "Ya hay una persona con ese email.";
    case "/errores/dominio-no-permitido": return "Ese email no es del dominio de la FIE.";
    case "/errores/ultimo-titular-de-permiso": return `No se puede dejar sin titular a este permiso: es la última persona activa con ${cuerpo.permiso}.`;
    case "/errores/permiso-insuficiente": return "Tu rol no puede otorgar ni quitar ese rol.";
    case "/errores/recurso-no-encontrado": return "Esa persona ya no existe. Recargá la página.";
    default: return (cuerpo && cuerpo.campo && cuerpo.detail) || "No se pudo guardar el cambio. Sigue como estaba.";
  }
}

function errorPersonas(id, texto) {
  $(id).textContent = texto;
  $(id).hidden = !texto;
}

function estadoDePersona(persona) {
  if (!persona.activo) return "De baja";
  return persona.ultimo_acceso_el ? "Activa" : "Habilitada, todavía no ingresó";
}

async function cambiarPersona(persona, cambio, foco) {
  errorPersonas("error-personas", "");
  try {
    const respuesta = await fetch(`/api/admin/personas/${persona.usuario_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify(cambio),
    });
    if (!respuesta.ok) errorPersonas("error-personas", mensajeDePersonas(await respuesta.json()));
  } catch {
    errorPersonas("error-personas", "No se pudo guardar el cambio. Sigue como estaba.");
  }
  await cargarPersonas();
  if (foco && document.contains(foco)) foco.focus(); else $("pestana-personas").focus();
}

function pintarPersonas(personas) {
  $("franja-interinato").hidden = personas.some((p) => p.activo && p.rol === "rgc");
  $("personas").replaceChildren(...personas.map((persona) => {
    const fila = document.createElement("tr");
    const acciones = document.createElement("div");
    acciones.className = "acciones";
    const rol = celda(ROLES[persona.rol].nombre);
    if (tocable(persona) && persona.activo) {
      const selector = document.createElement("select");
      selector.setAttribute("aria-label", `Rol de ${persona.nombre}`);
      for (const clave of otorgables()) selector.append(new Option(ROLES[clave].nombre, clave, false, clave === persona.rol));
      rol.replaceChildren(selector);
      acciones.append(boton("Cambiar rol", (b) => {
        if (selector.value !== persona.rol) cambiarPersona(persona, { rol: selector.value }, b);
      }));
    }
    if (tocable(persona)) {
      acciones.append(persona.activo
        ? boton("Dar de baja…", (b) => pedirBaja(persona, b))
        : boton("Reactivar", (b) => cambiarPersona(persona, { activo: true }, b)));
    }
    const accion = document.createElement("td");
    accion.append(acciones.childElementCount ? acciones : "—");
    fila.append(celda(persona.nombre), celda(persona.email), rol, celda(estadoDePersona(persona)), accion);
    return fila;
  }));
  $("tabla-personas").hidden = personas.length === 0;
}

async function cargarPersonas() {
  $("cargando-personas").hidden = false;
  try {
    const respuesta = await fetch("/api/admin/personas");
    // Dar de alta al primer RGC le quita la pestaña al técnico interino: se rearma el menú.
    if (respuesta.status === 403 || respuesta.status === 401) { window.location.reload(); return; }
    if (!respuesta.ok) throw new Error(String(respuesta.status));
    pintarPersonas((await respuesta.json()).personas);
    errorPersonas("error-personas", "");
  } catch {
    errorPersonas("error-personas", "No se pudieron leer las personas. Recargá la página para probar de nuevo.");
  } finally {
    $("cargando-personas").hidden = true;
  }
  pintarRolesDeAlta();
}

function pintarRolesDeAlta() {
  const elegido = document.querySelector('input[name="rol-alta"]:checked');
  $("roles-alta").replaceChildren($("roles-alta").firstElementChild, ...otorgables().map((clave, indice) => {
    const etiqueta = document.createElement("label");
    const opcion = document.createElement("input");
    opcion.type = "radio";
    opcion.name = "rol-alta";
    opcion.value = clave;
    opcion.required = true;
    opcion.checked = elegido ? elegido.value === clave : indice === 0;
    opcion.addEventListener("change", mostrarQuePuede);
    etiqueta.append(opcion, ` ${ROLES[clave].nombre}`);
    return etiqueta;
  }));
  mostrarQuePuede();
}

function mostrarQuePuede() {
  const elegido = document.querySelector('input[name="rol-alta"]:checked');
  $("que-puede").textContent = elegido ? ROLES[elegido.value].puede : "";
}

$("alta-persona").addEventListener("submit", async (evento) => {
  evento.preventDefault();
  errorPersonas("error-alta", "");
  $("boton-alta").disabled = true;
  try {
    const respuesta = await fetch("/api/admin/personas", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify({
        email: $("email-persona").value.trim(),
        nombre: $("nombre-persona").value.trim(),
        rol: document.querySelector('input[name="rol-alta"]:checked').value,
      }),
    });
    if (!respuesta.ok) {
      const cuerpo = await respuesta.json();
      errorPersonas("error-alta", mensajeDePersonas(cuerpo));
      $(cuerpo.campo === "nombre" ? "nombre-persona" : "email-persona").focus();
      return;
    }
    $("alta-persona").reset();
    await cargarPersonas();
    $("email-persona").focus();
  } catch {
    errorPersonas("error-alta", "No se pudo dar de alta. Probá de nuevo.");
  } finally {
    $("boton-alta").disabled = false;
  }
});

function pedirBaja(persona, foco) {
  $("titulo-baja").textContent = `¿Dar de baja a ${persona.nombre}?`;
  pendiente = { persona, foco };
  $("confirmar-baja").returnValue = "";
  $("confirmar-baja").showModal();
  $("cancelar-baja").focus();
}

$("confirmar-baja").addEventListener("close", () => {
  const { persona, foco } = pendiente;
  if ($("confirmar-baja").returnValue !== "baja") { foco.focus(); return; }
  cambiarPersona(persona, { activo: false }, null);
});

// ── P7e Retiradas ───────────────────────────────────────────────────────────────────────
// Restaurar lo decide `retiro` (DEC-138); la fila trae el conflicto que hoy lo impediría y la
// pantalla apaga el botón con el mismo texto que daría el 409 (recorrido R24).
const CONFLICTOS = {
  "pieza-duplicada": "No se puede restaurar: ya hay otra Pieza con el mismo archivo.",
  "lote-lleno": "No se puede restaurar: el Lote de esta Pieza ya tiene 100 Piezas.",
};
const NO_SE_PUDO_RESTAURAR = "No se pudo restaurar. La Pieza sigue retirada.";
let siguienteRetiradas = null;

function enlace(texto, { lote, pieza }) {
  const a = document.createElement("a");
  a.className = "enlace";
  // El href se arma acá, con el id escapado (test_seg_salida_modelo_texto).
  a.href = lote ? `/lote.html?lote=${encodeURIComponent(lote)}` : `/validar.html?pieza=${encodeURIComponent(pieza)}`;
  a.textContent = texto;
  return a;
}

const verPieza = (piezaId) => enlace("Ver la Pieza", { pieza: piezaId });

function avisarRestauracion(clase, ...partes) {
  $("aviso-restauracion").className = clase;
  $("aviso-restauracion").replaceChildren(...partes);
  $("aviso-restauracion").hidden = partes.length === 0;
}

function filaDeRetirada(retirada) {
  const fila = document.createElement("tr");
  const accion = document.createElement("td");
  const restaurar = boton("Restaurar", (b) => pedirRestaurar(retirada, b));
  const conflicto = CONFLICTOS[retirada.conflicto_de_restauracion];
  if (conflicto) {
    restaurar.disabled = true;
    const porque = document.createElement("p");
    porque.className = "aviso";
    porque.id = `conflicto-${retirada.pieza_id}`;
    porque.textContent = conflicto;
    restaurar.setAttribute("aria-describedby", porque.id);
    accion.append(restaurar, porque);
  } else {
    accion.append(restaurar);
  }
  fila.append(celda(retirada.nombre_archivo), celda(retirada.estado), celda(retirada.retirada_por),
    celda(fecha(retirada.retirada_el)), celda(`“${retirada.motivo}”`, "motivo"), accion);
  return fila;
}

// Sin `cursor` relee desde el principio; con él, agrega la página siguiente («Ver más»).
async function cargarRetiradas(cursor) {
  $("franja-interinato-retiradas").hidden = yo.rol === "rgc";
  $("cargando-retiradas").hidden = false;
  $("error-retiradas").hidden = true;
  try {
    const parametros = new URLSearchParams(cursor ? { cursor } : {});
    const respuesta = await fetch(`/api/admin/retiradas?${parametros}`);
    // Un técnico que dejó de ser interino pierde la pestaña: se rearma el menú.
    if (respuesta.status === 403 || respuesta.status === 401) { window.location.reload(); return; }
    if (!respuesta.ok) throw new Error(String(respuesta.status));
    const pagina = await respuesta.json();
    const filas = pagina.retiradas.map(filaDeRetirada);
    if (cursor) $("retiradas").append(...filas); else $("retiradas").replaceChildren(...filas);
    siguienteRetiradas = pagina.siguiente;
    const hay = $("retiradas").childElementCount > 0;
    $("tabla-retiradas").hidden = !hay;
    $("retiradas-vacio").hidden = hay;
    $("mas-retiradas").hidden = !siguienteRetiradas;
  } catch {
    $("error-retiradas").hidden = false;
  } finally {
    $("cargando-retiradas").hidden = true;
  }
}

$("reintentar-retiradas").addEventListener("click", () => cargarRetiradas());
$("mas-retiradas").addEventListener("click", () => cargarRetiradas(siguienteRetiradas));

function pedirRestaurar(retirada, foco) {
  $("titulo-restaurar").textContent = `¿Restaurar “${retirada.nombre_archivo}”?`;
  $("motivo-restaurar").value = "";
  $("boton-restaurar").disabled = true;
  pendiente = { retirada, foco };
  $("confirmar-restaurar").returnValue = "";
  $("confirmar-restaurar").showModal();
  $("cancelar-restaurar").focus();
}

$("motivo-restaurar").addEventListener("input", () => {
  $("boton-restaurar").disabled = $("motivo-restaurar").value.trim().length < 5;
});

function textoDeRestaurada(cuerpo) {
  const partes = ["Restaurada."];
  if (cuerpo.procesamiento_encolado) {
    partes.push(yo.sistema && yo.sistema.procesamiento_habilitado === false
      ? "Vuelve Recibida: esperando al motor (procesamiento detenido)."
      : "Vuelve Recibida y se procesa de nuevo.");
  }
  if (cuerpo.indexacion_encolada) partes.push("Vuelve Validada y figura «No indexada» hasta que se reindexe.");
  return partes.join(" ");
}

$("confirmar-restaurar").addEventListener("close", async () => {
  const { retirada, foco } = pendiente;
  if ($("confirmar-restaurar").returnValue !== "restaurar") { foco.focus(); return; }
  avisarRestauracion("", document.createTextNode("Restaurando…"));
  try {
    const { respuesta, cuerpo } = await postear(`/api/admin/piezas/${retirada.pieza_id}/restauracion`,
      { motivo: $("motivo-restaurar").value.trim() });
    if (respuesta.status === 403) { window.location.reload(); return; }
    if (respuesta.ok) {
      const partes = [document.createTextNode(textoDeRestaurada(cuerpo))];
      if (retirada.lote_id) {
        partes.push(document.createTextNode(` Está en el Lote “${retirada.lote_nombre}”. `),
          enlace("Ver el Lote", { lote: retirada.lote_id }));
      }
      avisarRestauracion("aviso", ...partes);
    } else if (cuerpo.type === "/errores/pieza-duplicada") {
      avisarRestauracion("error", document.createTextNode(`${CONFLICTOS["pieza-duplicada"]} `), verPieza(cuerpo.pieza_id));
    } else if (cuerpo.type === "/errores/lote-lleno") {
      avisarRestauracion("error", document.createTextNode(CONFLICTOS["lote-lleno"]));
    } else if (cuerpo.type === "/errores/transicion-invalida" || respuesta.status === 404) {
      avisarRestauracion("error", document.createTextNode("Esa Pieza ya no está retirada."));
    } else {
      avisarRestauracion("error", document.createTextNode((cuerpo.campo === "motivo" && cuerpo.detail) || NO_SE_PUDO_RESTAURAR));
    }
  } catch {
    avisarRestauracion("error", document.createTextNode(NO_SE_PUDO_RESTAURAR));
  }
  // La fila restaurada sale de la lista; las demás pueden haber cambiado de conflicto.
  await cargarRetiradas();
  $("pestana-retiradas").focus();
});

// --- P7f Programación de efemérides: la programación (060) y «Generar ahora» (059) --------------

const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];
const EFEMERIDES = "/efemerides.html";  // P8, la lista de revisión (061)
const SONDEO_MS = 3000;
// El pedido que esta pantalla hizo y espera: su `pedido_manual_el` (en ms) y qué pidió.
let esperando = null;
let sondeo = null;

function nombreDeObjetivo(objetivo) {
  if (!objetivo) return "la ventana de la programación";
  const [, mes, dia] = objetivo.fecha.split("-").map(Number);
  return objetivo.tipo === "mes" ? MESES[mes - 1] : `${dia} de ${MESES[mes - 1]}`;
}

const dosCifras = (n) => String(n).padStart(2, "0");

// La próxima vez que llega ese día (o ese mes) desde hoy: la efeméride se difunde en esa fecha.
function proximaFecha(tipo, dia, mes) {
  const hoy = new Date();
  hoy.setHours(0, 0, 0, 0);
  if (tipo === "mes") {
    const anio = mes - 1 >= hoy.getMonth() ? hoy.getFullYear() : hoy.getFullYear() + 1;
    return `${anio}-${dosCifras(mes)}-01`;
  }
  for (let anio = hoy.getFullYear(); anio <= hoy.getFullYear() + 8; anio++) {
    const fecha = new Date(anio, mes - 1, dia);
    if (fecha.getMonth() === mes - 1 && fecha.getDate() === dia && fecha >= hoy) {
      return `${anio}-${dosCifras(mes)}-${dosCifras(dia)}`;
    }
  }
  return null;  // ese día no existe en ese mes
}

function errorGenerar(texto) {
  $("error-generar").textContent = texto;
  $("error-generar").hidden = !texto;
}

function enlaceAEfemerides() {
  const enlace = document.createElement("a");
  enlace.className = "enlace";
  enlace.href = EFEMERIDES;
  enlace.textContent = "Ver en Efemérides";
  return enlace;
}

function mostrarResultado(texto, conEnlace) {
  const resultado = $("resultado-generar");
  resultado.replaceChildren(texto, ...(conEnlace ? [" ", enlaceAEfemerides()] : []));
  resultado.hidden = false;
}

function cuantas(n) {
  return n === 1 ? "1 efeméride Propuesta" : `${n} efemérides Propuestas`;
}

function textoDelResultado(corrida, objetivo) {
  if (corrida.generadas > 0) {
    return corrida.generadas === 1 ? "Se generó 1 efeméride." : `Se generaron ${corrida.generadas} efemérides.`;
  }
  const deque = objetivo && objetivo.tipo === "mes" ? "ese mes" : "ese día";
  const motivo = corrida.omitidas.length ? corrida.omitidas[0].motivo : "";
  if (motivo.startsWith("no hay Fichas")) {
    return `No hay Fichas Validadas con Fecha en ${deque}. No se generó nada.`;
  }
  if (motivo.includes("ya tiene")) return `Ya existe una efeméride de ${deque}. No se generó nada.`;
  return motivo ? `No se generó nada: ${motivo}` : "No se generó nada.";
}

const CORTADA_POR = { freno: "se cortó: procesamiento detenido", motor: "se cortó: el motor no respondió" };

function resumenDeCorrida(corrida) {
  if (!corrida) return "";
  const partes = [corrida.programada ? "programada" : "a mano", cuantas(corrida.generadas)];
  const n = corrida.omitidas.length;
  if (n) partes.push(n === 1 ? "1 omitida" : `${n} omitidas`);
  if (corrida.cortada_por) partes.push(CORTADA_POR[corrida.cortada_por]);
  return ` · ${partes.join(" · ")}`;
}

// --- P7f: la programación (060) ---

let leida = null;  // la programación como la devolvió el servidor: lo que «Cancelar» restaura

function valoresDelFormulario() {
  return {
    habilitada: document.querySelector('input[name="habilitada"]:checked')?.value === "si",
    periodicidad: $("periodicidad").value,
    anticipacion_dias: Number($("anticipacion").value),
    maximo_por_corrida: Number($("maximo").value),
  };
}

const enRango = (valor, min, max) => Number.isInteger(valor) && valor >= min && valor <= max;

function editada() {
  if (!leida) return false;
  const valores = valoresDelFormulario();
  return Object.keys(valores).some((clave) => valores[clave] !== leida[clave]);
}

function revisarProgramacion() {
  const { anticipacion_dias, maximo_por_corrida } = valoresDelFormulario();
  const anticipacionOk = $("anticipacion").value !== "" && enRango(anticipacion_dias, 0, 365);
  const maximoOk = $("maximo").value !== "" && enRango(maximo_por_corrida, 1, 500);
  $("error-anticipacion").hidden = anticipacionOk;
  $("error-maximo").hidden = maximoOk;
  $("anticipacion").setAttribute("aria-invalid", String(!anticipacionOk));
  $("maximo").setAttribute("aria-invalid", String(!maximoOk));
  const cambio = editada();
  $("guardar-programacion").disabled = !(cambio && anticipacionOk && maximoOk);
  $("cancelar-programacion").disabled = !cambio;
}

function llenarProgramacion(programacion) {
  document.querySelector(`input[name="habilitada"][value="${programacion.habilitada ? "si" : "no"}"]`).checked = true;
  $("periodicidad").value = programacion.periodicidad;
  $("anticipacion").value = String(programacion.anticipacion_dias);
  $("maximo").value = String(programacion.maximo_por_corrida);
}

function pintarProgramacion(programacion) {
  // El sondeo de «Generar ahora» no pisa lo que la persona está editando, ni la versión que
  // vio: si otra persona guarda en el medio, el PUT tiene que dar 409.
  if (!editada()) {
    leida = programacion;
    llenarProgramacion(programacion);
  }
  const proxima = programacion.proxima_corrida_el;
  if (!programacion.habilitada) {
    $("proxima-corrida").textContent = "La generación automática está desactivada: solo se genera con «Generar ahora».";
  } else if (Date.parse(proxima) <= Date.now()) {
    $("proxima-corrida").textContent = "Próxima corrida: en la próxima vuelta del planificador.";
  } else {
    $("proxima-corrida").textContent = `Próxima corrida: ${fecha(proxima)}.`;
  }
  revisarProgramacion();
}

for (const id of ["periodicidad", "anticipacion", "maximo"]) {
  $(id).addEventListener("input", revisarProgramacion);
}
document.querySelectorAll('input[name="habilitada"]').forEach((radio) => {
  radio.addEventListener("change", revisarProgramacion);
});

$("cancelar-programacion").addEventListener("click", () => {
  llenarProgramacion(leida);
  revisarProgramacion();
});

$("programacion").addEventListener("submit", async (evento) => {
  evento.preventDefault();
  $("error-programacion").hidden = true;
  $("programacion-guardada").hidden = true;
  if ($("guardar-programacion").disabled) return;
  $("guardar-programacion").disabled = true;
  let mensaje = "No se pudo guardar. La programación sigue como estaba.";
  try {
    const respuesta = await fetch("/api/admin/efemerides/programacion", {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify({ version_base: leida.version, ...valoresDelFormulario() }),
    });
    const cuerpo = await respuesta.json();
    if (respuesta.ok) {
      leida = cuerpo;
      pintarProgramacion(cuerpo);
      $("programacion-guardada").textContent = "Programación guardada. Las corridas en curso conservan la configuración anterior.";
      $("programacion-guardada").hidden = false;
      return;
    }
    if (cuerpo.type === "/errores/version-desactualizada") {
      mensaje = "Otra persona cambió la programación mientras la editabas. La programación sigue como"
        + " la dejó; revisala y volvé a guardar.";
      leida = cuerpo.fila_actual;
    }
  } catch {
    // sin conexión: el mensaje genérico
  }
  $("error-programacion").textContent = mensaje;
  $("error-programacion").hidden = false;
  llenarProgramacion(leida);
  pintarProgramacion(leida);
});

function pintarEfemerides(programacion) {
  $("efemerides-frenado").hidden = programacion.procesamiento_habilitado;
  $("efemerides-sin-modelo").hidden = !programacion.falta_modelo;
  $("boton-generar").disabled = programacion.falta_modelo;
  const pendiente = programacion.pedido_manual_el;
  $("efemerides-sin-planificador").hidden = programacion.planificador_vivo || !pendiente;
  const corrida = programacion.ultima_corrida;
  const mio = (iso) => Boolean(iso && esperando && Date.parse(iso) === esperando.marcado);
  // Mientras el pedido sigue marcado, se espera (también frenado: se atiende al reanudar).
  if (esperando && !mio(pendiente)) {
    if (corrida && mio(corrida.pedido_manual_el) && corrida.pedido_limpiado) {
      mostrarResultado(textoDelResultado(corrida, esperando.objetivo), corrida.generadas > 0);
    } else {
      mostrarResultado("Otro pedido reemplazó al tuyo: no se generó lo que pediste.", false);
    }
    esperando = null;
  }
  const otro = pendiente && !esperando;
  $("pedido-pendiente").hidden = !otro;
  if (otro) {
    $("pedido-pendiente").textContent = `Pedido sin atender: efemérides de`
      + ` ${nombreDeObjetivo(programacion.pedido_manual_objetivo)}, desde las ${hora(pendiente)}.`;
  }
  $("ultima-corrida").hidden = !programacion.ultima_corrida_el;
  if (programacion.ultima_corrida_el) {
    $("ultima-corrida").replaceChildren(
      `Última corrida: ${fecha(programacion.ultima_corrida_el)}${resumenDeCorrida(corrida)} · `,
      enlaceAEfemerides(),
    );
  }
  pintarProgramacion(programacion);
  if (!esperando && sondeo) {
    clearInterval(sondeo);
    sondeo = null;
  }
}

async function cargarEfemerides() {
  try {
    const respuesta = await fetch("/api/admin/efemerides/programacion");
    if (!respuesta.ok) throw new Error(String(respuesta.status));
    pintarEfemerides(await respuesta.json());
    $("error-efemerides").hidden = true;
  } catch {
    $("error-efemerides").textContent = "No se pudo leer la programación. Recargá la página para probar de nuevo.";
    $("error-efemerides").hidden = false;
  } finally {
    $("cargando-efemerides").hidden = true;
  }
}

function tipoElegido() {
  return document.querySelector('input[name="tipo-objetivo"]:checked').value;
}

function llenarMeses() {
  const actual = new Date().getMonth();
  for (const id of ["mes-del-dia", "mes-objetivo"]) {
    $(id).replaceChildren(...MESES.map((nombre, i) => {
      const opcion = document.createElement("option");
      opcion.value = String(i + 1);
      opcion.textContent = nombre;
      opcion.selected = i === actual;
      return opcion;
    }));
  }
}

document.querySelectorAll('input[name="tipo-objetivo"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    const deMes = tipoElegido() === "mes";
    $("dia-objetivo").disabled = deMes;
    $("mes-del-dia").disabled = deMes;
    $("mes-objetivo").disabled = !deMes;
  });
});

function mensajeDeGenerar(cuerpo, objetivo) {
  if (cuerpo.type === "/errores/efemeride-existente") {
    return `Ya existe una efeméride de ${objetivo.tipo === "mes" ? "este mes" : "este día"}.`;
  }
  if (cuerpo.type === "/errores/pedido-pendiente") {
    const pendiente = cuerpo.pedido_pendiente;
    return `Ya hay un pedido sin atender (efemérides de ${nombreDeObjetivo(pendiente.objetivo)},`
      + ` desde las ${hora(pendiente.pedido_manual_el)}). Esperá a que el planificador lo tome.`;
  }
  if (cuerpo.type === "/errores/pedido-invalido") return "Revisá el día y el mes.";
  return "No se pudo pedir la generación. Probá de nuevo.";
}

$("generar-ahora").addEventListener("submit", async (evento) => {
  evento.preventDefault();
  errorGenerar("");
  const tipo = tipoElegido();
  const mes = Number($(tipo === "mes" ? "mes-objetivo" : "mes-del-dia").value);
  const dia = Number($("dia-objetivo").value);
  if (tipo === "dia" && !(dia >= 1 && dia <= 31)) {
    errorGenerar("Falta: el día.");
    $("dia-objetivo").focus();
    return;
  }
  const fechaObjetivo = proximaFecha(tipo, dia, mes);
  if (!fechaObjetivo) {
    errorGenerar(`El ${dia} de ${MESES[mes - 1]} no existe.`);
    $("dia-objetivo").focus();
    return;
  }
  const objetivo = { tipo, fecha: fechaObjetivo };
  $("boton-generar").disabled = true;
  try {
    const respuesta = await fetch("/api/admin/efemerides/generacion", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify({ objetivo }),
    });
    const cuerpo = await respuesta.json();
    if (respuesta.status !== 202) {
      errorGenerar(mensajeDeGenerar(cuerpo, objetivo));
      return;
    }
    esperando = { marcado: Date.parse(cuerpo.pedido_manual_el), objetivo };
    $("resultado-generar").textContent = `Generando efemérides de ${nombreDeObjetivo(objetivo)}…`
      + " Cuando estén, aparecen en Efemérides como Propuestas.";
    $("resultado-generar").hidden = false;
    if (!sondeo) sondeo = setInterval(cargarEfemerides, SONDEO_MS);
    await cargarEfemerides();
  } catch {
    errorGenerar("No se pudo pedir la generación. Probá de nuevo.");
  } finally {
    $("boton-generar").disabled = !$("efemerides-sin-modelo").hidden;
  }
});

llenarMeses();

// ── P7a Modelos ─────────────────────────────────────────────────────────────────────────
// Una fila por capacidad; se edita de a una. El guardado valida el conjunto entero en el
// servidor: la pantalla solo muestra lo que responde.
const MODELOS = "/api/admin/modelos";
const ESTADO_DEL_MODELO = {
  descargado: "✓ Listo",
  no_descargado: "○ Falta descargar en el motor",
  motor_no_responde: "✗ No responde",
  no_aplica: "✓ Local, sin modelo",
};
const NOMBRE_DEL_PARAMETRO = {
  timeout_s: "Tope de tiempo (s)",
  num_ctx: "Contexto del modelo (num_ctx, tokens)",
  temperatura: "Temperatura",
  max_tokens_entrada: "Tope de tokens de entrada",
  max_tokens_salida: "Tope de tokens de salida",
  num_thread: "Hilos (num_thread)",
  dimension: "Dimensión del vector",
  prefijo_fragmento: "Prefijo de cada fragmento",
  prefijo_consulta: "Prefijo de cada consulta",
  ocr_escala: "OCR: escala de la imagen (1 a 4)",
  ocr_invertir: "OCR: invertir si el fondo es oscuro",
  ocr_binarizar: "OCR: binarización adaptativa",
  ocr_confianza_minima: "OCR: confianza mínima por palabra (0 a 100)",
  imagenes_maximo: "Imágenes embebidas: cuántas leer por Pieza (0, ninguna)",
  imagenes_tiempo_maximo_s: "Imágenes embebidas: tope de tiempo por Pieza (s)",
  imagenes_a_describir: "Imágenes sin texto: cuántas describir solas por Pieza (0, ninguna)",
};
const PARAMETROS_DE_TEXTO = ["prefijo_fragmento", "prefijo_consulta"];
let modelos = null;  // la última lectura: {capacidades, instrucciones}
let editando = null;  // la fila que se edita, tal como se leyó

function notaDelBanco(banco) {
  if (!banco || !banco.ganador) return "";
  const noChino = banco.mejor_no_chino ? `mejor no chino ${banco.mejor_no_chino}` : "sin candidato no chino";
  return `Banco: ganador ${banco.ganador} · ${noChino}`;
}

function pintarModelos() {
  const filas = modelos.capacidades.map((fila) => {
    const tr = document.createElement("tr");
    const modelo = document.createElement("td");
    modelo.textContent = fila.modelo || "— (OCR local)";
    const nota = notaDelBanco(fila.banco);
    if (nota) {
      const div = document.createElement("div");
      div.className = "nota-banco";
      div.textContent = nota;
      modelo.append(div);
    }
    const estado = ESTADO_DEL_MODELO[fila.disponibilidad] || "— No se pudo saber";
    const accion = document.createElement("td");
    accion.append(boton("Editar", (b) => editarModelo(fila, b)));
    tr.append(celda(fila.capacidad), celda(fila.motor), modelo, celda(fila.instruccion_id || "—"),
      celda(estado, `estado-modelo-${fila.disponibilidad || "desconocido"}`), accion);
    return tr;
  });
  $("modelos").replaceChildren(...filas);
  $("tabla-modelos").hidden = false;
  const conModelo = modelos.capacidades.filter((f) => f.disponibilidad !== "no_aplica");
  $("modelos-sin-motor").hidden = !conModelo.length
    || !conModelo.every((f) => f.disponibilidad === "motor_no_responde");
}

async function cargarModelos() {
  $("cargando-modelos").hidden = false;
  $("error-modelos").hidden = true;
  try {
    const respuesta = await fetch(MODELOS);
    if (!respuesta.ok) throw new Error(String(respuesta.status));
    modelos = await respuesta.json();
    pintarModelos();
  } catch {
    $("tabla-modelos").hidden = true;
    $("editar-modelo").hidden = true;
    $("error-modelos").hidden = false;
  } finally {
    $("cargando-modelos").hidden = true;
  }
}

$("reintentar-modelos").addEventListener("click", () => cargarModelos());

function campoDeParametro(clave, valor) {
  const id = `parametro-${clave}`;
  const contenedor = document.createElement("div");
  const etiqueta = document.createElement("label");
  etiqueta.htmlFor = id;
  etiqueta.textContent = NOMBRE_DEL_PARAMETRO[clave] || clave;
  const entrada = document.createElement("input");
  entrada.id = id;
  entrada.dataset.clave = clave;
  if (typeof valor === "boolean") {
    entrada.type = "checkbox";
    entrada.checked = valor;
  } else if (PARAMETROS_DE_TEXTO.includes(clave)) {
    entrada.type = "text";
    entrada.maxLength = 100;
  } else {
    entrada.type = "number";
    entrada.step = clave === "temperatura" || clave === "ocr_escala" ? "0.05" : "1";
    entrada.min = "0";
    entrada.inputMode = clave === "temperatura" || clave === "ocr_escala" ? "decimal" : "numeric";
  }
  if (entrada.type !== "checkbox") entrada.value = valor ?? "";
  contenedor.append(etiqueta, entrada);
  return contenedor;
}

function limpiarResultadoModelo() {
  $("errores-modelo").hidden = true;
  $("errores-modelo").replaceChildren();
  $("avisos-modelo").replaceChildren();
}

function editarModelo(fila, foco) {
  editando = fila;
  limpiarResultadoModelo();
  $("titulo-editar-modelo").textContent = `Editar «${fila.capacidad}»`;
  $("campo-modelo").hidden = fila.motor === "local";
  $("modelo").value = fila.modelo || "";
  const instrucciones = modelos.instrucciones[fila.capacidad] || [];
  $("campo-instruccion").hidden = instrucciones.length === 0;
  $("instruccion").replaceChildren(...instrucciones.map((id) => new Option(id, id)));
  $("instruccion").value = fila.instruccion_id || "";
  $("parametros").replaceChildren(...Object.entries(fila.parametros).map(([c, v]) => campoDeParametro(c, v)));
  $("aviso-embeber").hidden = fila.capacidad !== "embeber";
  $("num-ctx-compartido").hidden = true;
  $("editar-modelo").hidden = false;
  $("titulo-editar-modelo").focus();
  cargarHistorialDeModelo(fila.capacidad);
}

function valorDe(entrada) {
  if (entrada.type === "checkbox") return entrada.checked;
  if (PARAMETROS_DE_TEXTO.includes(entrada.dataset.clave)) return entrada.value;
  // Vacío o con letras se manda tal cual: lo rechaza la validación del servidor con su mensaje.
  return entrada.value === "" ? null : Number(entrada.value);
}

// La fila editada y, si cambió `num_ctx` de un modelo que usan otras filas, esas también: un
// mismo modelo con dos `num_ctx` no valida (Ollama lo recargaría en cada cambio de capacidad).
function cambiosDelFormulario() {
  const parametros = { ...editando.parametros };
  for (const entrada of $("parametros").querySelectorAll("input")) parametros[entrada.dataset.clave] = valorDe(entrada);
  const config = {
    motor: editando.motor,
    modelo: editando.motor === "local" ? editando.modelo : $("modelo").value.trim(),
    instruccion_id: $("campo-instruccion").hidden ? editando.instruccion_id : $("instruccion").value,
    parametros,
  };
  const cambios = [{ capacidad: editando.capacidad, version_base: editando.version, config }];
  const otras = parametros.num_ctx !== editando.parametros.num_ctx
    ? modelos.capacidades.filter((f) => f.capacidad !== editando.capacidad
      && f.motor === config.motor && f.modelo === config.modelo && "num_ctx" in f.parametros)
    : [];
  for (const otra of otras) {
    cambios.push({
      capacidad: otra.capacidad,
      version_base: otra.version,
      config: { motor: otra.motor, modelo: otra.modelo, instruccion_id: otra.instruccion_id,
        parametros: { ...otra.parametros, num_ctx: parametros.num_ctx } },
    });
  }
  $("num-ctx-compartido").textContent = otras.length
    ? `El modelo también lo usa ${otras.map((f) => `«${f.capacidad}»`).join(", ")}: se le cambia el mismo num_ctx.`
    : "";
  $("num-ctx-compartido").hidden = !otras.length;
  return cambios;
}

function mostrarErroresDeModelo(titulo, errores) {
  const lista = document.createElement("ul");
  lista.replaceChildren(...errores.map((e) => {
    const li = document.createElement("li");
    li.textContent = `${[e.capacidad, e.campo].filter(Boolean).join(" · ")}: ${e.mensaje}`;
    return li;
  }));
  const p = document.createElement("p");
  p.textContent = titulo;
  $("errores-modelo").replaceChildren(p, ...(errores.length ? [lista] : []));
  $("errores-modelo").hidden = false;
}

function mostrarAvisosDeModelo(encabezado, avisos) {
  const partes = [];
  if (encabezado) {
    const p = document.createElement("p");
    p.className = "resultado";
    p.textContent = encabezado;
    partes.push(p);
  }
  for (const aviso of avisos) {
    const p = document.createElement("p");
    p.className = "aviso-modelo";
    p.textContent = `(!) ${aviso.capacidad}: ${aviso.mensaje}`;
    partes.push(p);
  }
  $("avisos-modelo").replaceChildren(...partes);
}

async function enviarModelo(metodo, ruta) {
  limpiarResultadoModelo();
  const botones = ["cancelar-modelo", "verificar-modelo", "guardar-modelo"];
  botones.forEach((id) => { $(id).disabled = true; });
  try {
    const respuesta = await fetch(ruta, {
      method: metodo,
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify({ cambios: cambiosDelFormulario() }),
    });
    return { respuesta, cuerpo: await respuesta.json() };
  } finally {
    botones.forEach((id) => { $(id).disabled = false; });
  }
}

$("verificar-modelo").addEventListener("click", async () => {
  try {
    const { respuesta, cuerpo } = await enviarModelo("POST", `${MODELOS}/verificacion`);
    if (!respuesta.ok) {
      mostrarErroresDeModelo(cuerpo.detail || "No se pudo verificar.", []);
    } else if (!cuerpo.ok) {
      mostrarErroresDeModelo("La configuración no es válida:", cuerpo.errores);
    } else {
      mostrarAvisosDeModelo("La configuración es válida. Todavía no se guardó.", cuerpo.avisos);
    }
  } catch {
    mostrarErroresDeModelo("No se pudo verificar. Probá de nuevo.", []);
  }
});

$("editar-modelo").addEventListener("submit", async (evento) => {
  evento.preventDefault();
  const capacidad = editando.capacidad;
  try {
    const { respuesta, cuerpo } = await enviarModelo("PUT", MODELOS);
    if (respuesta.status === 409) {
      await cargarModelos();
      const actual = modelos.capacidades.find((f) => f.capacidad === capacidad);
      editarModelo(actual);
      mostrarErroresDeModelo(`Otra persona guardó «${cuerpo.capacidad}» mientras editabas. `
        + "El formulario muestra lo que quedó: revisalo y guardá de nuevo.", []);
    } else if (respuesta.status === 422 && cuerpo.errores) {
      mostrarErroresDeModelo("No se guardó nada: la configuración no es válida.", cuerpo.errores);
    } else if (!respuesta.ok) {
      mostrarErroresDeModelo(cuerpo.detail || "No se pudo guardar. Sigue como estaba.", []);
    } else {
      modelos.capacidades = cuerpo.filas;
      pintarModelos();
      editarModelo(cuerpo.filas.find((f) => f.capacidad === capacidad));
      const reindexar = cuerpo.requiere_reindexado
        ? " Reconstruí el índice con el comando reindexar."
        : "";
      mostrarAvisosDeModelo(`Guardado: rige para los trabajos nuevos.${reindexar}`, cuerpo.avisos);
    }
  } catch {
    mostrarErroresDeModelo("No se pudo guardar. Sigue como estaba.", []);
  }
});

$("cancelar-modelo").addEventListener("click", () => {
  const capacidad = editando && editando.capacidad;
  $("editar-modelo").hidden = true;
  editando = null;
  const fila = [...$("modelos").querySelectorAll("tr")]
    .find((tr) => tr.firstChild && tr.firstChild.textContent === capacidad);
  if (fila) fila.querySelector("button").focus(); else $("pestana-modelos").focus();
});

const textoDeCambio = (valores) => valores ? JSON.stringify(valores) : "—";

async function cargarHistorialDeModelo(capacidad) {
  $("tabla-historial-modelo").hidden = true;
  $("historial-modelo-vacio").hidden = true;
  try {
    const respuesta = await fetch(`${MODELOS}/${encodeURIComponent(capacidad)}/historial`);
    if (!respuesta.ok || !editando || editando.capacidad !== capacidad) return;
    const { eventos } = await respuesta.json();
    $("historial-modelo").replaceChildren(...eventos.map((e) => {
      const tr = document.createElement("tr");
      tr.append(celda(fecha(e.fecha)), celda(e.persona), celda(textoDeCambio(e.antes), "motivo"),
        celda(textoDeCambio(e.despues), "motivo"));
      return tr;
    }));
    $("tabla-historial-modelo").hidden = eventos.length === 0;
    $("historial-modelo-vacio").hidden = eventos.length > 0;
  } catch {
    // Sin historial el formulario sigue sirviendo.
  }
}

async function iniciar() {
  const respuesta = await fetch("/api/yo");
  if (!respuesta.ok) {
    window.location.href = "/";
    return;
  }
  yo = await respuesta.json();
  mostrarFranja(yo.sistema);
  if (yo.permisos.includes("OPERAR_SISTEMA")) mostrarIndice();
  const visibles = PESTANAS.filter((p) => yo.permisos.includes(p.permiso));
  $("sin-pestanas").hidden = visibles.length > 0;
  $("pestanas").replaceChildren(...visibles.map((pestana) => {
    const boton = document.createElement("button");
    boton.type = "button";
    boton.id = `pestana-${pestana.id}`;
    boton.setAttribute("role", "tab");
    boton.setAttribute("aria-controls", `panel-${pestana.id}`);
    boton.textContent = pestana.nombre;
    boton.addEventListener("click", () => elegir(pestana));
    return boton;
  }));
  if (visibles.length) elegir(visibles[0]);
}

iniciar();
