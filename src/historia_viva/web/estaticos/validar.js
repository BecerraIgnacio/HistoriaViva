// ── Entrada ───────────────────────────────────────────────────────────────
// Se entra como /validar.html?pieza=<pieza_id> desde la lista de index.html (§7).
const PIEZA_ID = new URLSearchParams(location.search).get("pieza");
const CLAVE_PROPORCION = "hv_proporcion_validar";

const $ = (id) => document.getElementById(id);

let ficha = null;       // La Ficha vigente que devolvió el servidor.
let detalle = null;     // El detalle de la Pieza: estado, nombre y tipo MIME.
let nombres = new Map(); // persona_id → nombre, para firmar el mensaje de Validada.
let sucio = false;      // Hay cambios sin guardar.
let medicionId = null;  // El reloj que arrancó el servidor al abrir esta Ficha.
let reloj = null;       // La apertura en curso: guardar la espera para no mandar un id nulo.
let relojCerrado = false; // Se guardó y todavía no se volvió a editar.
let puedeValidar = false; // Guardar la Ficha exige VALIDAR (DEC-124).
let puedeRetirar = false; // Quitar del Acervo exige RETIRAR_PIEZAS (DEC-124).
let usuarioId = null;   // Quién entró: un borrador es solo de quien lo escribió.
// La versión que se editó y sus valores: viaja como `version_base` y separa lo que cambió
// la persona de lo que cambió otra (modal M1).
let base = null;
let botonDelGuardado = null; // El que disparó el último guardado, para reintentarlo.
// 152: la Pieza está Recibida y se completan los campos duros y el Contexto mientras se lee.
let enLectura = false;
let visorEnEspera = false; // La vista de planilla o de texto sale de la v1: se arma al terminar.
// 155: las Afirmaciones de una foto, como las da GET /afirmaciones. Vacía para lo demás. 148: y las
// imágenes embebidas sin texto de un documento o una planilla, con las suyas.
const SIN_AFIRMACIONES = { afirmaciones: [], aviso: null, imagenes: [] };
let descripcionDeLaFoto = SIN_AFIRMACIONES;
let guardando = false; // Hay un guardado en curso.
// 146: el resumen que se rehace sin las Afirmaciones rechazadas.
let resumenDelModelo = null; // El último que propuso un modelo para el campo; `null`: el de la persona.
let resumenOfrecido = null; // Uno rehecho que no se puso en el campo porque la persona escribió el suyo.
let rehaciendoResumen = false; // Hay un rehecho en la cola: «Validar y firmar» lo espera.
let rehechoVisto = null; // `rehecho_el` del último que se atendió: cada uno se pone u ofrece una vez.
let avisoDelResumen = "";
let sondeoDelResumen = null;
let informeDelResumen = null; // El chequeo de inyección del rehecho que está en el campo, sin guardar.
let rehaciendoDesde = 0; // Cuándo empezó la espera: pasado `ESPERA_DEL_RESUMEN_MS` se puede no esperar.
let esperaSalteada = false; // La persona eligió firmar con el resumen que hay.
const ESPERA_DEL_RESUMEN_MS = 90000;
const decisionesEnVuelo = new Set(); // Los `orden` cuya decisión todavía no respondió.

// ── Cliente HTTP ──────────────────────────────────────────────────────────
// Los errores llegan en RFC 9457 y lo que se le muestra a la persona es el `detail`.
// Un 401 significa que la sesión venció: se vuelve a la Entrada con el aviso, con lo editado
// guardado en la pestaña para retomarlo al volver (E3, historia 87). Si no se puede guardar,
// no se sale: se avisa y lo escrito queda en pantalla. `X-HV` va en todo pedido: sin ella,
// una mutación responde 403 (CSRF, DEC-37).
async function pedir(url, opciones = {}) {
  const respuesta = await fetch(url, { ...opciones, headers: { "X-HV": "1", ...opciones.headers } });
  if (respuesta.status === 401) {
    if (sucio && base) {
      const borrador = {
        usuario: usuarioId, pieza: PIEZA_ID, pedido: cuerpoDelPedido(), base,
        // La Fecha a medio escribir no sale en el pedido: va tal cual está en las cajas.
        fecha: CAMPOS_DE_FECHA.map((id) => $(`fecha-${id}`).value),
      };
      try {
        sessionStorage.setItem("hv_borrador", JSON.stringify(borrador));
      } catch {
        mostrarFallo("Tu sesión venció y este navegador no dejó guardar lo que escribiste. " +
          "Copialo antes de salir y entrá de nuevo.", "No se pudo conservar lo editado");
        throw new Error("Sin sesión");
      }
    }
    sucio = false;
    location.href = "/?error=sesion-vencida";
    throw new Error("Sin sesión");
  }
  if (!respuesta.ok) {
    const problema = await respuesta.json().catch(() => ({}));
    const error = new Error(problema.detail || "No se pudo completar la operación. Probá de nuevo.");
    error.problema = problema;
    throw error;
  }
  return respuesta;
}

const comoJson = async (url) => (await pedir(url)).json();

// ── El selector de Fecha (§3) ─────────────────────────────────────────────
// Dos filas, Desde y Hasta, cada una con Día y Mes opcionales y Año. La precisión no se
// elige: se infiere de qué se completó. El contrato con el servidor no cambia: sale
// `fecha_inicio` + `precision_fecha`, y `fecha_fin` solo con precisión `rango`.
const MESES = [
  "enero", "febrero", "marzo", "abril", "mayo", "junio",
  "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
];
const EXTREMOS = { desde: "Desde", hasta: "Hasta" };
const CAMPOS_DE_FECHA = ["desde-dia", "desde-mes", "desde-anio", "hasta-dia", "hasta-mes", "hasta-anio"];

const dosDigitos = (n) => String(n).padStart(2, "0");
const anioValido = (valor) => /^\d{3,4}$/.test(String(valor).trim());
const diasDelMes = (anio, mes) => new Date(Date.UTC(anio, mes, 0)).getUTCDate();
const iso = (anio, mes, dia) => `${String(anio).padStart(4, "0")}-${dosDigitos(mes)}-${dosDigitos(dia)}`;
const campoFecha = (extremo, parte) => $(`fecha-${extremo}-${parte}`);

// Lee una fila. Devuelve `{ vacio }`, `{ error, parte }` o el período que la fila nombra:
// un año, un mes o un día, según hasta dónde llegó la persona.
function leerExtremo(extremo) {
  const dia = campoFecha(extremo, "dia").value.trim();
  const mes = campoFecha(extremo, "mes").value;
  const anio = campoFecha(extremo, "anio").value.trim();
  // `validity.badInput`: el navegador descarta lo que no es número y deja el valor vacío.
  const diaIlegible = campoFecha(extremo, "dia").validity.badInput;
  const anioIlegible = campoFecha(extremo, "anio").validity.badInput;
  if (!dia && !mes && !anio && !diaIlegible && !anioIlegible) return { vacio: true };
  if ((dia || diaIlegible) && !mes) {
    return { error: "Para indicar el día, elegí también el mes", partes: ["dia", "mes"] };
  }
  if (!anio && !anioIlegible) {
    return { error: `Falta el año de ${EXTREMOS[extremo]}`, partes: ["anio"] };
  }
  if (anioIlegible || !anioValido(anio)) {
    return { error: "Escribí el año con sus cuatro cifras, por ejemplo 1985", partes: ["anio"] };
  }
  const nAnio = Number(anio);
  if (!mes) return { granularidad: "anio", anio: nAnio };
  const nMes = Number(mes);
  if (!dia && !diaIlegible) return { granularidad: "mes", anio: nAnio, mes: nMes };
  const nDia = Number(dia);
  if (diaIlegible || !/^\d{1,2}$/.test(dia) || nDia < 1 || nDia > diasDelMes(nAnio, nMes)) {
    return { error: `Ese día no existe en ${MESES[nMes - 1]} de ${nAnio}`, partes: ["dia"] };
  }
  return { granularidad: "dia", anio: nAnio, mes: nMes, dia: nDia };
}

const comienzo = (p) => iso(p.anio, p.mes || 1, p.dia || 1);
function final(p) {
  if (p.granularidad === "anio") return iso(p.anio, 12, 31);
  if (p.granularidad === "mes") return iso(p.anio, p.mes, diasDelMes(p.anio, p.mes));
  return iso(p.anio, p.mes, p.dia);
}

function periodoEnPalabras(p) {
  if (p.granularidad === "anio") return String(p.anio);
  if (p.granularidad === "mes") return `${MESES[p.mes - 1]} de ${p.anio}`;
  return `${p.dia} de ${MESES[p.mes - 1]} de ${p.anio}`;
}

// La confirmación en lenguaje llano: mostrarle "1-ene-1998 a 31-dic-1998" a quien sabe
// "1998" es exactamente la fricción que este prototipo tiene que evitar (§3).
function fechaEnPalabras(precision, desde, hasta) {
  if (precision === "dia") return periodoEnPalabras(desde);
  if (precision === "mes") return `Algún día de ${periodoEnPalabras(desde)}`;
  if (precision === "anio") return `En ${desde.anio}`;
  if (precision === "decada") return `Década de ${desde.anio}`;
  return `Entre ${periodoEnPalabras(desde)} y ${periodoEnPalabras(hasta)}`;
}

// Todo lo que dicen los seis campos: la Fecha que arman (o `null`) y los errores de cada
// fila y del conjunto.
function analizarFecha() {
  const errores = { desde: null, hasta: null, rango: null };
  const desde = leerExtremo("desde");
  const hasta = leerExtremo("hasta");
  if (desde.error) errores.desde = desde;
  if (hasta.error) errores.hasta = hasta;
  if (desde.vacio && !hasta.vacio && !hasta.error) {
    errores.desde = { error: "Para usar Hasta, completá también Desde", partes: ["anio"] };
  }
  if (errores.desde || errores.hasta || desde.vacio) return { fecha: null, errores };

  if (hasta.vacio) {
    return {
      fecha: {
        fecha_inicio: comienzo(desde),
        fecha_fin: final(desde),
        precision_fecha: desde.granularidad,
        texto: fechaEnPalabras(desde.granularidad, desde),
      },
      errores,
    };
  }

  const inicio = comienzo(desde);
  const fin = final(hasta);
  if (fin < inicio) {
    errores.rango = "Hasta no puede ser anterior a Desde";
    return { fecha: null, errores };
  }
  // Solo años, de uno terminado en cero al que termina en nueve: es una década, y se
  // guarda con esa precisión, que dice más que un rango de diez años.
  const esDecada = desde.granularidad === "anio" && hasta.granularidad === "anio"
    && desde.anio % 10 === 0 && hasta.anio === desde.anio + 9;
  const precision = esDecada ? "decada" : "rango";
  return {
    fecha: {
      fecha_inicio: inicio,
      fecha_fin: fin,
      precision_fecha: precision,
      texto: fechaEnPalabras(precision, desde, hasta),
    },
    errores,
  };
}

// La Fecha tal como llegó al abrir, con la huella de los campos que la representan. Si la
// persona no los tocó, se devuelve esa y no la inferida: un `rango` guardado de 1960 a
// 1969 se ve igual que una década, y reinterpretarlo contaría como corrección algo que
// nadie cambió (user story 23).
let fechaCargada = null;
const huellaDeFecha = () => CAMPOS_DE_FECHA.map((id) => $(`fecha-${id}`).value).join("|");

function fechaElegida() {
  if (fechaCargada && fechaCargada.huella === huellaDeFecha()) return fechaCargada.fecha;
  return analizarFecha().fecha;
}

function pintarErrorDeExtremo(extremo, problema) {
  $(`error-${extremo}`).textContent = problema ? problema.error : "";
  for (const parte of ["dia", "mes", "anio"]) {
    const invalido = Boolean(problema && problema.partes.includes(parte));
    campoFecha(extremo, parte).setAttribute("aria-invalid", String(invalido));
  }
}

function refrescarFecha() {
  const { errores } = analizarFecha();
  pintarErrorDeExtremo("desde", errores.desde);
  pintarErrorDeExtremo("hasta", errores.hasta);
  // Rango invertido: se marca en el momento, sin esperar al guardado (E2 de CU-05).
  $("fecha-error").textContent = errores.rango || "";
  if (errores.rango) campoFecha("hasta", "anio").setAttribute("aria-invalid", "true");

  const fecha = fechaElegida();
  const confirmacion = $("fecha-confirmacion");
  confirmacion.textContent = fecha ? fecha.texto : "Sin fecha declarada";
  confirmacion.classList.toggle("vacia", !fecha);
}

// Del borde de un rango, la fila que lo escribe: un 1 de enero (o un 31 de diciembre)
// es un año; un 1 (o un último día de mes), un mes; lo demás, un día.
function extremoDesdeFecha(texto, esFin) {
  const anio = Number(texto.slice(0, 4));
  const mes = Number(texto.slice(5, 7));
  const dia = Number(texto.slice(8, 10));
  if (esFin ? mes === 12 && dia === 31 : mes === 1 && dia === 1) return { anio };
  if (esFin ? dia === diasDelMes(anio, mes) : dia === 1) return { anio, mes };
  return { anio, mes, dia };
}

function escribirExtremo(extremo, valores) {
  campoFecha(extremo, "dia").value = valores.dia ? String(valores.dia) : "";
  campoFecha(extremo, "mes").value = valores.mes ? dosDigitos(valores.mes) : "";
  campoFecha(extremo, "anio").value = valores.anio ? String(valores.anio) : "";
}

// Al reabrir una Ficha guardada, el control se reconstruye desde el frontmatter: los
// campos que ya se habían completado aparecen con su valor, no en blanco (US 20).
function cargarFecha(frontmatter) {
  fechaCargada = null;
  for (const extremo of Object.keys(EXTREMOS)) escribirExtremo(extremo, {});
  const precision = frontmatter.precision_fecha;
  const inicio = frontmatter.fecha_inicio;
  if (!precision || !inicio) return;
  const anio = Number(inicio.slice(0, 4));
  const mes = Number(inicio.slice(5, 7));
  const dia = Number(inicio.slice(8, 10));
  if (precision === "dia") escribirExtremo("desde", { anio, mes, dia });
  if (precision === "mes") escribirExtremo("desde", { anio, mes });
  if (precision === "anio") escribirExtremo("desde", { anio });
  if (precision === "decada") {
    const base = Math.floor(anio / 10) * 10;
    escribirExtremo("desde", { anio: base });
    escribirExtremo("hasta", { anio: base + 9 });
  }
  if (precision === "rango") {
    escribirExtremo("desde", extremoDesdeFecha(inicio, false));
    escribirExtremo("hasta", extremoDesdeFecha(frontmatter.fecha_fin || inicio, true));
  }
  const { fecha } = analizarFecha();
  if (!fecha) return;
  if (precision === "rango" && fecha.precision_fecha !== "rango") {
    const texto = fechaEnPalabras("rango", leerExtremo("desde"), leerExtremo("hasta"));
    fechaCargada = {
      huella: huellaDeFecha(),
      fecha: { ...fecha, precision_fecha: "rango", texto },
    };
  }
}

function poblarSelectoresDeFecha() {
  for (const extremo of Object.keys(EXTREMOS)) {
    for (const [indice, nombre] of MESES.entries()) {
      campoFecha(extremo, "mes").add(new Option(
        `${nombre.charAt(0).toUpperCase()}${nombre.slice(1)}`, dosDigitos(indice + 1)
      ));
    }
  }
}

// ── El panel de faltantes (§4) ────────────────────────────────────────────
// **La lista de obligatorios no se escribe acá.** Viene en `obligatorios` de
// GET /api/piezas/{id}/ficha, porque una segunda copia en el frontend es la forma en que
// las dos se desincronizan (contratos.md §3.1). Esta tabla solo dice, para cada campo,
// con qué control se llena y cuándo cuenta como vacío.
const CONTROL_DE_FECHA = {
  etiqueta: "Fecha",
  caja: "campo-fecha",
  foco: "fecha-desde-dia",
  vacio: () => fechaElegida() === null,
};

const CONTROL_DE_CAMPO = {
  tipo: { etiqueta: "Tipo de Pieza", caja: "campo-tipo", foco: "tipo", vacio: () => !$("tipo").value },
  tipo_actividad: {
    etiqueta: "Tipo de actividad",
    caja: "campo-tipo-actividad",
    foco: "tipo-actividad",
    vacio: () => !$("tipo-actividad").value,
  },
  // Los tres campos de la Fecha comparten un solo control: la persona completa lo que
  // sabe y la precisión y el rango se infieren; pedírselos aparte es la fricción que se
  // evita (§3).
  fecha_inicio: CONTROL_DE_FECHA,
  fecha_fin: CONTROL_DE_FECHA,
  precision_fecha: CONTROL_DE_FECHA,
  origen: { etiqueta: "Origen", caja: "campo-origen", foco: "origen", vacio: () => !$("origen").value.trim() },
  propietario: {
    etiqueta: "Propietario",
    caja: "campo-propietario",
    foco: "propietario",
    vacio: () => !$("propietario").value.trim(),
  },
  resumen: { etiqueta: "Resumen", caja: "campo-resumen", foco: "resumen", vacio: () => !$("resumen").value.trim() },
  // `archivo_original` es obligatorio pero lo completa el sistema: tiene etiqueta para los
  // mensajes y no tiene control, así que el panel no lo vigila (§4).
  archivo_original: { etiqueta: "Archivo original" },
  // No es un campo: el servidor lo suma a `campos_faltantes` con alguna sin decidir (144).
  afirmaciones: {
    get etiqueta() { return `decidir ${cuantasAfirmaciones(sinDecidir().length)}`; },
    caja: "campo-afirmaciones",
    get foco() { return `aceptar-${sinDecidir()[0].orden}`; },
    vacio: () => sinDecidir().length > 0,
  },
  // Tampoco es un campo: una imagen embebida se está describiendo y sus Afirmaciones no llegaron (148).
  descripciones: {
    get etiqueta() { return `esperar la descripción de ${cuantasImagenes(enCurso().length)}`; },
    caja: "campo-afirmaciones",
    get foco() { return `imagen-${enCurso()[0].imagen}`; },
    vacio: () => enCurso().length > 0,
  },
};

let vigilados = [];

// Del `obligatorios` que mandó el servidor quedan los que llena una persona, una vez cada
// uno: los tres campos de la Fecha apuntan al mismo control y se cuentan como uno solo.
// Las Afirmaciones también se vigilan: firmar exige decidirlas todas (ADR-0007).
function armarVigilados(obligatorios) {
  vigilados = [];
  const campos = [...obligatorios];
  if (descripcionDeLaFoto.afirmaciones.length || descripcionDeLaFoto.imagenes.length) campos.push("afirmaciones");
  if (descripcionDeLaFoto.imagenes.length) campos.push("descripciones");
  for (const campo of campos) {
    const control = CONTROL_DE_CAMPO[campo];
    if (control && control.vacio && !vigilados.includes(control)) vigilados.push(control);
  }
}

function refrescarFaltantes() {
  const faltan = vigilados.filter((vigilado) => vigilado.vacio());
  for (const vigilado of vigilados) {
    $(vigilado.caja).classList.toggle("falta", faltan.includes(vigilado));
  }
  const panel = $("faltantes");
  panel.classList.toggle("con-faltantes", faltan.length > 0);
  panel.classList.toggle("completo", faltan.length === 0);
  if (faltan.length === 0) {
    panel.replaceChildren(document.createTextNode("✓ Está todo. Podés Validar cuando quieras."));
    return;
  }
  const titulo = document.createElement("div");
  titulo.textContent = "⚠ Falta para validar:";
  const lista = document.createElement("ul");
  for (const vigilado of faltan) {
    const item = document.createElement("li");
    const enlace = document.createElement("button");
    enlace.type = "button";
    enlace.textContent = `Falta: ${vigilado.etiqueta}`;
    // Un clic lleva el foco al campo y lo resalta un instante (§4).
    enlace.addEventListener("click", () => {
      const caja = $(vigilado.caja);
      $(vigilado.foco).focus();
      caja.scrollIntoView({ block: "center", behavior: "smooth" });
      caja.classList.add("resaltado");
      setTimeout(() => caja.classList.remove("resaltado"), 1200);
    });
    item.append(enlace);
    lista.append(item);
  }
  panel.replaceChildren(titulo, lista);
}

// ── Marcas de "propuesto" vs. "editado" (§2) ──────────────────────────────
// `sinModelo`: de dónde vino el valor inicial si no lo propuso un modelo («Del archivo», «Del
// Agente»; Procedencia).
// `heredado`: lo copió el Lote (`campos_heredados`); la v1 lo trae, pero no lo propuso nadie.
const enBlanco = (valor) => valor === null || valor === undefined || String(valor).trim() === "";

function marcar(idMarca, idFilete, propuesto, actual, sinModelo = "", heredado = false) {
  const marca = $(idMarca);
  const filete = idFilete ? $(idFilete) : null;
  if (enBlanco(propuesto) && enBlanco(actual)) {
    marca.hidden = true;
    if (filete) filete.className = "";
    return;
  }
  // Sin propuesta, lo que haya lo escribió una persona.
  const igual = !enBlanco(propuesto) && String(propuesto).trim() === String(actual).trim();
  marca.hidden = false;
  if (igual && (sinModelo || heredado)) {
    marca.className = "marca editado";
    marca.textContent = sinModelo ? `${sinModelo}, sin cambios` : "Heredado del Lote, sin cambios";
    if (filete) filete.className = "editado-filete";
    return;
  }
  // En solo lectura nadie editó nada: «Editado por vos» no es para quien no valida.
  if (!igual && !puedeValidar) {
    marca.hidden = true;
    if (filete) filete.className = "";
    return;
  }
  marca.className = igual ? "marca propuesto" : "marca editado";
  marca.textContent = igual ? "🤖 Propuesto, revisá antes de guardar" : "Editado por vos";
  if (filete) filete.className = igual ? "propuesto-filete" : "editado-filete";
}

function esAporte() {
  return detalle.via_ingreso === "aporte";
}

const rotuloDeEstaFicha = () => rotuloDelCuerpo(
  detalle.tipo_mime, detalle.via_ingreso,
  descripcionDeLaFoto.afirmaciones.length > 0 || Boolean(descripcionDeLaFoto.aviso),
  conAfirmacionesFirmadas(),
);

function refrescarMarcas() {
  const propuesta = ficha.propuesta;
  // El resumen rehecho (146) también lo propuso un modelo: se compara con el último propuesto.
  marcar(
    "marca-resumen", "filete-resumen", resumenDelModelo ?? propuesta.frontmatter.resumen,
    $("resumen").value
  );
  marcar(
    "marca-transcripcion", "filete-transcripcion",
    propuesta.cuerpo, $("transcripcion").value, rotuloDeEstaFicha().sinModelo
  );
  const heredados = ficha.campos_heredados || [];
  // Lo que una persona escribió mientras se leía (152): tampoco lo propuso un modelo.
  const deLaPersona = ficha.campos_de_la_persona || [];
  const escrito = (campo) => (deLaPersona.includes(campo) ? "Escrito por una persona" : "");
  const fechaPropuesta = propuesta.frontmatter.fecha_inicio;
  const fecha = fechaElegida();
  marcar(
    "marca-fecha", null, fechaPropuesta, fecha ? fecha.fecha_inicio : "",
    escrito("fecha_inicio"), heredados.includes("fecha_inicio")
  );
  for (const [campo, id] of [
    ["tipo", "tipo"], ["tipo_actividad", "tipo-actividad"],
    ["origen", "origen"], ["propietario", "propietario"],
  ]) {
    marcar(
      `marca-${id}`, null, propuesta.frontmatter[campo], $(id).value,
      escrito(campo), heredados.includes(campo)
    );
  }
  // Las Carreras no las propone el modelo: llevan marca solo si vinieron del Lote (082) o las
  // escribió una persona mientras se leía (152).
  if (heredados.includes("carreras") || deLaPersona.includes("carreras")) {
    const lista = (ids) => [...ids].sort().join(",");
    const marcadas = casillasDeCarrera().filter((marca) => marca.checked).map((marca) => marca.value);
    marcar(
      "marca-carreras", null, lista(propuesta.frontmatter.carreras || []), lista(marcadas),
      escrito("carreras"), heredados.includes("carreras")
    );
  } else {
    $("marca-carreras").hidden = true;
  }
}

function alEditar() {
  refrescarMarcasDeTiempo();
  if (relojCerrado) {
    relojCerrado = false;
    arrancarElReloj();
  }
  sucio = true;
  refrescarFecha();
  refrescarFaltantes();
  refrescarMarcas();
  refrescarFirma();
}

// ── Tipos de Pieza ────────────────────────────────────────────────────────
// El desplegable se pinta con lo que devuelve GET /api/tipos: la taxonomía la define la
// SEU y se corrige entre dos Piezas de la sesión, sin tocar este archivo.
const CON_ACENTO = { fotografia: "Fotografía", resolucion: "Resolución" };
const etiquetaTipo = (tipo) =>
  CON_ACENTO[tipo] || `${tipo.charAt(0).toUpperCase()}${tipo.slice(1)}`;

// Los catálogos de la Ficha: tipos de Pieza, tipos de actividad (GET /api/tipos-actividad)
// y Carreras (GET /api/carreras), DEC-127.
const NOMBRE_DE_CARRERA = {};
const nombreDeCarrera = (id) => NOMBRE_DE_CARRERA[id] || `${id} (dada de baja)`;

function casillaDeCarrera(id) {
  const casilla = document.createElement("label");
  casilla.className = "casilla";
  const marca = document.createElement("input");
  marca.type = "checkbox";
  marca.value = id;
  casilla.append(marca, ` ${nombreDeCarrera(id)}`);
  $("carreras").append(casilla);
}

async function poblarCatalogos() {
  const [{ tipos, provisorios }, { tipos_actividad }, { carreras }] = await Promise.all([
    comoJson("/api/tipos"),
    comoJson("/api/tipos-actividad"),
    comoJson("/api/carreras"),
  ]);
  for (const { carrera_id, nombre } of carreras) {
    NOMBRE_DE_CARRERA[carrera_id] = nombre;
    casillaDeCarrera(carrera_id);
  }
  for (const tipo of tipos) {
    // «provisorio»: la SEU todavía no lo confirmó (C4).
    const marca = provisorios.includes(tipo) ? " (provisorio)" : "";
    $("tipo").add(new Option(`${etiquetaTipo(tipo)}${marca}`, tipo));
  }
  for (const tipo of tipos_actividad) $("tipo-actividad").add(new Option(tipo, tipo));
}

// El valor que la Ficha ya tiene se ofrece aunque esté dado de baja (DEC-127): sin su
// opción, el desplegable quedaría vacío y se guardaría `null`.
function elegir(id, valor, etiqueta) {
  const desplegable = $(id);
  if (valor && ![...desplegable.options].some((opcion) => opcion.value === valor)) {
    desplegable.add(new Option(`${etiqueta(valor)} (dado de baja)`, valor));
  }
  desplegable.value = valor || "";
}

const casillasDeCarrera = () => [...$("carreras").querySelectorAll("input")];

// Como `elegir`: una Carrera dada de baja que la Ficha ya tiene se ofrece marcada (DEC-127).
function elegirCarreras(ids) {
  for (const id of ids) {
    if (!casillasDeCarrera().some((marca) => marca.value === id)) casillaDeCarrera(id);
  }
  for (const marca of casillasDeCarrera()) marca.checked = ids.includes(marca.value);
}

// «Desconocido» es un Autor (DEC-126): la casilla vacía el campo y lo deja sin escribir.
function elegirAutor(autor) {
  const desconocido = autor === "Desconocido";
  $("autor-desconocido").checked = desconocido;
  $("autor").value = desconocido ? "" : autor || "";
  $("autor").disabled = desconocido;
}

const autorDelFormulario = () =>
  $("autor-desconocido").checked ? "Desconocido" : $("autor").value.trim() || null;

// ── El visor del original (US 10, US 11) ──────────────────────────────────
const visor = $("visor");
const NO_CARGA = "No se pudo mostrar el original. Podés validar igual, pero sin verlo.";
const LEYENDO = "Se está leyendo el original: la vista aparece cuando termine. " +
  "Mientras tanto, descargalo con el enlace de abajo.";
const ROTULO_WORD = "⚠ Vista del texto extraído del Word, no del original. " +
  "Para ver el documento tal cual, descargalo.";
const ROTULO_TXT = "⚠ Texto del archivo, normalizado: no es el original. " +
  "Para verlo tal cual, descargalo.";
// Un Aporte no es un archivo: lo mandó un Agente (DEC-140), y su texto es hostil hasta que
// una persona lo revise.
const ROTULO_APORTE = "⚠ Texto que envió un Agente, no una persona";
const ROTULO_PLANILLA = "⚠ Vista del contenido extraído, no es la planilla original. " +
  "Para verla tal cual, descargala.";
let escala = 1;
let medio = null;   // Lo que hay en el visor, si es algo a lo que el zoom le sirva.
let escalar = null; // Cómo se agranda: un archivo se agranda de ancho, un texto de letra.

// Un archivo pintado por el navegador —foto o PDF— se agranda de ancho, y con el visor a
// 100% "ajustar al ancho" es exactamente eso: ocupar el panel sin recorte.
const porAncho = () => { medio.style.width = `${escala * 100}%`; };

function aplicarEscala() {
  $("nivel-zoom").textContent = `${Math.round(escala * 100)}%`;
  if (medio && escalar) escalar();
}

function armarVisor() {
  const url = `/api/piezas/${encodeURIComponent(PIEZA_ID)}/original`;
  $("nombre-archivo").textContent = detalle.nombre_archivo;
  const enlace = $("enlace-original");
  enlace.href = url;
  const mime = detalle.tipo_mime || "";
  if (mime.startsWith("image/")) {
    medio = document.createElement("img");
    medio.src = url;
    medio.alt = `Original de la Pieza: ${detalle.nombre_archivo}`;
    // 145: la región de una Afirmación respaldada se marca encima, sobre la imagen completa.
    const lienzo = document.createElement("div");
    lienzo.id = "lienzo-original";
    lienzo.append(medio);
    // Si el archivo está pero el navegador no lo pinta (un TIFF), el aviso va igual.
    medio.addEventListener("error", async () => {
      if (await comprobarQueElOriginalEsta(url)) {
        visor.replaceChildren(aviso(NO_CARGA));
        medio = null;
      }
    });
    escalar = () => { lienzo.style.width = `${escala * 100}%`; };
    visor.replaceChildren(lienzo);
    visor.classList.add("arrastrable");
  } else if (mime === "application/pdf") {
    medio = document.createElement("iframe");
    medio.src = url;
    medio.title = `Original de la Pieza: ${detalle.nombre_archivo}`;
    // El visor de PDF del navegador trae su propio zoom y pide el archivo por rangos: acá
    // se le da el ancho y él resuelve las páginas.
    escalar = porAncho;
    visor.replaceChildren(medio);
    // Un `<iframe>` no avisa que el archivo no vino: ante un 404 o un 500 dispara `load`
    // igual, con la página de error adentro. Se pregunta aparte, o el panel queda en
    // blanco sin decir por qué y la persona no sabe que igual puede validar (US 10).
    comprobarQueElOriginalEsta(url);
  } else if (mime.startsWith("video/")) {
    armarReproductor(url, enlace);
  } else if (enLectura) {
    // La planilla y el texto extraído salen de la v1, que todavía no existe (152): el original
    // queda a un clic.
    paraDescargar(enlace);
    visor.replaceChildren(aviso(LEYENDO));
    medio = null;
    visorEnEspera = true;
  } else if (PLANILLAS.includes(mime)) {
    armarVisorDePlanilla(enlace).then(() => comprobarQueElOriginalEsta(url, medio !== null));
  } else {
    // Un Word no se puede pintar en el navegador sin traer una dependencia. En vez de no
    // mostrar nada se muestra el texto que extrajo la capacidad `transcribir` (o el de un
    // `.txt`), rotulado para que nadie lo confunda con el original, y el original queda a
    // un clic (US 10).
    if (esAporte()) $("panel-original").querySelector("h2").textContent = "Original (sin archivo)";
    armarVistaDeTexto(enlace);
    // Si el archivo falta, el aviso va arriba del texto: el enlace de descarga no va a andar,
    // pero la persona sigue validando contra el texto.
    comprobarQueElOriginalEsta(url, medio !== null);
  }
  aplicarEscala();
}

async function comprobarQueElOriginalEsta(url, sobreElTexto = false) {
  // Se pregunta con el rango más chico que existe: un byte alcanza para saber si el archivo
  // está, y la ruta no admite `HEAD`. Si falta en disco, el 404 trae su identificador
  // (`original-no-disponible`, DEC-112) y el aviso lo muestra; sobre el texto extraído, el
  // aviso se suma arriba y el texto queda. Devuelve si está.
  let texto = NO_CARGA;
  try {
    const respuesta = await fetch(url, { headers: { Range: "bytes=0-0" } });
    if (respuesta.ok) return true;
    const { archivo_original: identificador } = await respuesta.json();
    if (identificador) {
      texto = `No se pudo mostrar el original (identificador: ${identificador}). ` +
        "Podés validar igual, pero sin verlo.";
    }
  } catch { /* sin red o sin cuerpo: el aviso va sin identificador */ }
  if (sobreElTexto) {
    visor.prepend(aviso(texto));
    return false;
  }
  visor.replaceChildren(aviso(texto));
  medio = null;
  return false;
}

function paraDescargar(enlace) {
  // El servidor manda el Word, el `.txt` y la planilla como `attachment` (DEC-45): se bajan,
  // no se ven, y así el enlace dice lo que de verdad hace.
  enlace.download = detalle.nombre_archivo;
  enlace.removeAttribute("target");
  enlace.textContent = "Descargar el original";
}

function armarVistaDeTexto(enlace) {
  paraDescargar(enlace);
  const texto = ficha.propuesta.cuerpo;
  if (!texto) {
    visor.replaceChildren(aviso(NO_CARGA));
    medio = null;
    return;
  }
  const rotulo = document.createElement("p");
  rotulo.className = "rotulo-texto";
  // Un `.txt` no pasó por ningún modelo: su texto es el del archivo, en NFC y sin
  // caracteres de control (R-36).
  rotulo.textContent = esAporte()
    ? ROTULO_APORTE
    : detalle.tipo_mime.startsWith("text/plain") ? ROTULO_TXT : ROTULO_WORD;
  medio = document.createElement("pre");
  medio.className = "texto-extraido";
  medio.textContent = texto;
  // Un texto se agranda de letra, no de ancho: el bloque ya envuelve al ancho del panel,
  // así que "ajustar al ancho" —volver a 100%— sigue siendo lo que dice que es.
  escalar = () => { medio.style.fontSize = `${escala * 100}%`; };
  visor.replaceChildren(...(esAporte() && detalle.aporte ? [datosDelAporte()] : []), rotulo, medio);
}

// P5f: el original de un Aporte es el texto recibido; arriba, quién lo mandó, cuándo y por
// dónde. Todo lo dice el Agente, así que va con `textContent`.
const CANALES = { instagram: "Instagram", linkedin: "LinkedIn", web: "web", correo: "correo" };

function datosDelAporte() {
  const { agente, recibido_el: recibido, tipo_contenido: contenido, canal } = detalle.aporte;
  const lista = document.createElement("dl");
  lista.className = "datos-aporte";
  const cuando = new Date(recibido).toLocaleString("es-AR", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
  for (const [termino, valor] of [
    ["Aporte de", agente],
    ["Recibido", cuando],
    ["Contenido", canal ? `${contenido} · Canal: ${CANALES[canal] || canal}` : contenido],
  ]) {
    const dt = document.createElement("dt");
    dt.textContent = termino;
    const dd = document.createElement("dd");
    dd.textContent = valor;
    lista.append(dt, dd);
  }
  return lista;
}

// La planilla, una pestaña por hoja (historia 113): las tablas vienen del servidor, leídas de
// la v1 sin Contexto, y cada celda va con `textContent`: `=CMD()` o `<img onerror>` se ven
// como texto (DEC-45). Los avisos de tope y de hojas ocultas van arriba de las pestañas.
async function armarVisorDePlanilla(enlace) {
  paraDescargar(enlace);
  let planilla;
  try {
    planilla = await comoJson(`/api/piezas/${encodeURIComponent(PIEZA_ID)}/planilla`);
  } catch {
    visor.replaceChildren(aviso(NO_CARGA));
    medio = null;
    return;
  }
  const rotulo = document.createElement("p");
  rotulo.className = "rotulo-texto";
  rotulo.textContent = ROTULO_PLANILLA;
  const partes = [rotulo];
  if (planilla.avisos.length) {
    const avisos = document.createElement("ul");
    avisos.className = "avisos-planilla";
    avisos.setAttribute("aria-label", "Avisos de la extracción");
    for (const texto of planilla.avisos) {
      const item = document.createElement("li");
      item.textContent = texto;
      avisos.append(item);
    }
    partes.push(avisos);
  }
  if (!planilla.hojas.length) {
    visor.replaceChildren(...partes, aviso("No hay contenido extraído; descargá el original."));
    medio = null;
    return;
  }
  const lista = document.createElement("div");
  lista.className = "pestanas";
  lista.setAttribute("role", "tablist");
  lista.setAttribute("aria-label", "Hojas de la planilla");
  medio = document.createElement("div");
  medio.className = "hojas";
  const pestanas = planilla.hojas.map((hoja, i) => {
    const pestana = document.createElement("button");
    pestana.type = "button";
    pestana.id = `pestana-hoja-${i}`;
    pestana.setAttribute("role", "tab");
    pestana.setAttribute("aria-controls", `hoja-${i}`);
    pestana.textContent = hoja.nombre;
    lista.append(pestana);
    medio.append(panelDeHoja(hoja, i));
    return pestana;
  });
  const elegirHoja = (elegida, enfocar) => {
    pestanas.forEach((pestana, i) => {
      const activa = i === elegida;
      pestana.setAttribute("aria-selected", String(activa));
      pestana.tabIndex = activa ? 0 : -1;
      $(`hoja-${i}`).hidden = !activa;
    });
    if (enfocar) pestanas[elegida].focus();
  };
  pestanas.forEach((pestana, i) => pestana.addEventListener("click", () => elegirHoja(i, false)));
  // Patrón de pestañas de ARIA: las flechas, Inicio y Fin mueven el foco y activan la hoja.
  lista.addEventListener("keydown", (evento) => {
    const actual = pestanas.indexOf(document.activeElement);
    const destino = { ArrowRight: actual + 1, ArrowLeft: actual - 1, Home: 0,
      End: pestanas.length - 1 }[evento.key];
    if (actual < 0 || destino === undefined) return;
    evento.preventDefault();
    elegirHoja((destino + pestanas.length) % pestanas.length, true);
  });
  visor.classList.add("planilla");
  visor.replaceChildren(...partes, lista, medio);
  elegirHoja(0, false);
  escalar = () => { medio.style.fontSize = `${escala * 100}%`; };
  aplicarEscala();
}

function panelDeHoja(hoja, i) {
  const panel = document.createElement("div");
  panel.id = `hoja-${i}`;
  panel.className = "hoja";
  panel.setAttribute("role", "tabpanel");
  panel.setAttribute("aria-labelledby", `pestana-hoja-${i}`);
  if (hoja.nota) {
    const nota = document.createElement("p");
    nota.className = "nota-hoja";
    nota.textContent = hoja.nota;
    panel.append(nota);
  }
  if (!hoja.columnas.length) {
    panel.tabIndex = 0;
    panel.append(aviso("Esta hoja no tiene datos."));
    return panel;
  }
  // La región con scroll recibe foco: con el teclado también se recorre la tabla.
  const region = document.createElement("div");
  region.className = "tabla-hoja";
  region.tabIndex = 0;
  region.setAttribute("role", "region");
  region.setAttribute("aria-labelledby", `titulo-hoja-${i}`);
  const tabla = document.createElement("table");
  const titulo = tabla.createCaption();
  titulo.id = `titulo-hoja-${i}`;
  titulo.textContent = `Hoja «${hoja.nombre}» · filas ${hoja.desde}–${hoja.hasta} de ${hoja.total}`;
  const encabezado = tabla.createTHead().insertRow();
  for (const texto of ["Fila", ...hoja.columnas]) {
    const celda = document.createElement("th");
    celda.scope = "col";
    celda.textContent = texto;
    encabezado.append(celda);
  }
  const cuerpo = tabla.createTBody();
  hoja.filas.forEach((fila, k) => {
    const renglon = cuerpo.insertRow();
    // El número de la planilla: el encabezado es la fila `desde`, la que citan las búsquedas.
    const numero = document.createElement("th");
    numero.scope = "row";
    numero.textContent = String(hoja.desde + k + 1);
    renglon.append(numero);
    for (const texto of fila) renglon.insertCell().textContent = texto;
  });
  region.append(tabla);
  panel.append(region);
  return panel;
}

function aviso(texto) {
  const parrafo = document.createElement("p");
  parrafo.className = "sin-visor";
  parrafo.textContent = texto;
  return parrafo;
}

// ── El reproductor de video (P5e, ADR-0005) ───────────────────────────────
// Ningún modelo mira el video: lo mira quien valida, con los controles nativos, que piden el
// original por tramos (`Range`). Las marcas `[HH:MM:SS]` que escribe son la gramática de
// `dominio` (DEC-74, DEC-112): la búsqueda las cita con `#t=`.
const NO_REPRODUCE = "Tu navegador no puede reproducir este formato. Descargá el original " +
  "para verlo; igual podés completar la Ficha.";
const MARCA_DE_TIEMPO = /\[(\d{2}):([0-5]\d):([0-5]\d)\]/g;
const CODECS = {
  h264: "H.264", hevc: "HEVC", vp8: "VP8", vp9: "VP9", av1: "AV1", prores: "ProRes",
  mpeg4: "MPEG-4", aac: "AAC", mp3: "MP3", opus: "Opus", vorbis: "Vorbis",
};
let reproductor = null; // El `<video>`, si el original es un video que el navegador abre.

function esVideo() {
  return (detalle.tipo_mime || "").startsWith("video/");
}

function marcaDeTiempo(segundos) {
  const s = Math.max(0, Math.floor(segundos));
  const horas = dosDigitos(Math.floor(s / 3600));
  return `[${horas}:${dosDigitos(Math.floor(s / 60) % 60)}:${dosDigitos(s % 60)}]`;
}

function duracionLegible(segundos) {
  const s = Math.round(segundos);
  const h = Math.floor(s / 3600);
  const minutosYSegundos = `${dosDigitos(Math.floor(s / 60) % 60)}:${dosDigitos(s % 60)}`;
  return h ? `${h}:${minutosYSegundos}` : `${Math.floor(s / 60)}:${dosDigitos(s % 60)}`;
}

// La fila de datos técnicos sale de `ffprobe` con lista blanca (DEC-16): números y códecs,
// nunca las etiquetas del contenedor. No va a la Ficha ni se indexa.
function datosTecnicos() {
  const datos = detalle.metadata_original || {};
  const codec = (nombre) => CODECS[nombre] || String(nombre).toUpperCase();
  const partes = [];
  if (datos.duracion_s) partes.push(`Duración ${duracionLegible(datos.duracion_s)}`);
  if (datos.video?.ancho && datos.video?.alto) partes.push(`${datos.video.ancho}×${datos.video.alto}`);
  if (datos.video?.codec) partes.push(codec(datos.video.codec));
  if (datos.audio?.codec) partes.push(codec(datos.audio.codec));
  const megas = Math.max(1, Math.round(detalle.tamano_bytes / 1024 ** 2));
  partes.push(`${megas.toLocaleString("es-AR")} MB`);
  const bloque = document.createElement("div");
  bloque.className = "datos-tecnicos";
  const titulo = document.createElement("p");
  titulo.className = "rotulo-texto";
  titulo.textContent = "Datos técnicos del archivo (leídos del archivo, sin IA)";
  const linea = document.createElement("p");
  linea.textContent = partes.join(" · ");
  bloque.append(titulo, linea);
  return bloque;
}

function enlaceDeDescarga() {
  const enlace = document.createElement("a");
  enlace.href = `/api/piezas/${encodeURIComponent(PIEZA_ID)}/original?descarga=1`;
  enlace.download = detalle.nombre_archivo;
  enlace.textContent = "Descargar el original";
  return enlace;
}

function armarReproductor(url, enlace) {
  reproductor = document.createElement("video");
  reproductor.controls = true;
  reproductor.preload = "metadata";
  reproductor.src = url;
  reproductor.setAttribute("aria-label", `Original de la Pieza: ${detalle.nombre_archivo}`);
  medio = reproductor;
  escalar = porAncho;
  const boton = document.createElement("button");
  boton.type = "button";
  boton.id = "insertar-marca";
  boton.className = "insertar-marca";
  boton.setAttribute("aria-label", "Insertar la marca de tiempo actual en la descripción");
  boton.setAttribute("aria-keyshortcuts", "Alt+M");
  boton.hidden = !puedeValidar;
  const rotular = () => {
    const ahora = marcaDeTiempo(reproductor ? reproductor.currentTime : 0).slice(1, -1);
    boton.textContent = `Insertar marca de tiempo (${ahora}) en la descripción`;
  };
  rotular();
  boton.addEventListener("click", insertarMarca);
  reproductor.addEventListener("timeupdate", rotular);
  // Un códec que el navegador no abre (HEVC, ProRes) da `error`, o abre solo el audio.
  const noReproduce = async () => {
    if (!reproductor) return;
    reproductor = null;
    if (!(await comprobarQueElOriginalEsta(url))) return;
    const parrafo = aviso(NO_REPRODUCE);
    parrafo.append(" ", enlaceDeDescarga());
    visor.replaceChildren(parrafo, datosTecnicos());
    medio = null;
  };
  reproductor.addEventListener("error", noReproduce);
  reproductor.addEventListener("loadedmetadata", () => {
    if (reproductor && reproductor.videoWidth === 0) noReproduce();
  });
  visor.replaceChildren(reproductor, boton, datosTecnicos());
  visor.classList.add("video");
  enlace.after(" · ", enlaceDeDescarga());
}

// Escribe `[HH:MM:SS] ` donde está el cursor de la descripción (o en lugar de lo
// seleccionado) y le devuelve el foco: se anota sin soltar el reproductor (R8 paso 2).
function insertarMarca() {
  if (!reproductor || !puedeValidar) return;
  const descripcion = $("transcripcion");
  const marca = marcaDeTiempo(reproductor.currentTime);
  descripcion.setRangeText(`${marca} `, descripcion.selectionStart, descripcion.selectionEnd, "end");
  descripcion.focus();
  descripcion.dispatchEvent(new Event("input"));
  anunciar(`Marca ${marca} insertada`);
}

function anunciar(texto) {
  const region = $("anuncio");
  region.textContent = "";
  setTimeout(() => { region.textContent = texto; }, 100); // así el mismo texto se repite
}

// Las marcas escritas, como enlaces que llevan el reproductor a ese segundo (historia 115). El
// `href` es el de la Cita de la búsqueda: abierto aparte, el navegador salta solo.
function refrescarMarcasDeTiempo() {
  if (!esVideo()) return;
  const vistas = new Set();
  const items = [];
  for (const [marca, h, m, s] of $("transcripcion").value.matchAll(MARCA_DE_TIEMPO)) {
    if (vistas.has(marca)) continue;
    vistas.add(marca);
    const segundos = Number(h) * 3600 + Number(m) * 60 + Number(s);
    const enlace = document.createElement("a");
    enlace.href = `/api/piezas/${encodeURIComponent(PIEZA_ID)}/original#t=${encodeURIComponent(segundos)}`;
    enlace.textContent = marca;
    enlace.setAttribute("aria-label", `Ir a ${marca} en el video`);
    enlace.addEventListener("click", (evento) => {
      if (!reproductor) return; // sin reproductor, el enlace abre el original en ese segundo
      evento.preventDefault();
      reproductor.currentTime = segundos;
      reproductor.focus();
    });
    const item = document.createElement("li");
    item.append(enlace);
    items.push(item);
  }
  $("lista-marcas").replaceChildren(...items);
  $("marcas-de-tiempo").hidden = items.length === 0;
  // DEC-13: sin descripción se firma igual, pero el índice solo tiene el resumen.
  $("sin-descripcionDeLaFoto").hidden = $("transcripcion").value.trim() !== "";
}

// La fecha que dice el archivo es una pista junto al campo Fecha: no se precarga (DEC-16).
function pintarPistaDeFecha() {
  const creado = esVideo() && detalle.metadata_original?.creado_segun_archivo;
  const pista = $("pista-fecha");
  pista.hidden = !creado;
  if (!creado) return;
  const fecha = new Date(creado).toLocaleDateString(
    // En UTC, como lo guarda el archivo: en hora local, de 00 a 03 UTC daría el día anterior.
    "es-AR", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }
  );
  pista.textContent = `Pista: según el archivo, se creó el ${fecha}. No se carga sola: ` +
    "puede no ser la fecha de lo que muestra.";
}

$("zoom-mas").addEventListener("click", () => { escala = Math.min(escala + 0.25, 5); aplicarEscala(); });
$("zoom-menos").addEventListener("click", () => { escala = Math.max(escala - 0.25, 0.5); aplicarEscala(); });
$("zoom-ajustar").addEventListener("click", () => { escala = 1; aplicarEscala(); });

// Paneo por arrastre: con zoom, mover el original a mano es más rápido que las barras.
let paneo = null;
visor.addEventListener("pointerdown", (evento) => {
  if (!visor.classList.contains("arrastrable")) return;
  paneo = { x: evento.clientX, y: evento.clientY, izq: visor.scrollLeft, arr: visor.scrollTop };
  visor.classList.add("arrastrando");
  visor.setPointerCapture(evento.pointerId);
});
visor.addEventListener("pointermove", (evento) => {
  if (!paneo) return;
  visor.scrollLeft = paneo.izq - (evento.clientX - paneo.x);
  visor.scrollTop = paneo.arr - (evento.clientY - paneo.y);
});
for (const nombre of ["pointerup", "pointercancel"]) {
  visor.addEventListener(nombre, () => { paneo = null; visor.classList.remove("arrastrando"); });
}

// ── La barra divisoria ────────────────────────────────────────────────────
// La proporción es preferencia de pantalla, no dato de la Ficha: puede vivir en
// localStorage. Los datos de la Ficha viven en el servidor: lo único que queda en el
// navegador es el borrador ante un 401, en `sessionStorage` y hasta volver a entrar (E3).
const partida = $("partida");

function fijarProporcion(porcentaje) {
  const acotado = Math.min(70, Math.max(30, porcentaje));
  partida.style.setProperty("--proporcion", `${acotado}%`);
  // El separador enfocable de ARIA dice su valor (axe: aria-required-attr).
  $("divisor").setAttribute("aria-valuenow", String(Math.round(acotado)));
  try { localStorage.setItem(CLAVE_PROPORCION, String(acotado)); } catch { /* sin almacenamiento */ }
}

function proporcionGuardada() {
  try { return Number(localStorage.getItem(CLAVE_PROPORCION)) || 45; } catch { return 45; }
}

const divisor = $("divisor");
let arrastrandoDivisor = false;
divisor.addEventListener("pointerdown", (evento) => {
  arrastrandoDivisor = true;
  divisor.classList.add("arrastrando");
  divisor.setPointerCapture(evento.pointerId);
});
divisor.addEventListener("pointermove", (evento) => {
  if (!arrastrandoDivisor) return;
  const caja = partida.getBoundingClientRect();
  fijarProporcion(((evento.clientX - caja.left) / caja.width) * 100);
});
for (const nombre of ["pointerup", "pointercancel"]) {
  divisor.addEventListener(nombre, () => {
    arrastrandoDivisor = false;
    divisor.classList.remove("arrastrando");
  });
}
// Con teclado la barra también se mueve: el foco no puede quedar en un control muerto.
divisor.addEventListener("keydown", (evento) => {
  const paso = { ArrowLeft: -2, ArrowRight: 2 }[evento.key];
  if (paso === undefined) return;
  evento.preventDefault();
  const actual = parseFloat(getComputedStyle(partida).getPropertyValue("--proporcion")) || 45;
  fijarProporcion(actual + paso);
});

// ── Guardado (§5) ─────────────────────────────────────────────────────────
// Un solo camino de guardado para los dos botones: el resultado depende del estado real
// de los datos, no de cuál se tocó. Por eso "Validar y firmar" solo se deshabilita mientras se
// lee (152) y mientras falte decidir alguna Afirmación (155): esas dos firmas no tienen salida.
function cuerpoDelPedido() {
  const fecha = fechaElegida();
  return {
    version_base: base.version,
    medicion_id: medicionId,
    tipo: $("tipo").value || null,
    tipo_actividad: $("tipo-actividad").value || null,
    fecha_inicio: fecha ? fecha.fecha_inicio : null,
    precision_fecha: fecha ? fecha.precision_fecha : null,
    // `fecha_fin` viaja SOLO con precisión `rango`: con las otras cuatro el servidor la
    // deriva y una fecha_fin que lo contradiga se rechaza con 422.
    fecha_fin: fecha && fecha.precision_fecha === "rango" ? fecha.fecha_fin : null,
    origen: $("origen").value.trim() || null,
    propietario: $("propietario").value.trim() || null,
    autor: autorDelFormulario(),
    carreras: casillasDeCarrera().filter((marca) => marca.checked).map((marca) => marca.value),
    resumen: $("resumen").value.trim() || null,
    // Cuerpo y Contexto van separados: los une el servidor, dueño del formato.
    cuerpo: $("transcripcion").value,
    contexto: $("contexto").value,
    // Solo si se ofrece (el RGC, con los controles de C31): si no, el servidor conserva el que
    // estaba y un `publicable` de más sería un 409 o un 403 (125).
    ...($("campo-publicable").hidden ? {} : { publicable: $("publicable").checked }),
  };
}

// ── M1: otra persona guardó mientras editabas ─────────────────────────────
// Lo que se compara, campo por campo y como lo lee una persona.
const CAMPOS_DEL_CONFLICTO = [
  ["tipo", "Tipo de Pieza"], ["tipo_actividad", "Tipo de actividad"], ["fecha", "Fecha"],
  ["origen", "Origen"], ["propietario", "Propietario"], ["autor", "Autor"],
  ["carreras", "Carreras"], ["resumen", "Resumen"], ["cuerpo", "Cuerpo"],
  ["contexto", "Contexto"],
];
let conflicto = null; // El último 409, con la versión de la otra persona.

function fechaLegible(campos) {
  if (!campos.fecha_inicio || !campos.precision_fecha) return "";
  const periodo = (texto, granularidad) => {
    const [anio, mes, dia] = texto.split("-").map(Number);
    return { granularidad, anio, mes, dia };
  };
  return fechaEnPalabras(
    campos.precision_fecha,
    periodo(campos.fecha_inicio, campos.precision_fecha === "mes" ? "mes" : "dia"),
    periodo(campos.fecha_fin || campos.fecha_inicio, "dia"),
  );
}

const valoresDe = (version) => ({
  tipo: version.frontmatter.tipo ? etiquetaTipo(version.frontmatter.tipo) : "",
  tipo_actividad: version.frontmatter.tipo_actividad || "",
  fecha: fechaLegible(version.frontmatter),
  origen: version.frontmatter.origen || "",
  propietario: version.frontmatter.propietario || "",
  autor: version.frontmatter.autor || "",
  carreras: (version.frontmatter.carreras || []).map(nombreDeCarrera).sort().join(", "),
  resumen: version.frontmatter.resumen || "",
  cuerpo: (version.cuerpo || "").trim(),
  contexto: (version.contexto || "").trim(),
});

function valoresDelFormulario() {
  const pedido = cuerpoDelPedido();
  return valoresDe({ frontmatter: pedido, cuerpo: pedido.cuerpo, contexto: pedido.contexto });
}

const recortar = (texto) => (texto.length > 160 ? `${texto.slice(0, 160)}…` : texto);

function celda(valor, sinCambios = false) {
  const td = document.createElement("td");
  td.className = sinCambios ? "sin-cambios" : "";
  td.textContent = sinCambios ? "(sin cambios)" : recortar(valor) || "(vacío)";
  return td;
}

function mostrarConflicto(problema) {
  conflicto = problema;
  const validada = problema.estado === "Validada" && problema.validada_por;
  $("conflicto-validada").hidden = !validada;
  $("conflicto-validada-texto").textContent = validada
    ? `Esta Pieza ya fue validada por ${problema.validada_por}.`
    : "";
  const quien = problema.editada_por || "Otra persona";
  $("conflicto-quien").textContent =
    `${quien} guardó a las ${horaDe(problema.editada_el)} (versión ${problema.version_actual}). ` +
    "Lo que escribiste sigue acá; nada se pierde si no lo decidís vos.";
  $("conflicto-suyo").textContent = problema.editada_por
    ? `Lo de ${problema.editada_por.split(" ")[0]}`
    : "Lo nuevo";
  const mios = valoresDelFormulario();
  const suyos = valoresDe(problema.ficha);
  const filas = CAMPOS_DEL_CONFLICTO
    .filter(([clave]) => mios[clave] !== suyos[clave])
    .map(([clave, etiqueta]) => {
      const fila = document.createElement("tr");
      const campo = document.createElement("th");
      campo.scope = "row";
      campo.textContent = etiqueta;
      fila.append(
        campo, celda(mios[clave], mios[clave] === base.valores[clave]), celda(suyos[clave])
      );
      return fila;
    });
  if (filas.length === 0) {
    const fila = document.createElement("tr");
    const unica = document.createElement("td");
    unica.colSpan = 3;
    unica.textContent = "Guardó lo mismo que escribiste vos.";
    fila.append(unica);
    filas.push(fila);
  }
  $("conflicto-diferencias").replaceChildren(...filas);
  const descartar = $("conflicto-descartar");
  descartar.textContent = "Descartar los míos";
  delete descartar.dataset.confirmar;
  $("conflicto").showModal();
}

// Cierra el modal, deja lo tuyo en pantalla y muestra la versión nueva al lado.
function verVersionNueva() {
  const suyos = valoresDe(conflicto.ficha);
  $("version-nueva-titulo").textContent =
    `Versión ${conflicto.version_actual} de ${conflicto.editada_por || "otra persona"} (solo lectura)`;
  const items = CAMPOS_DEL_CONFLICTO.flatMap(([clave, etiqueta]) => {
    const termino = document.createElement("dt");
    termino.textContent = etiqueta;
    const valor = document.createElement("dd");
    valor.textContent = suyos[clave] || "(vacío)";
    return [termino, valor];
  });
  $("version-nueva-campos").replaceChildren(...items);
  $("version-nueva").hidden = false;
  $("conflicto").close();
  $("version-nueva").scrollIntoView({ block: "start" });
}

// Cómo se pone en el formulario cada campo de una versión.
const ESCRIBIR_CAMPO = {
  tipo: (version) => elegir("tipo", version.frontmatter.tipo, etiquetaTipo),
  tipo_actividad: (version) =>
    elegir("tipo-actividad", version.frontmatter.tipo_actividad, (tipo) => tipo),
  fecha: (version) => cargarFecha(version.frontmatter),
  origen: (version) => { $("origen").value = version.frontmatter.origen || ""; },
  propietario: (version) => { $("propietario").value = version.frontmatter.propietario || ""; },
  autor: (version) => elegirAutor(version.frontmatter.autor),
  carreras: (version) => elegirCarreras(version.frontmatter.carreras || []),
  resumen: (version) => { $("resumen").value = version.frontmatter.resumen || ""; },
  cuerpo: (version) => { $("transcripcion").value = version.cuerpo; },
  contexto: (version) => { $("contexto").value = version.contexto; },
};

// Lo que cambiaste se guarda sobre la versión nueva y lo que no tocaste toma lo de la otra
// persona, así no se pisa su trabajo. Si alguien volvió a guardar, vuelve el modal.
function aplicarSobreLaNueva() {
  const mios = valoresDelFormulario();
  for (const [clave] of CAMPOS_DEL_CONFLICTO) {
    if (mios[clave] === base.valores[clave]) ESCRIBIR_CAMPO[clave](conflicto.ficha);
  }
  base = { version: conflicto.version_actual, valores: valoresDe(conflicto.ficha) };
  // Lo que se firma ahora lleva lo de la versión nueva: su alerta pasa por M2 (109).
  ficha.informe_inyeccion = conflicto.ficha.informe_inyeccion;
  pintarAlerta();
  refrescarFecha();
  refrescarFaltantes();
  refrescarMarcas();
  $("conflicto").close();
  $("version-nueva").hidden = true;
  if (botonDelGuardado === $("guardar-completo")) firmar(botonDelGuardado);
  else guardar(botonDelGuardado);
}

// Recarga la vigente. Si cambió lo escrito a mano, pide un segundo clic.
async function descartarLosMios(evento) {
  const boton = evento.currentTarget;
  const mios = valoresDelFormulario();
  const aMano = mios.cuerpo !== base.valores.cuerpo || mios.contexto !== base.valores.contexto;
  if (aMano && !boton.dataset.confirmar) {
    boton.dataset.confirmar = "1";
    boton.textContent = "Sí, descartar lo que escribí";
    return;
  }
  $("conflicto").close();
  $("version-nueva").hidden = true;
  try {
    await recargarFicha();
  } catch (error) {
    $("error-guardado").textContent = error.message;
    $("error-guardado").hidden = false;
  }
}

// Los `campos_faltantes` que devuelve el servidor, con el nombre que la persona ve. La
// Fecha entra tres veces y sale una: es un solo dato en la pantalla.
function enPalabras(campos) {
  const vistos = [];
  for (const campo of campos) {
    const etiqueta = (CONTROL_DE_CAMPO[campo] || {}).etiqueta || campo;
    if (!vistos.includes(etiqueta)) vistos.push(etiqueta);
  }
  return vistos.join(", ");
}

function horaDe(marca) {
  const momento = marca ? new Date(marca) : new Date();
  const valido = !Number.isNaN(momento.getTime()) ? momento : new Date();
  return valido.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
}

// Con la Pieza Recibida viajan solo los campos duros y el Contexto (152).
function cuerpoEnLectura() {
  const {
    tipo, tipo_actividad, fecha_inicio, precision_fecha, fecha_fin, origen, propietario, autor,
    carreras, contexto,
  } = cuerpoDelPedido();
  return {
    tipo, tipo_actividad, fecha_inicio, precision_fecha, fecha_fin, origen, propietario, autor,
    carreras, contexto,
  };
}

async function guardarEnLectura() {
  const respuesta = await pedir(`/api/piezas/${encodeURIComponent(PIEZA_ID)}/en-lectura`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cuerpoEnLectura()),
  });
  ficha.campos_de_la_persona = (await respuesta.json()).campos_de_la_persona;
  base = { version: 0, valores: valoresDelFormulario() };
  sucio = false;
  refrescarMarcas();
  $("firma").textContent = `Guardado (${horaDe()}). ${SIN_FIRMA_EN_LECTURA}`;
}

async function guardar(boton) {
  // Un solo guardado a la vez: el segundo leería «Guardando…» como su texto y lo dejaría así.
  if (guardando) return;
  guardando = true;
  habilitarDecisiones();
  const otro = boton === $("guardar-completo") ? $("guardar-parcial") : $("guardar-completo");
  const textoOriginal = boton.textContent;
  boton.textContent = "Guardando…";
  boton.disabled = true;
  otro.disabled = true;
  $("error-guardado").hidden = true;
  $("reintentar").hidden = true;
  botonDelGuardado = boton;
  try {
    if (enLectura) {
      await guardarEnLectura();
      return;
    }
    await reloj;
    const respuesta = await pedir(`/api/piezas/${encodeURIComponent(PIEZA_ID)}/ficha`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cuerpoDelPedido()),
    });
    const resultado = await respuesta.json();
    base = { version: resultado.version, valores: valoresDelFormulario() };
    // La alerta pasa a ser la de lo guardado: firmar después la repite (109).
    ficha.informe_inyeccion = resultado.informe_inyeccion;
    informeDelResumen = null; // lo guardado ya trae el resumen revisado
    pintarAlerta();
    // Y el aviso de datos personales, el de lo que quedó guardado (125).
    ficha.datos_personales = resultado.datos_personales;
    pintarDatosPersonales();
    sucio = false;
    // El reloj que se acaba de cerrar no se reusa, y acá no se arranca otro: casi siempre
    // de la confirmación se sale navegando, y un reloj nuevo en este punto sería una fila
    // abandonada por cada Pieza validada. Si la persona vuelve a editar en esta misma
    // pantalla —guardó Incompleta y consiguió el dato—, `alEditar` arranca otro: ese
    // trabajo también se mide, aparte del intento anterior (US 20, US 24).
    medicionId = null;
    relojCerrado = true;
    pintarBadge(resultado.estado);
    // La firma escribió las aceptadas en el cuerpo (144): se trae la Ficha para mostrarlas, dentro
    // del guardado y antes de la confirmación, así nadie edita ni firma sobre lo que se repinta.
    // Lo guardado ya está; si esto falla, solo queda en pantalla lo de antes.
    if (resultado.estado === "Validada" && descripcionDeLaFoto.afirmaciones.length) {
      await recargarFicha(true).catch(() => {});
    }
    mostrarConfirmacion(resultado);
  } catch (error) {
    if (error.problema && error.problema.type === "/errores/version-desactualizada") {
      mostrarConflicto(error.problema);
      return;
    }
    // La lectura terminó mientras se guardaba: se trae la Ficha y lo escrito queda (152).
    if (enLectura && error.problema && error.problema.type === "/errores/transicion-invalida") {
      await terminarLectura();
      $("error-guardado").textContent =
        "La lectura terminó mientras guardabas. Lo que escribiste sigue acá: guardalo de nuevo.";
      $("error-guardado").hidden = false;
      return;
    }
    // `fetch` rechaza sin respuesta cuando no hay conexión.
    const sinConexion = error instanceof TypeError;
    $("error-guardado").textContent = sinConexion
      ? "No se pudo guardar. Lo escrito sigue acá."
      : error.message;
    $("error-guardado").hidden = false;
    $("reintentar").hidden = !sinConexion;
  } finally {
    guardando = false;
    siguienteAlFirmar = false; // Firmada, Incompleta o fallida: el próximo «Validar y firmar» no salta.
    boton.textContent = textoOriginal;
    boton.disabled = false;
    otro.disabled = false;
    refrescarFirma();
    habilitarDecisiones();
  }
}

// ── Alerta de inyección (109) ─────────────────────────────────────────────
// Los hallazgos que encienden la alerta: alta o media, como `InformeInyeccion.alerta`. Son de
// la versión que se abrió; lo que se edite después lo revisa el servidor al guardar.
// También los del resumen rehecho que la pantalla puso en el campo y todavía no se guardó (146):
// el servidor lo revisó al rehacerlo, y firmar no puede saltear su alerta.
function hallazgosDeAlerta() {
  return [ficha.informe_inyeccion, informeDelResumen].flatMap((informe) =>
    informe && informe.alerta ? informe.hallazgos.filter((h) => h.severidad !== "baja") : []);
}

const SECCIONES = {
  cuerpo: "cuerpo", contexto: "Contexto", resumen: "Resumen", origen: "Origen",
  texto_visible: "texto visible", afirmaciones: "Afirmaciones",
};

function lugarDe(hallazgo) {
  const lugar = [SECCIONES[hallazgo.seccion] || hallazgo.seccion];
  if (hallazgo.hoja) lugar.push(`hoja «${hallazgo.hoja}»`);
  lugar.push(hallazgo.fila ? `fila ${hallazgo.fila}` : `línea ${hallazgo.linea}`);
  return lugar.join(", ");
}

function cuantasFrases(hallazgos) {
  // Más de 50 hallazgos: el informe guarda los primeros (020).
  const mas = [ficha.informe_inyeccion, informeDelResumen].some((i) => i && i.truncado)
    ? "más de " : "";
  return hallazgos.length === 1 && !mas
    ? "Hay 1 frase que parece una orden a un asistente o texto oculto"
    : `Hay ${mas}${hallazgos.length} frases que parecen órdenes a un asistente o texto oculto`;
}

// El extracto es texto no confiable: siempre `textContent`, nunca HTML (RNF-21).
function listarHallazgos(lista, hallazgos) {
  const items = hallazgos.slice(0, 5).map((hallazgo) => {
    const item = document.createElement("li");
    item.textContent = `«${hallazgo.extracto}» (${lugarDe(hallazgo)})`;
    return item;
  });
  if (hallazgos.length > 5) {
    const resto = document.createElement("li");
    resto.textContent = `y ${hallazgos.length - 5} más`;
    items.push(resto);
  }
  lista.replaceChildren(...items);
}

function pintarAlerta() {
  const hallazgos = hallazgosDeAlerta();
  $("alerta-inyeccion").hidden = hallazgos.length === 0;
  if (!hallazgos.length) return;
  $("alerta-inyeccion-texto").textContent =
    `${cuantasFrases(hallazgos)}. No se bloqueó nada: leelas antes de firmar.`;
  listarHallazgos($("alerta-inyeccion-hallazgos"), hallazgos);
}

// ── Aviso de datos personales (125) ───────────────────────────────────────
// Lo calcula el servidor sobre lo guardado (`dominio`, por `validacion`); sin los controles de
// C31 la clave no llega y no se muestra nada. Dice qué y dónde, sin repetir el número.
const NOMBRE_DEL_DATO = { dni: "un DNI", cuil: "un CUIL", cbu: "un CBU" };
const LUGAR_DEL_DATO = { cuerpo: "el cuerpo", contexto: "el Contexto", resumen: "el resumen" };

function pintarDatosPersonales() {
  const avisos = ficha.datos_personales || [];
  $("aviso-datos-personales").hidden = avisos.length === 0;
  if (!avisos.length) return;
  const frases = [...new Set(avisos.map((a) =>
    `${NOMBRE_DEL_DATO[a.tipo] || a.tipo} en ${LUGAR_DEL_DATO[a.campo] || a.campo}`))];
  $("aviso-datos-personales-texto").textContent =
    `Lo guardado parece tener ${frases.join(", ")}. No se bloqueó nada: revisá si tiene que ` +
    "quedar y, si un pedido lo pide, que la Pieza no sea publicable.";
}

function pintarPublicable() {
  $("campo-publicable").hidden = ficha.puede_marcar_publicable !== true;
  $("publicable").checked = ficha.publicable !== false;
}

// «Validar y firmar» con alerta pasa por M2; sin hallazgos se firma directo. Nunca bloquea.
function firmar(boton) {
  const hallazgos = hallazgosDeAlerta();
  if (!hallazgos.length) {
    guardar(boton);
    return;
  }
  $("alerta-firma-texto").textContent = `${cuantasFrases(hallazgos)}:`;
  listarHallazgos($("alerta-firma-hallazgos"), hallazgos);
  $("alerta-firma").showModal();
}

function mostrarConfirmacion(resultado) {
  const caja = $("confirmacion");
  const validada = resultado.estado === "Validada";
  caja.className = validada ? "validada" : "incompleta";
  // Mismo tamaño de caja y mismo botón de salida en los dos casos: la única diferencia
  // es el ícono y el texto. Ninguno de los dos usa la palabra "error" (§5).
  $("confirmacion-titulo").textContent = validada
    ? "✓ Ficha Validada"
    : "● Ficha guardada, queda Incompleta";
  const quien = nombres.get(resultado.validada_por) || $("persona").textContent || "vos";
  $("confirmacion-cuerpo").textContent = validada
    ? `Quedó firmada por ${quien}, hoy a las ${horaDe(resultado.validada_el)}. ` +
      "Ya está lista para buscarse en el Acervo." +
      (resultado.metodo_autenticacion === "dev"
        ? " Firma de ensayo, identidad no verificada." : "")
    : "No se perdió nada de lo que cargaste. Cuando consigas el dato que falta, la volvés " +
      "a abrir desde tu lista y la completás.";
  const detalleFaltantes = $("confirmacion-detalle");
  const faltantes = resultado.campos_faltantes || [];
  detalleFaltantes.hidden = validada ? !descripcionDeLaFoto.afirmaciones.length : faltantes.length === 0;
  detalleFaltantes.textContent = validada
    ? aceptadasEnElCuerpo()
    : `Todavía falta: ${enPalabras(faltantes)}`;
  $("ver-ficha").hidden = !validada;
  caja.hidden = false;
  $("ir-a-la-siguiente").focus();
  if (validada && siguienteAlFirmar) abrirLaSiguientePendiente();
}

function aceptadasEnElCuerpo() {
  const aceptadas = descripcionDeLaFoto.afirmaciones.filter((a) => a.estado === "aceptada").length;
  if (aceptadas === 0) return "Rechazaste todas las Afirmaciones: ninguna entró al cuerpo de la Ficha.";
  const cuales = aceptadas === 1
    ? "La Afirmación aceptada quedó"
    : `Las ${aceptadas} Afirmaciones aceptadas quedaron`;
  return `${cuales} en el cuerpo de la Ficha; las rechazadas, no.`;
}

// «Ver la Ficha firmada»: cierra la confirmación y lleva al cuerpo, donde quedó lo firmado.
function verFichaFirmada() {
  $("confirmacion").hidden = true;
  $("transcripcion").focus();
}

// La siguiente Pendiente del mismo Lote, o de Mis Piezas si es suelta (147): el mismo criterio para
// «Ir a la siguiente» y para Enter. Una Pendiente ya está procesada: abre sin espera.
async function siguientePendiente() {
  const de = detalle.lote_id ? `lote_id=${encodeURIComponent(detalle.lote_id)}` : "mias=true";
  const { piezas } = await comoJson(`/api/piezas?estado=Pendiente&${de}`);
  return piezas.find((pieza) => pieza.pieza_id !== PIEZA_ID) || null;
}

const abrirPieza = (pieza) => { location.href = `/validar.html?pieza=${encodeURIComponent(pieza.pieza_id)}`; };

// "Ir a la siguiente": si no queda ninguna, se vuelve a la lista, que es donde vive el mensaje de
// cola vacía.
async function irALaSiguiente() {
  try {
    const siguiente = await siguientePendiente();
    if (siguiente) abrirPieza(siguiente);
    else location.href = "/";
  } catch {
    location.href = "/";
  }
}

// ── Salida ────────────────────────────────────────────────────────────────
function salir() {
  if (sucio && !confirm("Tenés cambios sin guardar. ¿Salir igual?")) return;
  sucio = false;
  location.href = "/";
}

window.addEventListener("beforeunload", (evento) => {
  if (sucio) evento.preventDefault();
});

// ── Pintado de la Ficha ───────────────────────────────────────────────────
function pintarBadge(estado) {
  $("badge").className = `badge ${estado}`;
  $("badge").textContent = estado;
}

function pintarFicha() {
  for (const escribir of Object.values(ESCRIBIR_CAMPO)) escribir(ficha);

  // Cambia el rótulo y la ayuda según de dónde salió el cuerpo, no el campo (§2).
  const cuerpo = rotuloDeEstaFicha();
  $("rotulo-transcripcion").textContent = cuerpo.rotulo;
  $("ayuda-transcripcion").textContent = cuerpo.ayuda;
  $("campo-transcripcion").classList.toggle("zona-propuesta", cuerpo.propuesto);
  if (ficha.requiere_analisis_humano || esVideo() || enLectura) {
    $("titulo-ficha").textContent = "Ficha"; // nadie propuso nada
    $("panel-ficha").setAttribute("aria-label", "Ficha");
  }
  if (!ficha.cuerpo && !esVideo() && !enLectura) {
    $("transcripcion").placeholder =
      "No se pudo generar la transcripción. Escribila vos o reintentá el procesamiento.";
  }

  pintarBadge(detalle.estado);
  pintarLectura();
  $("aviso-reapertura").hidden = detalle.estado !== "Incompleta";
  $("aviso-analisis-humano").hidden = !ficha.requiere_analisis_humano;
  pintarAlerta();
  pintarAfirmaciones();
  // El resumen guardado lo propuso un modelo (la v1 o un rehecho) o lo escribió una persona.
  resumenDelModelo = ficha.procedencia && ficha.procedencia.resumen === "modelo"
    ? ficha.frontmatter.resumen : null;
  pintarDatosPersonales();
  pintarPublicable();
  pintarAbiertaPor(detalle.en_edicion_por);
  $("marca-ensayo").hidden = !(detalle.estado === "Validada" && ficha.metodo_autenticacion === "dev");
  armarVigilados(ficha.obligatorios);
  refrescarFecha();
  refrescarFaltantes();
  refrescarMarcas();
  refrescarMarcasDeTiempo();
  pintarPistaDeFecha();
  base = { version: ficha.version, valores: valoresDe(ficha) };
  sucio = false;
  // Un rehecho que llegó con la pantalla cerrada se atiende al abrirla, después de `base`: si se
  // pone en el campo, queda como cambio sin guardar.
  atenderResumen(descripcionDeLaFoto.resumen, false);
  // Quien no tiene VALIDAR la abre en solo lectura (DEC-124, R-05).
  if (!puedeValidar) {
    $("faltantes").hidden = true; // «Podés Validar» o «Falta para validar» no son para él.
    for (const control of $("panel-ficha").querySelectorAll("input, select, textarea")) {
      control.disabled = true;
    }
  }
}

// ── Mientras se lee (152) ─────────────────────────────────────────────────
// Ningún modelo propone los campos duros: se completan con la Pieza Recibida. El cuerpo y el
// resumen no existen todavía y no se firma: «Validar y firmar» espera a la lectura.
const SIN_FIRMA_EN_LECTURA =
  "«Validar y firmar» se habilita cuando termine la lectura: todavía no hay cuerpo ni resumen.";
const FIRMA = $("firma").textContent;
let sondeo = null;

function pintarLectura() {
  $("aviso-en-lectura").hidden = !enLectura;
  for (const id of ["transcripcion", "resumen"]) {
    $(id).disabled = enLectura || !puedeValidar;
    if (enLectura) $(id).placeholder = "Aparece cuando termine la lectura.";
  }
  refrescarFirma();
}

// «Validar y firmar» espera a la lectura (152) y a que se decida cada Afirmación (155): con alguna
// sin decidir, el servidor igual dejaría la Ficha Incompleta (144). La razón queda escrita.
// Tampoco con un guardado o una decisión en vuelo: la firma saldría con lo que el servidor tenga.
// 148: ni con imágenes describiéndose. Ni con el resumen rehaciéndose (146): firmaría el de antes, que puede nombrar una rechazada.
const esperaElResumen = () => rehaciendoResumen && !esperaSalteada;
const firmable = () => !enLectura && !guardando && decisionesEnVuelo.size === 0
  && sinDecidir().length === 0 && !esperaElResumen() && enCurso().length === 0;
// Con todo completo, «Guardar como está» también firma: mientras se rehace el resumen, espera.
const firmaria = () => vigilados.every((vigilado) => !vigilado.vacio());

function refrescarFirma() {
  $("guardar-completo").disabled = !firmable();
  if (!guardando) $("guardar-parcial").disabled = esperaElResumen() && firmaria();
  if (!puedeValidar) return;
  const faltan = sinDecidir().length;
  const describiendose = enCurso().length;
  $("firma").textContent = enLectura ? SIN_FIRMA_EN_LECTURA
    : describiendose ? `Se está describiendo ${cuantasImagenes(describiendose)}: «Validar y firmar» ` +
      "se habilita cuando lleguen sus Afirmaciones y las decidas. «Guardar como está» la deja Incompleta."
    : faltan ? `Falta decidir ${cuantasAfirmaciones(faltan)}: «Validar y firmar» se habilita ` +
      "cuando decidas todas. «Guardar como está» la deja Incompleta."
    : esperaElResumen() && !$("no-esperar-resumen").hidden ? "«Validar y firmar» espera el " +
      "resumen nuevo. Si no querés esperarlo, tocá «No esperar el resumen nuevo», junto al resumen."
    : esperaElResumen() ? "Se está rehaciendo el resumen sin las Afirmaciones rechazadas: " +
      "«Validar y firmar» se habilita cuando llegue. Mientras, podés seguir revisando."
    : FIRMA;
}

// ── Afirmaciones de una foto (155, ADR-0007) ──────────────────────────────
// La descripción llega como una lista de datos cortos: cada uno se acepta o se rechaza con un clic
// y la decisión se guarda en el momento, con quién la tomó (144); solo las aceptadas entran al
// cuerpo al firmar. 145: cada una llega cruzada con la lectura automática; lo sin respaldo va
// primero, lo respaldado se acepta en bloque y aceptar lo de riesgo alto sin respaldo pide
// confirmación. El teclado (147) se suma sobre `itemDeAfirmacion` y `decidir`.
const ESTADO_DE_AFIRMACION = {
  sin_decidir: "Sin decidir", aceptada: "✓ Aceptada", rechazada: "✗ Rechazada",
};
const sinDecidir = () => descripcionDeLaFoto.afirmaciones.filter((a) => a.estado === "sin_decidir");
const cuantasAfirmaciones = (n) => (n === 1 ? "1 Afirmación" : `${n} Afirmaciones`);
const enCurso = () => descripcionDeLaFoto.imagenes.filter((i) => i.estado === "encolada");
const cuantasImagenes = (n) => (n === 1 ? "1 imagen" : `${n} imágenes`);
const ESTADO_DE_IMAGEN = {
  sin_describir: "Sin describir: se describen solas las primeras de cada Pieza.",
  encolada: "Describiéndose… Puede tardar unos minutos; las Afirmaciones aparecen solas.",
  descrita: "Descrita.",
  fallida: "La descripción falló.",
};
let sondeoDeDescripciones = null;

async function traerDescripcion() {
  descripcionDeLaFoto = enLectura
    ? SIN_AFIRMACIONES
    : await comoJson(`/api/piezas/${encodeURIComponent(PIEZA_ID)}/afirmaciones`);
}

// `salvoSiEscribio`: si la persona escribió mientras llegaba, no se le pisa lo escrito.
async function recargarFicha(salvoSiEscribio = false) {
  const url = `/api/piezas/${encodeURIComponent(PIEZA_ID)}`;
  const [nuevoDetalle, nuevaFicha, nuevaDescripcion] = await Promise.all([
    comoJson(url), comoJson(`${url}/ficha`), comoJson(`${url}/afirmaciones`),
  ]);
  if (salvoSiEscribio && sucio) return;
  detalle = nuevoDetalle;
  ficha = nuevaFicha;
  descripcionDeLaFoto = nuevaDescripcion;
  pintarFicha();
}

// Se deciden con la Pieza Pendiente o Incompleta, hasta que una firma las escribe en el cuerpo:
// desde ahí se corrigen en el cuerpo y el servidor rechaza la decisión (144).
function decidibles() {
  return puedeValidar && ["Pendiente", "Incompleta"].includes(detalle.estado)
    && !conAfirmacionesFirmadas();
}

// Lo dice el servidor, con la misma regla con que rechaza la decisión.
const conAfirmacionesFirmadas = () => Boolean(descripcionDeLaFoto.firmadas);

// Aceptar y Rechazar esperan al guardado y a la respuesta de su propia decisión.
function habilitarDecisiones() {
  for (const boton of $("bloque-afirmaciones").querySelectorAll("button[data-orden]")) {
    boton.disabled = guardando || decisionesEnVuelo.has(Number(boton.dataset.orden));
  }
  $("aceptar-respaldadas").disabled = guardando || decisionesEnVuelo.size > 0;
}

const respaldadasSinDecidir = () => sinDecidir().filter((a) => a.respaldada);

// «Aceptar las N respaldadas»: solo las que la lectura automática respalda y nadie decidió todavía.
function pintarEnBloque() {
  const n = respaldadasSinDecidir().length;
  const boton = $("aceptar-respaldadas");
  boton.hidden = !decidibles() || n === 0;
  boton.textContent = n === 1 ? "✓ Aceptar la 1 respaldada" : `✓ Aceptar las ${n} respaldadas`;
}

$("aceptar-respaldadas").addEventListener("click", async () => {
  let aceptadas = 0;
  for (const afirmacion of respaldadasSinDecidir()) {
    // La lista se armó al empezar: la que alguien decidió mientras tanto no se toca.
    if (afirmacion.estado !== "sin_decidir") continue;
    if (!(await decidir(afirmacion, "aceptada", false))) break;
    aceptadas += 1;
  }
  // El botón se va con la última: el foco queda en el título del grupo, no se pierde.
  $("titulo-respaldadas").focus();
  const faltan = sinDecidir().length;
  anunciar(`Aceptaste ${aceptadas === 1 ? "1 Afirmación respaldada" : `${aceptadas} Afirmaciones respaldadas`}. ` +
    (faltan ? `Falta decidir ${cuantasAfirmaciones(faltan)}.` : "Están todas decididas."));
});

function pintarAfirmaciones() {
  const { aviso, imagenes } = descripcionDeLaFoto;
  // Las de la foto entera van sueltas; las de cada imagen embebida, con su imagen (148).
  const afirmaciones = descripcionDeLaFoto.afirmaciones.filter((a) => !a.imagen);
  $("bloque-afirmaciones").hidden = !afirmaciones.length && !aviso && !imagenes.length;
  $("afirmaciones-titulo").textContent = imagenes.length
    ? "🤖 Afirmaciones de las imágenes" : "🤖 Afirmaciones de la imagen";
  // Plan B de 144: el modelo no cumplió el esquema y la descripción la escribe la persona.
  $("aviso-descripcion-fallida").hidden = !aviso;
  $("aviso-descripcion-fallida").textContent = aviso
    ? `${aviso}. Escribí vos en el cuerpo lo que se ve en la foto, arriba del texto visible.`
    : "";
  $("ayuda-afirmaciones").hidden = !afirmaciones.length && !imagenes.length;
  $("ayuda-afirmaciones").textContent = imagenes.length
    ? (decidibles()
      ? "Las imágenes casi sin texto del original las describe un modelo, un dato por renglón. " +
        "Aceptá lo que cada imagen muestra y rechazá lo demás: solo lo aceptado entra a la Ficha."
      : "Lo que un modelo propuso sobre cada imagen del original y cómo se decidió.")
    : decidibles()
      ? "Lo que un modelo dice que se ve en la foto, un dato por renglón. Aceptá las que el " +
        "original muestra y rechazá las demás: solo las aceptadas entran a la Ficha."
      : "Lo que un modelo propuso sobre la foto y cómo se decidió.";
  // Orden estable: dentro de cada grupo, el del modelo. Lo demás (el foco de «Falta») lo sigue.
  afirmaciones.sort((a, b) => Number(Boolean(a.respaldada)) - Number(Boolean(b.respaldada)));
  const sinRespaldo = afirmaciones.filter((a) => !a.respaldada);
  const respaldadas = afirmaciones.filter((a) => a.respaldada);
  $("grupo-sin-respaldo").hidden = !sinRespaldo.length;
  $("titulo-sin-respaldo").textContent = `Sin respaldo en la lectura automática (${sinRespaldo.length})`;
  $("afirmaciones-sin-respaldo").replaceChildren(...sinRespaldo.map(itemDeAfirmacion));
  $("grupo-respaldadas").hidden = !respaldadas.length;
  $("titulo-respaldadas").textContent = `Respaldadas por la lectura automática (${respaldadas.length})`;
  $("afirmaciones-respaldadas").replaceChildren(...respaldadas.map(itemDeAfirmacion));
  pintarEnBloque();
  $("imagenes-embebidas").replaceChildren(...imagenes.map(itemDeImagen));
  // 147: los atajos recorren las de la foto y después las de cada imagen embebida.
  const primera = $("campo-afirmaciones").querySelector("li.afirmacion");
  if (primera && decidibles()) primera.tabIndex = 0;
  $("atajos-afirmaciones").hidden = !decidibles() || !descripcionDeLaFoto.afirmaciones.length;
  habilitarDecisiones();
  vigilarDescripciones();
}

// 148: cada imagen embebida sin texto con su ubicación y su miniatura (sacada del original, no
// guardada), dónde está su descripción y sus Afirmaciones. Pasado el tope de las que se describen
// solas, «Describir esta imagen» la encola.
function itemDeImagen(imagen) {
  const caja = document.createElement("section");
  caja.className = "imagen-embebida";
  const titulo = document.createElement("h4");
  titulo.id = `imagen-${imagen.imagen}`;
  titulo.tabIndex = -1;
  titulo.textContent = imagen.ubicacion;
  caja.setAttribute("aria-labelledby", titulo.id);
  const miniatura = document.createElement("img");
  miniatura.className = "miniatura";
  miniatura.src = `/api/piezas/${encodeURIComponent(PIEZA_ID)}/imagenes/${encodeURIComponent(imagen.imagen)}/miniatura`;
  miniatura.alt = `Miniatura de la imagen «${imagen.ubicacion}» del original`;
  miniatura.loading = "lazy";
  // Sin miniatura (un .doc o un .xls procesado antes de 166 no tiene su convertido), el mismo
  // aviso que da el servidor en vez de una imagen rota.
  miniatura.addEventListener("error", () => {
    const aviso = document.createElement("p");
    aviso.className = "miniatura-ausente";
    aviso.textContent = "No se pudo mostrar la miniatura. La imagen se ve en el original.";
    miniatura.replaceWith(aviso);
  }, { once: true });
  const estado = document.createElement("p");
  estado.className = "estado-imagen";
  estado.textContent = imagen.aviso && imagen.estado === "descrita"
    ? `${imagen.aviso}. Si hace falta, escribí en el cuerpo lo que muestra.`
    : ESTADO_DE_IMAGEN[imagen.estado];
  caja.append(titulo, miniatura, estado);
  if (imagen.alerta_inyeccion) {
    const alerta = document.createElement("p");
    alerta.className = "aviso-reapertura";
    alerta.textContent = "⚠ Alerta de inyección: una de estas Afirmaciones parece una orden " +
      "dirigida a un modelo. Rechazala si la imagen no la muestra.";
    caja.append(alerta);
  }
  if (decidibles() && ["sin_describir", "fallida"].includes(imagen.estado)) {
    const boton = document.createElement("button");
    boton.type = "button";
    boton.textContent = "Describir esta imagen";
    boton.setAttribute("aria-describedby", titulo.id);
    boton.addEventListener("click", () => describirImagen(imagen, boton));
    caja.append(boton);
  }
  const propias = descripcionDeLaFoto.afirmaciones.filter((a) => a.imagen === imagen.imagen);
  if (propias.length) {
    const lista = document.createElement("ol");
    lista.className = "afirmaciones";
    lista.append(...propias.map(itemDeAfirmacion));
    caja.append(lista);
  }
  return caja;
}

async function describirImagen(imagen, boton) {
  boton.disabled = true;
  $("error-guardado").hidden = true;
  try {
    await pedir(
      `/api/piezas/${encodeURIComponent(PIEZA_ID)}/imagenes/${encodeURIComponent(imagen.imagen)}/descripcion`,
      { method: "POST" },
    );
  } catch (error) {
    $("error-guardado").textContent = error instanceof TypeError
      ? "No se pudo pedir la descripción. Probá de nuevo."
      : error.message;
    $("error-guardado").hidden = false;
    boton.disabled = false;
    return;
  }
  imagen.estado = "encolada";
  pintarAfirmaciones();
  armarVigilados(ficha.obligatorios);
  refrescarFaltantes();
  refrescarFirma();
  $(`imagen-${imagen.imagen}`).focus();
  anunciar(`Se pidió la descripción de «${imagen.ubicacion}». Puede tardar unos minutos.`);
}

// Mientras alguna se describe, se pregunta cada 5 s, como la lectura: las Afirmaciones aparecen
// sin recargar. No repinta con un guardado o una decisión en vuelo: espera a la vuelta siguiente.
function vigilarDescripciones() {
  if (sondeoDeDescripciones || !enCurso().length) return;
  sondeoDeDescripciones = setTimeout(async () => {
    sondeoDeDescripciones = null;
    try {
      if (!guardando && decisionesEnVuelo.size === 0) {
        const antes = enCurso();
        const nueva = await comoJson(`/api/piezas/${encodeURIComponent(PIEZA_ID)}/afirmaciones`);
        const llegaron = antes.filter((i) => {
          const ahora = nueva.imagenes.find((n) => n.imagen === i.imagen);
          return ahora && ahora.estado !== "encolada";
        });
        if (llegaron.length) {
          descripcionDeLaFoto = nueva;
          pintarAfirmaciones();
          armarVigilados(ficha.obligatorios);
          refrescarFaltantes();
          refrescarFirma();
          anunciar(`Llegaron las Afirmaciones de ${llegaron.map((i) => `«${i.ubicacion}»`).join(", ")}.`);
          return;
        }
      }
    } catch { /* se reintenta en la próxima vuelta */ }
    vigilarDescripciones();
  }, 5000);
}

// ── La región de una Afirmación sobre el original (145, «Show me the evidence») ──
// Se marca encima de la imagen entera, sin tapar ni recortar nada: la evidencia sacada de contexto
// convence de más. Lo que dice la marca está en texto en el ítem (`.respaldo`).
function resaltar(afirmacion) {
  const lienzo = $("lienzo-original");
  if (!lienzo) return;
  for (const marca of lienzo.querySelectorAll(".region-resaltada")) marca.remove();
  const imagen = lienzo.querySelector("img");
  const regiones = afirmacion?.region || [];
  if (!regiones.length || !imagen?.naturalWidth) return;
  const marcas = regiones.map((r) => {
    const marca = document.createElement("div");
    marca.className = "region-resaltada";
    marca.setAttribute("aria-hidden", "true");
    Object.assign(marca.style, {
      left: `${(r.izquierda / imagen.naturalWidth) * 100}%`,
      top: `${(r.arriba / imagen.naturalHeight) * 100}%`,
      width: `${(r.ancho / imagen.naturalWidth) * 100}%`,
      height: `${(r.alto / imagen.naturalHeight) * 100}%`,
    });
    return marca;
  });
  lienzo.append(...marcas);
  // Si la marca quedó fuera de la parte visible del visor, se lo corre hasta ella.
  const vista = visor.getBoundingClientRect();
  const caja = marcas[0].getBoundingClientRect();
  if (caja.top < vista.top || caja.bottom > vista.bottom) visor.scrollTop += caja.top - vista.top - 40;
  if (caja.left < vista.left || caja.right > vista.right) visor.scrollLeft += caja.left - vista.left - 40;
}

const dondeSeLee = (afirmacion) =>
  `En la imagen se lee: ${afirmacion.region.map((r) => `«${r.texto}»`).join(", ")}.`;

// Cada ítem es un grupo con el texto por nombre y dos botones de alternancia: el estado se lee en
// palabras, con ícono y en `aria-pressed`, no solo por el color.
function itemDeAfirmacion(afirmacion) {
  const { orden } = afirmacion;
  const item = document.createElement("li");
  item.id = `afirmacion-${orden}`;
  // 147: el ítem toma el foco para los atajos; un solo lugar de tabulación para todos (`pintarAfirmaciones`).
  if (decidibles()) item.tabIndex = -1;
  const texto = document.createElement("p");
  texto.className = "texto-afirmacion";
  texto.id = `texto-afirmacion-${orden}`;
  texto.textContent = afirmacion.texto;
  const grupo = document.createElement("div");
  grupo.className = "decision";
  grupo.setAttribute("role", "group");
  grupo.setAttribute("aria-labelledby", texto.id);
  const estado = document.createElement("span");
  estado.className = "estado-afirmacion";
  grupo.append(estado);
  if (decidibles()) {
    for (const [decision, verbo, icono] of [["aceptada", "Aceptar", "✓"], ["rechazada", "Rechazar", "✗"]]) {
      const boton = document.createElement("button");
      boton.type = "button";
      boton.id = `${verbo.toLowerCase()}-${orden}`;
      boton.dataset.decision = decision;
      boton.dataset.orden = String(orden);
      boton.setAttribute("aria-label", `${verbo} la Afirmación ${orden}`);
      const marca = document.createElement("span");
      marca.setAttribute("aria-hidden", "true");
      marca.textContent = icono;
      boton.append(marca, ` ${verbo}`);
      boton.addEventListener("click", () => decidir(afirmacion, decision));
      grupo.append(boton);
    }
  }
  item.append(texto);
  if (afirmacion.respaldada) {
    // La alternativa en texto de la región resaltada: lo que leyó ahí la lectura automática.
    const respaldo = document.createElement("p");
    respaldo.className = "respaldo";
    respaldo.id = `respaldo-${orden}`;
    respaldo.textContent = dondeSeLee(afirmacion);
    grupo.setAttribute("aria-describedby", respaldo.id);
    // Sin botones (firmada o sin VALIDAR), el foco llega igual para ver la región.
    if (!decidibles()) respaldo.tabIndex = 0;
    item.append(respaldo);
    item.addEventListener("mouseenter", () => resaltar(afirmacion));
    item.addEventListener("mouseleave", () => resaltar(null));
    item.addEventListener("focusin", () => resaltar(afirmacion));
    item.addEventListener("focusout", (evento) => {
      if (!item.contains(evento.relatedTarget)) resaltar(null);
    });
  }
  item.append(grupo);
  pintarDecision(item, afirmacion);
  return item;
}

// Buçinca et al. (2021): la fricción va solo donde el riesgo es alto. El servidor dice cuándo.
const MOTIVO_DE_CONFIRMACION = {
  datos: "nombra una sigla, una fecha, un número o un nombre que la lectura automática no " +
    "encontró en la imagen",
  personas: "habla de personas y la imagen no es una foto: en un cartel o una página, una " +
    "persona suele ser un ícono que el modelo tomó por gente",
};

function pintarDecision(item, afirmacion) {
  item.className = `afirmacion ${afirmacion.estado}`;
  item.querySelector(".estado-afirmacion").textContent = ESTADO_DE_AFIRMACION[afirmacion.estado];
  for (const boton of item.querySelectorAll("button")) {
    boton.setAttribute("aria-pressed", String(boton.dataset.decision === afirmacion.estado));
  }
}

// Se puede cambiar de idea hasta firmar. No toca el formulario: la decisión ya quedó guardada.
// Devuelve si la decisión quedó guardada. `avisar`: el bloque anuncia una sola vez, al final.
async function decidir(afirmacion, decision, avisar = true) {
  const { orden } = afirmacion;
  if (afirmacion.estado === decision || guardando || decisionesEnVuelo.has(orden)) return false;
  const motivo = decision === "aceptada" && MOTIVO_DE_CONFIRMACION[afirmacion.confirmar];
  if (motivo && !confirm(`La Afirmación ${orden} ${motivo}:\n\n«${afirmacion.texto}»\n\n` +
    "¿La aceptás igual? Revisala contra el original.")) return false;
  $("error-guardado").hidden = true;
  decisionesEnVuelo.add(orden);
  habilitarDecisiones();
  refrescarFirma();
  try {
    const respuesta = await pedir(
      `/api/piezas/${encodeURIComponent(PIEZA_ID)}/afirmaciones/${afirmacion.orden}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        // El servidor también la exige (SEG-21): sin confirmar, aceptarla es un 409.
        body: JSON.stringify({ decision, confirmada: Boolean(motivo) }),
      },
    );
    const antes = afirmacion.estado;
    Object.assign(afirmacion, await respuesta.json());
    // Cambió qué está rechazado: el servidor ya encoló el resumen sin las rechazadas (146).
    if ((antes === "rechazada") !== (afirmacion.estado === "rechazada")) {
      atenderResumen({ estado: "rehaciendo" }, true);
    }
  } catch (error) {
    $("error-guardado").textContent = error instanceof TypeError
      ? "No se pudo guardar la decisión. Probá de nuevo."
      : error.message;
    $("error-guardado").hidden = false;
    return false;
  } finally {
    decisionesEnVuelo.delete(orden);
    habilitarDecisiones();
    refrescarFirma();
  }
  pintarDecision($(`afirmacion-${afirmacion.orden}`), afirmacion);
  pintarEnBloque();
  refrescarFaltantes();
  refrescarFirma();
  const faltan = sinDecidir().length;
  if (avisar) {
    anunciar(`Afirmación ${afirmacion.orden} ${afirmacion.estado}. ` +
      (faltan ? `Falta decidir ${cuantasAfirmaciones(faltan)}.` : "Están todas decididas."));
  }
  return true;
}

// ── Resumen rehecho (146, ADR-0007) ───────────────────────────────────────
// Al rechazar una Afirmación, o volver atrás un rechazo, el servidor rehace el resumen sin las
// rechazadas en segundo plano. Llega como propuesta, sin versión nueva. Si el campo tiene el último
// resumen que propuso un modelo, se reemplaza; si la persona lo editó —guardado o no, porque se
// compara lo que hay en el campo—, no se pisa: se le ofrece el nuevo.
function atenderResumen(resumen, avisar) {
  if (!puedeValidar) return;
  const antes = rehaciendoResumen;
  rehaciendoResumen = Boolean(resumen) && resumen.estado === "rehaciendo";
  if (rehaciendoResumen) {
    if (!antes) {
      rehaciendoDesde = Date.now();
      esperaSalteada = false;
    }
    avisoDelResumen = motivoDeLaEspera(resumen);
    esperarElResumen();
  } else {
    clearInterval(sondeoDelResumen);
    sondeoDelResumen = null;
    $("no-esperar-resumen").hidden = true;
  }
  // Uno que la Ficha guardada ya vio (se guardó después de que llegó) no se vuelve a avisar.
  if (resumen && resumen.atendido && rehechoVisto === null) rehechoVisto = resumen.rehecho_el;
  if (resumen && resumen.estado === "listo" && resumen.texto && resumen.rehecho_el !== rehechoVisto) {
    rehechoVisto = resumen.rehecho_el;
    recibirResumen(resumen, avisar);
  } else if (resumen && resumen.estado === "fallido" && avisar) {
    avisoDelResumen = "No se pudo rehacer el resumen. Revisá que no nombre una Afirmación " +
      "rechazada antes de firmar.";
    anunciar(avisoDelResumen);
  }
  pintarAvisoDelResumen();
  refrescarFirma();
}

// Sin la cola, o con el motor que no contesta, el rehecho no llega: se dice por qué y la persona
// puede firmar con el resumen que hay, revisándolo ella.
function motivoDeLaEspera(resumen) {
  const demora = Date.now() - rehaciendoDesde > ESPERA_DEL_RESUMEN_MS;
  $("no-esperar-resumen").hidden = esperaSalteada || !(resumen.frenado || demora);
  if (resumen.frenado) {
    return "El procesamiento está en pausa: el resumen no se rehace hasta que se reanude. Podés " +
      "firmar con el resumen que hay si revisás que no nombre una Afirmación rechazada.";
  }
  return demora
    ? "El resumen nuevo está tardando: puede que el modelo no responda. Podés esperarlo o firmar " +
      "con el resumen que hay si revisás que no nombre una Afirmación rechazada."
    : "Rehaciendo el resumen sin las Afirmaciones rechazadas…";
}

function noEsperarElResumen() {
  esperaSalteada = true;
  $("no-esperar-resumen").hidden = true;
  avisoDelResumen = "No se espera el resumen nuevo: revisá el que hay antes de firmar.";
  pintarAvisoDelResumen();
  refrescarFirma();
  anunciar(`${avisoDelResumen} «Validar y firmar» está habilitado.`);
}

function recibirResumen(resumen, avisar) {
  const campo = $("resumen");
  const texto = resumen.texto;
  resumenOfrecido = null;
  $("no-esperar-resumen").hidden = true;
  if (campo.value.trim() === texto.trim()) {
    resumenDelModelo = texto; // ya está: lo guardó la persona sin cambios
    avisoDelResumen = "";
  } else if (resumenDelModelo !== null && campo.value.trim() === resumenDelModelo.trim()) {
    ponerResumen(resumen);
    avisoDelResumen = "Llegó el resumen nuevo, sin las Afirmaciones rechazadas. Revisalo antes " +
      "de firmar.";
    if (avisar) anunciar(avisoDelResumen);
  } else {
    resumenOfrecido = resumen;
    avisoDelResumen = "Llegó un resumen nuevo, sin las Afirmaciones rechazadas. No reemplazó el " +
      "que escribiste.";
    if (avisar) anunciar(`${avisoDelResumen} Podés usarlo con el botón «Usar el resumen nuevo».`);
  }
}

function ponerResumen(resumen) {
  $("resumen").value = resumen.texto;
  resumenDelModelo = resumen.texto;
  informeDelResumen = resumen.informe_inyeccion;
  pintarAlerta();
  alEditar(); // como si se escribiera: marca, faltantes y cambio sin guardar
}

function usarResumenNuevo() {
  ponerResumen(resumenOfrecido);
  resumenOfrecido = null;
  avisoDelResumen = "Pusiste el resumen nuevo. Revisalo antes de firmar.";
  pintarAvisoDelResumen();
  $("resumen").focus();
}

function pintarAvisoDelResumen() {
  $("aviso-resumen").hidden = !avisoDelResumen;
  $("aviso-resumen-texto").textContent = avisoDelResumen;
  $("usar-resumen-nuevo").hidden = resumenOfrecido === null;
}

function esperarElResumen() {
  if (sondeoDelResumen) return;
  sondeoDelResumen = setInterval(async () => {
    try {
      const { resumen } = await comoJson(`/api/piezas/${encodeURIComponent(PIEZA_ID)}/afirmaciones`);
      descripcionDeLaFoto.resumen = resumen;
      atenderResumen(resumen, true);
    } catch { /* se reintenta en la próxima vuelta */ }
  }, 2000);
}

// ── Validación con teclado (147, patrón de Rossum) ────────────────────────
// Con el foco en una Afirmación (el ítem o sus botones): A acepta, R rechaza, ↓/J y ↑/K mueven
// el foco y Enter, sobre el ítem, firma y abre la siguiente Pendiente. Las letras pasan por el
// mismo `decidir` que los botones: la confirmación de 145 y las guardas de 155 valen igual.
// Recorren las de la foto (por riesgo) y después las de cada imagen embebida (148), en ese orden.
// El oyente está en el bloque, no en el documento: escribiendo en un campo no se dispara nada.
// Sin Ctrl, Alt ni ⌘ (son del navegador) y sin `role="application"`: en modo exploración NVDA y
// JAWS se quedan con las letras y las flechas antes que la página, así que no se les pisa ninguna;
// ahí se decide con los botones. Ninguna letra está libre en los dos lectores salvo la J.
const MOVER = { ArrowDown: 1, j: 1, ArrowUp: -1, k: -1 };

$("campo-afirmaciones").addEventListener("keydown", (evento) => {
  if (evento.ctrlKey || evento.altKey || evento.metaKey || evento.isComposing || !decidibles()) return;
  const item = evento.target.closest("li.afirmacion");
  if (!item) return;
  const tecla = evento.key.length === 1 ? evento.key.toLowerCase() : evento.key;
  const afirmacion = descripcionDeLaFoto.afirmaciones.find((a) => item.id === `afirmacion-${a.orden}`);
  if (tecla in MOVER) {
    const items = [...$("campo-afirmaciones").querySelectorAll("li.afirmacion")];
    const destino = items[items.indexOf(item) + MOVER[tecla]];
    if (destino) {
      for (const otro of items) otro.tabIndex = -1;
      destino.tabIndex = 0;
      destino.focus();
      destino.scrollIntoView({ block: "nearest" });
    }
  } else if (tecla === "a") decidir(afirmacion, "aceptada");
  else if (tecla === "r") decidir(afirmacion, "rechazada");
  // Sobre un botón, Enter sigue siendo el clic del botón. Mantenida, no firma de nuevo.
  else if (tecla === "Enter" && evento.target === item && !evento.repeat) firmarConElTeclado();
  else return;
  evento.preventDefault();
});

let siguienteAlFirmar = false; // La firma salió de Enter: al quedar Validada se abre la siguiente.

// Enter firma solo si se puede; si no, dice qué falta. No hay firma sin decidir (ADR-0007).
function firmarConElTeclado() {
  const faltan = vigilados.filter((vigilado) => vigilado.vacio());
  const motivo = guardando ? "Esperá: todavía se está guardando la Ficha."
    : decisionesEnVuelo.size ? "Esperá: todavía se está guardando una decisión."
    : faltan.length
      ? `Todavía no se puede firmar. Falta: ${faltan.map((v) => v.etiqueta).join(", ")}.`
      // El resumen rehecho (146) u otra espera: lo que dice la línea de la firma.
      : firmable() ? "" : `Todavía no se puede firmar. ${$("firma").textContent}`;
  if (motivo) {
    $("error-guardado").textContent = motivo;
    $("error-guardado").hidden = false;
    return;
  }
  siguienteAlFirmar = true;
  firmar($("guardar-completo"));
}

// Volver a revisar desde la alerta de inyección cancela la firma, y con ella el salto.
$("alerta-firma").addEventListener("close", () => { if (!guardando) siguienteAlFirmar = false; });

// Enter firmó: abre la siguiente; si no queda ninguna, la confirmación lo dice y se queda.
async function abrirLaSiguientePendiente() {
  let siguiente;
  try {
    siguiente = await siguientePendiente();
  } catch {
    return; // Queda la confirmación, con «Ir a la siguiente».
  }
  if ($("confirmacion").hidden) return; // Mientras tanto eligió «Ver la Ficha firmada»: se queda.
  if (siguiente) {
    abrirPieza(siguiente);
    return;
  }
  const detalleFirma = $("confirmacion-detalle");
  detalleFirma.textContent = `${detalleFirma.hidden ? "" : `${detalleFirma.textContent} `}` +
    `No quedan Piezas Pendientes ${detalle.lote_id ? "en este Lote" : "en Mis Piezas"}.`;
  detalleFirma.hidden = false;
}

// Sondea como la lista (§6): cuando la Pieza deja de estar Recibida, trae la Ficha.
function esperarLaLectura() {
  sondeo = setInterval(async () => {
    try {
      const estado = (await comoJson(`/api/piezas/${encodeURIComponent(PIEZA_ID)}`)).estado;
      if (estado !== "Recibida") await terminarLectura();
    } catch { /* se reintenta en la próxima vuelta */ }
  }, 5000);
}

// La v1 ya tiene lo guardado; lo que se escribió y no se guardó se vuelve a poner encima, como
// «Aplicar mis cambios sobre la nueva» (M1): nada de lo de la persona se pisa.
async function terminarLectura() {
  if (!enLectura) return;
  clearInterval(sondeo);
  const pedido = cuerpoDelPedido();
  const mios = valoresDelFormulario();
  const tocados = CAMPOS_DEL_CONFLICTO.map(([clave]) => clave)
    .filter((clave) => mios[clave] !== base.valores[clave]);
  const contextoGuardado = base.valores.contexto;
  detalle = await comoJson(`/api/piezas/${encodeURIComponent(PIEZA_ID)}`);
  ficha = await comoJson(`/api/piezas/${encodeURIComponent(PIEZA_ID)}/ficha`);
  enLectura = false;
  await traerDescripcion();
  for (const id of ["transcripcion", "resumen"]) $(id).placeholder = "";
  $("titulo-ficha").textContent = "Ficha propuesta"; // pintarFicha lo cambia si nadie propuso
  $("panel-ficha").setAttribute("aria-label", "Ficha propuesta");
  pintarFicha();
  if (visorEnEspera) {
    visorEnEspera = false;
    armarVisor();
  }
  // Como `guardar_ficha_propuesta`: el Contexto que traía el texto leído va primero y el de la
  // persona después. Se separa lo leído de lo guardado y se une con lo que hay en pantalla, así
  // un guardado posterior no borra lo que trajo el texto.
  const leido = (ficha.contexto || "").trim();
  const delTexto = contextoGuardado && leido.endsWith(contextoGuardado)
    ? leido.slice(0, -contextoGuardado.length).trim()
    : leido;
  const contexto = [delTexto, pedido.contexto.trim()].filter(Boolean).join("\n\n");
  const version = { frontmatter: pedido, cuerpo: pedido.cuerpo, contexto };
  for (const clave of tocados) ESCRIBIR_CAMPO[clave](version);
  if (tocados.length) {
    sucio = true;
    refrescarFecha();
    refrescarFaltantes();
    refrescarMarcas();
  }
  if (puedeValidar) arrancarElReloj();
}

// El borrador se lee y se borra al abrir, se aplique o no: si quedara, la Entrada volvería a
// mandar acá cada vez (perdió VALIDAR, la retiraron, falló la red).
function tomarBorrador() {
  try {
    const borrador = JSON.parse(sessionStorage.getItem("hv_borrador"));
    sessionStorage.removeItem("hv_borrador");
    return borrador;
  } catch {
    return null; // sin almacenamiento
  }
}

// Lo que se editó antes de que se cerrara la sesión, sobre la versión que se había abierto:
// si otra persona guardó mientras tanto, el guardado responde 409 y vuelve el modal M1.
function retomarBorrador(borrador) {
  if (!borrador || borrador.pieza !== PIEZA_ID || borrador.usuario !== usuarioId) return;
  const { pedido } = borrador;
  const version = { frontmatter: pedido, cuerpo: pedido.cuerpo, contexto: pedido.contexto };
  for (const escribir of Object.values(ESCRIBIR_CAMPO)) escribir(version);
  CAMPOS_DE_FECHA.forEach((id, i) => { $(`fecha-${id}`).value = borrador.fecha[i]; });
  base = borrador.base;
  refrescarFecha();
  refrescarFaltantes();
  refrescarMarcas();
  sucio = true;
  $("aviso-borrador").hidden = false;
}

function pintarAbiertaPor(enEdicion) {
  const avisoAbierta = $("aviso-abierta-por");
  // «Si los dos guardan…» no es para quien no puede guardar.
  avisoAbierta.hidden = !enEdicion || !puedeValidar;
  if (avisoAbierta.hidden) return;
  const minutos = Math.max(0, Math.floor((Date.now() - Date.parse(enEdicion.desde)) / 60000));
  const hace = minutos < 1 ? "hace un momento" : `hace ${minutos} min`;
  avisoAbierta.textContent =
    `Abierta por ${enEdicion.nombre || "otra persona"} ${hace}. ` +
    "Si los dos guardan, el segundo recibe un aviso.";
}

// ── Arranque ──────────────────────────────────────────────────────────────
function escucharEdiciones() {
  for (const id of [
    "tipo", "tipo-actividad", "origen", "propietario", "autor", "resumen", "transcripcion",
    "contexto",
  ]) {
    $(id).addEventListener("input", alEditar);
  }
  $("autor-desconocido").addEventListener("change", () => {
    elegirAutor(autorDelFormulario());
    alEditar();
  });
  $("carreras").addEventListener("change", alEditar);
  $("publicable").addEventListener("change", alEditar);
  $("tipo").addEventListener("change", alEditar);
  $("tipo-actividad").addEventListener("change", alEditar);
  for (const control of document.querySelectorAll(
    ".control-fecha input, .control-fecha select"
  )) {
    control.addEventListener("input", alEditar);
    control.addEventListener("change", alEditar);
  }
}

function mostrarFallo(mensaje, titulo = "No se pudo abrir la Ficha") {
  $("fallo-titulo").textContent = titulo;
  $("fallo-detalle").textContent = mensaje;
  $("fallo").hidden = false;
}

async function nombreDeLaPersona() {
  const yo = await comoJson("/api/yo");
  nombres = new Map([[yo.usuario_id, yo.nombre]]);
  usuarioId = yo.usuario_id;
  $("persona").textContent = yo.nombre;
  $("franja-interruptor").hidden = yo.sistema.procesamiento_habilitado !== false;
  // A quien no tiene VALIDAR no se le ofrece guardar: el servidor le respondería 403.
  puedeValidar = yo.permisos.includes("VALIDAR");
  for (const sigue of document.querySelectorAll(".sigue")) sigue.hidden = !puedeValidar;
  $("guardar-completo").hidden = !puedeValidar;
  $("guardar-parcial").hidden = !puedeValidar;
  if (!puedeValidar) {
    $("firma").textContent = "Tu rol puede ver esta Ficha, pero no guardarla ni firmarla.";
  }
  puedeRetirar = yo.permisos.includes("RETIRAR_PIEZAS");
}

// ── M3: quitar la Pieza del Acervo (105, DEC-29) ──────────────────────────
// Se retira en cualquier estado; el servidor cancela el procesamiento en curso. Al confirmar
// se vuelve a la lista: la Pieza ya no aparece en ningún lado.
const MOTIVO_MINIMO = 5;

function abrirRetiro() {
  $("acciones").open = false;
  $("retiro-titulo").textContent = `¿Quitar "${detalle.nombre_archivo}" del Acervo?`;
  $("retiro-procesando").hidden = detalle.estado !== "Recibida";
  $("retiro-motivo").value = "";
  $("retiro-confirmar").disabled = true;
  $("retiro-error").hidden = true;
  $("retiro").showModal();
}

async function confirmarRetiro(evento) {
  evento.preventDefault();
  const motivo = $("retiro-motivo").value.trim();
  if (motivo.length < MOTIVO_MINIMO) return;
  $("retiro-confirmar").disabled = true;
  try {
    await pedir(`/api/admin/piezas/${encodeURIComponent(PIEZA_ID)}/retiro`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ motivo }),
    });
  } catch (error) {
    const tipo = error.problema?.type;
    // Otra persona ya la quitó, o ya no existe: no hay nada que reintentar.
    if (tipo === "/errores/transicion-invalida" || tipo === "/errores/pieza-no-encontrada") {
      $("retiro-error").textContent = "Ya no está en el Acervo. Volviendo a la lista…";
      $("retiro-error").hidden = false;
      sucio = false;
      setTimeout(() => { location.href = "/"; }, 1500);
      return;
    }
    $("retiro-error").textContent = tipo === "/errores/permiso-insuficiente"
      ? "Tu rol ya no puede quitar Piezas del Acervo. La Pieza sigue en el Acervo."
      : "No se pudo quitar. La Pieza sigue en el Acervo.";
    $("retiro-error").hidden = false;
    $("retiro-confirmar").disabled = tipo === "/errores/permiso-insuficiente";
    return;
  }
  sucio = false;
  location.href = "/";
}

// ── El reloj de la sesión (user story 22) ─────────────────────────────────
// Lo arranca el servidor al abrir la pantalla y lo cierra el guardado, que le devuelve
// el `medicion_id`. El navegador solo lo transporta: si el reloj lo llevara esta página,
// el tiempo por Pieza sería el de la máquina de quien valida y no un dato de la sesión.
//
// Medir no puede costar una validación: si la apertura falla, la pantalla se usa igual y
// lo que se pierde es la fila de la medición, no el trabajo de la persona.
function arrancarElReloj() {
  reloj = (async () => {
    try {
      const respuesta = await fetch(`/api/piezas/${encodeURIComponent(PIEZA_ID)}/apertura`, {
        method: "POST",
        headers: { "X-HV": "1" },
      });
      medicionId = respuesta.ok ? (await respuesta.json()).medicion_id : null;
    } catch {
      medicionId = null;
    }
  })();
  return reloj;
}

async function revisarSalud() {
  try {
    const salud = await comoJson("/api/salud");
    $("franja-degradado").hidden = salud.estado === "ok";
  } catch { /* la salud es informativa: si no contesta, no se avisa nada */ }
}

async function iniciar() {
  if (!PIEZA_ID) {
    mostrarFallo("No sé qué Pieza abrir. Volvé a la lista y elegí una.");
    return;
  }
  poblarSelectoresDeFecha();
  escucharEdiciones();
  fijarProporcion(proporcionGuardada());
  $("salir").addEventListener("click", salir);
  $("volver-fallo").addEventListener("click", () => { location.href = "/"; });
  $("guardar-parcial").addEventListener("click", (evento) => guardar(evento.currentTarget));
  $("guardar-completo").addEventListener("click", (evento) => firmar(evento.currentTarget));
  $("usar-resumen-nuevo").addEventListener("click", usarResumenNuevo);
  $("no-esperar-resumen").addEventListener("click", noEsperarElResumen);
  $("alerta-volver").addEventListener("click", () => $("alerta-firma").close());
  $("alerta-firmar").addEventListener("click", () => {
    $("alerta-firma").close();
    guardar($("guardar-completo"));
  });
  $("ir-a-la-siguiente").addEventListener("click", irALaSiguiente);
  // El Enter que firmó, mantenido, repite sobre «Ir a la siguiente» (toma el foco): no la aprieta.
  $("ir-a-la-siguiente").addEventListener("keydown", (evento) => {
    if (evento.repeat) evento.preventDefault();
  });
  $("ver-ficha").addEventListener("click", verFichaFirmada);
  $("reintentar").addEventListener("click", () => guardar(botonDelGuardado));
  $("conflicto-ver").addEventListener("click", verVersionNueva);
  // El enlace lleva a la Ficha validada en esta misma pantalla, de solo lectura y al lado
  // de lo tuyo: navegar a otra página perdería lo escrito.
  $("conflicto-ver-ficha").addEventListener("click", (evento) => {
    evento.preventDefault();
    verVersionNueva();
  });
  $("conflicto-aplicar").addEventListener("click", aplicarSobreLaNueva);
  $("conflicto-descartar").addEventListener("click", descartarLosMios);
  $("quitar").addEventListener("click", abrirRetiro);
  $("retiro-cancelar").addEventListener("click", () => $("retiro").close());
  $("retiro-form").addEventListener("submit", confirmarRetiro);
  // Alt+M inserta la marca desde cualquier lugar de la pantalla; `code` y no `key`: en macOS
  // Option+M escribe «µ».
  document.addEventListener("keydown", (evento) => {
    if (evento.altKey && !evento.ctrlKey && !evento.metaKey && evento.code === "KeyM" && reproductor) {
      evento.preventDefault();
      insertarMarca();
    }
  });
  $("retiro-motivo").addEventListener("input", (evento) => {
    $("retiro-confirmar").disabled = evento.target.value.trim().length < MOTIVO_MINIMO;
  });
  const borrador = tomarBorrador();

  try {
    await nombreDeLaPersona();
    detalle = await comoJson(`/api/piezas/${encodeURIComponent(PIEZA_ID)}`);
    $("acciones").hidden = !puedeRetirar;
    // Una Pieza Recibida todavía no tiene Ficha: se abre igual, con los campos duros y el
    // Contexto para completar mientras se lee (152).
    enLectura = detalle.estado === "Recibida";
    await poblarCatalogos();
    ficha = await comoJson(
      `/api/piezas/${encodeURIComponent(PIEZA_ID)}/${enLectura ? "en-lectura" : "ficha"}`
    );
    await traerDescripcion();
    armarVisor();
    pintarFicha();
    if (puedeValidar) retomarBorrador(borrador);
    partida.hidden = false;
    // El reloj arranca con la pantalla ya armada: lo que se mide es el trabajo de la
    // persona, no lo que tardó el navegador en pintar la Ficha (user story 22). Quien no
    // puede guardar no valida: no hay trabajo que medir.
    if (enLectura) esperarLaLectura();
    else if (puedeValidar) await arrancarElReloj();
  } catch (error) {
    mostrarFallo(error.message);
    return;
  }
  revisarSalud();
}

iniciar();
