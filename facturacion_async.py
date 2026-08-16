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

ESTILOS = ["GOLDEN ALE", "IRISH RED ALE", "APA", "IPA", "STOUT"]

C_CLIENTE, C_DOM, C_FACTURAR, C_VALOR_DOM, C_DOM_ALT, C_FACTURADO = 2, 19, 22, 24, 23, 26
C_PAGO, C_MEDIO = 3, 4
PARES_PROD = [(5, 6), (8, 9), (11, 12), (14, 15), (17, 18)]

PAGO_MAP = {"credito": "CREDIT", "contado": "CASH"}
MEDIO_MAP = {"efectivo": "CASH", "tranferencia débito": "DEBIT_CARD",
             "transferencia débito": "DEBIT_CARD", "transferencia": "BANK_TRANSFER"}


def col_letter(idx):
    s = ""
    idx += 1
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


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
        stock = {e: {"bot": 0.0, "litros": 0.0} for e in ESTILOS}
        cols = {e: {"bot": None, "litros": None} for e in ESTILOS}
        row_num = None
        last = None
        for i in range(len(rows) - 1, -1, -1):
            if any(c.strip() for c in rows[i]):
                last = rows[i]
                row_num = i + 1
                break
        # ⚠️ ADV_01 — INVENTARIO DE LITROS DESDE FERMENTADORES
        # El stock de litros se lee de las columnas de fermentadores (etiquetas con "PTL"
        # sin "LITRO"), NO de "Litros en barril" que es otra métrica distinta.
        if last:
            for j, cell in enumerate(last):
                up = cell.strip().upper()
                if not up or j == 0:
                    continue
                est = next((e for e in ESTILOS if e in up), None)
                if not est:
                    continue
                vcol = j - 1
                if "BOTELLA" in up:
                    stock[est]["bot"] = _f(last[vcol]) if vcol < len(last) else 0
                    cols[est]["bot"] = vcol
                elif "PTL" in up and "LITRO" not in up:
                    stock[est]["litros"] = _f(last[vcol]) if vcol < len(last) else 0
                    cols[est]["litros"] = vcol

        def _tab_name():
            m = svc.spreadsheets().get(
                spreadsheetId=SHEET_INVENTARIO, fields="sheets.properties.title").execute()
            return m["sheets"][0]["properties"]["title"]

        return stock, cols, row_num, _tab_name()

    data, headers = await loop.run_in_executor(None, _leer_remisiones)
    stock, inv_cols, inv_row, inv_tab = await loop.run_in_executor(None, _leer_inventario)

    assert headers, "No se encontraron encabezados en la hoja de remisiones"
    return data, headers, stock, inv_cols, inv_row, inv_tab


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


def verificar_stock_batch(filas, stock):
    assert isinstance(stock, dict), "stock debe ser un diccionario"
    total_necesario = {e: {"bot": 0.0, "litros": 0.0} for e in ESTILOS}
    sin_stock = []
    validas = []

    for f in filas:
        needed = {e: {"bot": 0.0, "litros": 0.0} for e in ESTILOS}
        ok = True
        for it in f["items"]:
            if it["ref"] == "DOM01":
                continue
            est = ESTILO_DE.get(it["ref"])
            if not est:
                continue
            tipo = "bot" if it["ref"].startswith("PTB") else "litros"
            needed[est][tipo] += it["quantity"]

        for est in ESTILOS:
            new_bot = total_necesario[est]["bot"] + needed[est]["bot"]
            new_lit = total_necesario[est]["litros"] + needed[est]["litros"]
            if new_bot > stock[est]["bot"] or new_lit > stock[est]["litros"]:
                disp_bot = max(0, stock[est]["bot"] - total_necesario[est]["bot"])
                disp_lit = max(0, stock[est]["litros"] - total_necesario[est]["litros"])
                logger.warning("   ⛔ Fila %s %s: %s necesita %sbot/%sL, disponible %sbot/%sL",
                               f["row_num"], f["cliente"], est,
                               int(needed[est]["bot"]), needed[est]["litros"],
                               int(disp_bot), disp_lit)
                ok = False

        if ok:
            validas.append(f)
            for est in ESTILOS:
                for tipo in ("bot", "litros"):
                    total_necesario[est][tipo] += needed[est][tipo]
        else:
            sin_stock.append(f)

    return validas, sin_stock, total_necesario


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


async def actualizar_sheets(svc, creadas, total_descontado, stock, inv_cols, inv_row, inv_tab):
    loop = asyncio.get_running_loop()
    updates = []

    for c in creadas:
        updates.append({
            "range": f"AA{c['row_num']}",
            "values": [[f"Facturado {c['factura']['id']}"]],
        })

    for est in ESTILOS:
        for tipo in ("bot", "litros"):
            d = total_descontado[est][tipo]
            if d and inv_cols[est][tipo] is not None:
                rng = f"'{inv_tab}'!{col_letter(inv_cols[est][tipo])}{inv_row}"
                updates.append({"range": rng, "values": [[stock[est][tipo]]]})

    if not updates:
        return

    if DRY:
        logger.info("\n📉 [DRY] Actualizaciones que se aplicarían:")
        for u in updates:
            logger.info("   %s → %s", u["range"], u["values"])
        return

    def _batch_remisiones():
        rem_updates = [u for u in updates if "AA" in u["range"]]
        if rem_updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_REMISIONES,
                body={"valueInputOption": "USER_ENTERED", "data": rem_updates},
            ).execute()

    def _batch_inventario():
        inv_updates = [u for u in updates if "AA" not in u["range"]]
        if inv_updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_INVENTARIO,
                body={"valueInputOption": "USER_ENTERED", "data": inv_updates},
            ).execute()

    await loop.run_in_executor(None, _batch_remisiones)
    await loop.run_in_executor(None, _batch_inventario)
    logger.info("\n📉 Inventario actualizado en el Sheet.")


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
    data, headers, stock, inv_cols, inv_row, inv_tab = await leer_hojas(svc)

    logger.info("📦 Inventario actual:")
    for e in ESTILOS:
        logger.info("   %s: %s bot, %sL", e, int(stock[e]["bot"]), stock[e]["litros"])

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

        for est in ESTILOS:
            for tipo in ("bot", "litros"):
                stock[est][tipo] -= total_descontado[est][tipo]

        if not DRY and creadas:
            logger.info("\n📝 Actualizando Google Sheets...")
            await actualizar_sheets(svc, creadas, total_descontado, stock, inv_cols, inv_row, inv_tab)

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
