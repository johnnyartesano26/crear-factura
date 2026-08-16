#!/usr/bin/env python3
"""
PIPELINE ASÍNCRONO DE FACTURACIÓN — Madre Monte (VERSIÓN CANÓNICA)
Unifica todas las versiones previas en una sola fuente de verdad.

Características (ver README de unificación en tecnicas-de-depuracion):
  - asyncio + aiohttp: búsqueda de clientes y facturas en paralelo (máx 5 concurrentes)
  - Bug 1 corregido: columna "Facturado" valida con .startswith("Facturado")
  - Bug 2 corregido: búsqueda de clientes en cascada + token matching (paréntesis)
  - Bug 3 corregido: paymentForm / paymentMethod obligatorios en Alegra
  - Bug 4 corregido: inventario de litros lee fermentadores ("PTL" sin "LITRO")
  - Logging con rotación de archivos + archivo de errores (config.py)
  - Depuración: Z1_DEBUG (pdb) y Z1_PROFILE (cProfile) vía config.py
  - Diagnóstico: resumen_facturacion.json con CONTEOS agregados (sin nombres)
    + detalle completo (con nombres) para notificación por Telegram

Uso:
  python facturacion_async.py
  python facturacion_async.py --dry-run
"""
import asyncio
import os
import sys
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

from config import setup_logging, debug_on_error, profile_if_enabled, timeit
from alegra_client_async import AlegraClientAsync

load_dotenv()
setup_logging()

logger = logging.getLogger("facturacion")

DRY = "--dry-run" in sys.argv

ALEGRA_API = os.getenv("ALEGRA_API_URL", "https://api.alegra.com/api/v1")
ALEGRA_EMAIL = os.getenv("ALEGRA_EMAIL", "")
ALEGRA_TOKEN = os.getenv("ALEGRA_TOKEN", os.getenv("ALEGRA_API_KEY", ""))

SHEET_REMISIONES = os.getenv("SHEET_REMISIONES", "1hBicxCSwnZpreEPmru_ZScZQjuHPXcRQBiWatC8AC1Q")
SHEET_INVENTARIO = os.getenv("SHEET_INVENTARIO", "1UHqPRV1stpnM5VHer9-8Z0H_UW0omiSoape7cIwzCGA")

MAX_CONCURRENT_ALEGRA = int(os.getenv("MAX_CONCURRENT_ALEGRA", "5"))
RESUMEN_JSON_PATH = os.getenv("RESUMEN_JSON_PATH", "resumen_facturacion.json")

MAPEO = {
    "PTB01": ("64", 8500), "PTB02": ("65", 8500), "PTB03": ("66", 8500),
    "PTB04": ("67", 9500), "PTB05": ("68", 8500),
    "PTL01": ("69", 18000), "PTL02": ("70", 18000), "PTL03": ("71", 18000),
    "PTL04": ("72", 19000), "PTL05": ("73", 18000), "DOM01": ("58", 12000),
}

ESTILO_DE = {
    "PTB01": "GOLDEN ALE", "PTB02": "IRISH RED ALE", "PTB03": "APA", "PTB04": "IPA", "PTB05": "STOUT",
    "PTL01": "GOLDEN ALE", "PTL02": "IRISH RED ALE", "PTL03": "APA", "PTL04": "IPA", "PTL05": "STOUT",
}

ESTILOS_BASE = ["GOLDEN ALE", "IRISH RED ALE", "APA", "IPA", "STOUT"]
ESTILOS_TEMPORADA = ["GERMAN PILS", "HIDROMIEL", "RED IPA"]
ESTILOS = ESTILOS_BASE + ESTILOS_TEMPORADA

# ⚠️ REGLA_NEGOCIO_01 — CASCADA BOTELLA → BARRIL → FERMENTADOR
# 1 botella = 0.330 L (valor canónico del sistema, ver ledger_inventario.py).
# Si no hay stock suficiente en botellas, el faltante se cubre desde barril y
# luego desde el fermentador. Ver README de unificación, sección Reglas de Negocio.
BOTELLA_L = 0.330

C_CLIENTE, C_DOM, C_FACTURAR, C_VALOR_DOM, C_DOM_ALT, C_FACTURADO = 2, 19, 22, 24, 23, 26
C_PAGO, C_MEDIO = 3, 4
PARES_PROD = [(5, 6), (8, 9), (11, 12), (14, 15), (17, 18)]

PAGO_MAP = {"credito": "CREDIT", "contado": "CASH"}
MEDIO_MAP = {"efectivo": "CASH", "tranferencia débito": "DEBIT_CARD",
             "transferencia débito": "DEBIT_CARD", "transferencia": "BANK_TRANSFER"}


def _normalizar_estilo(raw):
    """Mapea el nombre/código en bruto del Sheet al estilo canónico (8 estilos).

    Los estilos de temporada (GERMAN PILS, HIDROMIEL, RED IPA) no tienen código de
    producto Alegra, solo se leen para el inventario/conciliación.
    """
    if not raw:
        return None
    r = str(raw).strip().lower()
    if any(x in r for x in ['american india pale ale', 'vagabundo', 'ptl04', 'ptb04']):
        return 'IPA'
    if any(x in r for x in ['american pale ale', 'cienfuegos', 'ptl03', 'ptb03']):
        return 'APA'
    if any(x in r for x in ['golden ale', 'siempre viva', 'ptl01', 'ptb01']):
        return 'GOLDEN ALE'
    if any(x in r for x in ['irish red ale', 'mística', 'mistica', 'ptl02', 'ptb02']):
        return 'IRISH RED ALE'
    if any(x in r for x in ['stout', 'sangre negra', 'ptl05', 'ptb05']):
        return 'STOUT'
    if any(x in r for x in ['hidromiel', 'hidro']):
        return 'HIDROMIEL'
    if any(x in r for x in ['german pils']):
        return 'GERMAN PILS'
    if any(x in r for x in ['red ipa', 'red indian pale ale']):
        return 'RED IPA'
    return None


def _f(v, d=0):
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return d


async def leer_hojas(svc):
    loop = asyncio.get_running_loop()

    def _leer_remisiones():
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_REMISIONES, range="A:ZZ").execute()
        rows = result.get("values", [])
        return rows[1:] if len(rows) > 1 else [], rows[0] if rows else []

    def _leer_inventario():
        rows = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_INVENTARIO, range="A:ZZ").execute().get("values", [])
        stock = {e: {"bot": 0.0, "barril": 0.0, "litros": 0.0} for e in ESTILOS}
        last = None
        for i in range(len(rows) - 1, -1, -1):
            if any(c.strip() for c in rows[i]):
                last = rows[i]
                break
        if not last:
            return stock
        # ⚠️ ADV_01 — LECTURA POR COLUMNAS (igual que nucleo_de_inventario.py)
        # Fermentadores: columnas 1..14 (pares [litros, estilo])
        # Barriles:       columnas 15..28 (pares [cantidad, estilo])
        # Botellas:       columnas 29..40 (pares [cantidad, estilo])
        for i in range(1, 15, 2):
            litros = _f(last[i]) if i < len(last) else 0.0
            est = _normalizar_estilo(last[i + 1]) if i + 1 < len(last) else None
            if est and est in stock:
                stock[est]["litros"] += litros

        for i in range(15, 29, 2):
            cant = _f(last[i]) if i < len(last) else 0.0
            est = _normalizar_estilo(last[i + 1]) if i + 1 < len(last) else None
            if est and est in stock:
                stock[est]["barril"] += cant

        for i in range(29, 41, 2):
            cant = _f(last[i]) if i < len(last) else 0.0
            est = _normalizar_estilo(last[i + 1]) if i + 1 < len(last) else None
            if est and est in stock:
                stock[est]["bot"] += cant

        return stock

    data, headers = await loop.run_in_executor(None, _leer_remisiones)
    stock = await loop.run_in_executor(None, _leer_inventario)

    assert headers, "No se encontraron encabezados en la hoja de remisiones"
    return data, headers, stock


def extraer_items(row):
    items = []
    for pc, cc in PARES_PROD:
        if pc >= len(row) or cc >= len(row):
            continue
        cel = row[pc].strip()
        ref = next((r for r in MAPEO if r in cel), None)
        if not ref:
            continue
        cant = _f(row[cc].strip(), 0)
        if cant > 0:
            pid, precio = MAPEO[ref]
            items.append({
                "ref": ref, "id": pid, "quantity": cant, "price": precio,
                "tax": [{"id": 4}] if pid != "58" else [],
            })
    return items


def obtener_filas_procesables(data):
    filas = []
    for i, row in enumerate(data, start=2):
        if C_FACTURAR >= len(row) or row[C_FACTURAR].strip().lower() not in ("si", "sí", "true", "1"):
            continue
        if C_FACTURADO < len(row) and row[C_FACTURADO].strip().startswith("Facturado"):
            continue
        cliente = row[C_CLIENTE].strip() if C_CLIENTE < len(row) else ""
        if not cliente:
            continue
        items = extraer_items(row)
        if C_DOM < len(row) and row[C_DOM].strip().lower() in ("se incluye", "si", "sí", "true", "1"):
            pdom = 12000
            for cidx in (C_VALOR_DOM, C_DOM_ALT):
                if cidx < len(row) and row[cidx].strip():
                    v = _f(row[cidx].strip(), 0)
                    if v:
                        pdom = v * 1000 if v < 100 else v
                        break
            items.append({"ref": "DOM01", "id": "58", "quantity": 1, "price": pdom, "tax": []})
        if not items:
            continue
        pago = row[C_PAGO].strip().lower() if C_PAGO < len(row) else ""
        medio = row[C_MEDIO].strip().lower() if C_MEDIO < len(row) else ""
        filas.append({"row_num": i, "cliente": cliente, "items": items, "row_data": row,
                       "payment_form": PAGO_MAP.get(pago, "CASH"),
                       "payment_method": MEDIO_MAP.get(medio, "CASH")})
    return filas


def _faltante_estilo(stock_estilo, needed):
    """REGLA_NEGOCIO_01: litros que faltan tras la cascada botella → barril → fermentador.

    - Botellas: primero se cubren con stock de botella.
    - El faltante de botellas se convierte a litros (× BOTELLA_L) y se suma a los litros pedidos.
    - Los litros se cubren primero con barril y luego con fermentador.
    Devuelve el faltante final en litros (0 = suficiente).
    """
    rem_bot = max(0.0, needed["bot"] - stock_estilo["bot"])
    total_lit = needed["litros"] + rem_bot * BOTELLA_L
    cubierto_barril = min(total_lit, stock_estilo["barril"])
    rem_lit = total_lit - cubierto_barril
    return max(0.0, rem_lit - stock_estilo["litros"])


def verificar_stock_batch(filas, stock):
    assert isinstance(stock, dict), "stock debe ser un diccionario"
    total_necesario = {e: {"bot": 0.0, "litros": 0.0} for e in ESTILOS}

    # 1) Sumar la demanda total por estilo
    for f in filas:
        for it in f["items"]:
            if it["ref"] == "DOM01":
                continue
            est = ESTILO_DE.get(it["ref"])
            if not est:
                continue
            tipo = "bot" if it["ref"].startswith("PTB") else "litros"
            total_necesario[est][tipo] += it["quantity"]

    # 2) Determinar qué estilos no alcanzan con la cascada de 3 capas
    sin_stock_estilos = {est for est in ESTILOS if _faltante_estilo(stock[est], total_necesario[est]) > 0}

    # 3) Separar filas válidas vs sin stock (una fila es "sin stock" si usa un estilo inviable)
    validas = []
    sin_stock = []
    for f in filas:
        estilos_fila = {ESTILO_DE[it["ref"]] for it in f["items"]
                        if it["ref"] != "DOM01" and ESTILO_DE.get(it["ref"])}
        if estilos_fila & sin_stock_estilos:
            logger.warning("   ⛔ Fila %s %s: sin stock en %s",
                           f["row_num"], f["cliente"], ", ".join(sorted(estilos_fila & sin_stock_estilos)))
            sin_stock.append(f)
        else:
            validas.append(f)

    return validas, sin_stock, total_necesario


def aplicar_cascada_descuento(stock, total_necesario):
    """REGLA_NEGOCIO_01: aplica el descuento respetando la cascada botella → barril → fermentador.

    Solo modifica el inventario EN MEMORIA (para auditoría/historial); no escribe el Sheet.
    """
    for est in ESTILOS:
        bot = total_necesario[est]["bot"]
        lit = total_necesario[est]["litros"]
        # 1) botellas desde stock de botella
        rem_bot = max(0.0, bot - stock[est]["bot"])
        stock[est]["bot"] = round(max(0.0, stock[est]["bot"] - bot), 1)
        # 2) litros (pedidos + faltante de botellas) desde barril
        total_lit = lit + rem_bot * BOTELLA_L
        barril_antes = stock[est]["barril"]
        stock[est]["barril"] = round(max(0.0, barril_antes - total_lit), 1)
        rem_lit = max(0.0, total_lit - barril_antes)
        # 3) resto desde fermentador
        stock[est]["litros"] = round(max(0.0, stock[est]["litros"] - rem_lit), 1)
    return stock


def desglose_cascada(inicial, final, total_necesario):
    """REGLA_NEGOCIO_01: desglose del descuento por estilo (para el historial consultable)."""
    desglose = {}
    for est in ESTILOS:
        bot_pedidas = total_necesario[est]["bot"]
        lit_pedidos = total_necesario[est]["litros"]
        bot_de_stock = min(bot_pedidas, inicial[est]["bot"])
        bot_cubiertas_litros = max(0.0, bot_pedidas - inicial[est]["bot"])
        total_lit = lit_pedidos + bot_cubiertas_litros * BOTELLA_L
        lit_de_barril = min(total_lit, inicial[est]["barril"])
        lit_de_ferm = round(max(0.0, total_lit - inicial[est]["barril"]), 1)
        desglose[est] = {
            "bot_pedidas": int(round(bot_pedidas)),
            "bot_descontadas_de_stock": int(round(bot_de_stock)),
            "bot_cubiertas_por_litros": int(round(bot_cubiertas_litros)),
            "litros_pedidos": round(lit_pedidos, 1),
            "litros_descontados_de_barril": round(lit_de_barril, 1),
            "litros_descontados_de_fermentador": lit_de_ferm,
        }
    return desglose


@timeit
async def buscar_clientes_paralelo(alegra, filas):
    clientes_unicos = list(set(f["cliente"] for f in filas))
    assert clientes_unicos, "No hay clientes para buscar"
    cache = {}

    async def _buscar(nombre):
        result = await alegra.search_client_by_name(nombre)
        return nombre, result.get("id") if result else None

    tasks = [_buscar(n) for n in clientes_unicos]
    resultados = await asyncio.gather(*tasks)

    for nombre, cid in resultados:
        cache[nombre] = cid
        if not cid:
            logger.warning("   ❓ Cliente no encontrado en Alegra: %s", nombre)

    return cache


@timeit
async def crear_facturas_paralelo(alegra, filas, client_cache):
    hoy = datetime.now().strftime("%Y-%m-%d")
    vence = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

    async def _crear(fila):
        cid = client_cache.get(fila["cliente"])
        if not cid:
            return fila["row_num"], None, "sin_cliente"
        if DRY:
            resumen = ", ".join(f"{it['ref']}x{int(it['quantity'])}" for it in fila["items"])
            logger.info("   [DRY] Simularía factura para %s (%s)", fila["cliente"], resumen)
            return fila["row_num"], {"id": "DRY", "dry_run": True}, None
        try:
            items_alegra = [
                {"id": it["id"], "quantity": it["quantity"], "price": it["price"], "tax": it["tax"]}
                for it in fila["items"]
            ]
            resp = await alegra.create_invoice(
                cid, items_alegra, due_date=vence, date=hoy,
                payment_form=fila.get("payment_form", "CASH"),
                payment_method=fila.get("payment_method", "CASH"),
            )
            if resp and resp.get("id"):
                return fila["row_num"], resp, None
            return fila["row_num"], None, f"error_api: {resp}"
        except Exception as e:
            return fila["row_num"], None, str(e)

    tasks = [_crear(f) for f in filas]
    resultados = await asyncio.gather(*tasks)

    creadas = []
    sin_cliente = []
    errores = []

    for row_num, fac, error in resultados:
        fila = next(f for f in filas if f["row_num"] == row_num)
        if fac:
            resumen = ", ".join(f"{it['ref']}x{int(it['quantity'])}" for it in fila["items"])
            logger.info("   ✅ Factura %s — %s (%s)", fac["id"], fila["cliente"], resumen)
            creadas.append({"row_num": row_num, "factura": fac,
                            "cliente": fila["cliente"], "items": fila["items"]})
        elif error == "sin_cliente":
            sin_cliente.append(fila)
        else:
            logger.error("   ❌ Error factura %s: %s", fila["cliente"], error)
            errores.append(fila)

    return creadas, sin_cliente, errores


async def actualizar_sheets(svc, creadas):
    """Marca "Facturado {id}" en Remisiones (columna AA). NO toca el inventario.

    La deducción de inventario se registra en el historial (historial_facturacion.json),
    no en el Sheet, que es la fuente de verdad gestionada por nucleo_de_inventario.
    """
    loop = asyncio.get_running_loop()
    updates = [
        {"range": f"AA{c['row_num']}", "values": [[f"Facturado {c['factura']['id']}"]]}
        for c in creadas
    ]

    if not updates:
        return

    if DRY:
        logger.info("\n📝 [DRY] Se marcarían como Facturado:")
        for u in updates:
            logger.info("   %s → %s", u["range"], u["values"])
        return

    def _batch_remisiones():
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_REMISIONES,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()

    await loop.run_in_executor(None, _batch_remisiones)
    logger.info("\n📝 Remisiones marcadas como Facturado (el inventario NO se modifica).")


def generar_resumen_json(diagnostico: dict):
    """Escribe SOLO conteos agregados (sin nombres de clientes) para el dashboard público."""
    agregado = {
        "fecha": diagnostico["fecha"],
        "dry_run": diagnostico.get("dry_run", False),
        "pendientes_procesar": diagnostico.get("pendientes_procesar", 0),
        "emitidas": diagnostico.get("emitidas", 0),
        "sin_stock": len(diagnostico.get("sin_stock", [])),
        "sin_cliente": len(diagnostico.get("sin_cliente", [])),
        "errores": len(diagnostico.get("errores", [])),
        "inventario_inicial": diagnostico.get("inventario_inicial", {}),
        "inventario_final": diagnostico.get("inventario_final", {}),
        "descuento_inventario": diagnostico.get("descuento_inventario", {}),
        "resumen": diagnostico.get("resumen", ""),
    }
    try:
        with open(RESUMEN_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(agregado, f, ensure_ascii=False, indent=2)
        logger.info("📄 Diagnóstico agregado guardado en %s", RESUMEN_JSON_PATH)
    except Exception as e:
        logger.warning("No se pudo guardar el diagnóstico agregado: %s", e)
    return agregado


def construir_detalle_telegram(diagnostico: dict) -> str:
    """Detalle COMPLETO (con nombres) para notificación privada por Telegram."""
    lineas = []
    for c in diagnostico.get("detalle_emitidas", []):
        lineas.append(f"✅ #{c['factura_id']} → {c['cliente']}")
    for s in diagnostico.get("sin_stock", []):
        lineas.append(f"⛔ Sin stock: F{s['fila']} {s['cliente']}")
    for s in diagnostico.get("sin_cliente", []):
        lineas.append(f"❓ Sin cliente: F{s['fila']} {s['cliente']}")
    for s in diagnostico.get("errores", []):
        lineas.append(f"❌ Error: F{s['fila']} {s['cliente']}")
    if not lineas and diagnostico.get("emitidas", 0) == 0:
        lineas.append("✓ No hay facturas pendientes. Todo al día.")
    return "\n".join(lineas)


async def main():
    if not ALEGRA_EMAIL or not ALEGRA_TOKEN:
        logger.error("❌ Faltan ALEGRA_EMAIL / ALEGRA_TOKEN")
        sys.exit(1)

    assert SHEET_REMISIONES, "SHEET_REMISIONES no configurado"
    assert SHEET_INVENTARIO, "SHEET_INVENTARIO no configurado"

    logger.info("=" * 56)
    logger.info("🍺 FACTURACIÓN ASÍNCRONA — Madre Monte %s", "(DRY-RUN)" if DRY else "")
    logger.info("   Máx concurrentes a Alegra: %d | Debug: %s | Profile: %s",
                MAX_CONCURRENT_ALEGRA, os.getenv("Z1_DEBUG", "no"), os.getenv("Z1_PROFILE", "no"))
    logger.info("=" * 56)

    import googleapiclient.discovery
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    info = os.getenv("GOOGLE_CREDS_JSON")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if info:
        creds = service_account.Credentials.from_service_account_info(json.loads(info), scopes=scopes)
    else:
        path = os.getenv("GOOGLE_CREDS_PATH", "credenciales_google.json")
        creds = service_account.Credentials.from_service_account_file(path, scopes=scopes)
    creds.refresh(Request())
    svc = googleapiclient.discovery.build("sheets", "v4", credentials=creds)

    logger.info("📡 Leyendo hojas de cálculo...")
    data, headers, stock = await leer_hojas(svc)

    logger.info("📦 Inventario actual:")
    for e in ESTILOS:
        logger.info("   %s: %s bot | %sL barril | %sL fermentador",
                    e, int(stock[e]["bot"]), stock[e]["barril"], stock[e]["litros"])

    diagnostico = {
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": DRY,
        "pendientes_procesar": 0,
        "emitidas": 0,
        "sin_stock": [],
        "sin_cliente": [],
        "errores": [],
        "detalle_emitidas": [],
        "resumen": "",
    }

    filas = obtener_filas_procesables(data)
    diagnostico["pendientes_procesar"] = len(filas)
    if not filas:
        logger.info("Sin filas pendientes para facturar.")
        diagnostico["resumen"] = "0 emitidas | 0 sin stock | 0 sin cliente"
        generar_resumen_json(diagnostico)
        _escribir_salidas(diagnostico)
        return

    logger.info("\n📋 %s filas pendientes. Verificando stock...", len(filas))
    validas, sin_stock, total_descontado = verificar_stock_batch(filas, stock)
    diagnostico["sin_stock"] = [{"fila": f["row_num"], "cliente": f["cliente"]} for f in sin_stock]
    logger.info("   ✅ %s con stock suficiente | ⛔ %s sin stock", len(validas), len(sin_stock))

    if not validas:
        logger.info("Nada que facturar tras verificar stock.")
        diagnostico["resumen"] = f"0 emitidas | {len(sin_stock)} sin stock | 0 sin cliente"
        generar_resumen_json(diagnostico)
        _escribir_salidas(diagnostico)
        return

    alegra = AlegraClientAsync(email=ALEGRA_EMAIL, api_key=ALEGRA_TOKEN,
                               max_concurrent=MAX_CONCURRENT_ALEGRA)
    creadas = []
    sin_cliente_count = 0

    try:
        logger.info("\n🔍 Buscando %s clientes únicos en Alegra...", len(set(f["cliente"] for f in validas)))
        client_cache = await buscar_clientes_paralelo(alegra, validas)

        sin_clientes = [{"fila": f["row_num"], "cliente": f["cliente"]}
                        for f in validas if not client_cache.get(f["cliente"])]
        diagnostico["sin_cliente"] = sin_clientes
        sin_cliente_count = len(sin_clientes)

        con_cliente = [f for f in validas if client_cache.get(f["cliente"])]
        if sin_cliente_count:
            logger.info("   ❓ %s clientes no encontrados, se omitirán", sin_cliente_count)

        if not con_cliente:
            logger.info("Sin clientes válidos para facturar.")
            diagnostico["resumen"] = f"0 emitidas | {len(sin_stock)} sin stock | {sin_cliente_count} sin cliente"
            generar_resumen_json(diagnostico)
            _escribir_salidas(diagnostico)
            return

        logger.info("\n🧾 Creando %s facturas en paralelo...", len(con_cliente))
        creadas, _, errores = await crear_facturas_paralelo(alegra, con_cliente, client_cache)
        diagnostico["errores"] = [{"fila": f["row_num"], "cliente": f["cliente"]} for f in errores]
        diagnostico["detalle_emitidas"] = [
            {"fila": c["row_num"], "cliente": c["cliente"], "factura_id": c["factura"]["id"]}
            for c in creadas
        ]

        # REGLA_NEGOCIO_01: cascada botella → barril → fermentador (solo en memoria)
        inventario_inicial = {e: {"bot": stock[e]["bot"], "barril": stock[e]["barril"], "litros": stock[e]["litros"]} for e in ESTILOS}
        aplicar_cascada_descuento(stock, total_descontado)
        diagnostico["inventario_inicial"] = inventario_inicial
        diagnostico["inventario_final"] = {e: {"bot": stock[e]["bot"], "barril": stock[e]["barril"], "litros": stock[e]["litros"]} for e in ESTILOS}
        diagnostico["descuento_inventario"] = desglose_cascada(inventario_inicial, stock, total_descontado)

        if not DRY and creadas:
            logger.info("\n📝 Marcando facturas como Facturado en Remisiones...")
            await actualizar_sheets(svc, creadas)

    finally:
        await alegra.close()

    diagnostico["emitidas"] = len(creadas)
    diagnostico["resumen"] = (f"{len(creadas)} emitidas | {len(sin_stock)} sin stock "
                              f"| {sin_cliente_count} sin cliente | {len(diagnostico['errores'])} errores")

    generar_resumen_json(diagnostico)
    _escribir_salidas(diagnostico)

    logger.info("=" * 56)
    logger.info("📊 %s", diagnostico["resumen"])
    logger.info("=" * 56)


def _escribir_salidas(diagnostico: dict):
    """Escribe el detalle (con nombres) a un archivo para Telegram y el resumen a GITHUB_OUTPUT."""
    detalle = construir_detalle_telegram(diagnostico)
    try:
        with open("detalle_telegram.txt", "w", encoding="utf-8") as f:
            f.write(detalle)
    except IOError as e:
        logger.warning("No se pudo escribir detalle_telegram.txt: %s", e)

    gh = os.environ.get("GITHUB_OUTPUT")
    if not gh:
        return
    try:
        with open(gh, "a") as f:
            f.write(f"nuevas={diagnostico['emitidas']}\n")
            f.write(f"resumen={diagnostico['resumen']}\n")
    except IOError as e:
        logger.warning("No se pudo escribir GITHUB_OUTPUT: %s", e)


if __name__ == "__main__":
    with profile_if_enabled():
        with debug_on_error():
            asyncio.run(main())
