# Crear Factura — Automatización de facturación (Madre Monte)

Sistema que lee las remisiones del Google Sheet y emite facturas electrónicas en Alegra.

## Flujo

1. El cliente llena el formulario → nueva fila en el Sheet de Remisiones.
2. El operario marca **"Facturar" = "Si"** en la fila.
3. Se emite la factura en Alegra y se marca **"Facturado {id}"** en el Sheet.

## Mecanismos de ejecución activos

| Mecanismo | Cuándo | Notas |
|---|---|---|
| Botón "Emitir" (`index.html`) | Manual, al pulsar | Dispara `emitir.yml` vía `workflow_dispatch` |
| Cron (`emitir.yml`) | Diario, 11:00 AM Colombia | `cron '0 16 * * *'` (UTC) |

## Código principal

- `facturacion_async.py` — pipeline canónico (asíncrono, lectura de inventario con forward-fill).
- `sync_inventario.py` — regenera el inventario conciliado.
- `.github/workflows/emitir.yml` — workflow de emisión.
- `.github/workflows/sync.yml` — sync de inventario.
- `index.html` + `backend_apps_script.gs` — botón "Emitir" (front + backend Apps Script).

## Pendiente (para más adelante)

**Tercera posibilidad: disparo automático por Apps Script.**

Idea: un trigger en el Sheet de Remisiones que dispare `emitir.yml` automáticamente,
sin botón, al (a) enviarse el formulario y (b) marcar "Facturar" = "Si".

Se dejó redactada la solución (funciones `dispatchEmitir`, `emitirAlEnviarFormulario`
y `emitirAlMarcarFacturar`) y luego se retiró; por ahora se mantiene botón + cron.
Para implementarla luego:

1. Crear un script **contenedor** del Sheet de Remisiones (Extensiones → Apps Script).
2. Guardar `GH_TOKEN` (fine-grained, scope `Actions` sobre `crear-factura`) en
   Propiedades del script.
3. Añadir activadores **instalables**:
   - "Al enviarse el formulario" → dispara `emitir.yml`.
   - "Al editarse" (columna Facturar = "Si") → dispara `emitir.yml`.
