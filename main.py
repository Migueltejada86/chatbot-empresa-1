import os
import json
from datetime import datetime
from typing import Optional
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAI
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

# ENV VARS
DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL no está configurada")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY no está configurada")

# CLIENTS
client = OpenAI(api_key=OPENAI_API_KEY)
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

app = FastAPI(title="Chatbot El Descansito")

# SYSTEM PROMPT
SYSTEM_PROMPT = """Sos el asistente de WhatsApp de 'El Descansito', una posada en Villa Serrana, La Calera, Córdoba, Argentina.

SERVICIOS:
- Habitaciones: simple $40.000, doble $55.000, triple $70.000 por noche
- Pileta y quincho
- Desayuno incluido
- Check-in 14:00, check-out 11:00

RESPONDÉ: Corto, amable, en español rioplatense. Si piden reservar, pedí: nombre, DNI, fecha entrada, fecha salida, cantidad personas. Confirmá disponibilidad antes de cerrar. Si no sabés algo, decí que vas a consultar."""

def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS consultas (
            id SERIAL PRIMARY KEY,
            telefono VARCHAR(50),
            nombre VARCHAR(100),
            mensaje TEXT,
            respuesta TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS reservas (
            id SERIAL PRIMARY KEY,
            nombre VARCHAR(100),
            dni VARCHAR(20),
            telefono VARCHAR(50),
            fecha_entrada DATE,
            fecha_salida DATE,
            personas INT,
            habitacion VARCHAR(50),
            estado VARCHAR(20) DEFAULT 'pendiente',
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print("DB inicializada")

def guardar_consulta(telefono: str, nombre: str, mensaje: str, respuesta: str):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO consultas (telefono, nombre, mensaje, respuesta) VALUES (%s, %s, %s, %s)",
        (telefono, nombre, mensaje, respuesta)
    )
    conn.commit()
    conn.close()

def obtener_historial(telefono: str, limite: int = 5):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT mensaje, respuesta FROM consultas WHERE telefono = %s ORDER BY fecha DESC LIMIT %s",
        (telefono, limite)
    )
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))

def generar_respuesta(telefono: str, mensaje_usuario: str) -> str:
    historial = obtener_historial(telefono)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for h in historial:
        messages.append({"role": "user", "content": h["mensaje"]})
        messages.append({"role": "assistant", "content": h["respuesta"]})

    messages.append({"role": "user", "content": mensaje_usuario})

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=300
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error OpenAI: {e}")
        return "Disculpá, tuve un problema técnico. ¿Podés repetir?"

@app.on_event("startup")
async def startup_event():
    init_db()
    print(f"Twilio configurado: {twilio_client is not None}")

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
    ProfileName: Optional[str] = Form(None)
):
    telefono = From.replace("whatsapp:", "")
    nombre = ProfileName or "Cliente"
    mensaje = Body.strip()

    print(f"[{telefono}] {nombre}: {mensaje}")

    respuesta = generar_respuesta(telefono, mensaje)

    guardar_consulta(telefono, nombre, mensaje, respuesta)

    if twilio_client and TWILIO_PHONE_NUMBER:
        try:
            twilio_client.messages.create(
                from_=f"whatsapp:{TWILIO_PHONE_NUMBER}",
                to=From,
                body=respuesta
            )
        except Exception as e:
            print(f"Error enviando Twilio: {e}")

    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def root():
    return {"app": "Chatbot El Descansito", "status": "running"}