from fastapi import FastAPI, HTTPException, Request, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import os, json, psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from datetime import datetime, timedelta
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import uuid

load_dotenv()
app = FastAPI(title="El Descansito - Bot Restaurante")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN", "http://localhost:8080")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

# === CONFIG ===
TOTAL_MESAS = 10
DURACION_MESA_HORAS = 2.5
COSTO_DELIVERY = 3000
HORARIO_COCINA = {"inicio": "08:00", "fin": "23:00"}

# === MENU ===
MENU = {
    "ENTRADAS": [
        {"nombre": "Empanadas criollas", "precio": 3000},
        {"nombre": "Papas bravas", "precio": 9500},
        {"nombre": "Provoleta asada", "precio": 11000},
        {"nombre": "Langostino al Ajillo", "precio": 11000},
        {"nombre": "Tabla de fiambres", "precio": 12000}
    ],
    "MENU PRINCIPAL": [
        {"nombre": "Bifes de cuadril al verdeo con verduras asadas", "precio": 18000},
        {"nombre": "Bife de chorizo con rúcula y parmesano", "precio": 24000},
        {"nombre": "Bondiola braseada", "precio": 17000},
        {"nombre": "Temera braseada con puré", "precio": 17000},
        {"nombre": "Salmón rosado con vegetales al wok", "precio": 25000},
        {"nombre": "Matambre de cerdo", "precio": 17000},
        {"nombre": "Filet de trucha con ensalada", "precio": 23000},
        {"nombre": "Entrecot a la pimienta", "precio": 19500},
        {"nombre": "Ñoquis de papa con crema y boloñesa", "precio": 13500},
        {"nombre": "Sorrentinos de calabaza con salsa de mostaza", "precio": 13500},
        {"nombre": "Ñoquis de espinaca con salsa de hongos y crema", "precio": 14500}
    ],
    "PASTA / PLATOS CALIENTES": [
        {"nombre": "Canelones de humita con salsa blanca gratinados", "precio": 13500},
        {"nombre": "Milanesa napolitana con papas fritas", "precio": 15000},
        {"nombre": "Milanesa de peceto", "precio": 13500},
        {"nombre": "Lomo Kuate", "precio": 15000},
        {"nombre": "Pollo al limón con papas noisette", "precio": 12500},
        {"nombre": "Pacu a la parrilla", "precio": 18000},
        {"nombre": "Parrillada Completa", "precio": 24000},
        {"nombre": "Parrillada para 2 personas", "precio": 40000},
        {"nombre": "Menú Infantil", "precio": 11000}
    ],
    "ENSALADAS": [
        {"nombre": "Completa", "precio": 8500},
        {"nombre": "Caesar", "precio": 10000},
        {"nombre": "Salmón & Langostino", "precio": 15000}
    ],
    "POSTRES": [
        {"nombre": "Flan casero con crema y dulce de leche", "precio": 5000},
        {"nombre": "Panqueque tibio con bocha de helado", "precio": 5000},
        {"nombre": "Ensalada de fruta de estación", "precio": 4500},
        {"nombre": "Queso y dulce", "precio": 4500},
        {"nombre": "Frutillas con crema", "precio": 6500},
        {"nombre": "Bochas de helado", "precio": 3000},
        {"nombre": "Tiramisú", "precio": 5500},
        {"nombre": "Café", "precio": 3000}
    ],
    "BEBIDAS": [
        {"nombre": "Agua sin gas/con gas", "precio": 3500},
        {"nombre": "Saborizada", "precio": 3500},
        {"nombre": "Bebida Grande", "precio": 9000},
        {"nombre": "Jarra Limonada", "precio": 9000}
    ]
}

MENU_DEL_DIA = {
    0: "Lunes: Pollo al curry con arroz basmati - $12,000",
    1: "Martes: Pastel de papa casero - $11,500", 
    2: "Miércoles: Guiso de lentejas - $10,000",
    3: "Jueves: Tarta de verdura con ensalada - $9,500",
    4: "Viernes: Paella para 1 - $16,000",
    5: "Sábado: Locro criollo - $13,000",
    6: "Domingo: Asado banderita con papas - $18,000"
}

def get_db():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL no configurada")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reservas (
        id SERIAL PRIMARY KEY, nombre VARCHAR(100) NOT NULL, personas INTEGER NOT NULL,
        fecha DATE NOT NULL, hora TIME NOT NULL, telefono VARCHAR(20),
        estado VARCHAR(20) DEFAULT 'confirmada', comentarios TEXT,
        creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pedidos (
        id SERIAL PRIMARY KEY, tipo VARCHAR(20) NOT NULL, -- delivery/takeaway
        nombre VARCHAR(100) NOT NULL, telefono VARCHAR(20) NOT NULL,
        direccion TEXT, items JSONB NOT NULL, total INTEGER NOT NULL,
        estado VARCHAR(20) DEFAULT 'pendiente', -- pendiente/en_preparacion/en_camino/entregado
        comentarios TEXT, pago_tipo VARCHAR(20), -- transferencia/efectivo
        comprobante_url TEXT, creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()
    print("PostgreSQL: tablas listas")

init_db()

# === FUNCIONES RESERVAS ===
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
        c.execute("""
            SELECT COUNT(*) as ocupadas FROM reservas 
            WHERE fecha = %s AND estado = 'confirmada'
            AND (hora + interval '%s hours') > %s AND hora < %s
        """, (fecha_dt, DURACION_MESA_HORAS, hora_dt, fin.time()))
        ocupadas = c.fetchone()['ocupadas']
        conn.close()
        return {"fecha": fecha, "hora": hora, "mesas_libres": max(0, TOTAL_MESAS - ocupadas), "mesas_ocupadas": ocupadas}
    except Exception as e:
        return {"error": str(e)}

def crear_reserva(nombre: str, personas: int, fecha: str, hora: str, telefono: str = None, comentarios: str = None):
    try:
        if not esta_en_horario(hora):
            return {"error": "Fuera de horario. Atendemos 12-15hs y 20-00hs"}
        disp = ver_mesas_disponibles(fecha, hora)
        if disp.get("error"): return disp
        if disp["mesas_libres"] == 0:
            return {"error": f"No hay mesas libres el {fecha} a las {hora}"}
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO reservas (nombre, personas, fecha, hora, telefono, comentarios) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id", 
            (nombre, personas, datetime.strptime(fecha, "%d/%m/%Y").date(), 
             datetime.strptime(hora, "%H:%M").time(), telefono, comentarios)
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
        return {"error": "No encontré reserva activa"}
    except Exception as e:
        return {"error": str(e)}

# === FUNCIONES PEDIDOS ===
def buscar_plato(nombre_plato: str):
    nombre_plato = nombre_plato.lower()
    for categoria, platos in MENU.items():
        for plato in platos:
            if nombre_plato in plato["nombre"].lower():
                return plato
    return None

def calcular_total(items: list, tipo: str):
    total = sum(item["precio"] * item["cantidad"] for item in items)
    if tipo == "delivery":
        total += COSTO_DELIVERY
    return total

def crear_pedido(tipo: str, nombre: str, telefono: str, items: list, direccion: str = None, comentarios: str = None, pago_tipo: str = "efectivo"):
    try:
        ahora = datetime.now().time()
        hora_inicio = datetime.strptime(HORARIO_COCINA["inicio"], "%H:%M").time()
        hora_fin = datetime.strptime(HORARIO_COCINA["fin"], "%H:%M").time()
        if not (hora_inicio <= ahora <= hora_fin):
            return {"error": f"Cocina cerrada. Horario: {HORARIO_COCINA['inicio']} a {HORARIO_COCINA['fin']}hs"}
        
        total = calcular_total(items, tipo)
        conn = get_db()
        c = conn.cursor()
        c.execute(
            """INSERT INTO pedidos (tipo, nombre, telefono, direccion, items, total, comentarios, pago_tipo) 
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (tipo, nombre, telefono, direccion, json.dumps(items), total, comentarios, pago_tipo)
        )
        pedido_id = c.fetchone()['id']
        conn.commit()
        conn.close()
        return {"status": "creado", "id": pedido_id, "total": total, "detalle": f"Pedido #{pedido_id} {tipo}. Total: ${total}"}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

def actualizar_estado_pedido(pedido_id: int, nuevo_estado: str):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE pedidos SET estado=%s WHERE id=%s RETURNING telefono", (nuevo_estado, pedido_id))
        row = c.fetchone()
        conn.commit()
        conn.close()
        if row and twilio_client:
            telefono = row['telefono']
            if nuevo_estado == "en_camino":
                twilio_client.messages.create(
                    body=f"Tu pedido #{pedido_id} de El Descansito está en camino 🛵",
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=telefono
                )
        return {"status": "ok", "detalle": f"Pedido #{pedido_id} actualizado a {nuevo_estado}"}
    except Exception as e:
        return {"error": str(e)}

def enviar_menu():
    return {"url": f"{BASE_URL}/menu", "mensaje": "Acá tenés nuestro menú completo"}

def obtener_menu_del_dia():
    hoy = datetime.now().weekday()
    return {"menu": MENU_DEL_DIA[hoy]}

# === OPENAI TOOLS ===
tools = [
    {"type": "function", "function": {"name": "enviar_menu", "description": "Envía el link del menú completo"}},
    {"type": "function", "function": {"name": "obtener_menu_del_dia", "description": "Dice el menú del día/plato especial"}},
    {"type": "function", "function": {
        "name": "ver_mesas_disponibles",
        "description": "Consulta mesas libres. Usar antes de crear_reserva",
        "parameters": {"type": "object", "properties": {
            "fecha": {"type": "string", "description": "DD/MM/YYYY"},
            "hora": {"type": "string", "description": "HH:MM"}
        }, "required": ["fecha", "hora"]}
    }},
    {"type": "function", "function": {
        "name": "crear_reserva",
        "description": "Crea reserva. Pedir nombre, personas, fecha, hora. Comentarios opcional para eventos/alergias",
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string"}, "personas": {"type": "integer"},
            "fecha": {"type": "string"}, "hora": {"type": "string"},
            "telefono": {"type": "string"}, "comentarios": {"type": "string"}
        }, "required": ["nombre", "personas", "fecha", "hora"]}
    }},
    {"type": "function", "function": {
        "name": "crear_pedido",
        "description": "Crea pedido delivery o takeaway. Para delivery OBLIGATORIO pedir dirección y repetirla para confirmar. Items es lista de {nombre, precio, cantidad}",
        "parameters": {"type": "object", "properties": {
            "tipo": {"type": "string", "enum": ["delivery", "takeaway"]},
            "nombre": {"type": "string"}, "telefono": {"type": "string"},
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "nombre": {"type": "string"}, "precio": {"type": "integer"}, "cantidad": {"type": "integer"}
            }}},
            "direccion": {"type": "string"}, "comentarios": {"type": "string"},
            "pago_tipo": {"type": "string", "enum": ["transferencia", "efectivo"]}
        }, "required": ["tipo", "nombre", "telefono", "items"]}
    }},
    {"type": "function", "function": {
        "name": "cancelar_reserva",
        "description": "Cancela reserva por nombre y fecha",
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string"}, "fecha": {"type": "string"}
        }, "required": ["nombre", "fecha"]}
    }}
]

class ChatInput(BaseModel):
    mensaje: str
    user_id: str = "anonimo"

conversaciones = {}

SYSTEM_PROMPT = f"""Sos el asistente de 'El Descansito' en Alta Gracia por WhatsApp.
HOY ES {datetime.now().strftime('%d/%m/%Y')}. Año 2026.

HORARIOS: Cocina 8:00-23:00 todos los días. Reservas 12-15hs y 20-00hs.

SERVICIOS:
1. RESERVAS: Tenemos {TOTAL_MESAS} mesas. Cada reserva dura {DURACION_MESA_HORAS}hs. Pedir: nombre, personas, fecha, hora. Si es cumpleaños/aniversario, preguntá si quieren pre-ordenar platos.
2. DELIVERY: Costo ${COSTO_DELIVERY}. Pedir: nombre, teléfono, dirección COMPLETA, platos con cantidad. REPETIR dirección para confirmar. Preguntar pago: transferencia o efectivo en domicilio. Si es transferencia y no mandan PDF, decir "Ya lo cargamos mientras llega a tu domicilio".
3. TAKE AWAY: Sin costo delivery. Pedir nombre, teléfono, platos. Pagan en local o transferencia.
4. MENÚ: Si piden menú usá enviar_menu. Si piden menú del día usá obtener_menu_del_dia.

COMENTARIOS: Siempre preguntá si tienen alergias o pedidos especiales: "sin sal", "celíaco", "sin nuez". Guardalo en comentarios.

IMPORTANTE: 
- Para delivery SIEMPRE confirmar dirección repitiéndola.
- Para reservas SIEMPRE usar ver_mesas_disponibles antes de crear_reserva.
- Respondé corto, 2 líneas. Es WhatsApp.
- Si preguntan por platos, buscá en el menú y respondé precio.

MENÚ DEL DÍA: {MENU_DEL_DIA[datetime.now().weekday()]}
"""

def procesar_mensaje(user_id: str, mensaje: str, telefono: str = None) -> str:
    try:
        historial = conversaciones.get(user_id, [{"role": "system", "content": SYSTEM_PROMPT}])
        
        if len(historial) > 1 and historial[-1].get("role") == "tool":
            historial = [{"role": "system", "content": SYSTEM_PROMPT}]
        
        if telefono:
            mensaje = f"{mensaje}\n[Contexto: número {telefono}]"
        
        historial.append({"role": "user", "content": mensaje})
        
        for i in range(5):
            response = client.chat.completions.create(
                model="gpt-4o-mini", messages=historial, tools=tools, tool_choice="auto", max_tokens=250
            )
            msg = response.choices[0].message
            historial.append(msg)
            
            if not msg.tool_calls:
                respuesta_final = msg.content
                break
            
            for tool_call in msg.tool_calls:
                func_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                
                if telefono and not args.get("telefono"):
                    args["telefono"] = telefono.replace("whatsapp:", "")
                
                if func_name == "enviar_menu": result = enviar_menu()
                elif func_name == "obtener_menu_del_dia": result = obtener_menu_del_dia()
                elif func_name == "ver_mesas_disponibles": result = ver_mesas_disponibles(**args)
                elif func_name == "crear_reserva": result = crear_reserva(**args)
                elif func_name == "crear_pedido": result = crear_pedido(**args)
                elif func_name == "cancelar_reserva": result = cancelar_reserva(**args)
                else: result = {"error": "funcion desconocida"}
                
                historial.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(result, ensure_ascii=False)})
        else:
            respuesta_final = "Disculpá, hubo un error."
        
        historial.append({"role": "assistant", "content": respuesta_final})
        conversaciones[user_id] = historial[-12:]
        return respuesta_final
    
    except Exception as e:
        print(f"[ERROR CHAT] {str(e)}")
        conversaciones[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        return "Hubo un error. Probá de nuevo."

# === ENDPOINTS ===
@app.post("/chat")
async def chat(data: ChatInput):
    respuesta = procesar_mensaje(data.user_id, data.mensaje)
    return {"respuesta": respuesta}

@app.post("/webhook")
async def whatsapp_webhook(
    From: str = Form(...), Body: str = Form(...),
    MediaUrl0: str = Form(None), MediaContentType0: str = Form(None)
):
    try:
        # Si mandan PDF de transferencia
        if MediaUrl0 and "pdf" in MediaContentType0:
            resp = MessagingResponse()
            resp.message("Recibimos tu comprobante. Ya lo cargamos, va en camino a tu domicilio 🛵")
            return Response(content=str(resp), media_type="application/xml")
        
        respuesta = procesar_mensaje(user_id=From, mensaje=Body, telefono=From)
        resp = MessagingResponse()
        resp.message(respuesta)
        return Response(content=str(resp), media_type="application/xml")
    except Exception as e:
        print(f"[ERROR WHATSAPP] {str(e)}")
        resp = MessagingResponse()
        resp.message("Error. Intentá de nuevo.")
        return Response(content=str(resp), media_type="application/xml")

@app.get("/menu")
def get_menu():
    if not os.path.exists("data/menu.pdf"):
        raise HTTPException(status_code=404)
    return FileResponse("data/menu.pdf", media_type='application/pdf')

@app.get("/reservas")
def ver_reservas():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, nombre, personas, to_char(fecha, 'DD/MM/YYYY') as fecha, to_char(hora, 'HH24:MI') as hora, estado, telefono, comentarios, creado FROM reservas ORDER BY fecha DESC, hora DESC")
    rows = c.fetchall()
    conn.close()
    return {"total": len(rows), "reservas": rows}

@app.get("/pedidos")
def ver_pedidos():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, tipo, nombre, telefono, direccion, items, total, estado, comentarios, pago_tipo, creado FROM pedidos ORDER BY creado DESC")
    rows = c.fetchall()
    conn.close()
    return {"total": len(rows), "pedidos": rows}

@app.post("/pedidos/{pedido_id}/estado")
def cambiar_estado_pedido(pedido_id: int, estado: str = Form(...)):
    return actualizar_estado_pedido(pedido_id, estado)

@app.get("/panel")
def panel_admin():
    conn = get_db()
    c = conn.cursor()
    hoy = datetime.now().date()
    c.execute("SELECT COUNT(*) as total FROM reservas WHERE fecha=%s AND estado='confirmada'", (hoy,))
    reservas_hoy = c.fetchone()['total']
    c.execute("SELECT COUNT(*), SUM(total) FROM pedidos WHERE tipo='delivery' AND DATE(creado)=%s", (hoy,))
    del_data = c.fetchone()
    c.execute("SELECT COUNT(*), SUM(total) FROM pedidos WHERE tipo='takeaway' AND DATE(creado)=%s", (hoy,))
    ta_data = c.fetchone()
    conn.close()
    html = f"""
    <html><head><title>Panel El Descansito</title><style>
    body{{font-family:Arial;background:#f5f5f5;padding:20px}}
   .card{{background:white;padding:20px;margin:10px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}
    h1{{color:#e67e22}}.stat{{font-size:32px;font-weight:bold;color:#27ae60}}
    </style></head><body>
    <h1>🍽️ El Descansito - Panel</h1>
    <div class="card"><h3>Reservas Hoy</h3><div class="stat">{reservas_hoy}</div></div>
    <div class="card"><h3>Delivery Hoy</h3><div class="stat">{del_data['count'] or 0}</div><p>Ventas: ${del_data['sum'] or 0}</p></div>
    <div class="card"><h3>Take Away Hoy</h3><div class="stat">{ta_data['count'] or 0}</div><p>Ventas: ${ta_data['sum'] or 0}</p></div>
    <div class="card"><a href="/reservas">Ver Reservas</a> | <a href="/pedidos">Ver Pedidos</a></div>
    </body></html>
    """
    return HTMLResponse(content=html)

@app.get("/health")
def health():
    return {"status": "ok", "db": "postgres", "whatsapp": "enabled" if twilio_client else "disabled"}

@app.get("/")
def root():
    return {"servicio": "El Descansito Bot", "panel": "/panel", "webhook": "/webhook"}