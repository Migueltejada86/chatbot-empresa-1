from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import os, sqlite3, json
from openai import OpenAI
from datetime import datetime

load_dotenv()
app = FastAPI(title="Chatbot Restaurante")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

from fastapi.staticfiles import StaticFiles

app.mount("/static", StaticFiles(directory="static"), name="static")


BASE_URL = "https://chatbot-empresa-1-production.up.railway.app"

# DB para reservas
def init_db():
    conn = sqlite3.connect('restaurante.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reservas 
                 (id INTEGER PRIMARY KEY, nombre TEXT, personas INTEGER, fecha TEXT, hora TEXT, creado TEXT)''')
    conn.commit()
    conn.close()
init_db()

# Funciones que el bot puede llamar
def enviar_menu():
    return {"url": f"{BASE_URL}/menu", "mensaje": "Acá tenés nuestro menú completo"}

def crear_reserva(nombre: str, personas: int, fecha: str, hora: str):
    conn = sqlite3.connect('restaurante.db')
    c = conn.cursor()
    c.execute("INSERT INTO reservas (nombre, personas, fecha, hora, creado) VALUES (?,?,?,?,?)", 
              (nombre, personas, fecha, hora, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {
        "status": "confirmada", 
        "detalle": f"Reserva para {nombre}, {personas} personas, el {fecha} a las {hora}hs"
    }

def ver_mesas_disponibles(fecha: str, hora: str):
    # Simple: asumimos 10 mesas. Contamos reservas
    conn = sqlite3.connect('restaurante.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM reservas WHERE fecha=? AND hora=?", (fecha, hora))
    ocupadas = c.fetchone()[0]
    conn.close()
    disponibles = 10 - ocupadas
    return {"fecha": fecha, "hora": hora, "mesas_libres": max(0, disponibles)}

tools = [
    {
        "type": "function",
        "function": {
            "name": "enviar_menu",
            "description": "Envía el link del menú en PDF cuando el usuario lo pide"
        }
    },
    {
        "type": "function", 
        "function": {
            "name": "ver_mesas_disponibles",
            "description": "Consulta si hay mesas libres en fecha y hora específica",
            "parameters": {
                "type": "object",
                "properties": {
                    "fecha": {"type": "string", "description": "DD/MM/YYYY"},
                    "hora": {"type": "string", "description": "Formato HH:MM, ej 21:00"}
                },
                "required": ["fecha", "hora"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "crear_reserva",
            "description": "Crea una reserva de mesa. Pedir nombre, personas, fecha y hora antes",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "personas": {"type": "integer"},
                    "fecha": {"type": "string"},
                    "hora": {"type": "string"}
                },
                "required": ["nombre", "personas", "fecha", "hora"]
            }
        }
    }
]

class ChatInput(BaseModel):
    mensaje: str
    user_id: str = "test"

conversaciones = {}

@app.post("/chat")
async def chat(data: ChatInput):
    system_prompt = f"Hoy es 13/06/2026. Sos el asistente de 'Restaurante Demo'. Si piden menú, usá enviar_menu. Para reservas preguntá: nombre, cantidad de personas, fecha DD/MM/YYYY, hora HH:MM. Sé amable y breve. El link del menú es {BASE_URL}/menu"
    
    historial = conversaciones.get(data.user_id, [{"role": "system", "content": system_prompt}])
    historial.append({"role": "user", "content": data.mensaje})
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=historial,
        tools=tools,
        tool_choice="auto"
    )
    
    msg = response.choices[0].message
    historial.append(msg)
    
    if msg.tool_calls:
        for tool_call in msg.tool_calls:
            func_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
            
            if func_name == "enviar_menu":
                result = enviar_menu()
            elif func_name == "ver_mesas_disponibles":
                result = ver_mesas_disponibles(**args)
            elif func_name == "crear_reserva":
                result = crear_reserva(**args)
            
            historial.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result)
            })
        
        response_2 = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=historial
        )
        respuesta_final = response_2.choices[0].message.content
    else:
        respuesta_final = msg.content
    
    historial.append({"role": "assistant", "content": respuesta_final})
    conversaciones[data.user_id] = historial[-12:] # memoria corta
    return {"respuesta": respuesta_final, "user_id": data.user_id}

@app.get("/menu")
def get_menu():
    file_path = "data/menu.pdf"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Menú no encontrado")
    return FileResponse(path=file_path, media_type='application/pdf', filename="Menu.pdf")

@app.get("/health")
def health():
    return {"status": "ok", "version": "3.11"}

@app.get("/")
def root():
    return {"mensaje": "API Restaurante Online", "menu": "/menu", "docs": "/docs"}

@app.get("/reservas")
def ver_reservas():
    conn = sqlite3.connect('restaurante.db')
    c = conn.cursor()
    c.execute("SELECT id, nombre, personas, fecha, hora, creado FROM reservas ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    
    reservas = []
    for row in rows:
        reservas.append({
            "id": row[0],
            "nombre": row[1], 
            "personas": row[2],
            "fecha": row[3],
            "hora": row[4],
            "creado": row[5]
        })
    return {"total": len(reservas), "reservas": reservas}