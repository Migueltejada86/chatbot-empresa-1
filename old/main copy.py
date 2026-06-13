from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import os
from openai import OpenAI

load_dotenv()
app = FastAPI(title="Chatbot Empresa Prueba")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

class ChatInput(BaseModel):
    mensaje: str
    user_id: str = "test"

@app.get("/health")
def health():
    return {"status": "ok", "version": "3.11"}

@app.post("/chat")
async def chat(data: ChatInput):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Sos un asistente de una empresa. Respondé breve y profesional."},
                {"role": "user", "content": data.mensaje}
            ],
            max_tokens=200
        )
        return {
            "respuesta": response.choices[0].message.content,
            "user_id": data.user_id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))