// Sin `X-HV: 1` toda mutación responde 403: un formulario de otro sitio no puede ponerla
// (CSRF, DEC-37).
const CSRF = { "X-HV": "1" };
const ingreso = document.getElementById("ingreso");
const formIngreso = document.getElementById("form-ingreso");
const email = document.getElementById("email");
const errorIngreso = document.getElementById("error-ingreso");
const sesion = document.getElementById("sesion");
const quien = document.getElementById("quien");
const zona = document.getElementById("zona-carga");
const archivo = document.getElementById("archivo");
const aviso = document.getElementById("aviso-persona");
const errorCarga = document.getElementById("error-carga");
const avisoLista = document.getElementById("aviso-lista");
let permisos = []; // Los de la sesión (`/api/yo`); sin sesión, ninguno.

// Los cuatro roles del Director (DEC-124), como los nombra la SEU.
const NOMBRE_DEL_ROL = {
  rgc: "RGC",
  coordinador: "Coordinador de Extensión",
  director_carrera: "Director de Carrera",
  admin_tecnico: "Administrador técnico",
};

// El `?error=` con que vuelve el ingreso con Google (spec §3.3). Un código que no está acá no
// se muestra: la pantalla nunca repite lo que trae la URL.
const MENSAJE_DEL_INGRESO = {
  "dominio-no-permitido":
    "Esa cuenta no es de la FIE. Entrá con tu correo institucional (@fie.undef.edu.ar).",
  "persona-no-habilitada":
    "Tu cuenta es de la FIE pero todavía no está habilitada. Pedile el alta a la persona que " +
    "gestiona el acceso (el RGC; mientras no haya RGC, el Administrador técnico).",
  "cuenta-no-verificada":
    "Google no tiene verificado el correo de esa cuenta. Verificalo y probá de nuevo.",
  "acceso-cancelado": "Cancelaste el ingreso con Google. Podés probar de nuevo.",
  "intento-vencido": "El intento de ingreso venció o no es válido. Probá de nuevo.",
  "proveedor-no-disponible": "No pudimos conectar con Google. Probá de nuevo en un rato.",
  // No lo manda Google: lo pone esta página o validar.html ante un 401 (E3).
  "sesion-vencida": "Tu sesión venció por inactividad. Entrá de nuevo.",
};
const INGRESO = document.querySelector('meta[name="hv-ingreso"]').content;
document.getElementById("entrar-con-google").hidden = INGRESO !== "google";
formIngreso.hidden = INGRESO !== "dev";
if (INGRESO === "google") aviso.textContent = "Entrá con la cuenta de la FIE antes de subir una Pieza.";
const errorDeGoogle = new URLSearchParams(location.search).get("error");
if (Object.hasOwn(MENSAJE_DEL_INGRESO, errorDeGoogle)) {
  errorIngreso.textContent = MENSAJE_DEL_INGRESO[errorDeGoogle];
  errorIngreso.hidden = false;
}
// Leído el aviso, la URL vuelve a `/`: recargar no lo repite con la sesión abierta.
if (errorDeGoogle !== null) history.replaceState(null, "", "/");

// ── Lote (P3, mínimo de 026) ──────────────────────────────────────────────
const seccionLote = document.getElementById("seccion-lote");
const formLote = document.getElementById("form-lote");
const precisionLote = document.getElementById("lote-precision");
const hastaLote = document.getElementById("lote-hasta");
const errorLote = document.getElementById("error-lote");
const ACEPTA_SUELTA = archivo.accept;
const TOPE_DEL_LOTE = 100; // Fijo, sin variable de entorno (R-27).
let loteActivo = null;
let piezasDelLote = 0;

// Estado «Lote existente» de P3 (085): se llega con `?lote=` desde Lotes o desde el detalle.
// `progreso` es el de `GET /api/lotes/{id}`; un Lote recién creado no lo trae.
function mostrarLote(lote, progreso = null) {
  loteActivo = lote;
  piezasDelLote = progreso ? progreso.total : 0;
  document.getElementById("sin-lote").hidden = Boolean(lote);
  document.getElementById("con-lote").hidden = !lote;
  document.getElementById("rotulo-lote").textContent = progreso ? "Lote existente:" : "Cargando en el Lote";
  document.getElementById("lote-congelado").hidden = !lote || lote.contexto_editable !== false;
  pintarCupos();
  // Sin Lote también se sueltan varios: arman un Lote solo (151).
  document.getElementById("texto-zona").textContent = lote
    ? "Arrastrá archivos acá, o hacé clic para elegirlos"
    : "Arrastrá uno o varios archivos acá, o hacé clic para elegirlos";
  if (!lote) return;
  document.getElementById("lote-activo").textContent = `«${lote.nombre}»`;
  document.getElementById("ver-lote").href = `/lote.html?lote=${encodeURIComponent(lote.lote_id)}`;
  const heredados = [];
  if (lote.origen) heredados.push("el origen");
  if (lote.fecha_inicio) heredados.push("la Fecha");
  if (lote.propietario) heredados.push("el propietario");
  if (lote.carreras.length) heredados.push("las Carreras");
  // Un Lote armado al arrastrar varios archivos nace sin Contexto: se completa en el Lote (151).
  const faltan = [!lote.origen && "el origen", !lote.fecha_inicio && "la Fecha",
    !lote.propietario && "el propietario"].filter(Boolean);
  document.getElementById("lote-contexto").textContent = " · " + [
    lote.origen,
    lote.fecha_inicio && `${lote.fecha_inicio} a ${lote.fecha_fin}`,
    lote.propietario,
    heredados.length && `cada Pieza hereda ${enLista(heredados)}`,
    faltan.length && `completá ${enLista(faltan)} en el Lote mientras se leen`,
  ].filter(Boolean).join(" · ");
}

const enLista = (partes) => (partes.length === 1
  ? partes[0] : `${partes.slice(0, -1).join(", ")} y ${partes.at(-1)}`);

// Cuántas caben (R-27). Con el Lote lleno la zona de carga queda apagada; `enCola` son los
// archivos elegidos que todavía no subieron, para el aviso previo de los que no entran.
function pintarCupos(enCola = 0) {
  const caja = document.getElementById("lote-cupos");
  const caben = TOPE_DEL_LOTE - piezasDelLote;
  caja.hidden = !loteActivo || piezasDelLote === 0;
  caja.textContent = caben <= 0
    ? `Este Lote ya tiene ${TOPE_DEL_LOTE} Piezas.`
    : `Este Lote tiene ${piezasDelLote} de ${TOPE_DEL_LOTE} Piezas: caben ${caben} más.`
      + (enCola > caben ? " Creá otro Lote para el resto." : "");
  apagarZona(!permisos.includes("CARGAR") || (Boolean(loteActivo) && caben <= 0));
}

function apagarZona(apagada) {
  archivo.disabled = apagada;
  zona.classList.toggle("bloqueada", apagada);
  zona.setAttribute("aria-disabled", String(apagada));
}

// El total del Lote al momento de elegir: otra pestaña o persona pudo haber cargado.
async function refrescarCupos(elegidos) {
  try {
    const respuesta = await fetch(`/api/lotes/${encodeURIComponent(loteActivo.lote_id)}`);
    if (respuesta.ok) piezasDelLote = (await respuesta.json()).progreso.total;
  } catch { /* sin red: vale lo que ya se sabía; el servidor igual corta en el tope */ }
  pintarCupos(enCola() + elegidos);
}

// Los archivos elegidos que todavía no subieron.
const enCola = () => subidas.filter((s) => ["esperando", "subiendo"].includes(s.estado)).length;

// Las Carreras activas (GET /api/carreras), como casillas: son opcionales en el Lote.
async function cargarCarrerasDelLote() {
  const caja = document.getElementById("lote-carreras");
  let carreras = [];
  try {
    const respuesta = await fetch("/api/carreras");
    if (respuesta.ok) carreras = (await respuesta.json()).carreras;
  } catch {
    // Sin red: se crea el Lote sin Carreras.
  }
  if (!carreras.length) {
    const vacio = document.createElement("span");
    vacio.className = "aviso";
    vacio.textContent = "Todavía no hay carreras cargadas";
    caja.replaceChildren(vacio);
    return;
  }
  caja.replaceChildren(...carreras.map(({ carrera_id, nombre }) => {
    const rotulo = document.createElement("label");
    const casilla = document.createElement("input");
    casilla.type = "checkbox";
    casilla.value = carrera_id;
    rotulo.append(casilla, ` ${nombre}`);
    return rotulo;
  }));
}

// A qué caja apunta el `campo` del Problema que responde el servidor.
const CAJA_DEL_CAMPO = {
  nombre: "nombre", origen: "origen", fecha_inicio: "desde", precision_fecha: "desde",
  fecha_fin: "desde", evento: "evento", lugar: "lugar", propietario: "propietario",
  carreras: "carreras",
};
const CAJAS_DEL_LOTE = [
  "nombre", "origen", "desde", "hasta", "evento", "lugar", "propietario", "carreras",
];

function limpiarErroresDelLote() {
  for (const error of formLote.querySelectorAll(".error-campo")) {
    error.hidden = true;
    error.textContent = "";
  }
  for (const caja of formLote.querySelectorAll("[aria-invalid]")) {
    caja.removeAttribute("aria-invalid");
  }
}

// Muestra cada error bajo su caja y lleva el foco a la primera, en el orden del formulario.
function mostrarErroresDelLote(errores) {
  let primera = null;
  for (const caja of CAJAS_DEL_LOTE.filter((caja) => errores[caja])) {
    const error = document.getElementById(`error-lote-${caja}`);
    error.textContent = errores[caja];
    error.hidden = false;
    const control = caja === "carreras"
      ? document.querySelector("#lote-carreras input")
      : document.getElementById(`lote-${caja}`);
    if (!control) continue;
    control.setAttribute("aria-invalid", "true");
    control.setAttribute("aria-describedby", error.id);
    primera = primera || control;
  }
  if (primera) primera.focus();
  return Boolean(primera);
}

// El fin que corresponde a la precisión; el servidor lo verifica y normaliza el inicio
// (`derivar_rango`).
function finDe(desde, precision) {
  const [anio, mes] = desde.split("-").map(Number);
  const iso = (fecha) => fecha.toISOString().slice(0, 10);
  if (precision === "dia") return desde;
  if (precision === "mes") return iso(new Date(Date.UTC(anio, mes, 0)));
  if (precision === "anio") return `${anio}-12-31`;
  if (precision === "decada") return `${Math.floor(anio / 10) * 10 + 9}-12-31`;
  return hastaLote.value;
}

precisionLote.addEventListener("change", () => {
  const rango = precisionLote.value === "rango";
  hastaLote.hidden = !rango;
  hastaLote.required = rango;
  document.getElementById("rotulo-hasta").hidden = !rango;
});

formLote.addEventListener("submit", async (evento) => {
  evento.preventDefault();
  errorLote.hidden = true;
  limpiarErroresDelLote();
  const valor = (id) => document.getElementById(id).value.trim();
  const precision = precisionLote.value;
  const desde = valor("lote-desde");
  const faltan = {};
  if (!valor("lote-nombre")) faltan.nombre = "Escribí el nombre del Lote.";
  if (!valor("lote-origen")) faltan.origen = "Escribí de dónde vienen las Piezas.";
  if (!desde) faltan.desde = "Elegí la Fecha.";
  if (precision === "rango" && !valor("lote-hasta")) faltan.hasta = "Elegí hasta cuándo.";
  if (mostrarErroresDelLote(faltan)) return;
  const pedido = {
    nombre: valor("lote-nombre"),
    origen: valor("lote-origen"),
    fecha_inicio: desde,
    precision_fecha: precision,
    fecha_fin: finDe(desde, precision),
  };
  for (const campo of ["evento", "lugar", "propietario"]) {
    if (valor(`lote-${campo}`)) pedido[campo] = valor(`lote-${campo}`);
  }
  pedido.carreras = [...document.querySelectorAll("#lote-carreras input:checked")]
    .map((casilla) => casilla.value);
  try {
    const respuesta = await fetch("/api/lotes", {
      method: "POST",
      headers: { ...CSRF, "Content-Type": "application/json" },
      body: JSON.stringify(pedido),
    });
    const cuerpo = await respuesta.json().catch(() => ({}));
    if (respuesta.status === 401) return sesionVencida();
    if (respuesta.ok) {
      mostrarLote(cuerpo.lote);
      return;
    }
    const caja = cuerpo.campo === "fecha_fin" && precision === "rango"
      ? "hasta" : CAJA_DEL_CAMPO[cuerpo.campo];
    if (caja && mostrarErroresDelLote({ [caja]: cuerpo.detail || "Revisá este campo." })) return;
    errorLote.textContent = cuerpo.detail || "No se pudo crear el Lote. Probá de nuevo.";
  } catch {
    errorLote.textContent = "No se pudo crear el Lote. Probá de nuevo.";
  }
  errorLote.hidden = false;
});

document.getElementById("soltar-lote").addEventListener("click", () => mostrarLote(null));

// Cada rol ve solo lo suyo (DEC-124): los permisos los decide el servidor, no esta pantalla,
// que solo oculta lo que el servidor rechazaría.
function habilitarSesion(habilitada, permisosDeLaSesion = []) {
  permisos = permisosDeLaSesion;
  const puedeCargar = permisos.includes("CARGAR");
  seccionLote.hidden = !puedeCargar;
  if (seccionLote.hidden) mostrarLote(null);
  else cargarCarrerasDelLote();
  // Sin sesión la zona se ve bloqueada y pide entrar; con sesión, solo la ve quien carga.
  document.getElementById("seccion-carga").hidden = habilitada && !puedeCargar;
  apagarZona(!puedeCargar);
  document.getElementById("enlace-lotes").hidden = !puedeCargar;
  aviso.hidden = habilitada;
  document.getElementById("seccion-piezas").hidden = !habilitada || !trabajaPiezas();
  // «Todas» suma lo de las demás personas y los Aportes: es para tomar trabajo ajeno.
  document.getElementById("filtro-piezas").hidden = !permisos.includes("VALIDAR");
  // Mediciones solo con OPERAR_SISTEMA; Administración, con alguna de sus pestañas.
  for (const sigue of document.querySelectorAll(".sigue")) sigue.hidden = !puedeCargar;
  const operaElSistema = permisos.includes("OPERAR_SISTEMA");
  document.getElementById("seccion-mediciones").hidden = !operaElSistema;
  document.getElementById("enlace-admin").hidden = !operaElSistema && !permisos.includes("GESTIONAR_PERSONAS");
}

// Quién entró lo dice el servidor (`/api/yo`): la sesión vive en una cookie que esta
// página no lee.
async function cargarSesion() {
  let yo = null;
  try {
    const respuesta = await fetch("/api/yo");
    if (respuesta.ok) yo = await respuesta.json();
  } catch {
    // Sin red: se ofrece entrar.
  }
  ingreso.hidden = Boolean(yo);
  sesion.hidden = !yo;
  habilitarSesion(Boolean(yo), yo ? yo.permisos : []);
  document.getElementById("franja-interruptor").hidden =
    !yo || yo.sistema.procesamiento_habilitado !== false;
  yoActual = yo;
  pausado = Boolean(yo) && yo.sistema.procesamiento_habilitado === false;
  if (!yo) {
    clearTimeout(sondeo);
    vaciarGrupos();
    firmaDeLaLista = null;
    return;
  }
  quien.textContent = `${yo.nombre} — ${NOMBRE_DEL_ROL[yo.rol] || yo.rol}`;
  // Se le cerró la sesión editando una Ficha (validar.html guardó lo editado): vuelve a
  // ella. Si entró otra persona, lo editado no es suyo y se descarta.
  try {
    const borrador = JSON.parse(sessionStorage.getItem("hv_borrador"));
    if (borrador && borrador.usuario === yo.usuario_id) {
      location.href = `/validar.html?pieza=${encodeURIComponent(borrador.pieza)}`;
      return;
    }
    sessionStorage.removeItem("hv_borrador");
  } catch { /* sin almacenamiento: no hay nada que retomar */ }
  await retomarLote();
  // «+ Nuevo Lote» de Lotes (P10) llega con `#seccion-lote`, que recién ahora se ve.
  if (location.hash === "#seccion-lote" && !seccionLote.hidden) seccionLote.scrollIntoView();
  await cargarPiezas({ cargando: true });
}

// «Cargar más archivos» de Lotes (P10) y del detalle del Lote (P4) vuelven con `?lote=`: el
// Contexto queda en solo lectura y se va directo al paso 2, la zona de carga (085).
async function retomarLote() {
  const loteId = new URLSearchParams(location.search).get("lote");
  if (!loteId || !permisos.includes("CARGAR")) return;
  try {
    const respuesta = await fetch(`/api/lotes/${encodeURIComponent(loteId)}`);
    if (!respuesta.ok) return;
    const { lote, progreso } = await respuesta.json();
    mostrarLote(lote, progreso);
    zona.scrollIntoView({ block: "center" });
  } catch { /* sin red: se carga suelta */ }
}

// Un 401 en una acción: la sesión venció (E3). Se queda en esta página y no recarga, para
// que lo escrito (el formulario del Lote) siga ahí al volver a entrar con `dev`.
async function sesionVencida() {
  errorIngreso.textContent = MENSAJE_DEL_INGRESO["sesion-vencida"];
  errorIngreso.hidden = false;
  await cargarSesion();
}

async function entrar(evento) {
  evento.preventDefault();
  errorIngreso.hidden = true;
  let detalle = null;
  try {
    const respuesta = await fetch("/auth/dev", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify({ email: email.value }),
    });
    if (respuesta.ok) {
      await cargarSesion();
      return;
    }
    detalle = (await respuesta.json().catch(() => ({}))).detail;
  } catch {
    // No llegó al servidor: vale el texto genérico.
  }
  errorIngreso.textContent = detalle || "No se pudo entrar. Probá de nuevo.";
  errorIngreso.hidden = false;
}

async function salir() {
  try {
    await fetch("/auth/salir", { method: "POST", headers: CSRF });
  } catch {
    // Sin red: la página igual vuelve a preguntar quién sos al servidor.
  }
  await cargarSesion();
}

// ── Mis Piezas (P2, 087) ───────────────────────────────────────────────────
// Cinco grupos (DEC-123): lo que espera una persona, lo que el motor está leyendo, lo que
// falló y pide una decisión, lo que nadie leyó todavía (video, completada a mano) y lo
// Validado que aún no se puede buscar (DEC-146 punto 6). Una Validada ya buscable no se lista.
const GRUPOS = ["para-validar", "procesando", "fallaron", "analisis-humano", "no-indexadas"];

const NOMBRE_DEL_CAMPO = {
  tipo: "Tipo de Pieza",
  tipo_actividad: "Tipo de actividad",
  fecha_inicio: "Fecha",
  fecha_fin: "Fecha",
  precision_fecha: "Fecha",
  origen: "Origen",
  propietario: "Propietario",
  resumen: "Resumen",
  archivo_original: "Archivo original",
};
const faltan = (campos) => [...new Set(campos.map((c) => NOMBRE_DEL_CAMPO[c] || c))].join(", ");

function grupoDe(pieza) {
  if (pieza.estado === "Recibida") return pieza.fallida ? "fallaron" : "procesando";
  if (pieza.estado === "Validada") return pieza.no_indexada ? "no-indexadas" : null;
  return pieza.requiere_analisis_humano ? "analisis-humano" : "para-validar";
}

// Mis Piezas es de quien carga o valida: el Administrador técnico solo tiene el enlace de las
// mediciones y el Director de Carrera consulta desde Conversar (spec P2, DEC-124).
const trabajaPiezas = () => permisos.includes("CARGAR") || permisos.includes("VALIDAR");

// «Completar a mano» y «Reintentar» los pide quien subió la Pieza o el RGC, con CARGAR (R-06):
// el servidor responde 403 a cualquier otro, así que a cualquier otro no se le ofrecen.
const decideSobre = (pieza) => permisos.includes("CARGAR")
  && (pieza.creada_por === yoActual?.usuario_id || yoActual?.rol === "rgc");

function enlaceAFicha(piezaId, texto, clase) {
  const enlace = document.createElement("a");
  enlace.href = `/validar.html?pieza=${encodeURIComponent(piezaId)}`;
  enlace.className = clase;
  enlace.textContent = texto;
  return enlace;
}

function filaDePieza(pieza) {
  const fila = document.createElement("li");
  const marca = document.createElement("span");
  marca.className = "marca";
  const nombre = document.createElement("span");
  nombre.className = "nombre";
  // Un Aporte no tiene archivo ni quién lo subió: se nombra por su origen (spec P2).
  const titulo = pieza.creada_por === null ? "Aporte de Agente" : pieza.nombre_archivo;
  nombre.textContent = pieza.resumen ? `${titulo} · «${pieza.resumen}»` : titulo;
  const accion = document.createElement("span");
  accion.className = "accion";
  if (pieza.alerta_inyeccion) nombre.append(" ", marcaDeAlerta());
  fila.append(marca, nombre, accion);
  marcarFila(pieza, fila, marca, accion);
  return fila;
}

// «Esperando al motor» no es «falló»: el trabajo sigue encolado y se retoma solo, sin botones
// (DEC-146 punto 16, recorrido R4). La fila no dice si el trabajo está encolado o en curso:
// con el freno puesto o el motor caído toda Recibida no fallida espera al motor (084).
function marcarFila(pieza, fila, marca, accion) {
  const validar = permisos.includes("VALIDAR");
  marca.className = `marca estado-${pieza.estado}`;
  if (pieza.estado === "Recibida" && pieza.fallida) {
    marca.className = "marca error";
    marca.textContent = "● Recibida · falló. No se pudo procesar este archivo.";
    if (decideSobre(pieza)) accion.replaceChildren(...accionesDeFallida(pieza.pieza_id, fila, PANTALLA));
    return;
  }
  if (pieza.estado === "Recibida") {
    marca.textContent = pausado
      ? "● Recibida · esperando al motor (procesamiento detenido)"
      : motorCaido ? "● Recibida · esperando al motor. Se reintenta sola." : "● Recibida, procesando";
    // Los campos duros no los propone ningún modelo: se completan mientras se lee (152).
    if (validar) accion.append(enlaceAFicha(pieza.pieza_id, "Completar mientras se lee", "enlace"));
    return;
  }
  if (pieza.estado === "Validada") {
    marca.textContent = "✓ Validada · No indexada";
    accion.append(enlaceAFicha(pieza.pieza_id, "Ver la Ficha", "enlace"));
    fila.querySelector(".nombre").append(" Todavía no se puede buscar.");
    return;
  }
  if (pieza.estado === "Incompleta") {
    marca.textContent = `◐ Incompleta · Falta: ${faltan(pieza.campos_faltantes)}`;
  } else {
    marca.textContent = pieza.requiere_analisis_humano ? "▶ Requiere análisis humano" : "○ Pendiente";
  }
  // Sin VALIDAR toda Ficha se abre en solo lectura (DEC-124, R-05).
  const texto = pieza.estado === "Incompleta"
    ? "Continuar" : pieza.requiere_analisis_humano ? "Analizar" : "Validar";
  accion.append(validar
    ? enlaceAFicha(pieza.pieza_id, texto, "boton")
    : enlaceAFicha(pieza.pieza_id, "Ver la Ficha", "enlace"));
}

// Lo que Reintentar y Completar a mano (`piezas.js`) hacen después en esta pantalla.
const PANTALLA = {
  recargar: () => cargarPiezas(),
  avisar: (texto) => anunciar(texto),
  sesionVencida: () => sesionVencida(),
};

// Un `aria-live` no vuelve a anunciar el mismo texto: se vacía y se repone en otro momento.
function anunciar(texto) {
  avisoLista.textContent = "";
  setTimeout(() => { avisoLista.textContent = texto; }, 100);
}

let filtro = "mias";
let firmaDeLaLista = null; // qué se pintó la última vez, para anunciar solo un cambio real
let motorCaido = false;
let pausado = false;
let yoActual = null;
let saludLeidaEl = 0;

for (const opcion of document.querySelectorAll('input[name="filtro"]')) {
  opcion.addEventListener("change", () => {
    filtro = opcion.value;
    firmaDeLaLista = null;
    cargarPiezas({ cargando: true });
  });
}
document.getElementById("lista-reintentar").addEventListener("click", () => cargarPiezas({ cargando: true }));

const VACIO_MIAS = document.getElementById("lista-vacia").textContent;

function estadoDeLista(estado) {
  // Con «Todas» no tiene sentido mandar a «Todas».
  document.getElementById("lista-vacia").textContent =
    filtro === "mias" ? VACIO_MIAS : "No hay Piezas para validar.";
  document.getElementById("lista-cargando").hidden = estado !== "cargando";
  document.getElementById("lista-error").hidden = estado !== "error";
  document.getElementById("lista-vacia").hidden = estado !== "vacia";
  document.getElementById("grupos").hidden = estado !== "lista";
}

function vaciarGrupos() {
  for (const grupo of GRUPOS) {
    document.getElementById(grupo).replaceChildren();
    document.getElementById(grupo).hidden = true;
    document.getElementById(`titulo-${grupo}`).hidden = true;
  }
}

function pintarGrupos(piezas) {
  const porGrupo = Object.fromEntries(GRUPOS.map((g) => [g, []]));
  for (const pieza of piezas) {
    const grupo = grupoDe(pieza);
    if (grupo) porGrupo[grupo].push(pieza);
  }
  for (const grupo of GRUPOS) {
    const lista = document.getElementById(grupo);
    const titulo = document.getElementById(`titulo-${grupo}`);
    lista.replaceChildren(...porGrupo[grupo].map(filaDePieza));
    lista.hidden = titulo.hidden = porGrupo[grupo].length === 0;
    titulo.textContent = `${titulo.textContent.replace(/ \(\d+\)$/, "")} (${porGrupo[grupo].length})`;
  }
  return GRUPOS.reduce((n, g) => n + porGrupo[g].length, 0);
}

// El procesamiento tarda minutos y no empuja eventos: la pantalla sondea mientras haya algo
// leyéndose (una fallida espera una decisión, no al motor). Con «No indexadas» relee cada 10 s; sin nada en curso, no consulta.
const MS_ENTRE_SONDEOS = 3000;
const MS_ENTRE_SALUDES = 10000;
let sondeo = null;
let enVuelo = false;
let otraVez = false; // un pedido que llegó con otro en vuelo (el filtro cambió, Reintentar)

async function cargarPiezas({ cargando = false } = {}) {
  clearTimeout(sondeo);
  if (!trabajaPiezas()) return;
  if (enVuelo) {
    otraVez = true;
    return;
  }
  enVuelo = true;
  if (cargando) estadoDeLista("cargando");
  const pedido = filtro; // la respuesta de un filtro que ya no está puesto se descarta
  try {
    // shortcut: «Todas» trae todo el Acervo en cada sondeo y descarta acá las Validadas ya
    // indexadas; alcanza con cientos de Piezas. Con miles, filtrar en el servidor (paginación
    // con `limite` y `cursor`, o `estado` sin las Validadas indexadas).
    const respuesta = await fetch(pedido === "mias" ? "/api/piezas?mias=true" : "/api/piezas");
    if (respuesta.status === 401) return sesionVencida();
    if (!respuesta.ok) throw new Error(`/api/piezas respondió ${respuesta.status}`);
    const { piezas } = await respuesta.json();
    if (pedido !== filtro) return;
    const esperan = piezas.some((p) => p.estado === "Recibida" && !p.fallida);
    const visibles = piezas.filter((p) => grupoDe(p));
    // Una «No indexada» se va sola cuando termina la indexación: se sondea más despacio.
    const sinIndexar = visibles.some((p) => p.no_indexada);
    // El freno y el motor cambian sin avisar: se releen con el sondeo, cada `MS_ENTRE_SALUDES`.
    if (esperan && Date.now() - saludLeidaEl >= MS_ENTRE_SALUDES) await revisarSalud();
    if (pedido !== filtro) return;
    const firma = JSON.stringify(visibles.map((p) => [p.pieza_id, p.estado, p.fallida, p.no_indexada]));
    // Un único aviso, para toda la lista y solo si cambió desde el sondeo anterior (spec P2).
    if (firmaDeLaLista !== null && firma !== firmaDeLaLista) anunciar("Se actualizó la lista");
    firmaDeLaLista = firma;
    estadoDeLista(pintarGrupos(visibles) ? "lista" : "vacia");
    // Los Lotes son aparte: si no se pudieron leer, la lista de Piezas sigue.
    const lotesEsperan = await cargarMisLotes().catch(() => false);
    if (esperan || lotesEsperan || sinIndexar) {
      sondeo = setTimeout(cargarPiezas, esperan || lotesEsperan ? MS_ENTRE_SONDEOS : MS_ENTRE_SALUDES);
    }
  } catch {
    // Un sondeo que falla con la lista ya pintada la deja como está y vuelve a probar.
    if (firmaDeLaLista === null || pedido !== filtro) estadoDeLista("error");
    else sondeo = setTimeout(cargarPiezas, MS_ENTRE_SONDEOS);
  } finally {
    enVuelo = false;
    if (otraVez) {
      otraVez = false;
      cargarPiezas();
    }
  }
}

// Los contadores de cada Lote propio que todavía tiene Piezas sin validar (historia 72) y la
// espera estimada (086): los que procesan, con el `progreso` de su detalle; los que esperan
// validación no cambian solos y se pintan con su fila de la lista. Devuelve si alguno tiene
// Recibidas leyéndose, para seguir sondeando.
// shortcut: un `GET /api/lotes/{id}` por Lote Procesando en cada sondeo; alcanza con unos
// pocos a la vez.
async function cargarMisLotes() {
  const lista = document.getElementById("mis-lotes");
  const titulo = document.getElementById("titulo-mis-lotes");
  if (!permisos.includes("CARGAR")) {
    lista.hidden = titulo.hidden = true;
    return false;
  }
  const respuesta = await fetch("/api/lotes?creado_por=yo");
  if (!respuesta.ok) return false;
  const abiertos = (await respuesta.json()).lotes
    .filter((lote) => lote.estado === "Procesando" || lote.estado === "En validación");
  const vistas = (await Promise.all(abiertos.map((lote) => (lote.estado === "Procesando"
    ? fetch(`/api/lotes/${encodeURIComponent(lote.lote_id)}`).then((r) => (r.ok ? r.json() : null))
    : { lote, progreso: lote })))).filter(Boolean);
  lista.replaceChildren(...vistas.map(filaDeLote));
  lista.hidden = titulo.hidden = vistas.length === 0;
  return vistas.some(({ progreso }) => progreso.por_estado.Recibida > progreso.fallidas);
}

function filaDeLote({ lote, progreso }) {
  const fila = document.createElement("li");
  const marca = document.createElement("span");
  marca.className = "marca";
  marca.textContent = progreso.estado;
  const n = progreso.por_estado;
  const nombre = document.createElement("span");
  nombre.className = "nombre";
  nombre.textContent = [
    `${lote.nombre}: Recibidas ${n.Recibida} · Pendientes ${n.Pendiente}`
      + ` · Incompletas ${n.Incompleta} · Validadas ${n.Validada}`,
    progreso.fallidas && `${progreso.fallidas} ${progreso.fallidas === 1 ? "falló" : "fallaron"}`,
    textoDeEspera(n.Recibida - progreso.fallidas, progreso.estimacion),
  ].filter(Boolean).join(" · ");
  const accion = document.createElement("span");
  accion.className = "accion";
  const ver = document.createElement("a");
  ver.className = "enlace";
  ver.href = `/lote.html?lote=${encodeURIComponent(lote.lote_id)}`;
  ver.textContent = "Ver el Lote";
  accion.append(ver);
  fila.append(marca, nombre, accion);
  return fila;
}

async function revisarSalud() {
  saludLeidaEl = Date.now();
  try {
    const [salud, yo] = await Promise.all([
      fetch("/api/salud").then((r) => r.json()),
      fetch("/api/yo").then((r) => (r.ok ? r.json() : null)),
    ]);
    motorCaido = salud.estado !== "ok";
    document.getElementById("franja-degradado").hidden = !motorCaido;
    if (yo) {
      pausado = yo.sistema.procesamiento_habilitado === false;
      document.getElementById("franja-interruptor").hidden = !pausado;
    }
  } catch {
    // La salud es informativa: si no contesta, no se avisa nada.
  }
}

let subiendoSueltas = 0;  // las que van por `fetch`; las filas de abajo se cuentan aparte

async function subir(elegido) {
  if (!elegido || archivo.disabled) return;
  errorCarga.hidden = true;
  subiendoSueltas += 1;
  try {
    await subirSuelta(elegido);
  } finally {
    subiendoSueltas -= 1;
  }
}

async function subirSuelta(elegido) {
  // La fila aparece al instante: la persona no espera a que el servidor conteste (US 1, 2).
  const fila = filaDePieza({
    pieza_id: null, estado: "Recibida", nombre_archivo: elegido.name, resumen: null,
    creada_por: yoActual?.usuario_id, fallida: false,
  });
  const procesando = document.getElementById("procesando");
  procesando.prepend(fila);
  procesando.hidden = document.getElementById("titulo-procesando").hidden = false;
  estadoDeLista("lista");
  const datos = new FormData();
  // `lote_id` va antes que el archivo: el servidor lo necesita antes de guardarlo.
  if (loteActivo) datos.append("lote_id", loteActivo.lote_id);
  datos.append("archivo", elegido);
  let detalle = null;
  try {
    const respuesta = await fetch("/api/piezas", { method: "POST", headers: CSRF, body: datos });
    if (respuesta.ok) {
      // La Pieza ya está en el servidor y se está procesando: desde acá la fila la pinta
      // la lista, y el sondeo la lleva sola de Recibida a Pendiente (user story 3).
      await cargarPiezas();
      return;
    }
    if (respuesta.status === 401) {
      fila.remove();
      return sesionVencida();
    }
    detalle = (await respuesta.json().catch(() => ({}))).detail;
  } catch {
    // No llegó al servidor: vale el texto genérico.
  }
  fila.remove();
  errorCarga.textContent = detalle || "No se pudo subir el archivo. Probá de nuevo.";
  errorCarga.hidden = false;
}

// ── Carga múltiple en el Lote (P3 paso 2, 083) ─────────────────────────────
// Un pedido por archivo, de a dos (DEC-20): lo que se rechaza o se repite no frena al resto.
// XMLHttpRequest y no fetch: fetch no informa el avance de la subida.
const SUBIDAS_EN_PARALELO = 2;
const cargaLote = document.getElementById("carga-lote");
const listaSubidas = document.getElementById("subidas");
const cancelarFalta = document.getElementById("cancelar-falta");
const reintentarFallidos = document.getElementById("reintentar-fallidos");
let subidas = [];
let enEspera = [];

// Nunca «0 MB» para un archivo que pesa algo.
const enMB = (bytes) => `${Math.max(bytes / 1048576, bytes ? 0.1 : 0)
  .toLocaleString("es-AR", { maximumFractionDigits: 1 })} MB`;
const esVideo = (elegido) =>
  elegido.type.startsWith("video/") || /\.(mp4|mov|webm)$/i.test(elegido.name);

// El video se mide en GB binarios, como su tope (2 GiB es «2 GB», DEC-15).
const GiB = 2 ** 30;
const TOPE_VIDEO_BYTES = 2 * GiB;
const TOPE_VIDEO_S = 3 * 60 * 60;
const decimal = (n) => n.toLocaleString("es-AR", { maximumFractionDigits: 1 });
// Hacia arriba: 2 GiB y un poco nunca se lee «2 GB», como el tope.
const enGB = (bytes) => `${decimal(Math.ceil((bytes / GiB) * 10) / 10)} GB`;
const pesaDemasiado = (bytes, tope) =>
  `Rechazado: pesa ${enGB(bytes)} y el máximo para video es ${enGB(tope)}`;

// Un video suelto (Mis Piezas) también va por estas filas: barra, «Cancelar» y corte (R3).
function cargarConFilas(elegidos) {
  if (!elegidos.length || archivo.disabled) return;
  for (const elegido of elegidos) {
    const fila = document.createElement("li");
    const nombre = document.createElement("span");
    nombre.className = "nombre";
    nombre.textContent = elegido.name;
    const tamano = document.createElement("span");
    tamano.className = "tamano";
    tamano.textContent = elegido.size >= GiB ? enGB(elegido.size) : enMB(elegido.size);
    const barra = document.createElement("progress");
    barra.max = 100;
    barra.value = 0;
    barra.setAttribute("aria-label", `Avance de ${elegido.name}`);
    const marca = document.createElement("span");
    marca.className = "marca";
    fila.append(nombre, tamano, barra, marca);
    const subida = {
      elegido, loteId: loteActivo ? loteActivo.lote_id : null, fila, barra, marca, xhr: null,
    };
    if (esVideo(elegido)) {
      subida.cancelar = document.createElement("button");
      subida.cancelar.type = "button";
      subida.cancelar.className = "boton-secundario cancelar";
      subida.cancelar.textContent = "Cancelar";
      subida.cancelar.setAttribute("aria-label", `Cancelar ${elegido.name}`);
      subida.cancelar.addEventListener("click", () => { cancelar(subida); resumir(); });
      fila.append(subida.cancelar);
    }
    listaSubidas.append(fila);
    subidas.push(subida);
    // El tope lo controla el servidor; acá solo se evita subir 2 GB para oír que no entran.
    if (esVideo(elegido) && elegido.size > TOPE_VIDEO_BYTES) {
      pintar(subida, "rechazada", pesaDemasiado(elegido.size, TOPE_VIDEO_BYTES));
    } else {
      esperar(subida);
    }
  }
  cargaLote.hidden = false;
  avanzar();
}

// `estado`: esperando, subiendo, recibida, duplicada, rechazada, sin-subir, sin-lugar,
// cancelada.
const SIN_BARRA = ["rechazada", "sin-subir", "sin-lugar", "cancelada"];
function pintar(subida, estado, texto, ...acciones) {
  subida.estado = estado;
  subida.marca.className = `marca${["rechazada", "sin-subir"].includes(estado) ? " error" : ""}`;
  subida.marca.replaceChildren(texto, ...acciones);
  if (["recibida", "duplicada"].includes(estado)) subida.barra.value = 100;
  // Una barra llena en un rechazo diría que subió.
  subida.barra.hidden = SIN_BARRA.includes(estado);
  if (subida.cancelar) subida.cancelar.hidden = !["esperando", "subiendo"].includes(estado);
}

// Vuelve a la fila después de `segundos`, si nadie la canceló mientras tanto.
function reencolarEn(subida, segundos, texto) {
  pintar(subida, "esperando", texto);
  setTimeout(() => {
    if (subida.estado !== "esperando") return;
    esperar(subida);
    avanzar();
  }, segundos * 1000);
}

function esperar(subida, texto = "Esperando") {
  pintar(subida, "esperando", texto);
  enEspera.push(subida);
}

function avanzar() {
  while (subidas.filter((s) => s.estado === "subiendo").length < SUBIDAS_EN_PARALELO
         && enEspera.length) {
    enviar(enEspera.shift());
  }
  resumir();
}

function enviar(subida) {
  const { elegido, loteId } = subida;
  const xhr = new XMLHttpRequest();
  subida.xhr = xhr;
  let cuerpo = elegido;
  if (esVideo(elegido)) {
    // El video va crudo, sin multipart (`POST /api/piezas/video`).
    xhr.open("POST", `/api/piezas/video?nombre=${encodeURIComponent(elegido.name)}`
      + (loteId ? `&lote_id=${encodeURIComponent(loteId)}` : ""));
  } else {
    cuerpo = new FormData();
    // `lote_id` va antes que el archivo: el servidor lo necesita antes de guardarlo.
    cuerpo.append("lote_id", loteId);
    cuerpo.append("archivo", elegido);
    xhr.open("POST", "/api/piezas");
  }
  for (const [cabecera, valor] of Object.entries(CSRF)) xhr.setRequestHeader(cabecera, valor);
  subida.subido = false;
  subida.terminando = false;
  // El cuerpo llegó entero: desde acá cancelar ya no corta nada, el servidor lo está guardando.
  xhr.upload.onload = () => { subida.subido = true; };
  xhr.upload.onprogress = (evento) => {
    if (!evento.lengthComputable || subida.estado !== "subiendo" || subida.terminando) return;
    const porcentaje = Math.floor((evento.loaded / evento.total) * 100);
    subida.barra.value = porcentaje;
    // Un video tarda minutos: «212 de 480 MB» dice cuánto falta (R3).
    subida.marca.textContent = esVideo(elegido)
      ? `${enMB(evento.loaded)} de ${enMB(evento.total)}`
      : `Subiendo ${porcentaje} %`;
  };
  xhr.onload = () => {
    let respuesta = {};
    try { respuesta = JSON.parse(xhr.responseText); } catch { /* sin cuerpo JSON */ }
    terminar(subida, xhr.status, respuesta, Number(xhr.getResponseHeader("Retry-After")));
  };
  xhr.onerror = () => terminar(subida, 0, {});
  pintar(subida, "subiendo", "Subiendo 0 %");
  subida.barra.value = 0;
  xhr.send(cuerpo);
}

function botonReintentar(subida) {
  const boton = document.createElement("button");
  boton.type = "button";
  boton.className = "boton-secundario";
  boton.textContent = "Reintentar";
  boton.addEventListener("click", () => {
    subida.intentos = 0;
    esperar(subida);
    avanzar();
  });
  return boton;
}

const REINTENTOS_AUTOMATICOS = 3;

function terminar(subida, codigo, respuesta, reintentarEn) {
  if (subida.estado !== "subiendo") return;  // cancelada mientras subía
  // Sin `ffprobe` reintentar no lo arregla.
  const sinFfprobe = respuesta.motivo === "sin-ffprobe";
  const quedo = codigo === 200 || codigo === 201 || respuesta.type === "/errores/pieza-duplicada";
  if (subida.terminando && !quedo) {
    // Se canceló cuando ya había salido: si no quedó en el servidor, no se vuelve a subir.
    pintar(subida, "cancelada", "Cancelado");
  } else if (codigo === 200 || codigo === 201) {
    if (respuesta.procesamiento_pausado) document.getElementById("aviso-pausado").hidden = false;
    // 200: el mismo archivo ya estaba en este Lote; no cuenta como nuevo (DEC-20).
    if (respuesta.duplicada) pintar(subida, "duplicada", "Ya estaba en este Lote");
    else pintar(subida, "recibida", "Recibida");
    if (subida.loteId && loteActivo && subida.loteId === loteActivo.lote_id && !respuesta.duplicada) {
      piezasDelLote += 1;
      pintarCupos(enCola());
    }
  } else if (respuesta.type === "/errores/pieza-duplicada") {
    const enlace = document.createElement("a");
    enlace.className = "enlace";
    enlace.href = `/validar.html?pieza=${encodeURIComponent(respuesta.pieza_id)}`;
    enlace.textContent = "Ver la Pieza";
    pintar(subida, "duplicada", "Ya existe en el Acervo ", enlace);
  } else if (respuesta.type === "/errores/lote-lleno") {
    // Lo que espera tampoco entraría: se corta la tanda sin subirlo.
    pintar(subida, "rechazada", `Rechazado: ${respuesta.detail}`);
    enEspera = [];
    for (const otra of subidas.filter((s) => s.estado === "esperando")) {
      pintar(otra, "sin-lugar", "No entró: el Lote está lleno");
    }
    // El servidor dice que está lleno: el aviso y la zona apagada lo reflejan.
    if (loteActivo && subida.loteId === loteActivo.lote_id) {
      piezasDelLote = TOPE_DEL_LOTE;
      pintarCupos();
    }
  } else if (codigo === 0 && esVideo(subida.elegido)) {
    // Un corte no se retoma: el servidor no guardó nada y hay que mandarlo entero (R3, E4).
    pintar(subida, "sin-subir", "Se cortó la subida. Hay que empezar de nuevo ",
      botonReintentar(subida));
  } else if (codigo === 429) {
    // `demasiadas-subidas`: no es un rechazo; vuelve a la fila después de esperar.
    reencolarEn(subida, reintentarEn || 5, "Esperá: hay demasiadas subidas en curso");
  } else if ((codigo === 0 || codigo === 503) && !sinFfprobe && !esVideo(subida.elegido)
             && (subida.intentos || 0) < REINTENTOS_AUTOMATICOS) {
    // Un video no se reintenta solo: serían hasta 2 GB otra vez.
    subida.intentos = (subida.intentos || 0) + 1;
    const espera = 2 ** (subida.intentos - 1);  // 1, 2 y 4 s
    reencolarEn(subida, espera, `No se pudo subir; reintento ${subida.intentos} de `
      + `${REINTENTOS_AUTOMATICOS} en ${espera} s`);
  } else if (codigo === 0 || (codigo >= 500 && !sinFfprobe)) {
    const porque = codigo === 0 ? " (sin conexión)" : `: ${respuesta.detail || "falló el servidor"}`;
    pintar(subida, "sin-subir", `No se pudo subir${porque}`, botonReintentar(subida));
  } else if (codigo === 401) {
    // La sesión venció: no es un rechazo del archivo; se reintenta después de volver a entrar.
    pintar(subida, "sin-subir", "No se pudo subir: tu sesión venció. Volvé a entrar y reintentá.",
      botonReintentar(subida));
  } else if (codigo === 413 && esVideo(subida.elegido)) {
    pintar(subida, "rechazada",
      pesaDemasiado(subida.elegido.size, respuesta.tope_bytes || TOPE_VIDEO_BYTES));
  } else if (respuesta.type === "/errores/video-demasiado-largo") {
    const horas = (respuesta.tope_segundos || TOPE_VIDEO_S) / 3600;
    pintar(subida, "rechazada",
      `Rechazado: el video dura más de lo permitido (máximo ${horas} h)`);
  } else if (codigo === 413) {
    const tope = respuesta.tope_bytes ? ` y el máximo es ${enMB(respuesta.tope_bytes)}` : "";
    pintar(subida, "rechazada", `Rechazado: pesa ${enMB(subida.elegido.size)}${tope}`);
  } else if (codigo === 415 && !respuesta.motivo) {
    pintar(subida, "rechazada", "Rechazado: ese tipo de archivo no se admite");
  } else {
    pintar(subida, "rechazada", `Rechazado: ${respuesta.detail || "el servidor no lo aceptó"}`);
  }
  avanzar();
}

const plural = (n, palabra) => `${n} ${palabra}${n === 1 ? "" : "s"}`;

function resumir() {
  const cuantas = (estado) => subidas.filter((s) => s.estado === estado).length;
  const faltan = cuantas("esperando") + cuantas("subiendo");
  const terminadas = subidas.length - faltan;
  const rechazadas = plural(cuantas("rechazada"), "rechazado");
  const duplicadas = plural(cuantas("duplicada"), "duplicado");
  const resumen = document.getElementById("resumen-carga");
  if (faltan) {
    resumen.textContent = `Subiendo ${terminadas} de ${subidas.length} · ${rechazadas} · ${duplicadas}`;
  } else {
    const sinLugar = cuantas("sin-lugar");
    const noEntraron = !sinLugar ? "" : sinLugar === 1 ? ", 1 no entró" : `, ${sinLugar} no entraron`;
    const canceladas = cuantas("cancelada") ? `, ${plural(cuantas("cancelada"), "cancelado")}` : "";
    resumen.textContent = `${plural(cuantas("recibida"), "recibido")}, ${rechazadas}, `
      + `${duplicadas}, ${cuantas("sin-subir")} sin subir${noEntraron}${canceladas}`;
  }
  // Los botones de la tanda son del Lote; un video suelto tiene los suyos en la fila.
  const deLote = subidas.some((s) => s.loteId);
  cancelarFalta.hidden = !faltan || !deLote;
  reintentarFallidos.hidden = !cuantas("sin-subir") || !deLote;
  // Al terminar la tanda, la lista de Piezas toma las nuevas (y las sondea).
  if (!faltan && subidas.length) cargarPiezas();
}

// Descarta lo que espera y corta lo que todavía está mandando su cuerpo: el servidor ve un
// multipart sin terminar y no crea nada. Lo que ya mandó el cuerpo entero no se corta: puede
// estar confirmándose, y la fila muestra lo que conteste el servidor.
function cancelar(subida) {
  enEspera = enEspera.filter((s) => s !== subida);
  if (subida.estado === "subiendo" && subida.subido) {
    subida.terminando = true;
    subida.marca.textContent = "Terminando…";
    return;
  }
  if (subida.estado === "subiendo") subida.xhr.abort();
  pintar(subida, "cancelada", "Cancelado");
}

cancelarFalta.addEventListener("click", () => {
  for (const subida of subidas.filter((s) => ["esperando", "subiendo"].includes(s.estado))) {
    cancelar(subida);
  }
  resumir();
});

// Cerrar la pestaña corta la subida y no se retoma: el navegador pregunta antes (R3, E1).
window.addEventListener("beforeunload", (evento) => {
  const enCurso = subiendoSueltas
    || subidas.some((s) => ["esperando", "subiendo"].includes(s.estado));
  if (!enCurso) return;
  evento.preventDefault();
  evento.returnValue = "";
});

reintentarFallidos.addEventListener("click", () => {
  for (const subida of subidas.filter((s) => s.estado === "sin-subir")) {
    subida.intentos = 0;
    esperar(subida);
  }
  avanzar();
});

// Varios archivos sin Lote: el Lote se crea solo, con un nombre provisorio, y Origen, Fecha y
// Propietario se piden después en su pantalla (151). Las subidas siguen de a dos (DEC-20).
async function crearLoteImplicito(cuantos) {
  const ahora = new Date().toLocaleString("es-AR", { dateStyle: "short", timeStyle: "short" });
  errorCarga.hidden = true;
  try {
    const respuesta = await fetch("/api/lotes/implicito", {
      method: "POST",
      headers: { ...CSRF, "Content-Type": "application/json" },
      body: JSON.stringify({ nombre: `Carga del ${ahora} (${cuantos} archivos)` }),
    });
    if (respuesta.status === 401) {
      await sesionVencida();
      return null;
    }
    const cuerpo = await respuesta.json().catch(() => ({}));
    if (respuesta.ok) return cuerpo.lote;
    errorCarga.textContent = cuerpo.detail || "No se pudo armar el Lote. Probá de nuevo.";
  } catch {
    errorCarga.textContent = "No se pudo armar el Lote. Probá de nuevo.";
  }
  errorCarga.hidden = false;
  return null;
}

async function elegir(elegidos) {
  // Soltar sobre la zona apagada (sin CARGAR o con el Lote lleno) no sube nada.
  if (archivo.disabled) return;
  if (!loteActivo && elegidos.length > 1) {
    const lote = await crearLoteImplicito(elegidos.length);
    if (!lote) return;
    mostrarLote(lote);
  }
  if (loteActivo) {
    await refrescarCupos(elegidos.length);
    cargarConFilas(elegidos);
  } else if (elegidos[0] && esVideo(elegidos[0])) cargarConFilas([elegidos[0]]);
  else subir(elegidos[0]);
}

archivo.addEventListener("change", () => {
  elegir([...archivo.files]);
  archivo.value = "";
});

zona.addEventListener("dragover", (evento) => {
  evento.preventDefault();
  if (!archivo.disabled) zona.classList.add("encima");
});
zona.addEventListener("dragleave", () => zona.classList.remove("encima"));
zona.addEventListener("drop", (evento) => {
  evento.preventDefault();
  zona.classList.remove("encima");
  elegir([...evento.dataTransfer.files]);
});

zona.addEventListener("click", (evento) => {
  if (archivo.disabled) evento.preventDefault();
});

formIngreso.addEventListener("submit", entrar);
document.getElementById("salir").addEventListener("click", salir);

revisarSalud();
habilitarSesion(false);
cargarSesion();
