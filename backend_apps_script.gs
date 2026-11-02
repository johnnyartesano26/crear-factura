/**
 * Backend "Emitir Factura" — Madre Monte
 * ---------------------------------------
 * Dispara el workflow de GitHub Actions (emitir.yml) SIN exponer el token.
 * El navegador solo envía la CLAVE COMPARTIDA (para las 3 personas autorizadas).
 * El token de GitHub queda guardado en Propiedades del Script (nunca en el HTML público).
 *
 * ── CONFIGURACIÓN (una sola vez) ──────────────────────────────────────────
 * 1. Pega este código en https://script.google.com  (Nuevo proyecto).
 * 2. Menú  Proyecto → Configuración del proyecto → Propiedades del script:
 *      GH_TOKEN        = <token de GitHub con permiso Actions en crear-factura>
 *      CLAVE_EMITIR    = <la clave compartida para las 3 personas>  (ej: MadreMonte-Emitir-2026)
 *      REPO            = johnnyartesano26/crear-factura
 *      WORKFLOW        = emitir.yml
 *    (O ejecuta una vez la función  setup()  de abajo con tus valores y bórrala.)
 * 3. Implementar → Nueva implementación → Tipo "Aplicación web":
 *      - Ejecutar como:  Yo
 *      - Quién tiene acceso:  Cualquier usuario
 *    Copia la URL /exec y pégala en index.html (const BACKEND_URL).
 * ──────────────────────────────────────────────────────────────────────────
 */

/**
 * ── BITÁCORA / NOTA DE MANTENIMIENTO (2026-11-01) ────────────────────────
 * Incidente:
 *   El botón "⚡ Emitir" de index.html mostraba "❌ Error HTTP 401".
 *
 * Diagnóstico:
 *   El 401 NO provenía de Alegra. El botón dispara el workflow emitir.yml
 *   a través de la API de GitHub, y el Personal Access Token (PAT) que estaba
 *   embebido en index.html se encontraba inválido / revocado (GitHub respondía
 *   "Bad credentials"). Se confirmó que las credenciales de Alegra sí son
 *   válidas (la API responde HTTP 200).
 *
 * Medidas aplicadas:
 *   1. Se generó un PAT nuevo de GitHub con los scopes `repo` y `workflow`
 *      (los necesarios para disparar workflow_dispatch).
 *   2. Se actualizó el array ofuscado `T` dentro de dispararWorkflow() en
 *      index.html con el nuevo token.
 *   (El valor del token NO se registra aquí por seguridad.)
 *
 * Recomendación (APLICADA el 2026-11-01):
 *   index.html ya fue migrado a ESTE backend: se eliminó el fetch directo a
 *   GitHub (que llevaba el PAT embebido en el HTML) y ahora el botón llama a
 *   esta Web App vía BACKEND_URL, enviando solo la CLAVE_EMITIR. El GH_TOKEN
 *   vive únicamente en las Propiedades del Script y nunca se expone en el
 *   navegador del usuario final.
 *
 *   Pasos manuales restantes (quien configure esto):
 *   1. Crear/desplegar este script como Web App y copiar la URL /exec.
 *   2. En "Propiedades del Script":
 *        GH_TOKEN     = <PAT nuevo con scopes repo + workflow>
 *        CLAVE_EMITIR = <misma clave que en index.html>
 *        REPO         = johnnyartesano26/crear-factura
 *        WORKFLOW     = emitir.yml
 *   3. Pegar la URL /exec en index.html (const BACKEND_URL).
 *   4. Hacer commit + push para publicar el cambio en GitHub Pages.
 *   5. Rotar el PAT periódicamente.
 * ──────────────────────────────────────────────────────────────────────────
 */

function doPost(e) {
  var out = ContentService.createTextOutput().setMimeType(ContentService.MimeType.JSON);
  try {
    var props = PropertiesService.getScriptProperties();
    var claveOk = props.getProperty('CLAVE_EMITIR');
    var token = props.getProperty('GH_TOKEN');
    var repo = props.getProperty('REPO') || 'johnnyartesano26/crear-factura';
    var workflow = props.getProperty('WORKFLOW') || 'emitir.yml';

    var body = {};
    try { body = JSON.parse(e.postData.contents); } catch (_) {}
    var clave = (body.clave || '').toString().trim();

    if (!claveOk || clave !== claveOk) {
      return out.setContent(JSON.stringify({ ok: false, error: 'Clave incorrecta.' }));
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
      return out.setContent(JSON.stringify({ ok: true }));
    }
    return out.setContent(JSON.stringify({
      ok: false,
      error: 'GitHub respondió HTTP ' + code + '. ' + resp.getContentText()
    }));
  } catch (err) {
    return out.setContent(JSON.stringify({ ok: false, error: String(err) }));
  }
}

function doGet() {
  return ContentService
    .createTextOutput(JSON.stringify({ ok: true, msg: 'Backend Emitir Factura activo.' }))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Helper opcional: ejecuta esto UNA vez con tus valores para configurar
 * las propiedades sin usar el menú. Luego borra tus valores por seguridad.
 */
function setup() {
  PropertiesService.getScriptProperties().setProperties({
    GH_TOKEN: 'PEGAR_TOKEN_GITHUB_AQUI',
    CLAVE_EMITIR: 'PEGAR_CLAVE_COMPARTIDA_AQUI',
    REPO: 'johnnyartesano26/crear-factura',
    WORKFLOW: 'emitir.yml'
  });
}
