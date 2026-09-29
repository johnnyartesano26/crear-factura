/**
 * Trigger automático de facturación — Sheet de Remisiones
 * --------------------------------------------------------
 * Hace que `emitir.yml` corra SOLO, sin botón y sin esperar las 11 AM:
 *   - Al enviarse el formulario (nueva remisión).
 *   - Al marcar "Facturar" = "Si" en la hoja.
 *
 * Se instala en el Apps Script **contenedor** del Sheet de Remisiones
 * (NO en el backend del botón, que es una web app aparte).
 *
 * ── CÓMO INSTALAR ─────────────────────────────────────────────────────────
 * 1. Abre el Sheet de Remisiones → Extensiones → Apps Script.
 * 2. Pega este código y guarda.
 * 3. Configura las propiedades: ejecuta `setup()` una vez (edita los valores),
 *    guarda y luego borra `setup()` o déjala (no se ejecuta sola).
 *    Propiedades requeridas:
 *      GH_TOKEN = token de GitHub con permiso "Actions" sobre crear-factura
 *                 (el mismo fine-grained del botón).
 *      REPO     = johnnyartesano26/crear-factura
 *      WORKFLOW = emitir.yml
 * 4. Añade los activadores (Editar → Activadores del proyecto actual → Añadir):
 *      - Función: emitirAlEnviarFormulario  ·  Evento: "Al enviarse el formulario"
 *      - Función: emitirAlMarcarFacturar     ·  Evento: "Al editarse"
 *    (Deben ser activadores INSTALABLES: los simples no pueden usar UrlFetchApp.)
 * ───────────────────────────────────────────────────────────────────────────
 */

function dispatchEmitir() {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('GH_TOKEN');
  var repo = props.getProperty('REPO') || 'johnnyartesano26/crear-factura';
  var workflow = props.getProperty('WORKFLOW') || 'emitir.yml';

  if (!token) {
    Logger.log('GH_TOKEN no configurado en Propiedades del script.');
    return -1;
  }

  var resp = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + repo + '/actions/workflows/' + workflow + '/dispatches',
    {
      method: 'post',
      contentType: 'application/json',
      headers: {
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
      },
      payload: JSON.stringify({ ref: 'main' }),
      muteHttpExceptions: true
    }
  );

  var code = resp.getResponseCode();
  if (code === 204) {
    Logger.log('emitir.yml disparado OK');
  } else {
    Logger.log('Error disparando emitir.yml: HTTP ' + code + ' ' + resp.getContentText());
  }
  return code;
}

// Dispara al enviarse el formulario (nueva remisión).
// Nota: si "Facturar" viene vacío en el formulario, esta corrida no emite nada
// (inofensivo); la emisión ocurre cuando marcan "Facturar" = "Si" (ver abajo).
function emitirAlEnviarFormulario(e) {
  dispatchEmitir();
}

// Dispara solo si editaron la columna "Facturar" y el nuevo valor es "Si"/"sí".
function emitirAlMarcarFacturar(e) {
  if (!e || !e.range) return;
  var col = e.range.getColumn();
  var header = String(e.range.getSheet().getRange(1, col).getValue() || '').trim().toLowerCase();
  var valor = String(e.value || '').trim().toLowerCase();

  if (header === 'facturar' && (valor === 'si' || valor === 'sí' || valor === 'true' || valor === '1')) {
    dispatchEmitir();
  }
}

// Ejecutar UNA vez para guardar las propiedades, luego borrar los valores por seguridad.
function setup() {
  PropertiesService.getScriptProperties().setProperties({
    GH_TOKEN: 'PEGAR_TOKEN_GITHUB_AQUI',
    REPO: 'johnnyartesano26/crear-factura',
    WORKFLOW: 'emitir.yml'
  });
}
