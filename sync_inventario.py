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
import re
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
    if any(x in r for x in ['scottish', 'scottish ale']):
        return 'SCOTTISH'
    if r in ('vacio', 'vacío'):
        return 'VACIO'
    return None


def _f(v, d=0):
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return d


# ═══════════════════════════════════════════════════════════════════════════
# LECTURA CON FORWARD-FILL (REGLA "Celda vacía = sin cambios").
# El Sheet de inventario es incremental: cada fila nueva solo trae lo que cambió.
# Para conocer el stock actual hay que reproducir todo el historial en orden
# cronológico, igual que nucleo_de_inventario.py:
#   - Celda vacía  → conserva el último valor conocido.
#   - "0" explícito → "se agotó" (NO se confunde con vacío).
#   - "VACIO"      → tanque vaciado explícito (se resetea a 0).
# ═══════════════════════════════════════════════════════════════════════════

def _es_vacio(v):
    """True si la celda está vacía (None o string en blanco)."""
    return v is None or str(v).strip() == ''


def _to_float(v):
    """Convierte a float tolerando coma decimal; devuelve None si está vacío."""
    if _es_vacio(v):
        return None
    s = str(v).strip().replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def _sort_key(raw):
    """Convierte la marca temporal ("D/M/YYYY H:MM:SS") a clave ordenable."""
    if _es_vacio(raw):
        return ''
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?', str(raw).strip())
    if not m:
        return str(raw).strip()
    d, mo, y = m.group(1), m.group(2), m.group(3)
    h, mi, se = m.group(4) or '0', m.group(5) or '0', m.group(6) or '0'
    return f"{y}{int(mo):02d}{int(d):02d}{int(h):02d}{int(mi):02d}{int(se):02d}"


def _resolver_par(cant_raw, est_raw):
    """Resuelve un par CANTIDAD+ESTILO (barril/botella) con forward-fill."""
    if _es_vacio(cant_raw) and _es_vacio(est_raw):
        return None
    if _es_vacio(cant_raw) or _es_vacio(est_raw):
        return None
    estilo = _normalizar_estilo(est_raw)
    if not estilo or estilo == 'VACIO':
        return None
    cant = _to_float(cant_raw)
    if cant is None:
        return None
    return estilo, cant


def _resolver_ferm(prev, lit_raw, est_raw):
    """Resuelve el estado de un fermentador con forward-fill (por tanque)."""
    prev = prev or {'litros': 0.0, 'estilo': ''}
    if _es_vacio(lit_raw) and _es_vacio(est_raw):
        return dict(prev)
    lit = _to_float(lit_raw) if not _es_vacio(lit_raw) else None
    estilo = _normalizar_estilo(est_raw) if not _es_vacio(est_raw) else None
    if estilo == 'VACIO':
        return {'litros': 0.0, 'estilo': ''}
    if estilo is None:
        estilo = prev['estilo']
    if lit is None:
        lit = prev['litros']
    return {'litros': lit, 'estilo': estilo}


FERM_KEYS = ['F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7']


def leer_inventario(svc):
    """Lee el Sheet aplicando forward-fill sobre el historial completo.

    Devuelve stock[estilo] = {bot, barril, litros}, igual que facturacion_async.
    """
    rows = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_INVENTARIO, range="A:ZZ").execute().get("values", [])

    # Saltar encabezado, descartar filas vacías y ordenar cronológicamente.
    filas = [f for f in rows[1:] if f]
    filas.sort(key=lambda f: _sort_key(f[0]) if len(f) > 0 else '')

    ferm_state = {}
    barril_state = {}
    botella_state = {}

    for fila in filas:
        # Fermentadores F1..F7 (columnas 1..14, pares [litros, estilo]).
        for i, key in enumerate(FERM_KEYS):
            lit_raw = fila[1 + i * 2] if len(fila) > 1 + i * 2 else ''
            est_raw = fila[2 + i * 2] if len(fila) > 2 + i * 2 else ''
            ferm_state[key] = _resolver_ferm(ferm_state.get(key), lit_raw, est_raw)

        # Barriles (columnas 15..28): acumular por estilo si se repite en la fila.
        row_barril = {}
        for i in range(15, 29, 2):
            if len(fila) > i + 1:
                r = _resolver_par(fila[i], fila[i + 1])
                if r:
                    row_barril[r[0]] = row_barril.get(r[0], 0.0) + r[1]
        for e, c in row_barril.items():
            barril_state[e] = c

        # Botellas (columnas 29..40): idem.
        row_botella = {}
        for i in range(29, 41, 2):
            if len(fila) > i + 1:
                r = _resolver_par(fila[i], fila[i + 1])
                if r:
                    row_botella[r[0]] = row_botella.get(r[0], 0.0) + r[1]
        for e, c in row_botella.items():
            botella_state[e] = c

    stock = {e: {"bot": 0.0, "barril": 0.0, "litros": 0.0} for e in ESTILOS}
    for data in ferm_state.values():
        est = data["estilo"]
        if est in stock:
            stock[est]["litros"] += data["litros"]
    for est, cant in barril_state.items():
        if est in stock:
            stock[est]["barril"] = cant
    for est, cant in botella_state.items():
        if est in stock:
            stock[est]["bot"] = cant

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
