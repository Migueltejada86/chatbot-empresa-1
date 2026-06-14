from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import os, sqlite3, json
from openai import OpenAI
from datetime import datetime

load_dotenv()
app = FastAPI(title="Chatbot Restaurante Demo")

# CORS para que funcione desde cualquier frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir archivos estáticos: /static/chat.html y /static/reservas.html
app.mount("/static", StaticFiles(directory="static"), name="static")

# Cliente OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# URL base para links
BASE_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN", "http://localhost:8080")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"

# === BASE DE DATOS ===
def init_db():
    conn = sqlite3.connect('restaurante.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reservas 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  nombre TEXT NOT NULL, 
                  personas INTEGER NOT NULL, 
                  fecha TEXT NOT NULL, 
                  hora TEXT NOT NULL, 
                  creado TEXT NOT NULL)''')
    conn.commit()
    conn.close()
    print("DB inicializada")

init_db()

# === FUNCIONES PARA EL BOT ===
def enviar_menu():
    """Devuelve el link del menú en PDF"""
    return {
        "url": f"{BASE_URL}/menu", 
        "mensaje": "Acá tenés nuestro menú completo con todos los platos y precios"
    }

def ver_mesas_disponibles(fecha: str, hora: str):
    """Consulta cuántas mesas quedan libres. Asumimos 10 mesas totales."""
    conn = sqlite3.connect('restaurante.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM reservas WHERE fecha=? AND hora=?", (fecha, hora))
    ocupadas = c.fetchone()[0]
    conn.close()
    disponibles = 10 - ocupadas
    return {
        "fecha": fecha, 
        "hora": hora, 
        "mesas_libres": max(0, disponibles),
        "mesas_ocupadas": ocupadas
    }

def crear_reserva(nombre: str, personas: int, fecha: str, hora: str):
    """Crea una reserva nueva en la base de datos"""
    # Validación básica
    try:
        datetime.strptime(fecha, "%d/%m/%Y")
    except ValueError:
        return {"error": "Formato de fecha inválido. Usá DD/MM/YYYY"}
    
    disponibilidad = ver_mesas_disponibles(fecha, hora)
    if disponibilidad["mesas_libres"] == 0:
        return {"error": f"No hay mesas libres el {fecha} a las {hora}. Elegí otro horario."}
    
    conn = sqlite3.connect('restaurante.db')
    c = conn.cursor()
    c.execute(
        "INSERT INTO reservas (nombre, personas, fecha, hora, creado) VALUES (?,?,?,?,?)", 
        (nombre, personas, fecha, hora, datetime.now().isoformat())
    )
    reserva_id = c.lastrowid
    conn.commit()
    conn.close()
    
    return {
        "status": "confirmada",
        "id": reserva_id,
        "detalle": f"Reserva #{reserva_id} confirmada para {nombre}, {personas} personas, el {fecha} a las {hora}hs"
    }

# Definición de herramientas para OpenAI
tools = [
    {
        "type": "function",
        "function": {
            "name": "enviar_menu",
            "description": "Envía el link del menú en PDF cuando el usuario lo pide o quiere ver platos/precios"
        }
    },
    {
        "type": "function", 
        "function": {
            "name": "ver_mesas_disponibles",
            "description": "Consulta disponibilidad de mesas en una fecha y hora específica antes de reservar",
            "parameters": {
                "type": "object",
                "properties": {
                    "fecha": {"type": "string", "description": "Fecha en formato DD/MM/YYYY"},
                    "hora": {"type": "string", "description": "Hora en formato HH:MM, ej: 21:00"}
                },
                "required": ["fecha", "hora"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "crear_reserva",
            "description": "Crea una reserva de mesa. SIEMPRE pedir nombre, cantidad de personas, fecha DD/MM/YYYY y hora HH:MM antes de llamar",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Nombre del cliente"},
                    "personas": {"type": "integer", "description": "Cantidad de personas para la mesa"},
                    "fecha": {"type": "string", "description": "Fecha formato DD/MM/YYYY"},
                    "hora": {"type": "string", "description": "Hora formato HH:MM"}
                },
                "required": ["nombre", "personas", "fecha", "hora"]
            }
        }
    }
]

# === MODELOS ===
class ChatInput(BaseModel):
    mensaje: str
    user_id: str = "anonimo"

# Memoria simple por usuario
conversaciones = {}

# === SYSTEM PROMPT ===
SYSTEM_PROMPT = f"""Sos el asistente virtual de 'Restaurante Demo', ubicado en Alta Gracia. 
Hoy es {datetime.now().strftime('%d/%m/%Y')}.

TUS TAREAS:
1. Si piden el menú, carta, platos o precios: usá la función enviar_menu.
2. Si quieren reservar mesa: pedí estos 4 datos en orden: nombre, cantidad de personas, fecha DD/MM/YYYY, hora HH:MM. 
   Antes de confirmar, podés usar ver_mesas_disponibles para chequear.
3. Sé amable, breve y profesional. Máximo 3 líneas por respuesta.
4. El menú está en: {BASE_URL}/menu
5. Horario del restaurante: 12:00 a 15:00 y 20:00 a 00:00. No tomes reservas fuera de horario.

NUNCA inventes disponibilidad. Siempre usá las funciones.
Si falta un dato para reservar, preguntalo. No asumas nada.
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
        
        # Si el modelo quiere llamar funciones
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
                else:
                    result = {"error": "Función no reconocida"}
                
                historial.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False)
                })
            
            # Segunda llamada para que el bot redacte la respuesta final
            response_2 = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=historial,
                max_tokens=200
            )
            respuesta_final = response_2.choices[0].message.content
        else:
            respuesta_final = msg.content
        
        historial.append({"role": "assistant", "content": respuesta_final})
        conversaciones[data.user_id] = historial[-10:] # Guarda últimas 10 interacciones
        
        return {"respuesta": respuesta_final, "user_id": data.user_id}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/menu")
def get_menu():
    file_path = "data/menu.pdf"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="menu.pdf no encontrado en /data")
    return FileResponse(path=file_path, media_type='application/pdf', filename="Menu-Restaurante-Demo.pdf")

@app.get("/reservas")
def ver_reservas():
    """Endpoint para que el dueño vea todas las reservas en JSON"""
    conn = sqlite3.connect('restaurante.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM reservas ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return {"total": len(rows), "reservas": [dict(row) for row in rows]}

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/")
def root():
    return {
        "servicio": "API Restaurante Demo Online",
        "menu": f"{BASE_URL}/menu",
        "chat": f"{BASE_URL}/static/chat.html",
        "admin_reservas": f"{BASE_URL}/static/reservas.html",
        "docs": f"{BASE_URL}/docs"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)