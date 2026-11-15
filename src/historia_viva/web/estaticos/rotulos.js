// Cómo se rotula el cuerpo de la Ficha según de dónde salió (Procedencia, 135). Va aparte de
// `validar.js` para probarlo sin la pantalla.

// El `.xls` entra convertido (153): su visor es el de la planilla, armado sobre la Ficha.
const PLANILLAS = [
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-excel",
  "text/csv",
];
// Su cuerpo sale literal del archivo, sin ningún modelo de lenguaje: PyMuPDF, python-docx, el
// lector de planillas o el texto mismo.
const DEL_ARCHIVO = [
  "text/plain",
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/msword",
  ...PLANILLAS,
];
const AYUDA_DEL_ARCHIVO =
  "Es el texto del archivo subido: ningún modelo lo leyó ni lo cambió. Corregí lo que haga falta.";
// La Ficha no guarda qué páginas pasaron por Tesseract: la aclaración va en todo PDF.
const AYUDA_ESCANEADAS =
  " Si alguna página estaba escaneada, su texto salió de una lectura automática (OCR), no de " +
  "un modelo de lenguaje: puede tener letras mal leídas.";

// `propuesto`: lo escribió un modelo y lleva la marca 🤖. `sinModelo`: la marca cuando no.
// `conAfirmaciones`: la foto se describió en Afirmaciones (o falló esa descripción, 144) y su
// cuerpo es solo lo que leyó el OCR, sin prosa del modelo (ADR-0007). `firmadas`: una firma ya
// escribió las aceptadas bajo «## Afirmaciones» y el cuerpo tiene las dos cosas.
function rotuloDelCuerpo(tipoMime, viaIngreso, conAfirmaciones = false, firmadas = false) {
  const mime = (tipoMime || "").split(";")[0].trim();
  if (mime.startsWith("image/") && conAfirmaciones && firmadas) {
    return {
      rotulo: "Afirmaciones aceptadas y texto visible en la imagen",
      ayuda: "Bajo «## Afirmaciones», las que aceptó una persona antes de firmar. Bajo «## Texto " +
        "visible», lo que leyó una lectura automática (OCR), no un modelo de lenguaje: puede " +
        "tener letras mal leídas. Corregí lo que haga falta.",
      propuesto: false, sinModelo: "Lectura automática",
    };
  }
  if (mime.startsWith("image/") && conAfirmaciones) {
    return {
      rotulo: "Texto visible en la imagen",
      ayuda: "Lo leyó una lectura automática (OCR), no un modelo de lenguaje: no inventa, pero " +
        "puede tener letras mal leídas. Corregí lo que haga falta.",
      propuesto: false, sinModelo: "Lectura automática",
    };
  }
  if (mime.startsWith("image/")) {
    return {
      rotulo: "🤖 Descripción de la imagen",
      ayuda: "Descripción de lo que se ve en la foto, más cualquier texto visible en ella " +
        "(carteles, dorsos, sellos).",
      propuesto: true, sinModelo: "",
    };
  }
  if (mime.startsWith("video/")) {
    // Ningún modelo vio el video (ADR-0005): la descripción la escribe quien valida.
    return {
      rotulo: "Descripción de lo que se ve y se oye (usá marcas [HH:MM:SS])",
      ayuda: "Nadie propuso nada: la descripción la escribís vos. El botón de la marca de " +
        "tiempo o Alt+M escriben el momento del video donde está el cursor.",
      propuesto: false, sinModelo: "",
    };
  }
  // El Aporte de un Agente también es `text/plain`: decide la vía de ingreso, no el MIME (DEC-140).
  if (viaIngreso === "aporte") {
    return {
      rotulo: "Texto que envió un Agente",
      ayuda: "Esto lo envió un Agente, no una persona. Revisalo antes de firmar.",
      propuesto: false, sinModelo: "Del Agente",
    };
  }
  if (viaIngreso === "archivo" && DEL_ARCHIVO.includes(mime)) {
    return {
      rotulo: "Texto del archivo",
      ayuda: AYUDA_DEL_ARCHIVO + (mime === "application/pdf" ? AYUDA_ESCANEADAS : ""),
      propuesto: false, sinModelo: "Del archivo",
    };
  }
  return {
    rotulo: "🤖 Transcripción",
    ayuda: "Este es el texto que el sistema leyó del archivo. Corregí lo que esté mal.",
    propuesto: true, sinModelo: "",
  };
}
