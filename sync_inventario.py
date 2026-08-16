#!/usr/bin/env python3
"""
SYNC DE INVENTARIO — Madre Monte (workflow ligero)
Lee el Sheet de inventario en vivo (8 estilos, por columnas) y regenera
`inventario_conciliado.json` (físico + deducciones + neto) SIN tocar Alegra
ni Remisiones.

Usado por el workflow `sync.yml`, disparado por:
  - onEdit (Apps Script del Sheet), para actualización instantánea.
  - schedule (respaldo), por si el trigger falla.

Variables de entorno:
  GOOGLE_CREDS_JSON   credenciales de la service account (mismo que facturación)
  HISTORIAL_PATH      ruta del historial_facturacion.json existente
  OUT_PATH            salida (default: inventario_conciliado.json)
"""
import os
import json
import logging
from datetime import datetime

from dotenv import load_dotenv
import googleapiclient.discovery
from google.oauth2 import service_account
from google.auth.transport.requests import Request

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync")

SHEET_INVENTARIO = os.getenv("SHEET_INVENTARIO", "1UHqPRV1stpnM5VHer9-8Z0H_UW0omiSoape7cIwzCGA")
HISTORIAL_PATH = os.getenv("HISTORIAL_PATH", "historial_facturacion.json")
OUT_PATH = os.getenv("OUT_PATH", "inventario_conciliado.json")

ESTILOS = ["GOLDEN ALE", "IRISH RED ALE", "APA", "IPA", "STOUT",
           "GERMAN PILS", "HIDROMIEL", "RED IPA"]


def _normalizar_estilo(raw):
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


def leer_inventario(svc):
    """Lee la última fila del Sheet por columnas (igual que facturación y nucleo)."""
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

    # Fermentadores: columnas 1..14 (pares [litros, estilo])
    for i in range(1, 15, 2):
        litros = _f(last[i]) if i < len(last) else 0.0
        est = _normalizar_estilo(last[i + 1]) if i + 1 < len(last) else None
        if est and est in stock:
            stock[est]["litros"] += litros

    # Barriles: columnas 15..28 (pares [cantidad, estilo])
    for i in range(15, 29, 2):
        cant = _f(last[i]) if i < len(last) else 0.0
        est = _normalizar_estilo(last[i + 1]) if i + 1 < len(last) else None
        if est and est in stock:
            stock[est]["barril"] += cant

    # Botellas: columnas 29..40 (pares [cantidad, estilo])
    for i in range(29, 41, 2):
        cant = _f(last[i]) if i < len(last) else 0.0
        est = _normalizar_estilo(last[i + 1]) if i + 1 < len(last) else None
        if est and est in stock:
            stock[est]["bot"] += cant

    return stock


def leer_historial():
    hist = []
    if os.path.exists(HISTORIAL_PATH):
        try:
            hist = json.load(open(HISTORIAL_PATH, encoding="utf-8"))
        except Exception as e:
            logger.warning("No se pudo leer el historial: %s", e)
            hist = []
    if not isinstance(hist, list):
        hist = []
    return hist


def main():
    info = os.getenv("GOOGLE_CREDS_JSON")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if info:
        creds = service_account.Credentials.from_service_account_info(json.loads(info), scopes=scopes)
    else:
        path = os.getenv("GOOGLE_CREDS_PATH", "credenciales_google.json")
        creds = service_account.Credentials.from_service_account_file(path, scopes=scopes)
    creds.refresh(Request())
    svc = googleapiclient.discovery.build("sheets", "v4", credentials=creds)

    logger.info("📡 Leyendo inventario del Sheet...")
    stock = leer_inventario(svc)
    for e in ESTILOS:
        logger.info("   %s: %s bot | %sL barril | %sL fermentador",
                    e, int(stock[e]["bot"]), stock[e]["barril"], stock[e]["litros"])

    hist = leer_historial()
    logger.info("📚 Historial: %d registros", len(hist))

    deducciones = {e: {"bot": 0.0, "barril": 0.0, "litros": 0.0} for e in ESTILOS}
    for r in hist:
        d = r.get("descuento_inventario", {})
        for e in ESTILOS:
            de = d.get(e, {})
            deducciones[e]["bot"] += de.get("bot_descontadas_de_stock", 0)
            deducciones[e]["barril"] += de.get("litros_descontados_de_barril", 0)
            deducciones[e]["litros"] += de.get("litros_descontados_de_fermentador", 0)

    neto = {}
    for e in ESTILOS:
        f = stock[e]
        neto[e] = {
            "bot": round(f["bot"] - deducciones[e]["bot"], 1),
            "barril": round(f["barril"] - deducciones[e]["barril"], 1),
            "litros": round(f["litros"] - deducciones[e]["litros"], 1),
        }

    conciliado = {
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fisico": stock,
        "deducciones": deducciones,
        "neto": neto,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(conciliado, f, ensure_ascii=False, indent=2)
    logger.info("✅ Conciliado escrito en %s", OUT_PATH)


if __name__ == "__main__":
    main()
