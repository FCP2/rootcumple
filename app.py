import os
import json
import time
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
@app.before_first_request
def init_all():
    global driver, wks
    # Driver
    driver = build_driver()
    ensure_logged_in()
    # Sheets
    wks = init_gspread()

# ===== Rutas =====
@app.route("/")
def home():
    ok = False
    try:
        ok = ensure_logged_in()
    except Exception:
        pass
    if ok:
        return "✅ Sesión lista. Sheets conectado. Usa /status, /preview, /send_pending"
    else:
        html = """
        <html>
          <head><meta http-equiv="refresh" content="5"></head>
          <body style="font-family: system-ui; padding: 24px;">
            <h2>Escanea el QR (WhatsApp > Dispositivos vinculados)</h2>
            <img src="/qr.png" alt="QR" style="max-width: 480px; border: 1px solid #ccc" />
          </body>
        </html>
        """
        return Response(html, mimetype="text/html")

@app.route("/qr.png")
def qr_png():
    png = driver.get_screenshot_as_png()
    return send_file(BytesIO(png), mimetype="image/png")

@app.route("/status")
def status():
    try:
        ok = ensure_logged_in()
        # prueba rápida de sheets
        _ = wks.row_values(1)
        return jsonify({"logged_in": ok, "worksheet": WORKSHEET_NAME})
    except Exception as e:
        return jsonify({"logged_in": False, "error": str(e)}), 500

@app.route("/preview")
def preview():
    """
    Vista previa de qué filas se enviarían según SEND_MODE:
      - today: Fecha == hoy
      - until_today: Fecha <= hoy
    y Enviado != 'sí'
    """
    today = today_mx()
    records = wks.get_all_records()  # asume encabezados: Nombre, Cargo, Fecha, Enviado
    pending = []
    for idx, row in enumerate(records, start=2):  # datos empiezan en fila 2
        nombre = (row.get("Nombre") or "").strip()
        cargo = (row.get("Cargo") or "").strip()
        fecha = parse_ddmmyy(row.get("Fecha") or row.get("Fecha (dd/mm/yy)") or row.get("Fecha(dd/mm/yy)"))
        enviado = (row.get("Enviado") or "").strip().lower()

        if enviado == "sí":
            continue
        if not fecha:
            continue

        if (SEND_MODE == "today" and fecha == today) or (SEND_MODE == "until_today" and fecha <= today):
            pending.append({"row": idx, "Nombre": nombre, "Cargo": cargo, "Fecha": fecha.isoformat()})

    return jsonify({"today": today.isoformat(), "mode": SEND_MODE, "to_send": pending})

@app.route("/send_pending", methods=["POST", "GET"])
def send_pending():
    """
    Envía mensajes para las filas pendientes (según /preview) a DEST_NUMBERS.
    Después marca Enviado = 'sí'.
    """
    if not DEST_NUMBERS:
        return jsonify({"error": "Configura DEST_NUMBERS (comma-separated)"}), 400

    today = today_mx()
    records = wks.get_all_records()
    sent = []
    for idx, row in enumerate(records, start=2):
        nombre = (row.get("Nombre") or "").strip()
        cargo = (row.get("Cargo") or "").strip()
        fecha = parse_ddmmyy(row.get("Fecha") or row.get("Fecha (dd/mm/yy)") or row.get("Fecha(dd/mm/yy)"))
        enviado = (row.get("Enviado") or "").strip().lower()

        if enviado == "sí" or not fecha:
            continue

        if not ((SEND_MODE == "today" and fecha == today) or (SEND_MODE == "until_today" and fecha <= today)):
            continue

        # Mensaje
        msg = f"🎉 *Recordatorio* \n- Nombre: {nombre}\n- Cargo: {cargo}\n- Fecha: {fecha.strftime('%d/%m/%Y')}"
        ok_all = True
        for num in DEST_NUMBERS:
            try:
                send_whatsapp_text(num, msg)
            except Exception as e:
                ok_all = False
                print(f"Error enviando a {num}: {e}")

        if ok_all:
            # Marca Enviado = "sí" en la columna correspondiente.
            # Buscamos la columna 'Enviado' por encabezado:
            headers = wks.row_values(1)
            try:
                col_enviado = headers.index("Enviado") + 1
            except ValueError:
                return jsonify({"error": "No se encontró la columna 'Enviado' en encabezados"}), 400

            wks.update_cell(idx, col_enviado, "sí")
            sent.append({"row": idx, "Nombre": nombre})

        time.sleep(1)  # pequeño delay para no saturar

    return jsonify({"today": today.isoformat(), "mode": SEND_MODE, "sent": sent, "count": len(sent)})

# Ping simple
@app.route("/ping")
def ping():
    return "pong"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)