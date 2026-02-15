import os
import time
from datetime import datetime
from typing import List, Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import mercadopago

load_dotenv()

app = FastAPI()

PORT = int(os.getenv("PORT", 3000))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:3000")

print(f"DEBUG: FRONTEND_URL loaded: {FRONTEND_URL}")

@app.get("/")
def read_root():
    return {"status": "Backend running successfully", "docs": "/docs"}

# ================== CORS ==================
raw_origins = os.getenv("ALLOWED_ORIGINS", "")
if raw_origins == "*" or not raw_origins:
    ALLOWED_ORIGINS = ["*"]
else:
    ALLOWED_ORIGINS = [o.strip() for o in raw_origins.replace("\n", ",").split(",") if o.strip()]

print(f"DEBUG: ALLOWED_ORIGINS loaded: {ALLOWED_ORIGINS}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================== Modelos de datos para n8n ==================
class ReservationPaymentRequest(BaseModel):
    name: str
    email: str
    start_time: str  # ISO format
    end_time: str    # ISO format
    amount: int
    service_name: str
    phone: Optional[str] = None # Nuevo campo para WhatsApp

# ================== MERCADOPAGO ==================
mp_sdk = mercadopago.SDK(
    os.getenv("MP_ACCESS_TOKEN", "TEST-...")
)

# ================== WEBPAY ==================
WEBPAY_CONFIG = {
    "commerce_code": os.getenv("WEBPAY_COMMERCE_CODE", "597055555532"),
    "api_key": os.getenv(
        "WEBPAY_API_KEY",
        "579B532A7440BB0C9079DED94D31EA1615BACEB56610332264630D42D0A36B1C"
    ),
    "base_url": (
        "https://webpay3g.transbank.cl"
        if os.getenv("WEBPAY_ENVIRONMENT") == "LIVE"
        else "https://webpay3gint.transbank.cl"
    )
}

# ================== DATA ==================
products = [
    {"id": 1, "name": "Suculenta", "price": 5000},
    {"id": 2, "name": "Cactus", "price": 7000},
]

transactions = {}

# ================== MODELS ==================
class CreatePaymentRequest(BaseModel):
    amount: int


class ConfirmPaymentRequest(BaseModel):
    token: str


class MPItem(BaseModel):
    name: str
    price: float
    quantity: Optional[int] = 1


class MPPreferenceRequest(BaseModel):
    items: List[MPItem]


# ================== ROUTES ==================
@app.get("/api/products")
def get_products():
    return products


# ========== WEBPAY ==========

@app.post("/api/create-payment")
def create_payment(data: CreatePaymentRequest):
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Monto inválido")

    buy_order = f"ORDER{int(time.time())}"
    session_id = f"SESS{int(time.time())}"
    return_url = f"{FRONTEND_URL}/payment-result"

    payload = {
        "buy_order": buy_order,
        "session_id": session_id,
        "amount": data.amount,
        "return_url": return_url,
    }

    url = f"{WEBPAY_CONFIG['base_url']}/rswebpaytransaction/api/webpay/v1.2/transactions"

    headers = {
        "Tbk-Api-Key-Id": WEBPAY_CONFIG["commerce_code"],
        "Tbk-Api-Key-Secret": WEBPAY_CONFIG["api_key"],
        "Content-Type": "application/json",
        "Date": datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"),
    }

    response = requests.post(url, json=payload, headers=headers)

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=response.text)

    resp_data = response.json()

    transactions[resp_data["token"]] = {
        "status": "pending",
        "amount": payload["amount"],
        "buy_order": buy_order,
        "created_at": datetime.utcnow(),
    }

    return {
        "success": True,
        "payment_url": resp_data["url"],
        "token": resp_data["token"],
    }



# ========== MERCADOPAGO ==========
@app.post("/api/mp/create-preference")
def create_mp_preference(data: MPPreferenceRequest):
    if not data.items:
        raise HTTPException(status_code=400, detail="Items requeridos")

    preference = {
        "items": [
            {
                "title": item.name[:255],
                "unit_price": item.price,
                "quantity": item.quantity or 1,
                "currency_id": "CLP",
                "description": item.name[:255],
                "category_id": "others",
            }
            for item in data.items
        ],
        "back_urls": {
            "success": f"{FRONTEND_URL}/mp-payment-success",
            "failure": f"{FRONTEND_URL}/mp-payment-failure",
            "pending": f"{FRONTEND_URL}/mp-payment-pending",
        },
        "auto_return": "approved",
        "notification_url": f"{BACKEND_URL}/api/mp/notifications",
        "external_reference": f"ORDER_MP_{int(time.time())}",
        "payment_methods": {"installments": 1},
    }

    result = mp_sdk.preference().create(preference)
    response = result["response"]

    return {
        "success": True,
        "preferenceId": response["id"],
        "init_point": response.get("init_point"),
        "sandbox_init_point": response.get("sandbox_init_point"),
    }


@app.get("/api/mp/payment/{payment_id}")
def get_mp_payment(payment_id: str):
    result = mp_sdk.payment().get(payment_id)
    return {
        "success": True,
        "payment": result["response"],
    }

# ========== RESERVACIONES N8N ==========

# URL del Webhook de n8n (Configurar en .env)
N8N_WEBHOOK_URL = os.getenv("N8N_CONFIRMATION_WEBHOOK", "https://tu-ngrok.ngrok-free.app/rest/webhooks/reserva-confirmada")

@app.post("/api/reserva/crear-pago")
def crear_pago_reserva(data: ReservationPaymentRequest):
    print("-----------------------------------------")
    print(f"RECIBIENDO PETICIÓN DE N8N PARA: {data.name}")
    print(f"DATOS: {data.dict()}")
    print("-----------------------------------------")

    buy_order = f"RES{int(time.time())}"
    session_id = f"SESS{int(time.time())}"
    return_url = f"{FRONTEND_URL}/payment-result"

    payload = {
        "buy_order": buy_order,
        "session_id": session_id,
        "amount": data.amount,
        "return_url": return_url,
    }

    url = f"{WEBPAY_CONFIG['base_url']}/rswebpaytransaction/api/webpay/v1.2/transactions"
    headers = {
        "Tbk-Api-Key-Id": WEBPAY_CONFIG["commerce_code"],
        "Tbk-Api-Key-Secret": WEBPAY_CONFIG["api_key"],
        "Content-Type": "application/json",
        "Date": datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"),
    }

    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code != 200:
        print(f"ERROR TRANSBANK: {response.text}")
        raise HTTPException(status_code=response.status_code, detail=f"Error con Transbank: {response.text}")

    resp_data = response.json()

    # Guardamos los datos de la reserva asociados al token
    transactions[resp_data["token"]] = {
        "status": "pending",
        "reserva_data": data.dict(),
        "buy_order": buy_order,
        "amount": data.amount,
        "created_at": datetime.utcnow()
    }

    # CONSTRUIMOS EL LINK FINAL AQUÍ PARA EL BOT
    # Agregamos el teléfono como parámetro de retorno para recuperarlo después si es necesario
    link_final = f"{resp_data['url']}?token_ws={resp_data['token']}"
    print(f"LINK GENERADO PARA {data.name} ({data.phone or 'Sin teléfono'}): {link_final}")

    return {
        "success": True,
        "payment_url": link_final,
        "token": resp_data["token"]
    }

@app.post("/api/confirm-payment")
def confirm_payment(data: ConfirmPaymentRequest):
    """
    Confirma el pago con Webpay y notifica a n8n si es una reserva.
    """
    token = data.token

    # 1. Si ya tenemos el token en nuestro diccionario local y el estado NO es pendiente,
    # significa que ya lo procesamos (posible doble clic o re-render).
    if token in transactions and transactions[token].get("status") != "pending":
        return {
            "success": transactions[token].get("status") == "AUTHORIZED",
            "status": transactions[token].get("status"),
            "details": transactions[token].get("details", {})
        }

    url = f"{WEBPAY_CONFIG['base_url']}/rswebpaytransaction/api/webpay/v1.2/transactions/{token}"
    headers = {
        "Tbk-Api-Key-Id": WEBPAY_CONFIG["commerce_code"],
        "Tbk-Api-Key-Secret": WEBPAY_CONFIG["api_key"],
        "Content-Type": "application/json",
        "Date": datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"),
    }

    response = requests.put(url, headers=headers)
    
    if response.status_code != 200:
        # Si ya fue confirmada o el token es inválido
        raise HTTPException(status_code=500, detail="Error al confirmar transacción")

    result = response.json()
    status = result.get("status")

    if token in transactions:
        transactions[token]["status"] = status
        transactions[token]["updated_at"] = datetime.utcnow()
        transactions[token]["details"] = result

        # Si el pago fue autorizado y es una reserva, avisamos a n8n
        if status == "AUTHORIZED":
            reserva = transactions[token].get("reserva_data")
            if reserva:
                try:
                    requests.post(N8N_WEBHOOK_URL, json={
                        "status": "paid",
                        "token": token,
                        "buy_order": transactions[token]["buy_order"],
                        "nombre": reserva["name"],
                        "email": reserva["email"],
                        "startTime": reserva["start_time"],
                        "endTime": reserva["end_time"],
                        "service": reserva["service_name"],
                        "amount": transactions[token]["amount"],
                        "from_number": reserva.get("phone", ""), # Devolvemos el teléfono para WA
                        "payment_details": result # Enviamos todo lo que Transbank nos dio
                    }, timeout=10)
                    print(f"Notificación enviada a n8n para reserva: {reserva['name']}")
                except Exception as e:
                    print(f"Error avisando a n8n: {e}")

    return {
        "success": status == "AUTHORIZED",
        "status": status,
        "details": result
    }

print("BACKEND RUNNING - WEBPAY ENV:", os.getenv("WEBPAY_ENVIRONMENT", "INTEGRATION"))