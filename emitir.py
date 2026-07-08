"""
EMITIR FACTURAS — Madre Monte
Lee el consolidado de ventas desde Google Sheets y genera facturas en Alegra.

Uso:
  python emitir.py              # Procesa las filas pendientes
  python emitir.py --dry-run    # Solo muestra lo que se emitiría
"""

import csv
import io
import os
import sys
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("emitir")

# ── Configuración ──
SHEET_ID = "1WFq09-LDg4le6FqC9BnBZfKZXSmb8Kn195wnFZaEMys"
ALEGRA_EMAIL = os.getenv("ALEGRA_EMAIL", "")
ALEGRA_TOKEN = os.getenv("ALEGRA_TOKEN", "")
ALEGRA_API = "https://api.alegra.com/api/v1"

# Catálogo de productos (código → id Alegra, precio)
PRODUCTOS = {
    "PTB01": {"id": 64, "nombre": "Golden Ale 330ml", "precio": 8500},
    "PTB02": {"id": 65, "nombre": "Irish Red Ale 330ml", "precio": 8500},
    "PTB03": {"id": 66, "nombre": "APA 330ml", "precio": 8500},
    "PTB04": {"id": 67, "nombre": "IPA 330ml", "precio": 9500},
    "PTB05": {"id": 68, "nombre": "Stout 330ml", "precio": 8500},
    "PTL01": {"id": 69, "nombre": "Golden Ale (litro)", "precio": 18000},
    "PTL02": {"id": 70, "nombre": "Irish Red (litro)", "precio": 18000},
    "PTL03": {"id": 71, "nombre": "APA (litro)", "precio": 18000},
    "PTL04": {"id": 72, "nombre": "IPA (litro)", "precio": 19000},
    "PTL05": {"id": 73, "nombre": "Stout (litro)", "precio": 18000},
    "DOM01": {"id": 58, "nombre": "Domicilio", "precio": 12000},
    "TAX": 4,  # IVA
}

dry_run = "--dry-run" in sys.argv


def alegra_request(method, path, data=None):
    """Llama a la API de Alegra."""
    url = f"{ALEGRA_API}/{path}"
    auth = urllib.request.HTTPBasicAuth(ALEGRA_EMAIL, ALEGRA_TOKEN)
    headers = {"Content-Type": "application/json"}

    body = None
    if data is not None:
        body = json.dumps(data).encode()

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = json.loads(e.read())
        logger.error(f"Alegra API error {e.code}: {err}")
        return None


def buscar_cliente(nombre):
    """Busca un cliente en Alegra por nombre."""
    resp = alegra_request("GET", f"contacts?query={urllib.parse.quote(nombre)}&limit=5")
    if resp and resp.get("data"):
        for c in resp["data"]:
            if nombre.lower() in (c.get("name", "").lower()):
                return c
    return None


def descargar_consolidado():
    """Descarga el CSV desde Google Sheets."""
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
    logger.info(f"Descargando consolidado...")
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8-sig")


def leer_pendientes(csv_text):
    """Lee las filas pendientes de facturar del CSV."""
    reader = csv.DictReader(io.StringIO(csv_text))
    pendientes = []

    for row in reader:
        fecha = row.get("Fecha", "").strip()
        cliente = row.get("Nombre de cliente", "").strip()
        factura = row.get("Número de factura", "").strip()
        valor_str = row.get("Valor de la factura", "").strip()
        domicilio_str = row.get("Valor del domicilio", "").strip()
        observaciones = row.get("Observaciones", "").strip().lower()

        # Saltar si ya fue procesada
        if "emitido" in observaciones or "procesado" in observaciones:
            continue

        if not cliente or not factura:
            continue

        try:
            valor = float(valor_str.replace(".", "").replace(",", "."))
        except ValueError:
            valor = 0

        try:
            domicilio = float(domicilio_str.replace(".", "").replace(",", "."))
        except ValueError:
            domicilio = 0

        pendientes.append({
            "fecha": fecha,
            "cliente": cliente.strip(),
            "factura": factura.strip(),
            "valor": int(valor),
            "domicilio": int(domicilio),
        })

    return pendientes


def crear_factura_alegra(cliente_nombre, items, total):
    """Crea una factura en Alegra."""
    cliente = buscar_cliente(cliente_nombre)
    if not cliente:
        logger.error(f"Cliente no encontrado: {cliente_nombre}")
        return None

    line_items = []
    for item in items:
        prod = PRODUCTOS.get(item["codigo"])
        if not prod:
            logger.warning(f"Producto no encontrado: {item['codigo']}")
            continue
        line_items.append({
            "id": prod["id"],
            "quantity": item["cantidad"],
            "price": prod["precio"],
            "tax": [{"id": TAX["id"] if isinstance(TAX, dict) else 4}],
        })

    payload = {
        "client": {"id": cliente["id"]},
        "items": line_items,
        "dueDate": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        "status": "draft",
        "paymentForm": "CASH",
    }

    if dry_run:
        logger.info(f"[DRY RUN] Factura para {cliente_nombre}: {len(line_items)} items, ${total:,.0f}")
        return {"id": "dry-run"}

    resp = alegra_request("POST", "invoices", payload)
    if resp:
        logger.info(f"✅ Factura creada: FE-{resp.get('id')} para {cliente_nombre}")
        return resp
    return None


def main():
    if not ALEGRA_EMAIL or not ALEGRA_TOKEN:
        logger.error("❌ Configura ALEGRA_EMAIL y ALEGRA_TOKEN como variables de entorno")
        sys.exit(1)

    # 1. Leer consolidado
    csv_text = descargar_consolidado()
    pendientes = leer_pendientes(csv_text)

    if not pendientes:
        logger.info("✅ No hay facturas pendientes. Todo al día.")
        return

    logger.info(f"📋 {len(pendientes)} facturas pendientes por emitir")

    # 2. Procesar cada una
    exitosas = 0
    fallidas = 0

    for p in pendientes:
        logger.info(f"\n📄 {p['factura']} — {p['cliente']} — ${p['valor']:,}")

        items = []
        # Intentar inferir productos del valor
        # (En una versión completa, el formulario incluiría códigos de producto)
        valor_total = p["valor"]

        # Si el valor tiene domicilio incluido, separarlo
        if p["domicilio"] > 0:
            items.append({"codigo": "DOM01", "cantidad": 1})
            valor_total -= PRODUCTOS["DOM01"]["precio"]

        # Distribuir el valor restante entre posibles productos
        # (Simplificación: asumimos que el valor ya viene calculado)
        if valor_total > 0:
            # Buscar el producto más probable según el valor
            for codigo, prod in sorted(PRODUCTOS.items(), key=lambda x: -x[1]["precio"]):
                if codigo == "DOM01":
                    continue
                if valor_total >= prod["precio"] and valor_total % prod["precio"] == 0:
                    cant = valor_total // prod["precio"]
                    items.append({"codigo": codigo, "cantidad": cant})
                    break
            else:
                # No se pudo inferir — crear un item genérico
                items.append({"codigo": "PTB01", "cantidad": round(valor_total / PRODUCTOS["PTB01"]["precio"])})

        resultado = crear_factura_alegra(p["cliente"], items, p["valor"])
        if resultado:
            exitosas += 1
        else:
            fallidas += 1

    logger.info(f"\n✅ {exitosas} facturas emitidas, ❌ {fallidas} fallidas")


if __name__ == "__main__":
    main()
