"use strict";
// P6 Conversación (051): fuentes primero. Desde 134 se usa como un chat: burbujas en el hilo, la
// caja de pregunta abajo y las Fichas de cada respuesta en su bloque «n Fuentes». Todo texto del
// Acervo o del servidor se pinta con `textContent`; los enlaces son rutas propias con los ids
// escapados (RNF-21).
const CSRF = { "X-HV": "1" };
const $ = (id) => document.getElementById(id);

const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];
const FILTROS = [
  { clave: "tipo", nombre: "Tipo de Pieza", ruta: "/api/tipos",
    lista: (c) => c.tipos.map((t) => [t, etiquetaTipo(t)]) },
  { clave: "tipo_actividad", nombre: "Tipo de actividad", ruta: "/api/tipos-actividad",
    lista: (c) => c.tipos_actividad.map((t) => [t, t]) },
  { clave: "carrera", nombre: "Carrera", ruta: "/api/carreras",
    lista: (c) => c.carreras.map((x) => [x.carrera_id, x.nombre]) },
];
// Como en Validar: el catálogo guarda el nombre sin acento.
const CON_ACENTO = { fotografia: "Fotografía", resolucion: "Resolución" };
const etiquetaTipo = (tipo) =>
  CON_ACENTO[tipo] || `${tipo.charAt(0).toUpperCase()}${tipo.slice(1)}`;
const NO_DISPONIBLE = {
  "/errores/indice-no-disponible":
    "La conversación no está disponible ahora (el índice no responde). Avisá al Administrador técnico.",
  "/errores/capa-modelos-no-disponible": "La conversación no está disponible ahora. Probá de nuevo en un momento.",
  "/errores/servicio-no-disponible": "La conversación no está disponible ahora. Probá de nuevo en un momento.",
  "/errores/tiempo-agotado": "La búsqueda tardó demasiado. Probá de nuevo en un momento.",
  "/errores/limite-de-consultas": "Llegaste al tope de preguntas. Probá de nuevo en unos minutos.",
};
// Los turnos que quedan a la vista; los anteriores se pliegan (R14 E3): nada se borra.
const TURNOS_VISIBLES = 6;

const nombresDe = {}; // valor de un filtro → su nombre visible (la Carrera viaja como slug)
let conversaciones = []; // las mías, de `GET /api/conversaciones`: { id, titulo, cuando }
let siguiente = null; // el cursor de la página que sigue del historial
let actual = null;
let ocupado = false;
let detener = null; // el AbortController del turno en curso («Detener», E4)
const celular = matchMedia("(max-width: 900px)"); // el historial se pliega (134)

// ── Fechas en lenguaje llano ────────────────────────────────────────────────────────────
function fechaLlana(desde, hasta, precision) {
  const [a, m, d] = desde.split("-").map(Number);
  if (precision === "dia") return `${d} de ${MESES[m - 1]} de ${a}`;
  if (precision === "mes") return `${MESES[m - 1]} de ${a}`;
  if (precision === "anio") return String(a);
  if (precision === "decada") return `década de ${a}`;
  return `del ${fechaLlana(desde, desde, "dia")} al ${fechaLlana(hasta, hasta, "dia")}`;
}

const diaLlano = (iso) => fechaLlana(iso, iso, "dia");

function hayFiltros(filtros) {
  return Boolean(filtros.desde || filtros.hasta || FILTROS.some((f) => (filtros[f.clave] || []).length));
}

function filtrosComoTexto(filtros) {
  const partes = [];
  if (filtros.desde && filtros.hasta) partes.push(`Fecha: del ${diaLlano(filtros.desde)} al ${diaLlano(filtros.hasta)}`);
  else if (filtros.desde) partes.push(`Fecha: desde el ${diaLlano(filtros.desde)}`);
  else if (filtros.hasta) partes.push(`Fecha: hasta el ${diaLlano(filtros.hasta)}`);
  for (const filtro of FILTROS) {
    const valores = filtros[filtro.clave] || [];
    if (valores.length) partes.push(`${filtro.nombre}: ${valores.map((v) => nombresDe[v] || v).join(", ")}`);
  }
  return partes.length ? partes.join("; ") : "sin filtros";
}

// ── Filtros ──────────────────────────────────────────────────────────────────────────────
async function cargarFiltros() {
  for (const filtro of FILTROS) {
    const respuesta = await fetch(filtro.ruta);
    if (!respuesta.ok) continue;
    const grupo = $(`filtro-${filtro.clave}`);
    for (const [valor, nombre] of filtro.lista(await respuesta.json())) {
      nombresDe[valor] = nombre;
      const etiqueta = document.createElement("label");
      const casilla = document.createElement("input");
      casilla.type = "checkbox";
      casilla.value = valor;
      casilla.name = filtro.clave;
      etiqueta.append(casilla, document.createTextNode(nombre));
      grupo.append(etiqueta);
    }
  }
}

function filtrosElegidos() {
  const filtros = {};
  if ($("desde").value) filtros.desde = $("desde").value;
  if ($("hasta").value) filtros.hasta = $("hasta").value;
  for (const filtro of FILTROS) {
    const marcadas = [...document.querySelectorAll(`input[name="${filtro.clave}"]:checked`)];
    if (marcadas.length) filtros[filtro.clave] = marcadas.map((c) => c.value);
  }
  return filtros;
}

function limpiarFiltros() {
  $("desde").value = "";
  $("hasta").value = "";
  for (const casilla of document.querySelectorAll("#filtros input[type=checkbox]")) casilla.checked = false;
  refrescarQuitar();
}

// Quita los filtros del próximo turno; no reenvía nada: la persona decide si pregunta de nuevo.
function quitarFiltros() {
  limpiarFiltros();
  $("estado").textContent = "Filtros quitados: se aplican a la próxima pregunta.";
}

// «Quitar filtros» solo cuando hay algo que quitar; el botón plegado dice cuántos hay (134).
function refrescarQuitar() {
  const filtros = filtrosElegidos();
  const n = (filtros.desde || filtros.hasta ? 1 : 0)
    + FILTROS.reduce((suma, f) => suma + (filtros[f.clave] || []).length, 0);
  $("quitar-filtros").hidden = !n;
  $("resumen-filtros").textContent = n ? `Filtros (${n} ${n === 1 ? "activo" : "activos"})` : "Filtros";
}

// ── Historial (las mías, agrupadas por día) ──────────────────────────────────────────────
function diaDe(iso) {
  const dia = new Date(iso);
  const hoy = new Date();
  const ayer = new Date(hoy.getFullYear(), hoy.getMonth(), hoy.getDate() - 1);
  const mismo = (a, b) => a.toDateString() === b.toDateString();
  if (mismo(dia, hoy)) return "Hoy";
  if (mismo(dia, ayer)) return "Ayer";
  return `${dia.getDate()} de ${MESES[dia.getMonth()]} de ${dia.getFullYear()}`;
}

function pintarHistorial() {
  const grupos = [];
  for (const c of conversaciones) {
    const dia = diaDe(c.cuando);
    if (!grupos.length || grupos[grupos.length - 1].dia !== dia) grupos.push({ dia, items: [] });
    grupos[grupos.length - 1].items.push(c);
  }
  $("historial").replaceChildren(...grupos.map(({ dia, items }) => {
    const grupo = document.createElement("li");
    const titulo = document.createElement("h3");
    titulo.textContent = dia;
    const lista = document.createElement("ul");
    lista.append(...items.map((c) => {
      const item = document.createElement("li");
      const boton = document.createElement("button");
      boton.type = "button";
      boton.className = "abrir";
      boton.textContent = c.titulo || "Conversación nueva";
      boton.addEventListener("click", () => abrir(c.id));
      if (c.id === actual) {
        boton.setAttribute("aria-current", "true");
        boton.prepend(document.createTextNode("> "));
      }
      item.append(boton);
      return item;
    }));
    grupo.append(titulo, lista);
    return grupo;
  }));
  $("historial-vacio").hidden = conversaciones.length > 0;
}

// Si el historial no carga se puede conversar igual (R14 E1). Con `cursor`, suma la página que
// sigue («Mostrar más»); sin él, empieza de nuevo.
async function cargarHistorial(cursor) {
  $("error-historial").hidden = true;
  try {
    const respuesta = await fetch(cursor
      ? `/api/conversaciones?cursor=${encodeURIComponent(cursor)}` : "/api/conversaciones");
    if (respuesta.status === 401) return sesionVencida();
    if (!respuesta.ok) throw new Error(String(respuesta.status));
    const pagina = await respuesta.json();
    const nuevas = pagina.conversaciones.map((c) => (
      { id: c.conversacion_id, titulo: c.titulo, cuando: c.actualizada_el }));
    if (cursor) {
      conversaciones.push(...nuevas.filter((n) => !conversaciones.some((c) => c.id === n.id)));
    } else {
      // El servidor no lista una vacía: la actual sin mensajes se sigue pintando acá.
      const vacia = conversaciones.find((c) => c.id === actual && !nuevas.some((n) => n.id === c.id));
      conversaciones = vacia ? [vacia, ...nuevas] : nuevas;
    }
    siguiente = pagina.siguiente;
    $("mas-historial").hidden = !siguiente;
    pintarHistorial();
  } catch {
    $("error-historial").hidden = false;
  }
}

function hiloVacio() {
  $("hilo").replaceChildren();
  franjas([]);
  $("estado").textContent = "";
  mostrarError("");
}

// Las franjas del índice y de la búsqueda semántica hablan del último turno.
function franjas(avisos) {
  $("franja-indice").hidden = !avisos.includes("indice_incompleto");
  $("franja-semantica").hidden = !avisos.includes(AVISO_SEMANTICA);
}

// El hilo baja al último turno; mientras llega la respuesta, solo si la persona no subió a leer.
function alFinal(forzar) {
  const caja = $("desplazable");
  if (forzar || caja.scrollHeight - caja.scrollTop - caja.clientHeight < 120) caja.scrollTop = caja.scrollHeight;
}

function marcarActual(id) {
  actual = id;
  history.replaceState(null, "", `/conversacion.html?conversacion=${encodeURIComponent(id)}`);
  pintarHistorial();
}

// «Nueva conversación»: hilo vacío y filtros limpios; la anterior queda en el historial.
async function nueva() {
  const respuesta = await fetch("/api/conversaciones", { method: "POST", headers: CSRF });
  if (respuesta.status === 401) return sesionVencida();
  if (!respuesta.ok) {
    mostrarError((await respuesta.json()).detail || "No se pudo empezar la conversación.");
    return false;
  }
  const creada = await respuesta.json();
  conversaciones.unshift({ id: creada.conversacion_id, titulo: null, cuando: creada.actualizada_el });
  hiloVacio();
  limpiarFiltros();
  $("vacia").hidden = false;
  marcarActual(creada.conversacion_id);
  return true;
}

// Reabre una de las mías con sus fuentes y Citas; se puede seguir escribiendo.
async function abrir(id) {
  if (ocupado) return;
  const respuesta = await fetch(`/api/conversaciones/${encodeURIComponent(id)}`);
  if (respuesta.status === 401) return sesionVencida();
  if (!respuesta.ok) {
    mostrarError(respuesta.status === 404
      ? "No encontramos esa conversación." : "No se pudo abrir la conversación.");
    return;
  }
  const conversacion = await respuesta.json();
  hiloVacio();
  marcarActual(conversacion.conversacion_id);
  let turnoActual = null;
  let ultima = null;
  conversacion.mensajes.forEach((mensaje, i) => {
    if (mensaje.autor === "persona") {
      turnoActual = nuevoTurno(mensaje.texto, mensaje.filtros || {});
      return;
    }
    if (!turnoActual) return;
    ultima = mensaje;
    pintarRespuestaGuardada(turnoActual, mensaje, i === conversacion.mensajes.length - 1);
  });
  $("vacia").hidden = conversacion.mensajes.length > 0;
  if (ultima?.estado === "sin_semantica") franjas([AVISO_SEMANTICA]);
  plegar();
  alFinal(true);
  if (celular.matches) $("plegar-historial").open = false;
}

// ── Turno ────────────────────────────────────────────────────────────────────────────────
function mostrarError(texto) {
  $("error-turno").textContent = texto;
  $("error-turno").hidden = !texto;
}

function sesionVencida() {
  location.href = "/?error=sesion-vencida";
  return false;
}

// Un turno del hilo: la pregunta y sus filtros como texto; la respuesta se agrega después.
function nuevoTurno(pregunta, filtros) {
  const turno = document.createElement("li");
  turno.className = "turno";
  turno.pregunta = pregunta;
  $("hilo").append(turno);
  agregarAlTurno(turno, "Vos", pregunta, "persona");
  // Sin filtros no se repite «sin filtros» bajo cada burbuja (134).
  if (hayFiltros(filtros)) {
    const filtrosDelTurno = document.createElement("p");
    filtrosDelTurno.className = "aviso filtros-turno";
    filtrosDelTurno.textContent = `Filtros de este turno: ${filtrosComoTexto(filtros)}`;
    turno.append(filtrosDelTurno);
  }
  alFinal(true);
  return turno;
}

// Una burbuja del hilo. Quién habla lo dice el lado de la burbuja; el lector de pantalla lo lee.
function agregarAlTurno(turno, autor, texto, clase) {
  const renglon = document.createElement("div");
  renglon.className = clase || "";
  const quien = document.createElement("strong");
  quien.className = "oculto";
  quien.textContent = `${autor}: `;
  const cuerpo = document.createElement("span");
  cuerpo.textContent = texto;
  renglon.append(quien, cuerpo);
  turno.append(renglon);
  return renglon;
}

// «Busqué: …» cuando la pregunta se reformuló con el historial (R14, paso 1).
function mostrarReformulada(turno, reformulada) {
  if (!reformulada) return;
  const busque = document.createElement("p");
  busque.className = "aviso";
  busque.textContent = `Busqué: “${reformulada}”`;
  turno.append(busque);
}

// Los turnos viejos se pliegan para no perder el foco; nada se borra.
function plegar() {
  const visibles = [...$("hilo").children].filter((li) => li.classList.contains("turno"));
  if (visibles.length <= TURNOS_VISIBLES) return;
  let plegados = $("plegados");
  if (!plegados) {
    const item = document.createElement("li");
    const detalle = document.createElement("details");
    const resumen = document.createElement("summary");
    plegados = document.createElement("ol");
    plegados.id = "plegados";
    detalle.append(resumen, plegados);
    item.append(detalle);
    $("hilo").prepend(item);
  }
  plegados.append(...visibles.slice(0, visibles.length - TURNOS_VISIBLES));
  const n = plegados.children.length;
  plegados.previousElementSibling.textContent = `Mostrar ${n} ${n === 1 ? "turno anterior" : "turnos anteriores"}`;
}

function enlaceAlOriginal(piezaId, cita) {
  const ancla = /#(page|t)=(\d+)$/.exec(cita.enlace);
  const enlace = document.createElement("a");
  // La página de un PDF o el segundo de un video (048, 050).
  if (!ancla) enlace.href = `/api/piezas/${encodeURIComponent(piezaId)}/original`;
  else if (ancla[1] === "page") enlace.href = `/api/piezas/${encodeURIComponent(piezaId)}/original#page=${encodeURIComponent(ancla[2])}`;
  else enlace.href = `/api/piezas/${encodeURIComponent(piezaId)}/original#t=${encodeURIComponent(ancla[2])}`;
  enlace.target = "_blank";
  enlace.rel = "noopener";
  enlace.textContent = "Abrir el original";
  return enlace;
}

// El bloque plegable «n Fuentes» debajo de la burbuja `renglon` (134, antes el panel de la
// derecha): las Fichas con sus Citas. Plegado, nombra las Fichas; la `[n]` lo abre en su Cita.
function bloqueDeFuentes(renglon, fichas) {
  const bloque = document.createElement("details");
  bloque.className = "fuentes-turno";
  const resumen = document.createElement("summary");
  const n = fichas.length;
  const titulos = fichas.map((f) => (f.disponible === false ? "Pieza no disponible" : f.resumen));
  resumen.textContent = `${n} ${n === 1 ? "Fuente" : "Fuentes"}: ${titulos.join(" · ")}`;
  const lista = document.createElement("ol");
  renglon.citas = new Map(); // fragmento → su renglón en el bloque, destino de la `[n]`
  lista.append(...fichas.map((ficha) => {
    const item = document.createElement("li");
    const titulo = document.createElement("h3");
    // Retirada o ya no Validada desde ese turno (E8): ni su texto ni su original.
    if (ficha.disponible === false) {
      titulo.textContent = "Pieza no disponible";
      item.append(titulo);
      return item;
    }
    titulo.textContent = ficha.resumen;
    const datos = document.createElement("p");
    datos.className = "aviso";
    datos.textContent = `Validada · ${etiquetaTipo(ficha.tipo)} · ${ficha.tipo_actividad} · `
      + fechaLlana(ficha.fecha_inicio, ficha.fecha_fin, ficha.precision_fecha)
      // Historia 145: un Aporte lo mandó un Agente; quien consulta pesa la fuente.
      + (ficha.via_ingreso === "aporte" ? " · Aporte de un Agente" : "");
    const citas = document.createElement("ul");
    for (const cita of ficha.citas) {
      const linea = document.createElement("li");
      renglon.citas.set(cita.fragmento_id, linea);
      linea.tabIndex = -1;
      const donde = document.createElement("span");
      donde.className = "ubicacion";
      donde.textContent = `Cita: ${cita.ubicacion_legible}`;
      linea.append(donde, " ");
      // Al reabrir, la Cita guardada no trae el texto del fragmento: la ubicación y el original.
      if (cita.texto) {
        const texto = document.createElement("q");
        texto.textContent = cita.texto;
        linea.append(texto, " ");
      }
      linea.append(enlaceAlOriginal(ficha.pieza_id, cita));
      citas.append(linea);
    }
    // Todo rol con CONSULTAR abre la Ficha; sin VALIDAR, en solo lectura (R-05).
    const ver = document.createElement("a");
    ver.href = `/validar.html?pieza=${encodeURIComponent(ficha.pieza_id)}`;
    ver.textContent = "Ver la Ficha";
    item.append(titulo, datos, citas, ver);
    return item;
  }));
  bloque.append(resumen, lista);
  renglon.after(bloque);
  renglon.bloque = bloque;
  return bloque;
}

// ── Respuesta en vivo ────────────────────────────────────────────────────────────────────
const SIN_EVIDENCIA = "No encontré evidencia suficiente en el Acervo.";
const FALLO_LA_REDACCION = "No se pudo redactar la respuesta. "
  + "Las fuentes de abajo siguen siendo el resultado.";
const NO_VERIFICADA = "Una cita de la respuesta no se pudo verificar: no la tomes como respaldo.";
const REDACCION_NO_DISPONIBLE = "La redacción no está disponible ahora. Las fuentes de abajo sí.";
const SE_CORTO = "Se cortó la conexión. Quedó lo que llegó a escribirse.";
const DETENIDA = "Detenida por vos.";
const SIN_SEMANTICA = "Búsqueda semántica no disponible.";
const AVISO_SEMANTICA = "busqueda_semantica_no_disponible";

// Al llegar `citas`, cada `[n]` del texto ya escrito pasa a ser un enlace a su Cita en el bloque,
// o queda tachada si no se verificó (R-15). El texto no cambia: se rearma con nodos de texto.
function marcarCitas(renglon, citas) {
  const cuerpo = renglon.querySelector("span");
  const porMarca = new Map(citas.citas.map((c) => [c.marca, c]));
  const partes = cuerpo.textContent.split(/\[(\d{1,3})\]/);
  const nodos = [];
  partes.forEach((parte, i) => {
    const cita = i % 2 ? porMarca.get(Number(parte)) : null;
    if (!(i % 2) || !cita) {
      nodos.push(document.createTextNode(i % 2 ? `[${parte}]` : parte));
    } else if (cita.verificada) {
      const enlace = document.createElement("a");
      // Con la query de la Conversación abierta: sin ella, el navegador recargaría la página.
      enlace.href = `/conversacion.html?conversacion=${encodeURIComponent(actual)}#cita-${encodeURIComponent(cita.fragmento_id)}`;
      enlace.textContent = `[${parte}]`; // tal cual: el texto no cambia (R-15)
      enlace.setAttribute("aria-label", `Cita ${cita.marca}: ir a la fuente`);
      // Sin navegar: se abre el bloque «n Fuentes» de esta respuesta y se va a la Cita.
      enlace.addEventListener("click", (evento) => {
        evento.preventDefault();
        if (renglon.bloque) renglon.bloque.open = true;
        const destino = renglon.citas?.get(cita.fragmento_id);
        destino?.scrollIntoView({ block: "center" });
        destino?.focus();
      });
      nodos.push(enlace);
    } else {
      const tachada = document.createElement("s");
      tachada.className = "no-verificada";
      tachada.textContent = `[${parte}]`; // tal cual: el texto no cambia (R-15)
      tachada.setAttribute("aria-label", `Cita ${cita.marca} no verificada`);
      nodos.push(tachada);
    }
  });
  cuerpo.replaceChildren(...nodos);
  renglon.conCitas = citas.citas.some((c) => c.verificada); // se puede proponer como relato
  if (citas.no_verificadas) {
    const aviso = document.createElement("p");
    aviso.className = "aviso-cita";
    aviso.setAttribute("role", "alert");
    aviso.textContent = NO_VERIFICADA;
    renglon.append(aviso);
  }
}

// El modelo no tuvo evidencia: su prosa se reemplaza por el aviso y las fuentes quedan (053).
function sinEvidencia(renglon) {
  renglon.className = "sin-evidencia";
  renglon.querySelector("span").textContent = SIN_EVIDENCIA;
  renglon.querySelector(".aviso-cita")?.remove();
}

// El modelo falló después de las fuentes (E3): sin texto a medias, la lista queda y el turno se
// puede reintentar sin repetir la pregunta, si es el último. Si no llegó a escribir nada, la
// redacción no estaba disponible (sin el modelo de `responder`).
function falloLaRedaccion(renglon, mensajeId, reintentable) {
  const escrito = renglon.querySelector("span").textContent;
  renglon.className = "respuesta fallida";
  renglon.querySelector("span").replaceChildren();
  renglon.querySelector(".aviso-cita")?.remove();
  const aviso = document.createElement("p");
  aviso.className = "aviso-cita";
  aviso.setAttribute("role", "alert");
  aviso.textContent = escrito ? FALLO_LA_REDACCION : REDACCION_NO_DISPONIBLE;
  renglon.append(aviso);
  if (renglon.bloque) renglon.bloque.open = true; // las fuentes son el resultado
  if (reintentable) botonReintentar(renglon, mensajeId);
}

// Se cortó la conexión a mitad (E9) o la persona la detuvo (E4): lo escrito queda, rotulado, y
// se guardó `interrumpida` (R-17).
function cortada(renglon, mensajeId, reintentable, rotulo = SE_CORTO) {
  renglon.className = "respuesta cortada";
  const aviso = document.createElement("p");
  aviso.className = "aviso-cita";
  if (rotulo === SE_CORTO) aviso.setAttribute("role", "status"); // «Detener» lo anuncia `estado`
  aviso.textContent = rotulo;
  renglon.append(aviso);
  if (reintentable) botonReintentar(renglon, mensajeId);
}

function botonReintentar(renglon, mensajeId) {
  const conversacion = actual;
  const reintentar = document.createElement("button");
  reintentar.type = "button";
  reintentar.textContent = "Reintentar este turno";
  reintentar.addEventListener("click", async () => {
    if (ocupado) return;
    reintentar.remove();
    // Si el reintento no llega a abrir el flujo (503, 409 o sin red), el botón vuelve.
    if (!(await turno(`/api/conversaciones/${encodeURIComponent(conversacion)}/mensajes/`
      + `${encodeURIComponent(mensajeId)}/reintentar`, null, renglon.closest(".turno")))) renglon.append(reintentar);
  });
  renglon.append(reintentar);
}

// «Proponer como relato» (DEC-147): solo con REVISAR_EFEMERIDES y bajo una respuesta completa con
// alguna Cita verificada; el servidor lo vuelve a decidir (409 `respuesta-no-proponible`).
let puedeProponer = false;
let aProponer = null; // { boton, mensajeId, conversacion } que espera la confirmación

function botonProponer(renglon, mensajeId) {
  if (!puedeProponer) return;
  const boton = document.createElement("button");
  boton.type = "button";
  boton.className = "proponer";
  boton.textContent = "Proponer como relato";
  boton.addEventListener("click", () => {
    aProponer = { boton, mensajeId, conversacion: actual };
    $("confirmar-relato").returnValue = "";
    $("confirmar-relato").showModal();
  });
  renglon.append(boton);
}

async function proponer({ boton, mensajeId, conversacion }) {
  boton.disabled = true;
  try {
    const respuesta = await fetch(`/api/conversaciones/${encodeURIComponent(conversacion)}`
      + `/mensajes/${encodeURIComponent(mensajeId)}/relato`, { method: "POST", headers: CSRF });
    if (respuesta.status === 401) return sesionVencida();
    const cuerpo = await respuesta.json().catch(() => ({}));
    if (respuesta.ok || cuerpo.type === "/errores/relato-ya-propuesto") {
      const aviso = document.createElement("p");
      aviso.className = "aviso";
      aviso.setAttribute("role", "status");
      aviso.textContent = respuesta.ok ? "Quedó propuesto. " : "Esta respuesta ya se propuso como relato. ";
      const enlace = document.createElement("a"); // el Relato y el 409 traen `relato_id`
      enlace.href = `/efemerides.html?relato=${encodeURIComponent(cuerpo.relato_id)}`;
      enlace.textContent = "Revisarlo en Relatos";
      aviso.append(enlace);
      boton.replaceWith(aviso);
      return;
    }
    mostrarError(cuerpo.detail || "No se pudo proponer el relato.");
  } catch {
    mostrarError("Se cortó la conexión. Probá de nuevo.");
  }
  boton.disabled = false;
}

$("confirmar-relato").addEventListener("close", () => {
  if ($("confirmar-relato").returnValue === "proponer") proponer(aProponer);
  else aProponer.boton.focus();
});

// Una respuesta guardada, al reabrir: como quedó al terminar su turno.
function pintarRespuestaGuardada(turnoEl, mensaje, esLaUltima) {
  mostrarReformulada(turnoEl, mensaje.consulta_reformulada);
  const sinFichas = !mensaje.fichas.length;
  const renglon = agregarAlTurno(turnoEl, "Historia Viva", mensaje.texto,
    sinFichas ? "sin-evidencia" : "respuesta");
  if (sinFichas) return renglon;
  // Sin búsqueda semántica la lista es todo el resultado: queda abierta.
  bloqueDeFuentes(renglon, mensaje.fichas).open = mensaje.estado === "sin_semantica";
  if (mensaje.estado === "sin_evidencia") sinEvidencia(renglon);
  else if (mensaje.estado === "error") falloLaRedaccion(renglon, mensaje.mensaje_id, esLaUltima);
  else if (mensaje.estado === "interrumpida") cortada(renglon, mensaje.mensaje_id, esLaUltima);
  else {
    marcarCitas(renglon, { citas: mensaje.citas, no_verificadas: mensaje.citas_no_verificadas });
    if (mensaje.estado === "completa" && renglon.conCitas) botonProponer(renglon, mensaje.mensaje_id);
  }
  return renglon;
}

// Los eventos de `text/event-stream` en orden; un `: ping` no es un evento (Contrato §1.8).
async function* eventos(respuesta) {
  const lector = respuesta.body.getReader();
  const decodificador = new TextDecoder();
  let resto = "";
  for (;;) {
    const { value, done } = await lector.read();
    resto += decodificador.decode(value || new Uint8Array(), { stream: !done });
    let corte;
    while ((corte = resto.indexOf("\n\n")) >= 0) {
      const bloque = resto.slice(0, corte);
      resto = resto.slice(corte + 2);
      const nombre = /^event: (.*)$/m.exec(bloque);
      const datos = /^data: (.*)$/m.exec(bloque);
      if (nombre && datos) yield { nombre: nombre[1], datos: JSON.parse(datos[1]) };
    }
    if (done) return;
  }
}

async function enviar(evento) {
  evento.preventDefault();
  const pregunta = $("pregunta").value.trim();
  if (ocupado || pregunta.length < 3) {
    if (!ocupado) mostrarError("Escribí una pregunta de al menos 3 caracteres.");
    return;
  }
  if (!actual && !(await nueva())) return;
  $("vacia").hidden = true;
  const filtros = filtrosElegidos();
  const turnoEl = nuevoTurno(pregunta, filtros);
  $("pregunta").value = "";
  await turno(`/api/conversaciones/${encodeURIComponent(actual)}/mensajes`,
    JSON.stringify({ texto: pregunta, filtros }), turnoEl);
  plegar();
}

// Un turno: la pregunta nueva o el reintento de la última (053). `cuerpo` es null al reintentar.
// Devuelve si se abrió el flujo de eventos.
async function turno(url, cuerpo, turnoEl) {
  const pregunta = turnoEl.pregunta;
  ocupado = true;
  $("enviar").disabled = true;
  $("pregunta").disabled = true; // se vuelve a escribir cuando termina el turno (134)
  mostrarError("");
  // En el hilo, «Buscando…» hasta las fuentes y «Escribiendo…» hasta el primer fragmento (134).
  const indicador = document.createElement("p");
  indicador.className = "indicador";
  indicador.textContent = "Buscando…";
  turnoEl.append(indicador);
  alFinal(true);
  const comienzo = Date.now();
  const reloj = setInterval(() => {
    const s = Math.floor((Date.now() - comienzo) / 1000);
    $("estado").textContent = `Buscando fuentes… ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }, 1000);
  $("estado").textContent = "Buscando fuentes…";
  let renglon = null; // el de la respuesta en el hilo, si hubo fuentes
  let mensajeId = null;
  let abierto = false;
  const controlador = new AbortController();
  detener = controlador;
  try {
    const respuesta = await fetch(url, {
      method: "POST",
      headers: cuerpo ? { ...CSRF, "Content-Type": "application/json" } : CSRF,
      body: cuerpo,
      signal: controlador.signal,
    });
    if (respuesta.status === 401) return sesionVencida();
    if (!respuesta.ok) {
      const problema = await respuesta.json().catch(() => ({}));
      mostrarError(NO_DISPONIBLE[problema.type] || problema.detail || "No se pudo mandar la pregunta.");
      $("estado").textContent = "";
      return false;
    }
    abierto = true;
    const registro = conversaciones.find((c) => c.id === actual);
    if (registro && !registro.titulo) {
      registro.titulo = pregunta.slice(0, 80);
      pintarHistorial();
    }
    for await (const { nombre, datos } of eventos(respuesta)) {
      if (nombre === "fuentes") {
        clearInterval(reloj);
        mensajeId = datos.mensaje_id;
        indicador.remove();
        mostrarReformulada(turnoEl, datos.consulta_reformulada);
        franjas(datos.avisos || []);
        const n = datos.fichas.length;
        $("estado").textContent = n
          ? `Se encontraron ${n} fuentes. Ir a las fuentes.`
          : "No se encontraron Fichas Validadas.";
        if ((datos.avisos || []).includes(AVISO_SEMANTICA)) {
          // Sin `embeber` (R-16): solo la lista por filtros, abierta; no hay redacción que esperar.
          const aviso = agregarAlTurno(turnoEl, "Historia Viva", SIN_SEMANTICA, "sin-evidencia");
          if (n) bloqueDeFuentes(aviso, datos.fichas).open = true;
          $("estado").textContent = `${SIN_SEMANTICA} ${$("estado").textContent}`;
        } else if (!n) {
          const conFiltros = hayFiltros(datos.filtros);
          const item = agregarAlTurno(turnoEl, "Historia Viva", conFiltros
            ? `${SIN_EVIDENCIA} No hay Fichas Validadas con los filtros elegidos `
              + `(${filtrosComoTexto(datos.filtros)}). No ampliamos los filtros por vos.`
            : SIN_EVIDENCIA, "sin-evidencia");
          if (conFiltros) {
            const quitar = document.createElement("button");
            quitar.type = "button";
            quitar.textContent = "Quitar filtros";
            quitar.addEventListener("click", quitarFiltros);
            item.append(" ", quitar);
          }
        } else {
          // La respuesta se escribe acá a medida que llega; se lee entera al terminar.
          renglon = agregarAlTurno(turnoEl, "Historia Viva", "", "respuesta");
          renglon.setAttribute("aria-busy", "true");
          indicador.textContent = "Escribiendo…";
          renglon.append(indicador);
          bloqueDeFuentes(renglon, datos.fichas);
          $("estado").textContent += " Redactando la respuesta…";
          $("detener").hidden = false;
        }
        alFinal(true);
      } else if (nombre === "token" && renglon) {
        indicador.remove();
        renglon.querySelector("span").append(datos.texto);
        alFinal(false);
      } else if (nombre === "citas" && renglon) {
        marcarCitas(renglon, datos);
      } else if (nombre === "fin") {
        if (renglon && datos.sin_evidencia) {
          sinEvidencia(renglon);
          if (renglon.bloque) renglon.bloque.open = true;
          $("estado").textContent = `${SIN_EVIDENCIA} Las fuentes siguen a la vista.`;
        } else if (renglon) {
          $("estado").textContent = "Respuesta completa.";
          if (renglon.conCitas) botonProponer(renglon, datos.mensaje_id);
        }
      } else if (nombre === "error") {
        if (renglon) {
          indicador.remove();
          // `turno-en-curso`: el turno venció y otro tomó la Conversación; no se guardó nada.
          const tomado = datos.type === "/errores/turno-en-curso";
          falloLaRedaccion(renglon, mensajeId, !tomado);
          if (tomado) mostrarError(datos.detail);
          $("estado").textContent = "";
        } else mostrarError(datos.detail || "No se pudo completar el turno.");
      }
    }
  } catch {
    if (controlador.signal.aborted && renglon) {
      // «Detener» (E4): el servidor cancela la redacción y guarda lo escrito.
      cortada(renglon, mensajeId, false, DETENIDA);
      $("estado").textContent = "Respuesta detenida.";
    } else if (renglon && mensajeId) {
      // Con la respuesta a medias, lo escrito queda y el turno se puede reintentar (E9).
      cortada(renglon, mensajeId, true);
    } else mostrarError("Se cortó la conexión. Probá de nuevo.");
  } finally {
    $("detener").hidden = true;
    detener = null;
    indicador.remove();
    // Con `fin`, con `error` o con la conexión cortada: la respuesta deja de estar en curso.
    if (renglon) renglon.removeAttribute("aria-busy");
    clearInterval(reloj);
    ocupado = false;
    $("enviar").disabled = false;
    $("pregunta").disabled = false;
    // El foco se perdió al deshabilitar la caja (o en «Detener», que se ocultó): vuelve a la caja.
    if (document.activeElement === document.body) $("pregunta").focus();
    alFinal(false);
  }
  if (abierto) cargarHistorial(); // la más reciente sube al principio
  return abierto;
}

// ── Inicio ───────────────────────────────────────────────────────────────────────────────
async function iniciar() {
  const yo = await fetch("/api/yo");
  if (yo.status === 401) return sesionVencida();
  const persona = await yo.json();
  $("persona").textContent = `${persona.nombre} · ${persona.rol}`;
  puedeProponer = persona.permisos.includes("REVISAR_EFEMERIDES");
  $("franja-interruptor").hidden = !persona.sistema || persona.sistema.procesamiento_habilitado !== false;
  if (persona.permisos.includes("VALIDAR")) {
    $("franja-interruptor").textContent = "Procesamiento detenido: lo que se firme no entra en la "
      + "búsqueda hasta que se reanude. La conversación sigue funcionando.";
  }
  await Promise.all([cargarFiltros(), cargarHistorial()]);
  refrescarQuitar();
  const pedida = new URLSearchParams(location.search).get("conversacion");
  if (pedida) await abrir(pedida);
}

$("compositor").addEventListener("submit", enviar);
$("detener").addEventListener("click", () => detener?.abort());
$("pregunta").addEventListener("keydown", (evento) => {
  if (evento.key !== "Enter" || evento.shiftKey) return;
  evento.preventDefault();
  $("compositor").requestSubmit();
});
$("quitar-filtros").addEventListener("click", quitarFiltros);
$("filtros").addEventListener("change", refrescarQuitar);
$("nueva").addEventListener("click", () => { if (!ocupado) nueva(); });
$("reintentar-historial").addEventListener("click", () => cargarHistorial());
$("mas-historial").addEventListener("click", () => cargarHistorial(siguiente));
$("ir-historial").addEventListener("click", () => {
  $("plegar-historial").open = true;
  $("titulo-historial").focus();
});
// Abre el bloque «n Fuentes» de la última respuesta que tiene y le pasa el foco.
$("ir-fuentes").addEventListener("click", () => {
  const bloque = [...document.querySelectorAll(".fuentes-turno")].pop();
  if (!bloque) {
    $("estado").textContent = "Todavía no hay fuentes en esta conversación.";
    return;
  }
  bloque.open = true;
  bloque.querySelector("summary").focus();
  bloque.scrollIntoView({ block: "nearest" });
});
// Esc detiene la respuesta mientras se escribe (como «Detener»), salvo con el diálogo abierto.
document.addEventListener("keydown", (evento) => {
  if (evento.key === "Escape" && detener && !$("detener").hidden && !$("confirmar-relato").open) {
    detener.abort();
  }
});
// En celular el historial arranca plegado; en la computadora, siempre abierto.
$("plegar-historial").open = !celular.matches;
celular.addEventListener("change", () => { $("plegar-historial").open = !celular.matches; });
iniciar();
