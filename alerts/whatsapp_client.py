import requests

import config


def _request(method, path, **kwargs):
    if not config.WHATSAPP_SERVICE_TOKEN:
        raise RuntimeError("WHATSAPP_SERVICE_TOKEN is not configured")
    response = requests.request(
        method,
        f"{config.WHATSAPP_SERVICE_URL}{path}",
        headers={"Authorization": f"Bearer {config.WHATSAPP_SERVICE_TOKEN}"},
        timeout=20,
        **kwargs,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "WhatsApp service request failed")
    return payload


def get_status():
    return _request("GET", "/status")


def request_pairing_code(phone=None):
    phone = phone or config.WHATSAPP_PHONE
    if not phone:
        raise ValueError("Set WHATSAPP_PHONE in .env in international format")
    return _request("POST", "/pair", json={"phone": phone})


def send_message(jid, text):
    if not jid:
        raise ValueError("WHATSAPP_RECIPIENT_JID is empty")
    return _request("POST", "/send", json={"jid": jid, "text": text})
