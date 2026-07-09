#!/usr/bin/env python3
"""
EMITIR FACTURAS — Madre Monte (versión nube, corregida)
Replica la lógica del pipeline local facturar_google_sheets_v2.py:
  - Lee el Sheet de REMISIONES (productos + cantidades reales, NO adivina)
  - Lee el Sheet de INVENTARIO y verifica stock por estilo
  - Busca el cliente en Alegra y crea la factura BORRADOR
  - Evita duplicados con emitidas.json (idempotente)

Uso:
  python emitir.py            # emite las remisiones pendientes
  python emitir.py --dry-run  # muestra qué se emitiría, sin crear nada
"""
import csv, io, os, sys, json, base64, time, hashlib, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta

DRY = "--dry-run" in sys.argv

ALEGRA_EMAIL = os.getenv("ALEGRA_EMAIL", "")
ALEGRA_TOKEN = os.getenv("ALEGRA_TOKEN", "")
ALEGRA_API = "https://api.alegra.com/api/v1"

SHEET_REMISIONES = "1hBicxCSwnZpreEPmru_ZScZQjuHPXcRQBiWatC8AC1Q"
SHEET_INVENTARIO = "1UHqPRV1stpnM5VHer9-8Z0H_UW0omiSoape7cIwzCGA"

HERE = os.path.dirname(os.path.abspath(__file__))
ESTADO = os.path.join(HERE, "emitidas.json")

# código → (id Alegra, precio, estilo)
MAPEO = {
    "PTB01": ("64", 8500, "GOLDEN ALE"), "PTB02": ("65", 8500, "IRISH RED ALE"),
    "PTB03": ("66", 8500, "APA"), "PTB04": ("67", 9500, "IPA"),
    "PTB05": ("68", 8500, "STOUT"),
    "PTL01": ("69", 18000, "GOLDEN ALE"), "PTL02": ("70", 18000, "IRISH RED ALE"),
    "PTL03": ("71", 18000, "APA"), "PTL04": ("72", 19000, "IPA"),
    "PTL05": ("73", 18000, "STOUT"), "DOM01": ("58", 12000, "DOMICILIO"),
}
ESTILOS = ["GOLDEN ALE", "IRISH RED ALE", "APA", "IPA", "STOUT"]

# columnas del Sheet de Remisiones (0-indexed)
C_MARCA, C_CLIENTE, C_DOM, C_FACTURAR, C_VALOR_DOM, C_FACTURADO = 0, 2, 19, 22, 24, 26
PARES_PROD = [(5, 6), (8, 9), (11, 12), (14, 15), (17, 18)]


def log(msg):
    print(msg, flush=True)


# ── Alegra (urllib + Basic auth) ──
def _auth_header():
    raw = f"{ALEGRA_EMAIL}:{ALEGRA_TOKEN}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def alegra(method, path, data=None):
    url = f"{ALEGRA_API}/{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": _auth_header(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    for intento in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** intento); continue
            log(f"   ❌ Alegra {e.code}: {e.read()[:200]}")
            return None
        except Exception as e:
            log(f"   ❌ Alegra intento {intento}: {e}")
            time.sleep(intento)
    return None


def buscar_cliente(nombre):
    q = urllib.parse.quote(nombre)
    res = alegra("GET", f"contacts?name={q}&limit=5")
    if isinstance(res, list) and res:
        for c in res:
            if nombre.lower() in (c.get("name", "") or "").lower():
                return c["id"]
        return res[0]["id"]
    return None


# ── Google Sheets (CSV público, sin credenciales) ──
def leer_sheet(sheet_id):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def _f(val, d=0):
    try:
        return float(str(val).replace(",", "."))
    except Exception:
        return d


def get_inventario():
    """Lee la última fila del inventario. Robusto a cambios de columnas:
    detecta el estilo y el tipo (Botella/Litro) por la etiqueta de cada
    columna ESTILO y toma el valor de la columna CANTIDAD anterior."""
    rows = leer_sheet(SHEET_INVENTARIO)
    inv = {e: {"bot": 0, "litros": 0} for e in ESTILOS}
    last = None
    for r in reversed(rows):
        if any(c.strip() for c in r):
            last = r
            break
    if not last:
        return inv
    for j, cell in enumerate(last):
        up = cell.strip().upper()
        if not up or j == 0:
            continue
        estilo = next((e for e in ESTILOS if e in up), None)
        if not estilo:
            continue
        val = _f(last[j - 1]) if j - 1 >= 0 else 0
        if "BOTELLA" in up:
            inv[estilo]["bot"] += val
        elif "LITRO" in up:
            inv[estilo]["litros"] += val
    return inv


def verificar_stock(ref, cant, inv):
    if ref == "DOM01" or ref not in MAPEO:
        return True, "OK"
    estilo = MAPEO[ref][2]
    st = inv.get(estilo, {})
    if ref.startswith("PTB"):
        disp = st.get("bot", 0)
        return (cant <= disp), f"{estilo}: {cant} bot pedidas / {int(disp)} disp"
    if ref.startswith("PTL"):
        disp = st.get("litros", 0)
        return (cant <= disp), f"{estilo}: {cant}L pedidos / {disp}L disp"
    return True, "OK"


def extraer_items(row):
    items = []
    for pc, cc in PARES_PROD:
        if pc >= len(row) or cc >= len(row):
            continue
        m = None
        cel = row[pc].strip()
        for ref in MAPEO:
            if ref in cel:
                m = ref; break
        if not m:
            continue
        cant = _f(row[cc].strip(), 0)
        if cant > 0:
            pid, precio, _ = MAPEO[m]
            items.append({"ref": m, "id": pid, "quantity": cant, "price": precio,
                          "tax": [{"id": 4}] if pid != "58" else []})
    return items


def clave_fila(row):
    marca = row[C_MARCA].strip() if len(row) > C_MARCA else ""
    if marca:
        return marca
    return "h:" + hashlib.sha1(("|".join(row)).encode()).hexdigest()[:16]


def cargar_estado():
    if os.path.exists(ESTADO):
        try:
            return json.load(open(ESTADO, encoding="utf-8"))
        except Exception:
            pass
    return {"emitidas": [], "facturas": []}


def main():
    if not ALEGRA_EMAIL or not ALEGRA_TOKEN:
        log("❌ Faltan ALEGRA_EMAIL / ALEGRA_TOKEN")
        sys.exit(1)

    log("=" * 55)
    log(f"🍺 EMISIÓN DE FACTURAS — Madre Monte {'(DRY-RUN)' if DRY else ''}")
    log("=" * 55)

    inv = get_inventario()
    if inv:
        for e, d in inv.items():
            log(f"   📦 {e}: {int(d['bot'])} bot, {d['litros']}L")
    else:
        log("   ⚠️ Inventario vacío o inaccesible")

    estado = cargar_estado()
    ya = set(estado["emitidas"])

    rows = leer_sheet(SHEET_REMISIONES)
    if len(rows) < 2:
        log("Sin datos en Remisiones."); return
    data = rows[1:]

    nuevas = sin_stock = ya_emit = sin_cliente = 0
    creadas = []

    for i, row in enumerate(data, start=2):
        # marcado como facturar = sí
        if C_FACTURAR >= len(row) or row[C_FACTURAR].strip().lower() not in ("si", "sí", "true", "1"):
            continue
        # ya facturado en el Sheet (columna AA)
        if C_FACTURADO < len(row) and row[C_FACTURADO].strip().lower() not in ("", "no", "false", "0"):
            ya_emit += 1; continue
        # ya emitido por nosotros antes (idempotencia)
        key = clave_fila(row)
        if key in ya:
            ya_emit += 1; continue

        cliente = row[C_CLIENTE].strip() if C_CLIENTE < len(row) else ""
        if not cliente:
            continue

        items = extraer_items(row)
        # domicilio
        if C_DOM < len(row) and row[C_DOM].strip().lower() in ("se incluye", "si", "sí", "1"):
            pdom = 12000
            if C_VALOR_DOM < len(row) and row[C_VALOR_DOM].strip():
                v = _f(row[C_VALOR_DOM].strip(), 12)
                pdom = v * 1000 if v < 100 else v
            items.append({"ref": "DOM01", "id": "58", "quantity": 1, "price": pdom, "tax": []})
        if not items:
            continue

        # inventario
        ok_stock = True
        for it in items:
            ok, msg = verificar_stock(it["ref"], it["quantity"], inv)
            if not ok:
                log(f"   ⛔ Fila {i} {cliente}: {msg}")
                ok_stock = False
        if not ok_stock:
            sin_stock += 1; continue

        # cliente en Alegra
        cid = buscar_cliente(cliente)
        if not cid:
            log(f"   ❓ Cliente no encontrado en Alegra: {cliente}")
            sin_cliente += 1; continue

        resumen = ", ".join(f"{it['ref']}x{int(it['quantity'])}" for it in items)
        if DRY:
            log(f"   [DRY] {cliente}: {resumen}")
            nuevas += 1
            continue

        payload = {
            "client": cid,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "dueDate": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
            "items": [{"id": it["id"], "quantity": it["quantity"], "price": it["price"], "tax": it["tax"]} for it in items],
            "paymentForm": "CASH", "paymentMethod": "CASH", "status": "draft",
        }
        fac = alegra("POST", "invoices", payload)
        if fac and fac.get("id"):
            log(f"   ✅ Factura {fac['id']} — {cliente} ({resumen})")
            creadas.append({"id": fac["id"], "cliente": cliente,
                            "total": fac.get("total", 0), "fecha": datetime.now().isoformat()})
            estado["emitidas"].append(key)
            nuevas += 1
        else:
            log(f"   ❌ Error creando factura de {cliente}")

    log("=" * 55)
    log(f"📊 RESUMEN: ✅ {nuevas} emitidas | ⛔ {sin_stock} sin stock | "
        f"❓ {sin_cliente} sin cliente | ⏭️ {ya_emit} ya estaban")
    log("=" * 55)

    if not DRY and creadas:
        estado["facturas"] = (estado.get("facturas", []) + creadas)[-500:]
        with open(ESTADO, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=2)

    # salida para el workflow
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"nuevas={nuevas}\n")
            f.write(f"resumen={nuevas} emitidas, {sin_stock} sin stock, {sin_cliente} sin cliente, {ya_emit} ya estaban\n")


if __name__ == "__main__":
    main()
