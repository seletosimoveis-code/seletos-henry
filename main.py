"""
main.py
=======
Servidor FastAPI da Seletos Imoveis.
"""

import re
import time
import json as json_lib
import logging
import asyncio
from urllib.parse import parse_qs
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent import AgentManager
from audio import transcribe_audio_url
from gabriel.agent import GabrielManager, PIPE_TO_FUNIL
from zapi import ZAPIClient
from kommo import (
    KommoClient,
    PIPE_ALUGUEL, PIPE_AVULSO,
    get_pipe_captacao, get_pipe_lancamentos, get_pipe_investidor,
)
from config import RATE_LIMIT_MAX_PER_MIN, HENRY_MAX_LEAD_AGE_HOURS
from crm_enricher import enrich_lead_crm

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

# Rate limiting: phone → lista de timestamps das últimas mensagens
_rate_timestamps: dict[str, list[float]] = {}

# Deduplicação: evita processar a mesma mensagem duas vezes em 30 segundos
# (Z-API às vezes envia webhooks duplicados; Kommo message[add] pode chegar junto com Z-API)
_msg_dedup: dict[str, float] = {}   # "phone:hash" → timestamp
_processing_phones: set[str] = set()  # phones com process_message em andamento

def _is_duplicate_message(phone: str, text: str) -> bool:
    """True se a mesma mensagem já foi processada nos últimos 30 segundos para este número."""
    key = f"{phone}:{hash(text) & 0xFFFFFF}"
    now = time.time()
    for k in list(_msg_dedup):
        if now - _msg_dedup[k] > 30:
            del _msg_dedup[k]
    if key in _msg_dedup:
        return True
    _msg_dedup[key] = now
    return False

def _is_rate_limited(phone: str) -> bool:
    """Retorna True se o número excedeu o limite de mensagens por minuto."""
    now = time.time()
    timestamps = _rate_timestamps.setdefault(phone, [])
    timestamps[:] = [t for t in timestamps if now - t < 60]
    if len(timestamps) >= RATE_LIMIT_MAX_PER_MIN:
        logger.warning(f"[{phone}] Rate limit atingido ({RATE_LIMIT_MAX_PER_MIN} msg/min) — ignorando")
        return True
    timestamps.append(now)
    return False


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

    # Detecta áudio (ptt = Push To Talk = gravação de voz; audio = arquivo de áudio)
    audio_data  = body.get("audio") or {}
    audio_url   = audio_data.get("audioUrl", "")
    audio_mime  = audio_data.get("mimeType", "audio/ogg")
    is_audio    = bool(audio_url) and not text

    if not phone or (not text and not is_audio):
        return JSONResponse({"status": "ignored", "reason": "empty phone or text/audio"})

    # Z-API envia esse texto quando o cliente usa a função "responder a mensagem" (quote/reply).
    # Não é uma mensagem real — ignorar silenciosamente para não confundir Henry.
    if "'messageContextInfo' is not yet supported" in (text or ""):
        logger.info(f"[{phone}] messageContextInfo ignorado (reply de Z-API)")
        return JSONResponse({"status": "ignored", "reason": "messageContextInfo"})

    if body.get("fromMe"):
        # Mensagem enviada pelo atendente humano — registra no histórico sem responder.
        asyncio.create_task(record_outgoing_message(phone, text or "[áudio]"))
        return JSONResponse({"status": "recorded", "reason": "fromMe — adicionado ao histórico"})

    if _is_rate_limited(phone):
        return JSONResponse({"status": "ignored", "reason": "rate limit"})

    if _is_duplicate_message(phone, text or audio_url):
        logger.warning(f"[{phone}] Mensagem duplicada detectada — ignorando")
        return JSONResponse({"status": "ignored", "reason": "duplicate"})

    asyncio.create_task(process_message(phone, text, name, audio_url=audio_url, audio_mime=audio_mime))
    return JSONResponse({"status": "queued"})


async def process_message(
    phone: str,
    text: str,
    name: str,
    audio_url: str = "",
    audio_mime: str = "audio/ogg",
):
    # ── Lock por telefone: evita processar duas mensagens do mesmo número em paralelo ──
    if phone in _processing_phones:
        logger.info(f"[{phone}] Já processando — aguardando 3s antes de descartar")
        await asyncio.sleep(3)
        if phone in _processing_phones:
            logger.warning(f"[{phone}] Ainda em processamento — descartando mensagem concorrente")
            return
    _processing_phones.add(phone)

    try:
        await _process_message_inner(phone, text, name, audio_url=audio_url, audio_mime=audio_mime)
    finally:
        _processing_phones.discard(phone)


async def _process_message_inner(
    phone: str,
    text: str,
    name: str,
    audio_url: str = "",
    audio_mime: str = "audio/ogg",
):
    # ── Transcrição de áudio (Whisper) ──────────────────────────────────────────
    if audio_url and not text:
        logger.info(f"[{phone}] Áudio recebido — transcrevendo com Whisper...")
        transcript = await asyncio.to_thread(transcribe_audio_url, audio_url, audio_mime)
        if not transcript:
            logger.warning(f"[{phone}] Transcrição falhou — ignorando mensagem de áudio")
            return
        text = transcript
        logger.info(f"[{phone}] Áudio → texto: '{text[:80]}'")

    logger.info(f"[{phone}] Mensagem: {text[:80]}")
    try:
        # Gabriel ativo: só bloqueia se Gabriel estiver em modo humano
        if gabriel.is_active(phone):
            if gabriel.is_human_mode(phone):
                logger.info(f"[{phone}] Gabriel em modo humano — ignorando")
                return
        # Henry ativo: só bloqueia se Henry estiver em modo humano (e Gabriel não estiver ativo)
        elif henry.is_human_mode(phone):
            logger.info(f"[{phone}] Henry em modo humano — ignorando")
            return

        lead_ctx = await asyncio.to_thread(kommo.get_lead_context, phone)

        # ── Lead retornando — Gabriel reativação automática ───────────────────
        # Condições para detectar retorno (todas devem ser verdadeiras):
        #   1. Gabriel não está ativo para este número
        #   2. Henry não está em modo humano
        #   3. Henry NÃO tem histórico ativo (se tivesse, ainda estaria conversando)
        #   4. O lead já está em funil de cliente — não está na Recepção
        #
        # Guard #3 evita falso-positivo com leads do Canal Pro que chegam pré-movidos
        # para Aluguel/Avulso mas ainda estão sendo atendidos pelo Henry.
        if (
            not gabriel.is_active(phone)
            and not henry.is_human_mode(phone)
            and not henry.get_history(phone)
        ):
            pipe_id_ret   = lead_ctx.get("pipe_id")
            funil_retorno = PIPE_TO_FUNIL.get(pipe_id_ret) if pipe_id_ret else None
            if funil_retorno:
                logger.info(
                    f"[{phone}] Lead retornando detectado — "
                    f"pipe {pipe_id_ret} ({funil_retorno}) — Gabriel reativado"
                )
                # Carrega preferências de conversas anteriores (aprendizado comportamental)
                lead_id_ret = lead_ctx.get("id")
                if lead_id_ret:
                    pref_note_ret = await asyncio.to_thread(
                        kommo.get_preference_note, lead_id_ret
                    )
                    if pref_note_ret:
                        lead_ctx["preference_history"] = pref_note_ret
                        logger.info(f"[{phone}] Preferências anteriores carregadas para lead retornando")

                lead_ctx["is_returning"] = True
                henry.set_human_mode(phone)         # bloqueia Henry de processar
                gabriel.reactivate(phone, funil_retorno)

                await asyncio.to_thread(zapi.send_typing, phone, 1500)
                await asyncio.sleep(1.5)

                response_ret, handoff_ret = await asyncio.to_thread(
                    gabriel.chat, phone, text, name, lead_ctx
                )
                await asyncio.to_thread(zapi.send_text, phone, response_ret)
                logger.info(
                    f"[{phone}] Gabriel respondeu ao lead retornando "
                    f"({len(response_ret)} chars)"
                )

                if handoff_ret:
                    history_ret = gabriel.get_history(phone)
                    funil_ret   = gabriel.get_funil(phone)
                    await asyncio.to_thread(
                        kommo.update_lead_after_gabriel, phone, history_ret, handoff_ret, funil_ret
                    )
                    gabriel.set_human_mode(phone)
                    asyncio.create_task(asyncio.to_thread(
                        enrich_lead_crm, phone, lead_id_ret, [], history_ret
                    ))
                return

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

                # Enriquecimento silencioso do CRM (Leo AiRM)
                # Roda com o histórico COMPLETO (Henry + Gabriel) para máxima extração
                lead_id_gab  = lead_ctx.get("id")
                henry_hist   = henry.get_history(phone)
                gabriel_hist = history
                asyncio.create_task(asyncio.to_thread(
                    enrich_lead_crm, phone, lead_id_gab, henry_hist, gabriel_hist
                ))
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

            # Ativa Gabriel diretamente — não depende do webhook Kommo
            _FUNIL_MAP = {
                "GABRIEL_ALUGUEL"     : "aluguel",
                "GABRIEL_AVULSO"      : "avulso",
                "GABRIEL_CAPTACAO"    : "captacao",
                "GABRIEL_LANCAMENTOS" : "lancamentos",
                "GABRIEL_INVESTIDOR"  : "investidor",
            }
            funil_gab = _FUNIL_MAP.get(handoff)
            if funil_gab:
                lead_ctx_gab = await asyncio.to_thread(kommo.get_lead_context, phone)
                # Complementa com dados extraídos do histórico do Henry
                # (garante que orçamento, bairro etc. cheguem ao Gabriel mesmo que
                #  a atualização do CRM ainda não tenha sido propagada)
                henry_texto = " ".join(m["content"] for m in henry.get_history(phone))
                extra_ctx   = await asyncio.to_thread(kommo.extract_henry_data, henry_texto, handoff)
                for k, v in extra_ctx.items():
                    if v and not lead_ctx_gab.get(k):
                        lead_ctx_gab[k] = v

                # Aprendizado comportamental (Leo AiRM):
                # busca nota de preferências de conversas anteriores e injeta no contexto Gabriel
                lead_id_for_prefs = lead_ctx_gab.get("id")
                if lead_id_for_prefs:
                    pref_note = await asyncio.to_thread(kommo.get_preference_note, lead_id_for_prefs)
                    if pref_note:
                        lead_ctx_gab["preference_history"] = pref_note
                        logger.info(f"[{phone}] Preferências comportamentais carregadas para Gabriel")

                first_msg_gab = await asyncio.to_thread(
                    gabriel.activate, phone, funil_gab, name, lead_ctx_gab
                )
                await asyncio.to_thread(zapi.send_typing, phone, 2500)
                await asyncio.sleep(2.5)
                await asyncio.to_thread(zapi.send_text, phone, first_msg_gab)
                logger.info(f"[{phone}] Gabriel ativado diretamente — funil: {funil_gab}")

            else:
                # Handoff não-Gabriel (SUPORTE, CORRETOR, URGENTE, JURIDICO, etc.)
                # Enriquece o CRM com o que o Henry coletou
                lead_id_henry = lead_ctx.get("id")
                asyncio.create_task(asyncio.to_thread(
                    enrich_lead_crm, phone, lead_id_henry, history, []
                ))

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

    Lógica de modo humano proativo:
      Se não havia histórico do bot antes desta mensagem E Gabriel não está ativo,
      o humano foi o PRIMEIRO a falar (não o bot). Ativa modo humano imediatamente
      para o bot não interferir quando o cliente responder.

    Por que funciona sem falso-positivo com ecos do próprio bot:
      Quando Henry/Gabriel respondem, adicionam ao histórico em chat() ANTES de
      chamar send_text(). Então quando a Z-API ecoa o fromMe de volta, já existe
      histórico → tinha_historico = True → modo humano NÃO é ativado.
    """
    try:
        if gabriel.is_active(phone) and not gabriel.is_human_mode(phone):
            gabriel.record_outgoing(phone, text)
        elif not henry.is_human_mode(phone):
            # Verifica ANTES de registrar se o bot já tinha falado com este número
            tinha_historico = bool(henry.get_history(phone))
            henry.record_outgoing(phone, text)

            # Humano proativo: nenhum histórico de bot + Gabriel inativo
            # → atendente iniciou a conversa — bot não deve interferir
            if not tinha_historico and not gabriel.is_active(phone):
                henry.set_human_mode(phone)
                logger.info(
                    f"[{phone}] Humano iniciou conversa proativamente — "
                    f"modo humano ativado (bot não interferirá)"
                )
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

    leads_body = body.get("leads") or {}

    # ── Mensagens via chat (Wimoveis, Instagram, FB, web forms) → Henry proativo ─
    # Kommo envia message[add] quando um lead manda mensagem por canal não-WhatsApp.
    # Ativamos Henry da mesma forma que fazemos para leads[add].
    messages_body = body.get("message") or {}
    for event in messages_body.get("add", []):
        # Kommo usa notação PHP: message[add][0][contact_id]
        # → parseado como {'0': {'contact_id': '123', ...}}
        msg_data = event.get("0") if isinstance(event, dict) and "0" in event else event
        if not isinstance(msg_data, dict):
            continue
        try:
            contact_id  = int(msg_data.get("contact_id", 0) or 0)
            entity_type = msg_data.get("entity_type", "")
        except (TypeError, ValueError):
            continue
        # entity_type '2' = lead; também aceitamos string 'lead'
        if contact_id and entity_type in ("2", "lead"):
            logger.info(f"Kommo message[add] — contact_id={contact_id} (canal web/chat)")
            asyncio.create_task(activate_henry_for_contact(contact_id))

    # ── Novos leads → Henry proativo ──────────────────────────────────────────
    for event in leads_body.get("add", []):
        try:
            lead_id = int(event.get("id", 0))
        except (TypeError, ValueError):
            continue
        if lead_id:
            logger.info(f"Kommo leads[add] — lead_id={lead_id}")
            asyncio.create_task(activate_henry_for_lead(lead_id))

    # ── Mudança de status → Gabriel proativo ──────────────────────────────────
    leads_events = leads_body.get("status", [])
    if not leads_events and not leads_body.get("add"):
        logger.info("Kommo: sem eventos de add ou status")
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


async def activate_henry_for_contact(contact_id: int):
    """
    Ativa Henry para um contato que enviou mensagem via canal não-WhatsApp
    (Wimoveis, Instagram, Facebook, formulário web).
    Busca o lead_id pelo contact_id e delega para activate_henry_for_lead.
    """
    try:
        await asyncio.sleep(3)   # aguarda enriquecimento do Kommo
        lead_id = await asyncio.to_thread(kommo.get_lead_id_for_contact, contact_id)
        if not lead_id:
            logger.warning(f"Contact {contact_id} sem lead ativo — Henry não ativado")
            return
        logger.info(f"Contact {contact_id} → lead {lead_id} — ativando Henry")
        await activate_henry_for_lead(lead_id)
    except Exception as e:
        logger.error(f"Erro ao ativar Henry para contact {contact_id}: {e}", exc_info=True)


async def activate_henry_for_lead(lead_id: int):
    """
    Ativa Henry proativamente quando novo lead chega no Kommo via qualquer canal
    (OLX/Canal Pro, Instagram, Facebook, formulário web — sem WhatsApp direto).
    """
    try:
        await asyncio.sleep(5)   # aguarda WebConnect/KWID finalizar enriquecimento
        phone, name, lead_ctx = await asyncio.to_thread(
            kommo.get_lead_phone_and_context, lead_id
        )
        if not phone:
            logger.warning(f"Lead {lead_id} sem telefone — Henry nao ativado")
            return

        # Não reativa se já há atendimento em andamento para este número
        if henry.is_human_mode(phone) or gabriel.is_active(phone) or gabriel.is_human_mode(phone):
            logger.info(f"[{phone}] Ja tem atendimento ativo — nao reativa Henry")
            return
        if henry.get_history(phone):
            logger.info(f"[{phone}] Henry ja tem historico para {phone} — nao reativa proativamente")
            return

        # Guard de reativação após restart: só ativa Henry para leads "frescos"
        # Evita que um restart do Railway reative o Henry em leads já atendidos
        created_at  = lead_ctx.get("created_at", 0)
        lead_age_h  = (time.time() - created_at) / 3600 if created_at else 0
        if lead_age_h > HENRY_MAX_LEAD_AGE_HOURS:
            logger.info(
                f"[{phone}] Lead {lead_id} tem {lead_age_h:.1f}h — "
                f"acima do limite de {HENRY_MAX_LEAD_AGE_HOURS}h para ativação proativa. Ignorando."
            )
            return

        # Se motivação já é conhecida (Canal Pro SELL/RENT), move o lead para o funil correto
        motivo = lead_ctx.get("motivo_busca", "")
        if motivo:
            await asyncio.to_thread(kommo.move_lead_by_motivo, lead_id, motivo)

        first_msg = await asyncio.to_thread(
            henry.activate, phone, name, lead_ctx
        )
        await asyncio.to_thread(zapi.send_typing, phone, 2000)
        await asyncio.sleep(2)
        await asyncio.to_thread(zapi.send_text, phone, first_msg)
        logger.info(f"[{phone}] Henry ativado proativamente — lead {lead_id}")

    except Exception as e:
        logger.error(f"Erro ao ativar Henry para lead {lead_id}: {e}", exc_info=True)


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
@app.api_route("/admin/reset/{phone}", methods=["GET", "POST"])
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
