from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import os, json, psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from datetime import datetime, timedelta
from dateutil import parser
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()
app = FastAPI(title="Chatbot Restaurante Demo")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN", "http://localhost:8080")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"

# Twilio para WhatsApp y SMS
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP = os.getenv("TWILIO_WHATSAPP_NUMBER") # whatsapp:+14155238886
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID else None

# === DB POSTGRESQL ===
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reservas (
        id SERIAL PRIMARY KEY,
        nombre VARCHAR(100) NOT NULL,
        personas INTEGER NOT NULL,
        fecha DATE NOT NULL,
        hora TIME NOT NULL,
        telefono VARCHAR(20),
        estado VARCHAR(20) DEFAULT 'confirmada',
        creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db()

# === LÓGICA DE MESAS ===
TOTAL_MESAS = 10
DURACION_MESA_HORAS = 2.5 # 2 horas y media promedio por mesa

def ver_mesas_disponibles(fecha: str, hora: str):
    """Chequea mesas libres considerando que cada reserva bloquea 2.5hs"""
    try:
        fecha_dt = datetime.strptime(fecha, "%d/%m/%Y").date()
        hora_dt = datetime.strptime(hora, "%H:%M").time()
        inicio_nueva = datetime.combine(fecha_dt, hora_dt)
        fin_nueva = inicio_nueva + timedelta(hours=DURACION_MESA_HORAS)
        
        conn = get_db()
        c = conn.cursor()
        
        # Buscamos reservas que se solapen en el rango de 2.5hs
        c.execute("""
            SELECT COUNT(*) as ocupadas FROM reservas 
            WHERE fecha = %s 
            AND estado = 'confirmada'
            AND (
                (hora >= %s AND hora < %s) OR 
                (hora + interval '%s hours' > %s AND hora <= %s)
            )
        """, (fecha_dt, hora_dt, fin_nueva.time(), DURACION_MESA_HORAS, hora_dt, hora_dt))
        
        ocupadas = c.fetchone()['ocupadas']
        conn.close()
        
        disponibles = TOTAL_MESAS - ocupadas
        return {
            "fecha": fecha, 
            "hora": hora, 
            "mesas_libres": max(0, disponibles),
            "mesas_ocupadas": ocupadas,
            "total_mesas": TOTAL_MESAS
        }
    except Exception as e:
        return {"error": str(e)}

def crear_reserva(nombre: str, personas: int, fecha: str, hora: str, telefono: str = None):
    try:
        fecha_dt = datetime.strptime(fecha, "%d/%m/%Y").date()
        hora_dt = datetime.strptime(hora, "%H:%M").time()
        
        # Validar horario restaurante: 12-15 y 20-00
        if not ((hora_dt >= datetime.strptime("12:00", "%H:%M").time() and hora_dt <= datetime.strptime("15:00", "%H:%M").time()) or
                (hora_dt >= datetime.strptime("20:00", "%H:%M").time() or hora_dt <= datetime.strptime("00:00", "%H:%M").time())):
            return {"error": "Horario fuera de servicio. Atendemos 12-15hs y 20-00hs"}
        
        disponibilidad = ver_mesas_disponibles(fecha, hora)
        if disponibilidad["mesas_libres"] == 0:
            return {"error": f"No hay mesas libres el {fecha} a las {hora}. Probá otro horario."}
        
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO reservas (nombre, personas, fecha, hora, telefono) VALUES (%s,%s,%s) RETURNING id", 
            (nombre, personas, fecha_dt, hora_dt, telefono)
        )
        reserva_id = c.fetchone()['id']
        conn.commit()
        conn.close()
        
        return {
            "status": "confirmada",
            "id": reserva_id,
            "detalle": f"Reserva #{reserva_id} para {nombre}, {personas} personas, el {fecha} a las {hora}hs"
        }
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

def cancelar_reserva(nombre: str, fecha: str):
    """Cancela reserva por nombre y fecha"""
    try:
        fecha_dt = datetime.strptime(fecha, "%d/%m/%Y").date()
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "UPDATE reservas SET estado='cancelada' WHERE nombre ILIKE %s AND fecha=%s AND estado='confirmada' RETURNING id", 
            (f"%{nombre}%", fecha_dt)
        )
        cancelada = c.fetchone()
        conn.commit()
        conn.close()
        
        if cancelada:
            return {"status": "cancelada", "detalle": f"Reserva #{cancelada['id']} de {nombre} cancelada"}
        return {"error": "No encontré reserva activa con ese nombre y fecha"}
    except Exception as e:
        return {"error": str(e)}

def enviar_menu():
    return {"url": f"{BASE_URL}/menu", "mensaje": "Acá tenés nuestro menú completo"}

# === TOOLS PARA OPENAI ===
tools = [
    {"type": "function", "function": {
        "name": "enviar_menu",
        "description": "Envía el link del menú PDF"
    }},
    {"type": "function", "function": {
        "name": "ver_mesas_disponibles",
        "description": "Consulta mesas libres en fecha y hora. Usar antes de crear_reserva.",
        "parameters": {"type": "object", "properties": {
            "fecha": {"type": "string", "description": "DD/MM/YYYY"},
            "hora": {"type": "string", "description": "HH:MM"}
        }, "required": ["fecha", "hora"]}
    }},
    {"type": "function", "function": {
        "name": "crear_reserva",
        "description": "Crea reserva. Pedir nombre, personas, fecha DD/MM/YYYY, hora HH:MM, telefono opcional.",
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string"},
            "personas": {"type": "integer"},
            "fecha": {"type": "string"},
            "hora": {"type": "string"},
            "telefono": {"type": "string", "description": "Para recordatorio por WhatsApp"}
        }, "required": ["nombre", "personas", "fecha", "hora"]}
    }},
    {"type": "function", "function": {
        "name": "cancelar_reserva",
        "description": "Cancela reserva existente. Pedir nombre y fecha.",
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string"},
            "fecha": {"type": "string", "description": "DD/MM/YYYY"}
        }, "required": ["nombre", "fecha"]}
    }}
]

class ChatInput(BaseModel):
    mensaje: str
    user_id: str = "anonimo"

conversaciones = {}

SYSTEM_PROMPT = f"""Sos recepcionista de 'Restaurante Demo' en Alta Gracia.
HOY ES {datetime.now().strftime('%d/%m/%Y')}. Año 2026.

REGLAS:
1. Tenemos solo {TOTAL_MESAS} mesas. Cada reserva ocupa {DURACION_MESA_HORAS}hs.
2. Horario: 12:00-15:00 y 20:00-00:00. No tomes reservas fuera.
3. Para reservar pedí: nombre, personas, fecha DD/MM/YYYY, hora HH:MM, teléfono para WhatsApp.
4. Antes de crear_reserva, SIEMPRE usá ver_mesas_disponibles.
5. Si piden menú, usá enviar_menu.
6. Si piden cancelar, usá cancelar_reserva con nombre y fecha.
7. Si no hay mesas: "No tenemos disponibilidad en ese horario. ¿Probamos 30 min antes o después?"
8. Sé breve, 2 líneas máximo. Año actual siempre 2026.

Menu: {BASE_URL}/menu
"""

# === ENDPOINTS ===
@app.post("/chat")
async def chat(data: ChatInput):
    try:
        historial = conversaciones.get(data.user_id, [{"role": "system", "content": SYSTEM_PROMPT}])
        historial.append({"role": "user", "content": data.mensaje})
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=historial,
            tools=tools,
            tool_choice="auto",
            max_tokens=200
        )
        
        msg = response.choices[0].message
        historial.append(msg)
        
        if msg.tool_calls:
            for tool_call in msg.tool_calls:
                func_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                
                if func_name == "enviar_menu": result = enviar_menu()
                elif func_name == "ver_mesas_disponibles": result = ver_mesas_disponibles(**args)
                elif func_name == "crear_reserva": result = crear_reserva(**args)
                elif func_name == "cancelar_reserva": result = cancelar_reserva(**args)
                
                historial.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(result)})
            
            response_2 = client.chat.completions.create(model="gpt-4o-mini", messages=historial, max_tokens=200)
            respuesta_final = response_2.choices[0].message.content
        else:
            respuesta_final = msg.content
        
        historial.append({"role": "assistant", "content": respuesta_final})
        conversaciones[data.user_id] = historial[-12:]
        return {"respuesta": respuesta_final, "user_id": data.user_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# === WHATSAPP TWILIO ===
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    """Endpoint que Twilio llama cuando llega un WhatsApp"""
    form = await request.form()
    incoming_msg = form.get('Body', '').strip()
    sender = form.get('From', '') # whatsapp:+549...
    
    # Reutilizamos la lógica del /chat
    user_id = sender.replace("whatsapp:", "")
    historial = conversaciones.get(user_id, [{"role": "system", "content": SYSTEM_PROMPT}])
    historial.append({"role": "user", "content": incoming_msg})
    
    response = client.chat.completions.create(model="gpt-4o-mini", messages=historial, tools=tools, tool_choice="auto")
    msg = response.choices[0].message
    historial.append(msg)
    
    if msg.tool_calls:
        for tool_call in msg.tool_calls:
            func_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
            if func_name == "enviar_menu": result = enviar_menu()
            elif func_name == "ver_mesas_disponibles": result = ver_mesas_disponibles(**args)
            elif func_name == "crear_reserva": result = crear_reserva(**args)
            elif func_name == "cancelar_reserva": result = cancelar_reserva(**args)
            historial.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(result)})
        
        response_2 = client.chat.completions.create(model="gpt-4o-mini", messages=historial)
        respuesta_final = response_2.choices[0].message.content
    else:
        respuesta_final = msg.content
    
    historial.append({"role": "assistant", "content": respuesta_final})
    conversaciones[user_id] = historial[-12:]
    
    # Responder a Twilio
    resp = MessagingResponse()
    resp.message(respuesta_final)
    return str(resp)

@app.get("/menu")
def get_menu():
    file_path = "data/menu.pdf"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="menu.pdf no encontrado")
    return FileResponse(path=file_path, media_type='application/pdf', filename="Menu.pdf")

@app.get("/reservas")
def ver_reservas():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM reservas WHERE estado='confirmada' ORDER BY fecha DESC, hora DESC")
    rows = c.fetchall()
    conn.close()
    return {"total": len(rows), "reservas": [dict(row) for row in rows]}

@app.get("/health")
def health():
    return {"status": "ok", "db": "postgres", "mesas": TOTAL_MESAS}

@app.get("/")
def root():
    return {"servicio": "API Restaurante Online", "menu": "/menu", "chat": "/static/chat.html"}