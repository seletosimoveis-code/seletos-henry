"""
main.py
=======
Servidor FastAPI da Seletos Imoveis.

Endpoints:
  GET  /health                  - healthcheck
  POST /webhook/zapi            - mensagens WhatsApp (Z-API)
  POST /webhook/kommo           - eventos de pipeline (Kommo) -> ativa Gabriel proativamente
  POST /admin/reset/{phone}     - reinicia conversa
  GET  /admin/status/{phone}    - estado atual de um numero

Fluxo WhatsApp:
  Z-API -> /webhook/zapi -> Henry (triagem) OU Gabriel (qualificacao) -> resposta

Fluxo proativo:
  Kommo move lead para funil -> /webhook/kommo -> Gabriel envia 1a mensagem
"""

import logging
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent import AgentManager
from gabriel.agent import GabrielManager, PIPE_TO_FUNIL
from zapi import ZAPIClient
from kommo import (
    KommoClient,
    PIPE_ALUGUEL, PIPE_AVULSO,
    get_pipe_captacao, get_pipe_lancamentos, get_pipe_investidor,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app     = FastAPI(title="Seletos Bot", version="2.0.0")
henry   = AgentManager()
gabriel = GabrielManager()
zapi    = ZAPIClient()
kommo   = KommoClient()


@app.on_event("startup")
async def startup():
    await asyncio.to_thread(_populate_pipe_map)


def _populate_pipe_map():
    PIPE_TO_FUNIL[PIPE_ALUGUEL] = "aluguel"
    PIPE_TO_FUNIL[PIPE_AVULSO]  = "avulso"
    captacao    = get_pipe_captacao()
    lancamentos = get_pipe_lancamentos()
    investidor  = get_pipe_investidor()
    if captacao:    PIPE_TO_FUNIL[captacao]    = "captacao"
    if lancamentos: PIPE_TO_FUNIL[lancamentos] = "lancamentos"
    if investidor:  PIPE_TO_FUNIL[investidor]  = "investidor"
    logger.info(f"Pipelines Gabriel mapeados: {PIPE_TO_FUNIL}")


@app.get("/health")
def health():
    return {"status": "ok", "bot": "Henry + Gabriel — Seletos Imoveis"}


# =============================================================================
# WEBHOOK Z-API
# =============================================================================
@app.post("/webhook/zapi")
async def webhook_zapi(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "reason": "invalid json"}, status_code=400)

    if body.get("fromMe"):
        return JSONResponse({"status": "ignored", "reason": "fromMe"})
    if body.get("isGroup"):
        return JSONResponse({"status": "ignored", "reason": "group"})
    if body.get("isNewsletter"):
        return JSONResponse({"status": "ignored", "reason": "newsletter"})
    if body.get("type") != "ReceivedCallback":
        return JSONResponse({"status": "ignored", "reason": "not a message"})

    phone = body.get("phone", "").strip()
    text  = (body.get("text") or {}).get("message", "").strip()
    name  = body.get("senderName", "").strip()

    if not phone or not text:
        return JSONResponse({"status": "ignored", "reason": "empty phone or text"})

    asyncio.create_task(process_message(phone, text, name))
    return JSONResponse({"status": "queued"})


async def process_message(phone: str, text: str, name: str):
    logger.info(f"[{phone}] Mensagem: {text[:80]}")
    try:
        # Modo humano final -> silencio
        if gabriel.is_human_mode(phone) or henry.is_human_mode(phone):
            logger.info(f"[{phone}] Modo humano ativo — ignorando")
            return

        lead_ctx = await asyncio.to_thread(kommo.get_lead_context, phone)
        await asyncio.to_thread(zapi.send_typing, phone, 1500)
        await asyncio.sleep(1.5)

        # Gabriel ativo (qualificacao em andamento)
        if gabriel.is_active(phone):
            response, handoff = await asyncio.to_thread(
                gabriel.chat, phone, text, name, lead_ctx
            )
            await asyncio.to_thread(zapi.send_text, phone, response)
            logger.info(f"[{phone}] Gabriel respondeu ({len(response)} chars)")
            if handoff:
                logger.info(f"[{phone}] Gabriel handoff: {handoff}")
                history = gabriel.get_history(phone)
                funil   = gabriel.get_funil(phone)
                await asyncio.to_thread(
                    kommo.update_lead_after_gabriel, phone, history, handoff, funil
                )
                gabriel.set_human_mode(phone)
            return

        # Henry (triagem inicial)
        response, handoff = await asyncio.to_thread(
            henry.chat, phone, text, name, lead_ctx
        )
        await asyncio.to_thread(zapi.send_text, phone, response)
        logger.info(f"[{phone}] Henry respondeu ({len(response)} chars)")
        if handoff:
            logger.info(f"[{phone}] Henry handoff: {handoff}")
            history = henry.get_history(phone)
            await asyncio.to_thread(
                kommo.update_lead_after_bot, phone, history, handoff
            )
            henry.set_human_mode(phone)
            # Gabriel ativado proativamente via webhook Kommo

    except Exception as e:
        logger.error(f"[{phone}] Erro: {e}", exc_info=True)
        try:
            await asyncio.to_thread(
                zapi.send_text, phone,
                "Desculpe, tive uma instabilidade. Um de nossos atendentes vai retornar em breve! 🙏"
            )
        except Exception:
            pass


# =============================================================================
# WEBHOOK KOMMO — ativa Gabriel proativamente
# =============================================================================
@app.post("/webhook/kommo")
async def webhook_kommo(request: Request):
    """
    Recebe eventos de mudanca de status do Kommo.
    Quando lead entra em funil Gabriel, ele manda a 1a mensagem.
    """
    try:
        body = await request.json()
    except Exception:
        try:
            form = await request.form()
            body = dict(form)
        except Exception:
            return JSONResponse({"status": "error"}, status_code=400)

    logger.info(f"Kommo webhook: {str(body)[:200]}")

    leads_events = (body.get("leads") or {}).get("status", [])
    if not leads_events:
        return JSONResponse({"status": "ignored", "reason": "no lead status events"})

    for event in leads_events:
        lead_id     = event.get("id")
        pipeline_id = event.get("pipeline_id")
        if not lead_id or not pipeline_id:
            continue
        funil = PIPE_TO_FUNIL.get(pipeline_id)
        if not funil:
            logger.info(f"Pipeline {pipeline_id} nao e funil Gabriel — ignorando")
            continue
        asyncio.create_task(activate_gabriel_for_lead(lead_id, pipeline_id, funil))

    return JSONResponse({"status": "ok"})


async def activate_gabriel_for_lead(lead_id: int, pipeline_id: int, funil: str):
    try:
        await asyncio.sleep(2)
        phone, name, lead_ctx = await asyncio.to_thread(
            kommo.get_lead_phone_and_context, lead_id
        )
        if not phone:
            logger.warning(f"Lead {lead_id} sem telefone — Gabriel nao ativado")
            return
        if gabriel.is_human_mode(phone) or gabriel.is_active(phone):
            logger.info(f"[{phone}] Ja tem bot ativo — Gabriel nao reativado")
            return

        henry.set_human_mode(phone)
        first_msg = await asyncio.to_thread(
            gabriel.activate, phone, funil, name, lead_ctx
        )
        await asyncio.to_thread(zapi.send_typing, phone, 2000)
        await asyncio.sleep(2)
        await asyncio.to_thread(zapi.send_text, phone, first_msg)
        logger.info(f"[{phone}] Gabriel ativado proativamente — funil: {funil}")

    except Exception as e:
        logger.error(f"Erro ao ativar Gabriel para lead {lead_id}: {e}", exc_info=True)


# =============================================================================
# ADMIN
# =============================================================================
@app.post("/admin/reset/{phone}")
async def reset_conversation(phone: str):
    henry.reset_conversation(phone)
    gabriel.reset(phone)
    return {"status": "ok", "message": f"Conversa de {phone} reiniciada"}


@app.get("/admin/status/{phone}")
async def get_status(phone: str):
    return {
        "phone"          : phone,
        "henry_mode"     : henry.is_human_mode(phone),
        "gabriel_active" : gabriel.is_active(phone),
        "gabriel_funil"  : gabriel.get_funil(phone),
        "gabriel_human"  : gabriel.is_human_mode(phone),
        "henry_history"  : len(henry.get_history(phone)),
        "gabriel_history": len(gabriel.get_history(phone)),
    }
