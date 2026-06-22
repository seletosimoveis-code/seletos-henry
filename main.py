"""
main.py
=======
Servidor FastAPI da Seletos Imoveis.
"""

import re
import json as json_lib
import logging
import asyncio
from urllib.parse import parse_qs
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

    if body.get("fromMe"):
        # Mensagem enviada pelo atendente humano — registra no histórico sem responder.
        # Henry/Gabriel aprendem o que foi dito para manter contexto da conversa.
        asyncio.create_task(record_outgoing_message(phone, text))
        return JSONResponse({"status": "recorded", "reason": "fromMe — adicionado ao histórico"})

    asyncio.create_task(process_message(phone, text, name))
    return JSONResponse({"status": "queued"})


async def process_message(phone: str, text: str, name: str):
    logger.info(f"[{phone}] Mensagem: {text[:80]}")
    try:
        if gabriel.is_human_mode(phone) or henry.is_human_mode(phone):
            logger.info(f"[{phone}] Modo humano ativo — ignorando")
            return

        lead_ctx = await asyncio.to_thread(kommo.get_lead_context, phone)
        await asyncio.to_thread(zapi.send_typing, phone, 1500)
        await asyncio.sleep(1.5)

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

    except Exception as e:
        logger.error(f"[{phone}] Erro: {e}", exc_info=True)
        try:
            await asyncio.to_thread(
                zapi.send_text, phone,
                "Desculpe, tive uma instabilidade. Um de nossos atendentes vai retornar em breve! 🙏"
            )
        except Exception:
            pass


async def record_outgoing_message(phone: str, text: str):
    """
    Registra mensagem enviada pelo atendente humano no histórico do bot ativo.
    Não gera resposta — apenas mantém o contexto para o próximo turno do cliente.
    """
    try:
        if gabriel.is_active(phone) and not gabriel.is_human_mode(phone):
            gabriel.record_outgoing(phone, text)
        elif not henry.is_human_mode(phone):
            henry.record_outgoing(phone, text)
        else:
            logger.info(f"[{phone}] fromMe em modo humano total — ignorado")
    except Exception as e:
        logger.error(f"[{phone}] Erro ao registrar fromMe: {e}")


# =============================================================================
# WEBHOOK KOMMO — ativa Gabriel proativamente
# =============================================================================

def _parse_kommo_form(raw: bytes) -> dict:
    """
    Kommo envia webhooks como application/x-www-form-urlencoded com
    notacao PHP: leads[status][0][id]=123&leads[status][0][pipeline_id]=456
    """
    params = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    result: dict = {}
    for key, vals in params.items():
        value = vals[0] if vals else ""
        parts = [key.split("[")[0]] + re.findall(r"\[([^\]]*)\]", key)
        curr = result
        for i, part in enumerate(parts[:-1]):
            next_key = parts[i + 1]
            if next_key.isdigit():
                curr.setdefault(part, [])
                idx = int(next_key)
                while len(curr[part]) <= idx:
                    curr[part].append({})
                curr = curr[part][idx]
            else:
                if isinstance(curr, dict):
                    curr.setdefault(part, {})
                    curr = curr[part]
        last = parts[-1]
        if isinstance(curr, dict) and not last.isdigit():
            curr[last] = value
    return result


@app.post("/webhook/kommo")
async def webhook_kommo(request: Request):
    """
    Recebe eventos do Kommo.
    Retorna 200 imediatamente para evitar retries; processa em background.
    """
    raw = await request.body()
    ct  = request.headers.get("content-type", "")
    logger.info(f"Kommo webhook CT={ct!r} raw={raw[:300]}")
    asyncio.create_task(_process_kommo_event(raw, ct))
    return JSONResponse({"status": "ok"})


async def _process_kommo_event(raw: bytes, content_type: str):
    body: dict = {}
    try:
        body = json_lib.loads(raw)
    except Exception:
        pass
    if not body:
        try:
            body = _parse_kommo_form(raw)
        except Exception as e:
            logger.error(f"Kommo parse error: {e} raw={raw[:200]}")
            return

    logger.info(f"Kommo parsed: {str(body)[:400]}")

    leads_events = (body.get("leads") or {}).get("status", [])
    if not leads_events:
        logger.info("Kommo: sem eventos de status")
        return

    for event in leads_events:
        try:
            lead_id     = int(event.get("id", 0))
            pipeline_id = int(event.get("pipeline_id", 0))
        except (TypeError, ValueError):
            continue
        if not lead_id or not pipeline_id:
            continue
        funil = PIPE_TO_FUNIL.get(pipeline_id)
        if not funil:
            logger.info(f"Pipeline {pipeline_id} nao e funil Gabriel")
            continue
        asyncio.create_task(activate_gabriel_for_lead(lead_id, pipeline_id, funil))


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
            logger.info(f"[{phone}] Bot ja ativo — nao reativa Gabriel")
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


@app.get("/admin/pipelines")
async def debug_pipelines():
    """Debug: mostra os pipelines encontrados na conta Kommo."""
    from kommo import (
        _todos_os_pipelines, get_pipe_captacao, get_pipe_corretores,
        get_pipe_lancamentos, get_pipe_investidor, PIPE_ALUGUEL, PIPE_AVULSO,
        _pipe_id_cache,
    )
    # força re-busca limpando cache de lista (não o de IDs individuais)
    _pipe_id_cache.pop("all", None)
    todos = await asyncio.to_thread(_todos_os_pipelines)
    return {
        "todos_pipelines"     : [{"id": p["id"], "name": p.get("name")} for p in todos],
        "pipe_to_funil"       : PIPE_TO_FUNIL,
        "PIPE_ALUGUEL"        : PIPE_ALUGUEL,
        "PIPE_AVULSO"         : PIPE_AVULSO,
        "get_pipe_captacao"   : await asyncio.to_thread(get_pipe_captacao),
        "get_pipe_lancamentos": await asyncio.to_thread(get_pipe_lancamentos),
        "get_pipe_investidor" : await asyncio.to_thread(get_pipe_investidor),
        "get_pipe_corretores" : await asyncio.to_thread(get_pipe_corretores),
    }
