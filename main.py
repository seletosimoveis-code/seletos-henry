"""
main.py
=======
Servidor FastAPI do bot da Seletos Imóveis.
Recebe webhooks do Z-API, processa com Claude e responde via WhatsApp.

Fluxo:
  Z-API → POST /webhook/zapi → process_message() → Claude → Z-API (envia resposta)
                                                          ↓
                                                    Kommo (atualiza lead)
"""

import logging
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import ZAPI_INSTANCE_ID
from agent import AgentManager
from zapi import ZAPIClient
from kommo import KommoClient

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── App ──────────────────────────────────────────────────────────────────────
app    = FastAPI(title="Seletos Bot", version="1.0.0")
agent  = AgentManager()
zapi   = ZAPIClient()
kommo  = KommoClient()


# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "bot": "Henry — Seletos Imóveis"}


# ─── Webhook Z-API ────────────────────────────────────────────────────────────
@app.post("/webhook/zapi")
async def webhook_zapi(request: Request):
    """
    Endpoint que recebe todas as notificações do Z-API.
    Responde imediatamente (200) e processa em background.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "reason": "invalid json"}, status_code=400)

    # ── Filtros de mensagens ignoradas ────────────────────────────────────────
    if body.get("fromMe"):
        return JSONResponse({"status": "ignored", "reason": "fromMe"})
    if body.get("isGroup"):
        return JSONResponse({"status": "ignored", "reason": "group"})
    if body.get("isNewsletter"):
        return JSONResponse({"status": "ignored", "reason": "newsletter"})
    if body.get("type") != "ReceivedCallback":
        return JSONResponse({"status": "ignored", "reason": "not a message"})

    # ── Extrai dados ──────────────────────────────────────────────────────────
    phone = body.get("phone", "").strip()
    text  = (body.get("text") or {}).get("message", "").strip()
    name  = body.get("senderName", "").strip()

    if not phone or not text:
        return JSONResponse({"status": "ignored", "reason": "empty phone or text"})

    # ── Processa em background ────────────────────────────────────────────────
    asyncio.create_task(process_message(phone, text, name))
    return JSONResponse({"status": "queued"})


# ─── Processamento da mensagem ────────────────────────────────────────────────
async def process_message(phone: str, text: str, name: str):
    """Orquestra: contexto → Claude → resposta → handoff se necessário."""
    logger.info(f"[{phone}] Mensagem recebida: {text[:80]}")

    try:
        # Verifica se está em modo humano (bot silencioso)
        if agent.is_human_mode(phone):
            logger.info(f"[{phone}] Modo humano ativo — ignorando")
            return

        # Busca contexto do lead no Kommo (assíncrono em thread)
        lead_ctx = await asyncio.to_thread(kommo.get_lead_context, phone)

        # Simula "digitando..." por 1.5s
        await asyncio.to_thread(zapi.send_typing, phone, 1500)
        await asyncio.sleep(1.5)

        # Gera resposta com Claude
        response, handoff_reason = await asyncio.to_thread(
            agent.chat, phone, text, name, lead_ctx
        )

        # Envia resposta ao cliente
        await asyncio.to_thread(zapi.send_text, phone, response)
        logger.info(f"[{phone}] Resposta enviada ({len(response)} chars)")

        # Handoff se necessário
        if handoff_reason:
            logger.info(f"[{phone}] Handoff detectado: {handoff_reason}")
            history = agent.get_history(phone)
            await asyncio.to_thread(
                kommo.update_lead_after_bot, phone, history, handoff_reason
            )
            agent.set_human_mode(phone)

    except Exception as e:
        logger.error(f"[{phone}] Erro no processamento: {e}", exc_info=True)
        # Mensagem de fallback para o cliente
        try:
            await asyncio.to_thread(
                zapi.send_text, phone,
                "Desculpe, tive uma instabilidade. Um de nossos atendentes vai retornar em breve! 🙏"
            )
        except Exception:
            pass


# ─── Endpoint de controle (uso interno) ───────────────────────────────────────
@app.post("/admin/reset/{phone}")
async def reset_conversation(phone: str):
    """
    Reinicia a conversa de um número (remove modo humano e limpa histórico).
    Útil quando o corretor quer devolver o lead para o bot.
    """
    agent.reset_conversation(phone)
    return {"status": "ok", "message": f"Conversa de {phone} reiniciada"}


@app.get("/admin/status/{phone}")
async def get_status(phone: str):
    """Retorna estado atual de um número."""
    return {
        "phone"      : phone,
        "human_mode" : agent.is_human_mode(phone),
        "history_len": len(agent.get_history(phone)),
    }
