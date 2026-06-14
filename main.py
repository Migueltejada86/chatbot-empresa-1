from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import os, json, psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from datetime import datetime, timedelta

load_dotenv()
app = FastAPI(title="Chatbot Restaurante Demo")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN", "http://localhost:8080")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"

# === CONFIGURACIÓN RESTAURANTE ===
TOTAL_MESAS = 10
DURACION_MESA_HORAS = 2.5
HORARIOS = [("12:00", "15:00"), ("20:00", "00:00")] # almuerzo y cena

# === POSTGRESQL ===
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
    print("PostgreSQL: tabla reservas lista")

init_db()

# === LÓGICA DE MESAS CON SOLAPAMIENTO ===
def esta_en_horario(hora_str: str) -> bool:
    h = datetime.strptime(hora_str, "%H:%M").time()
    return (h >= datetime.strptime("12:00", "%H:%M").time() and h <= datetime.strptime("15:00", "%H:%M").time()) or \
           (h >= datetime.strptime("20:00", "%H:%M").time() or h <= datetime.strptime("00:00", "%H:%M").time())

def ver_mesas_disponibles(fecha: str, hora: str):
    try:
        fecha_dt = datetime.strptime(fecha, "%d/%m/%Y").date()
        hora_dt = datetime.strptime(hora, "%H:%M").time()
        inicio = datetime.combine(fecha_dt, hora_dt)
        fin = inicio + timedelta(hours=DURACION_MESA_HORAS)
        
        conn = get_db()
        c = conn.cursor()
        
        # Busca reservas que se pisen: si la nueva empieza antes que termine otra, o si otra empieza antes que termine la nueva
        c.execute("""
            SELECT COUNT(*) as ocupadas FROM reservas 
            WHERE fecha = %s AND estado = 'confirmada'
            AND (
                (hora + interval '%s hours') > %s AND hora < %s
            )
        """, (fecha_dt, DURACION_MESA_HORAS, hora_dt, fin.time()))
        
        ocupadas = c.fetchone()['ocupadas']
        conn.close()
        
        return {
            "fecha": fecha, 
            "hora": hora, 
            "mesas_libres": max(0, TOTAL_MESAS - ocupadas),
            "mesas_ocupadas": ocupadas
        }
    except Exception as e:
        return {"error": str(e)}

def crear_reserva(nombre: str, personas: int, fecha: str, hora: str, telefono: str = None):
    try:
        if not esta_en_horario(hora):
            return {"error": "Fuera de horario. Atendemos 12-15hs y 20-00hs"}
        
        disp = ver_mesas_disponibles(fecha, hora)
        if disp.get("error"): return disp
        if disp["mesas_libres"] == 0:
            return {"error": f"No hay mesas libres el {fecha} a las {hora}. Duración promedio: {DURACION_MESA_HORAS}hs por mesa."}
        
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO reservas (nombre, personas, fecha, hora, telefono) VALUES (%s,%s,%s) RETURNING id", 
            (nombre, personas, datetime.strptime(fecha, "%d/%m/%Y").date(), 
             datetime.strptime(hora, "%H:%M").time(), telefono)
        )
        reserva_id = c.fetchone()['id']
        conn.commit()
        conn.close()
        return {"status": "confirmada", "id": reserva_id, "detalle": f"Reserva #{reserva_id} para {nombre}, {personas} personas, {fecha} {hora}hs"}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

def cancelar_reserva(nombre: str, fecha: str):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "UPDATE reservas SET estado='cancelada' WHERE nombre ILIKE %s AND fecha=%s AND estado='confirmada' RETURNING id", 
            (f"%{nombre}%", datetime.strptime(fecha, "%d/%m/%Y").date())
        )
        row = c.fetchone()
        conn.commit()
        conn.close()
        if row:
            return {"status": "cancelada", "detalle": f"Reserva #{row['id']} de {nombre} cancelada"}
        return {"error": "No encontré reserva activa con ese nombre y fecha"}
    except Exception as e:
        return {"error": str(e)}

def enviar_menu():
    return {"url": f"{BASE_URL}/menu", "mensaje": "Acá tenés nuestro menú"}

# === OPENAI TOOLS ===
tools = [
    {"type": "function", "function": {"name": "enviar_menu", "description": "Envía el link del menú PDF"}},
    {"type": "function", "function": {
        "name": "ver_mesas_disponibles",
        "description": "Chequea mesas libres antes de reservar",
        "parameters": {"type": "object", "properties": {
            "fecha": {"type": "string"}, "hora": {"type": "string"}
        }, "required": ["fecha", "hora"]}
    }},
    {"type": "function", "function": {
        "name": "crear_reserva",
        "description": "Crea reserva. Pedir nombre, personas, fecha DD/MM/YYYY, hora HH:MM, telefono opcional",
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string"}, "personas": {"type": "integer"},
            "fecha": {"type": "string"}, "hora": {"type": "string"}, "telefono": {"type": "string"}
        }, "required": ["nombre", "personas", "fecha", "hora"]}
    }},
    {"type": "function", "function": {
        "name": "cancelar_reserva",
        "description": "Cancela reserva. Pedir nombre y fecha DD/MM/YYYY",
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string"}, "fecha": {"type": "string"}
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
1. Tenemos {TOTAL_MESAS} mesas. Cada reserva dura {DURACION_MESA_HORAS}hs promedio.
2. Horario: 12:00-15:00 y 20:00-00:00. Fuera de eso no hay servicio.
3. Para reservar: nombre, personas, fecha DD/MM/YYYY, hora HH:MM, teléfono para confirmación.
4. ANTES de crear_reserva SIEMPRE usá ver_mesas_disponibles.
5. Si no hay lugar: "No hay disponibilidad. ¿Probamos 30 min antes o después?"
6. Para cancelar pedí nombre y fecha.
7. Si piden menú usá enviar_menu.
8. Sé breve, 2 líneas. Año actual 2026.
"""

# === ENDPOINTS ===
@app.post("/chat")
async def chat(data: ChatInput):
    historial = conversaciones.get(data.user_id, [{"role": "system", "content": SYSTEM_PROMPT}])
    historial.append({"role": "user", "content": data.mensaje})
    
    response = client.chat.completions.create(model="gpt-4o-mini", messages=historial, tools=tools, tool_choice="auto", max_tokens=200)
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
            historial.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(result, ensure_ascii=False)})
        
        response_2 = client.chat.completions.create(model="gpt-4o-mini", messages=historial, max_tokens=200)
        respuesta_final = response_2.choices[0].message.content
    else:
        respuesta_final = msg.content
    
    historial.append({"role": "assistant", "content": respuesta_final})
    conversaciones[data.user_id] = historial[-12:]
    return {"respuesta": respuesta_final}

@app.get("/menu")
def get_menu():
    if not os.path.exists("data/menu.pdf"):
        raise HTTPException(status_code=404, detail="menu.pdf no encontrado")
    return FileResponse("data/menu.pdf", media_type='application/pdf', filename="Menu.pdf")

@app.get("/reservas")
def ver_reservas():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, nombre, personas, to_char(fecha, 'DD/MM/YYYY') as fecha, to_char(hora, 'HH24:MI') as hora, estado, telefono, creado FROM reservas ORDER BY fecha DESC, hora DESC")
    rows = c.fetchall()
    conn.close()
    return {"total": len(rows), "reservas": rows}

@app.get("/health")
def health():
    return {"status": "ok", "db": "postgres", "mesas": TOTAL_MESAS, "duracion_mesa": f"{DURACION_MESA_HORAS}hs"}

@app.get("/")
def root():
    return {"servicio": "Restaurante Demo API", "docs": "/docs", "chat": "/static/chat.html"}