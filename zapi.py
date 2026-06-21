"""
zapi.py
=======
Cliente Z-API para envio de mensagens WhatsApp.
Documentação: https://developer.z-api.io
"""

import logging
import requests
from config import ZAPI_INSTANCE_ID, ZAPI_TOKEN, ZAPI_CLIENT_TOKEN

logger = logging.getLogger(__name__)

_BASE = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}"


class ZAPIClient:
    def _headers(self):
        h = {"Content-Type": "application/json"}
        if ZAPI_CLIENT_TOKEN:
            h["Client-Token"] = ZAPI_CLIENT_TOKEN
        return h

    def send_text(self, phone: str, message: str) -> bool:
        """Envia mensagem de texto para um número WhatsApp."""
        url     = f"{_BASE}/send-text"
        payload = {"phone": phone, "message": message}
        try:
            r = requests.post(url, json=payload, headers=self._headers(), timeout=15)
            r.raise_for_status()
            logger.info(f"[{phone}] Mensagem enviada ({len(message)} chars)")
            return True
        except Exception as e:
            logger.error(f"[{phone}] Erro ao enviar mensagem Z-API: {e}")
            return False

    def send_typing(self, phone: str, duration_ms: int = 2000):
        """Simula 'digitando...' antes de responder (mais humano)."""
        url = f"{_BASE}/send-option-chain"
        # Z-API: envia status de digitando
        try:
            requests.post(
                f"{_BASE}/send-message-status",
                json={"phone": phone, "status": "COMPOSING", "duration": duration_ms},
                headers=self._headers(),
                timeout=5,
            )
        except Exception:
            pass  # não é crítico
