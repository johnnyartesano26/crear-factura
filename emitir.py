#!/usr/bin/env python3
"""
EMITIR FACTURAS — Madre Monte (nube)
Basado en el script vigente facturar_google_sheets.py + mejoras solicitadas:

  1) Lee Remisiones (productos/cantidades reales) e Inventario (stock actual).
  2) Verifica stock por estilo antes de facturar.
  3) Crea la factura BORRADOR en Alegra.
  4) Marca la columna AA ("Facturado") en Remisiones (evita duplicados,
     igual que el pipeline local → comparten el mismo control).
  5) RESTA del inventario las botellas/litros facturados (última fila del
     Sheet de inventario, que se reinicia solo con cada registro semanal).

Credenciales:
  - Alegra: ALEGRA_EMAIL, ALEGRA_TOKEN (env)
  - Google: GOOGLE_CREDS_JSON (contenido) o GOOGLE_CREDS_PATH (archivo)

Uso:
  python emitir.py            # emite y actualiza inventario
  python emitir.py --dry-run  # solo muestra qué haría (no crea ni escribe)
"""
import os, sys, json, base64, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta

from google.oauth2 import service_account
from google.auth.transport.requests import Request
import googleapiclient.discovery
import googleapiclient.errors


def gexec(req, tries=5):
    """Ejecuta una petición de Google API con reintentos ante errores
    transitorios (429/5xx como el 503 'service unavailable')."""
    for intento in range(1, tries + 1):
        try:
            return req.execute()
        except googleapiclient.errors.HttpError as e:
            code = getattr(e.resp, "status", None)
            if str(code) in ("429", "500", "502", "503", "504") and intento < tries:
                time.sleep(2 ** intento)
                continue
            raise

DRY = "--dry-run" in sys.argv

ALEGRA_EMAIL = os.getenv("ALEGRA_EMAIL", "")
ALEGRA_TOKEN = os.getenv("ALEGRA_TOKEN", "")
ALEGRA_API = "https://api.alegra.com/api/v1"

SHEET_REMISIONES = "1hBicxCSwnZpreEPmru_ZScZQjuHPXcRQBiWatC8AC1Q"
SHEET_INVENTARIO = "1UHqPRV1stpnM5VHer9-8Z0H_UW0omiSoape7cIwzCGA"
RANGE = "A:ZZ"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# código → (id Alegra, precio)
MAPEO = {
    "PTB01": ("64", 8500), "PTB02": ("65", 8500), "PTB03": ("66", 8500),
    "PTB04": ("67", 9500), "PTB05": ("68", 8500),
    "PTL01": ("69", 18000), "PTL02": ("70", 18000), "PTL03": ("71", 18000),
    "PTL04": ("72", 19000), "PTL05": ("73", 18000), "DOM01": ("58", 12000),
}
# código → estilo (para inventario)
ESTILO_DE = {
    "PTB01": "GOLDEN ALE", "PTB02": "IRISH RED ALE", "PTB03": "APA", "PTB04": "IPA", "PTB05": "STOUT",
    "PTL01": "GOLDEN ALE", "PTL02": "IRISH RED ALE", "PTL03": "APA", "PTL04": "IPA", "PTL05": "STOUT",
}
ESTILOS = ["GOLDEN ALE", "IRISH RED ALE", "APA", "IPA", "STOUT"]

C_CLIENTE, C_DOM, C_FACTURAR, C_VALOR_DOM, C_DOM_ALT, C_FACTURADO = 2, 19, 22, 24, 23, 26
PARES_PROD = [(5, 6), (8, 9), (11, 12), (14, 15), (17, 18)]


def log(m): print(m, flush=True)


# ───────── Google Sheets ─────────
def sheets_service():
    info = os.getenv("GOOGLE_CREDS_JSON")
    if info:
        creds = service_account.Credentials.from_service_account_info(json.loads(info), scopes=SCOPES)
    else:
        path = os.getenv("GOOGLE_CREDS_PATH", "credenciales_google.json")
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    creds.refresh(Request())
    return googleapiclient.discovery.build("sheets", "v4", credentials=creds)


def col_letter(idx):  # 0-based → A1
    s = ""; idx += 1
    while idx > 0:
        idx, r = divmod(idx - 1, 26); s = chr(65 + r) + s
    return s


def leer_valores(svc, sid):
    return gexec(svc.spreadsheets().values().get(spreadsheetId=sid, range=RANGE)).get("values", [])


def tab_name(svc, sid):
    m = gexec(svc.spreadsheets().get(spreadsheetId=sid, fields="sheets.properties.title"))
    return m["sheets"][0]["properties"]["title"]


def _f(v, d=0):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return d


def leer_inventario(svc):
    """Devuelve (stock, cols, row_num, tab). stock[estilo]={'bot','litros'};
    cols[estilo]={'bot':col_idx,'litros':col_idx} para poder escribir el descuento."""
    rows = leer_valores(svc, SHEET_INVENTARIO)
    stock = {e: {"bot": 0.0, "litros": 0.0} for e in ESTILOS}
    cols = {e: {"bot": None, "litros": None} for e in ESTILOS}
    row_num = None
    last = None
    for i in range(len(rows) - 1, -1, -1):
        if any(c.strip() for c in rows[i]):
            last = rows[i]; row_num = i + 1; break
    if not last:
        return stock, cols, row_num, None
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
        elif "LITRO" in up:
            stock[est]["litros"] = _f(last[vcol]) if vcol < len(last) else 0
            cols[est]["litros"] = vcol
    return stock, cols, row_num, tab_name(svc, SHEET_INVENTARIO)


# ───────── Alegra ─────────
def _auth():
    return "Basic " + base64.b64encode(f"{ALEGRA_EMAIL}:{ALEGRA_TOKEN}".encode()).decode()


def alegra(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(f"{ALEGRA_API}/{path}", data=body, method=method,
                                 headers={"Authorization": _auth(), "Content-Type": "application/json",
                                          "Accept": "application/json"})
    for intento in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** intento); continue
            log(f"   ❌ Alegra {e.code}: {e.read()[:200]}"); return None
        except Exception as e:
            log(f"   ❌ Alegra {intento}: {e}"); time.sleep(intento)
    return None


def buscar_cliente(nombre):
    res = alegra("GET", f"contacts?name={urllib.parse.quote(nombre)}&limit=5")
    if isinstance(res, list) and res:
        for c in res:
            if nombre.lower() in (c.get("name", "") or "").lower():
                return c["id"]
        return res[0]["id"]
    return None


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
            items.append({"ref": ref, "id": pid, "quantity": cant, "price": precio,
                          "tax": [{"id": 4}] if pid != "58" else []})
    return items


def verificar(ref, cant, stock):
    if ref == "DOM01":
        return True, ""
    est = ESTILO_DE.get(ref)
    if not est:
        return True, ""
    if ref.startswith("PTB"):
        disp = stock[est]["bot"]
        return cant <= disp, f"{est}: {int(cant)} bot / {int(disp)} disp"
    disp = stock[est]["litros"]
    return cant <= disp, f"{est}: {cant}L / {disp}L disp"


def main():
    if not ALEGRA_EMAIL or not ALEGRA_TOKEN:
        log("❌ Faltan ALEGRA_EMAIL / ALEGRA_TOKEN"); sys.exit(1)

    log("=" * 56)
    log(f"🍺 EMISIÓN DE FACTURAS — Madre Monte {'(DRY-RUN)' if DRY else ''}")
    log("=" * 56)

    svc = sheets_service()

    stock, cols, inv_row, inv_tab = leer_inventario(svc)
    log("📦 Inventario actual:")
    for e in ESTILOS:
        log(f"   {e}: {int(stock[e]['bot'])} bot, {stock[e]['litros']}L")

    rows = leer_valores(svc, SHEET_REMISIONES)
    if len(rows) < 2:
        log("Sin datos en Remisiones."); return
    data = rows[1:]

    nuevas = sin_stock = sin_cliente = ya = 0
    creadas = []
    descuentos = {e: {"bot": 0.0, "litros": 0.0} for e in ESTILOS}

    for i, row in enumerate(data, start=2):
        if C_FACTURAR >= len(row) or row[C_FACTURAR].strip().lower() not in ("si", "sí", "true", "1"):
            continue
        if C_FACTURADO < len(row) and row[C_FACTURADO].strip():
            ya += 1; continue
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
                        pdom = v * 1000 if v < 100 else v; break
            items.append({"ref": "DOM01", "id": "58", "quantity": 1, "price": pdom, "tax": []})
        if not items:
            continue

        # 1) verificar inventario
        ok = True
        for it in items:
            good, msg = verificar(it["ref"], it["quantity"], stock)
            if not good:
                log(f"   ⛔ Fila {i} {cliente}: {msg}"); ok = False
        if not ok:
            sin_stock += 1; continue

        # cliente en Alegra
        cid = buscar_cliente(cliente)
        if not cid:
            log(f"   ❓ Cliente no encontrado en Alegra: {cliente}"); sin_cliente += 1; continue

        resumen = ", ".join(f"{it['ref']}x{int(it['quantity'])}" for it in items)

        if DRY:
            log(f"   [DRY] {cliente}: {resumen}")
        else:
            payload = {"client": cid, "date": datetime.now().strftime("%Y-%m-%d"),
                       "dueDate": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
                       "items": [{"id": it["id"], "quantity": it["quantity"], "price": it["price"], "tax": it["tax"]} for it in items],
                       "paymentForm": "CASH", "paymentMethod": "CASH", "status": "draft"}
            fac = alegra("POST", "invoices", payload)
            if not (fac and fac.get("id")):
                log(f"   ❌ Error creando factura de {cliente}"); continue
            log(f"   ✅ Factura {fac['id']} — {cliente} ({resumen})")
            creadas.append({"id": fac["id"], "cliente": cliente, "total": fac.get("total", 0)})
            # marcar Facturado (col AA)
            try:
                gexec(svc.spreadsheets().values().update(
                    spreadsheetId=SHEET_REMISIONES, range=f"AA{i}",
                    valueInputOption="USER_ENTERED", body={"values": [[f"Facturado {fac['id']}"]]}))
            except Exception as e:
                log(f"   ⚠️ No se pudo marcar AA fila {i}: {e}")

        # 2) descontar del inventario (en memoria; se escribe al final)
        for it in items:
            est = ESTILO_DE.get(it["ref"])
            if not est:
                continue
            tipo = "bot" if it["ref"].startswith("PTB") else "litros"
            stock[est][tipo] -= it["quantity"]
            descuentos[est][tipo] += it["quantity"]
        nuevas += 1

    # 3) escribir el inventario descontado en la última fila
    cambios = {e: d for e, d in descuentos.items() if d["bot"] or d["litros"]}
    if cambios and inv_row:
        updates = []
        for est, d in cambios.items():
            for tipo in ("bot", "litros"):
                if d[tipo] and cols[est][tipo] is not None:
                    rng = f"'{inv_tab}'!{col_letter(cols[est][tipo])}{inv_row}"
                    updates.append({"range": rng, "values": [[stock[est][tipo]]]})
        if DRY:
            log("\n📉 Descuentos de inventario que se aplicarían:")
            for est, d in cambios.items():
                log(f"   {est}: -{int(d['bot'])} bot, -{d['litros']}L  →  queda {int(stock[est]['bot'])} bot, {stock[est]['litros']}L")
        elif updates:
            gexec(svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_INVENTARIO,
                body={"valueInputOption": "USER_ENTERED", "data": updates}))
            log("\n📉 Inventario actualizado (descontado) en el Sheet.")

    log("=" * 56)
    resumen_txt = f"✅ {nuevas} emitidas | ⛔ {sin_stock} sin stock | ❓ {sin_cliente} sin cliente | ⏭️ {ya} ya estaban"
    log("📊 " + resumen_txt)
    log("=" * 56)

    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"nuevas={nuevas}\n")
            f.write(f"resumen={resumen_txt}\n")


if __name__ == "__main__":
    main()
