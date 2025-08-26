import os
import json
import time
import threading
from io import BytesIO
from datetime import datetime, date
from dateutil import tz
from dateutil.parser import parse as dtparse

from flask import Flask, send_file, Response, jsonify, request

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# ===== Config =====
PORT = int(os.getenv("PORT", "10000"))
PROFILE_DIR = os.getenv("WA_PROFILE_DIR", "/data/whatsapp")
SHEET_KEY = os.getenv("SHEET_KEY", "").strip()
SHEET_NAME = os.getenv("SHEET_NAME", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Sheet1").strip()
DEST_NUMBERS = [n.strip() for n in os.getenv("DEST_NUMBERS", "").split(",") if n.strip()]
SEND_MODE = os.getenv("SEND_MODE", "today").lower()  # "today" | "until_today"

# Zona horaria MX
MX_TZ = tz.gettz(os.getenv("TZ", "America/Mexico_City"))

app = Flask(__name__)
driver = None
wks = None  # worksheet de Sheets

# ===== Helpers: Google Sheets =====
def init_gspread():
    """Inicializa gspread usando el JSON en la env var GCP_CREDENTIALS_JSON."""
    creds_json = os.getenv("GCP_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("Falta variable de entorno GCP_CREDENTIALS_JSON")

    info = json.loads(creds_json)
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(info, scopes=scope)
    gc = gspread.authorize(credentials)

    if SHEET_KEY:
        sh = gc.open_by_key(SHEET_KEY)
    elif SHEET_NAME:
        sh = gc.open(SHEET_NAME)
    else:
        raise RuntimeError("Debes configurar SHEET_KEY o SHEET_NAME")

    return sh.worksheet(WORKSHEET_NAME)

def parse_ddmmyy(s):
    """Convierte 'dd/mm/yy' o 'dd/mm/yyyy' a date."""
    s = (s or "").strip()
    if not s:
        return None
    # Manejo robusto: intenta dd/mm/yy primero
    try:
        # Fuerzo dayfirst
        dt = dtparse(s, dayfirst=True)
        return dt.date()
    except Exception:
        return None

def today_mx():
    return datetime.now(MX_TZ).date()

# ===== Helpers: Selenium/WhatsApp =====
def build_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1280,900")

    os.makedirs(PROFILE_DIR, exist_ok=True)
    chrome_options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    chrome_options.add_argument("--profile-directory=Default")

    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=chrome_options)

def ensure_logged_in():
    driver.get("https://web.whatsapp.com/")
    # Espera a que cargue el buscador (si ya está logeado)
    for _ in range(60):
        try:
            driver.find_element(By.XPATH, "//div[@contenteditable='true' and @role='textbox']")
            return True
        except Exception:
            time.sleep(1)
    return False

def send_whatsapp_text(phone: str, text: str):
    # URL directa con mensaje codificado
    from urllib.parse import quote
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}"
    driver.get(url)

    # Espera caja de texto y botón "Enviar"
    for _ in range(30):
        try:
            # Campo editable presente?
            driver.find_element(By.XPATH, "//div[@contenteditable='true' and @role='textbox']")
            break
        except Exception:
            time.sleep(1)

    # Simula "Enter" para enviar (WhatsApp envía al presionar Enter)
    from selenium.webdriver.common.keys import Keys
    box = driver.find_element(By.XPATH, "//div[@contenteditable='true' and @role='textbox']")
    box.send_keys(Keys.ENTER)
    time.sleep(2)  # pequeña espera para asegurar envío
    return True

# ===== Flask lifecycle =====
app = Flask(__name__)
driver = None
wks = None
_init_lock = threading.Lock()
_initialized = False

def init_all():
    """
    Corre una sola vez (con lock). Prepara Selenium + Sheets.
    No debe lanzar excepciones que tumben el server: loguea y continúa.
    """
    global driver, wks, _initialized
    with _init_lock:
        if _initialized:
            return
        try:
            print("Init: creando WebDriver...")
            d = build_driver()
            print("Init: verificando login de WhatsApp...")
            _ = ensure_logged_in()  # puede devolver False si aún falta escanear QR
            print("Init: conectando a Google Sheets...")
            ws = init_gspread()
            driver = d
            wks = ws
            _initialized = True
            print("Init OK: Selenium + Sheets listos.")
        except Exception as e:
            # No tumbar el server, solo loguear
            print("Init error:", e)

def ensure_init_async():
    """
    Dispara la init en segundo plano si aún no está lista.
    """
    global _initialized
    if not _initialized:
        threading.Thread(target=init_all, daemon=True).start()
# ===== Rutas =====
@app.route("/")
def home():
    # Dispara init y muestra estado sin bloquear
    ensure_init_async()

    if _initialized:
        # Si driver existe, intenta verificar si ya hay sesión
        ready = False
        try:
            ready = ensure_logged_in()
        except Exception:
            pass

        if ready:
            return "✅ Sesión lista. Sheets conectado. Endpoints: /status, /preview, /send_pending"
        else:
            # QR aún no visible o cargando
            html = """
            <html>
              <head><meta http-equiv="refresh" content="5"></head>
              <body style="font-family: system-ui; padding: 24px;">
                <h2>Escanea el QR (WhatsApp &gt; Dispositivos vinculados)</h2>
                <p>La página se actualiza cada 5s hasta que se detecte la sesión.</p>
                <img src="/qr.png" alt="QR" style="max-width: 480px; border: 1px solid #ccc" />
                <p><a href="/status" target="_blank">Ver estado</a></p>
              </body>
            </html>
            """
            return Response(html, mimetype="text/html")
    else:
        # Aún inicializando
        html = """
        <html>
          <head><meta http-equiv="refresh" content="5"></head>
          <body style="font-family: system-ui; padding: 24px;">
            <h2>Inicializando…</h2>
            <p>Preparando Selenium (Chrome) y conexión a Google Sheets.</p>
            <p>Refresca en unos segundos.</p>
            <p><a href="/status" target="_blank">Ver estado</a></p>
          </body>
        </html>
        """
        return Response(html, mimetype="text/html")

@app.route("/qr.png")
def qr_png():
    # Asegura que la init fue disparada
    ensure_init_async()
    if driver is None:
        # Aún no listo: devuelve PNG vacío válido
        return send_file(BytesIO(b""), mimetype="image/png")
    try:
        png = driver.get_screenshot_as_png()
        return send_file(BytesIO(png), mimetype="image/png")
    except Exception:
        return send_file(BytesIO(b""), mimetype="image/png")

@app.route("/status")
def status():
    ensure_init_async()
    info = {
        "initialized": _initialized,
        "driver": driver is not None,
        "worksheet": WORKSHEET_NAME if wks else None,
        "sheet_key": bool(SHEET_KEY),
        "sheet_name": SHEET_NAME if SHEET_NAME else None,
        "tz": str(MX_TZ),
    }
    # Intento rápido de comprobar login si hay driver
    if driver is not None:
        try:
            info["logged_in"] = ensure_logged_in()
        except Exception as e:
            info["logged_in"] = False
            info["driver_error"] = str(e)
    else:
        info["logged_in"] = False
    return jsonify(info)

@app.route("/preview")
def preview():
    """
    Vista previa de filas a enviar según:
      - SEND_MODE: "today" -> Fecha == hoy
                   "until_today" -> Fecha <= hoy
      - Enviado != "sí"
    """
    ensure_init_async()
    if wks is None:
        return jsonify({"error": "Sheets no inicializado todavía"}), 503

    hoy = today_mx()
    records = wks.get_all_records()  # Encabezados: Nombre, Cargo, Fecha (o Fecha(dd/mm/yy)), Enviado
    pending = []

    for idx, row in enumerate(records, start=2):  # datos desde fila 2
        nombre = (row.get("Nombre") or "").strip()
        cargo = (row.get("Cargo") or "").strip()
        # Soportar varias etiquetas para la fecha
        fecha = parse_ddmmyy(row.get("Fecha") or row.get("Fecha (dd/mm/yy)") or row.get("Fecha(dd/mm/yy)"))
        enviado = (row.get("Enviado") or "").strip().lower()
        if enviado == "sí" or not fecha:
            continue

        if (SEND_MODE == "today" and fecha == hoy) or (SEND_MODE == "until_today" and fecha <= hoy):
            pending.append({"row": idx, "Nombre": nombre, "Cargo": cargo, "Fecha": fecha.isoformat()})

    return jsonify({"today": hoy.isoformat(), "mode": SEND_MODE, "to_send": pending})

@app.route("/send_pending", methods=["GET", "POST"])
def send_pending():
    """
    Envía mensajes a DEST_NUMBERS para filas pendientes y marca Enviado = "sí".
    """
    ensure_init_async()
    if not DEST_NUMBERS:
        return jsonify({"error": "Configura DEST_NUMBERS (comma-separated)"}), 400
    if driver is None:
        return jsonify({"error": "WebDriver no inicializado todavía"}), 503
    if wks is None:
        return jsonify({"error": "Sheets no inicializado todavía"}), 503

    hoy = today_mx()
    headers = wks.row_values(1)
    try:
        col_enviado = headers.index("Enviado") + 1
    except ValueError:
        return jsonify({"error": "No se encontró la columna 'Enviado' en encabezados"}), 400

    records = wks.get_all_records()
    sent = []

    for idx, row in enumerate(records, start=2):
        nombre = (row.get("Nombre") or "").strip()
        cargo = (row.get("Cargo") or "").strip()
        fecha = parse_ddmmyy(row.get("Fecha") or row.get("Fecha (dd/mm/yy)") or row.get("Fecha(dd/mm/yy)"))
        enviado = (row.get("Enviado") or "").strip().lower()

        if enviado == "sí" or not fecha:
            continue
        if not ((SEND_MODE == "today" and fecha == hoy) or (SEND_MODE == "until_today" and fecha <= hoy)):
            continue

        # Mensaje
        msg = f"🎉 *Recordatorio*\n- Nombre: {nombre}\n- Cargo: {cargo}\n- Fecha: {fecha.strftime('%d/%m/%Y')}"
        ok_all = True
        for num in DEST_NUMBERS:
            try:
                send_whatsapp_text(num, msg)
            except Exception as e:
                ok_all = False
                print(f"Error enviando a {num}: {e}")

        if ok_all:
            wks.update_cell(idx, col_enviado, "sí")
            sent.append({"row": idx, "Nombre": nombre})

        time.sleep(1)  # pequeño respiro para no saturar

    return jsonify({"today": hoy.isoformat(), "mode": SEND_MODE, "sent": sent, "count": len(sent)})

@app.route("/ping")
def ping():
    return "pong"


# =========================
# Main
# =========================
if __name__ == "__main__":
    # ¡No llames init_all() aquí! Arrancamos rápido y la init corre async.
    print("Starting Flask on PORT", PORT)
    app.run(host="0.0.0.0", port=PORT)
