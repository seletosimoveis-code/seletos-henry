"""
main.py
=======
Servidor FastAPI da Seletos Imoveis.
"""

import os
import re
import time
import json as json_lib
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent import AgentManager, EQUIPE_TAG, hydrate_from_store as henry_hydrate
from audio import transcribe_audio_url
from gabriel.agent import GabrielManager, PIPE_TO_FUNIL, hydrate_from_store as gabriel_hydrate
import store
from zapi import ZAPIClient
from kommo import (
    KommoClient, canon_phone, is_equipe_phone,
    PIPE_ALUGUEL, PIPE_AVULSO, PIPE_RECEPCAO,
    STATUS_GANHO, STATUS_PERDIDO,
    get_pipe_captacao, get_pipe_lancamentos, get_pipe_investidor,
)
from config import RATE_LIMIT_MAX_PER_MIN, HENRY_MAX_LEAD_AGE_HOURS
from crm_enricher import enrich_lead_crm
import followup
import demandas
import retroativo
import faseb
import estoque
import alertas

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

# ─── Agregador de mensagens picadas (debounce) ────────────────────────────────
# Brasileiro digita em pedaços ("Já vi aqui" / "Já sei"). Sem agregação, cada
# pedaço virava uma resposta do bot (caso Ismael 11/07, 2 respostas em 30s).
# Espera DEBOUNCE_S juntando os pedaços e processa UMA vez.
DEBOUNCE_S = float(os.getenv("MSG_DEBOUNCE_SECONDS", "6"))
_msg_buffer:  dict[str, list[str]]   = {}
_buffer_task: dict[str, asyncio.Task] = {}
_buffer_name: dict[str, str]          = {}

# Marcador de posse do caminho REATIVO (caso Jô, 12/07): quando uma mensagem
# chega pelo Z-API, o caminho reativo é DONO da conversa pelos próximos 90s —
# a ativação proativa via Kommo (message[add]/leads[add]) fica dispensada,
# mesmo que o histórico ainda não exista (debounce + consulta ao CRM atrasam
# a gravação e reabriam a corrida da dupla saudação).
_recent_inbound: dict[str, float] = {}


async def _debounced_process(phone: str):
    try:
        await asyncio.sleep(DEBOUNCE_S)
    except asyncio.CancelledError:
        return   # chegou mais um pedaço — a nova task processa tudo junto
    texts = _msg_buffer.pop(phone, [])
    name  = _buffer_name.pop(phone, "")
    _buffer_task.pop(phone, None)
    if texts:
        await process_message(phone, "\n".join(texts), name)


# ─── Proteção contra tempestade de webhooks ───────────────────────────────────
# Máximo de ativações proativas (Henry/Gabriel) processadas em paralelo.
# Sem isso, um evento em massa no Kommo (ex: fechar 248 leads de uma vez)
# dispara centenas de tasks simultâneas → OOM → crash loop (incidente 05/07/2026).
_activation_sem = asyncio.Semaphore(5)


async def _with_activation_sem(coro):
    """Executa uma corrotina de ativação respeitando o limite de concorrência."""
    async with _activation_sem:
        await coro

# ─── Pausa por intervenção humana ─────────────────────────────────────────────
# Quando um atendente envia mensagem pelo WhatsApp, o bot pausa automaticamente.
# Horário comercial (seg–sex 8h–17h) → 4 horas | Fora do horário → próximo dia útil 8h
HUMAN_PAUSE_HOURS = float(os.getenv("HUMAN_PAUSE_HOURS", "4"))
_human_pause_until: dict[str, float] = {}   # phone → timestamp de retomada permitida
_BR_TZ = timezone(timedelta(hours=-3))


def _calc_resume_timestamp(ts: float) -> float:
    """
    Calcula quando o bot pode retomar após intervenção humana.
      Horário comercial (seg–sex 8h–17h) → +HUMAN_PAUSE_HOURS horas
      Fora do horário / fim de semana    → próximo dia útil às 8h (Brasília)
    """
    dt         = datetime.fromtimestamp(ts, tz=_BR_TZ)
    is_weekday = dt.weekday() < 5        # 0=seg … 4=sex
    is_business = 8 <= dt.hour < 17

    if is_weekday and is_business:
        return ts + HUMAN_PAUSE_HOURS * 3600

    # Próximo dia útil às 8h
    next_day = dt.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while next_day.weekday() >= 5:       # pula sábado e domingo
        next_day += timedelta(days=1)
    return next_day.timestamp()


def _is_human_paused(phone: str) -> bool:
    """True se o bot ainda deve ficar em silêncio (intervenção humana ativa)."""
    resume_at = _human_pause_until.get(phone)
    if not resume_at:
        return False
    if time.time() < resume_at:
        return True
    # Timer expirou — remove e libera o bot
    del _human_pause_until[phone]
    store.del_state(phone, "pause_until")
    logger.info(f"[{phone}] Pausa humana encerrada — bot liberado para retomar")
    return False


def _is_bot_echo(phone: str, text: str) -> bool:
    """
    Verifica se o fromMe é eco da última mensagem enviada pelo próprio bot.

    chat() adiciona a resposta ao histórico ANTES de send_text(), então quando
    Z-API ecoa o fromMe, a mensagem já está como último turno do assistente.
    """
    if not text:
        return False
    t = text.strip()

    def _ultimo_igual(hist: list) -> bool:
        if not hist or hist[-1]["role"] != "assistant":
            return False
        # Turnos da equipe carregam EQUIPE_TAG no conteúdo — remover antes de
        # comparar, senão o eco deixa de ser reconhecido e vira pausa fantasma.
        conteudo = hist[-1]["content"].strip()
        if conteudo.startswith(EQUIPE_TAG):
            conteudo = conteudo[len(EQUIPE_TAG):].strip()
        return conteudo == t

    # Verifica Gabriel (tem prioridade — pode estar ativo com Henry em human mode)
    if _ultimo_igual(gabriel.get_history(phone)):
        return True

    # Verifica Henry
    if _ultimo_igual(henry.get_history(phone)):
        return True

    return False

def _is_duplicate_message(phone: str, text: str) -> bool:
    """
    True se a mesma mensagem já foi processada nos últimos 90 segundos.
    Texto normalizado (strip+lower) — Z-API às vezes reenvia a mesma mensagem
    com variações de espaço/quote, gerando resposta dupla do bot.
    """
    norm = (text or "").strip().lower()
    key  = f"{phone}:{hash(norm) & 0xFFFFFF}"
    now  = time.time()
    for k in list(_msg_dedup):
        if now - _msg_dedup[k] > 90:
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
    # ── Reidratação do estado persistido (Fase 3) ──────────────────────────────
    # Reconstrói conversas, modos e pausas do SQLite — o bot não "esquece" mais
    # os clientes após restart/deploy (incidente 05/07/2026).
    await asyncio.to_thread(_hydrate_state)
    # ── Follow-up automático (cadência anti-abandono) ──────────────────────────
    followup.start(henry, gabriel, kommo, zapi, _is_human_paused)
    # ── Radar de Demandas (relatório diário/semanal de captação) ───────────────
    demandas.start(zapi)
    # ── Retroativo (revisão silenciosa sob demanda via /admin/retroativo) ──────
    retroativo.init(zapi)
    # ── Fase B: revalidação ativa do Gabriel (via /admin/faseb) ────────────────
    faseb.init(gabriel, henry, kommo, zapi, _is_human_paused)
    # ── E3: motor de oferta (inventário do site + matching DEmanda) ────────────
    estoque.init(zapi, kommo, _is_human_paused)
    # ── Alertas em tempo real (Jana geral / Sr. Hygino Assú) ───────────────────
    alertas.init(zapi)


def _hydrate_state():
    try:
        henry_hydrate()
        gabriel_hydrate()
        now = time.time()
        restauradas = 0
        for phone, ts in store.all_state("pause_until").items():
            try:
                ts_f = float(ts)
            except (TypeError, ValueError):
                continue
            if ts_f > now:
                _human_pause_until[phone] = ts_f
                restauradas += 1
            else:
                store.del_state(phone, "pause_until")
        logger.info(f"Pausas humanas restauradas: {restauradas}")
    except Exception as e:
        logger.error(f"Hydrate geral falhou (bot segue sem estado prévio): {e}")


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

    # ── fromMe ANTES do filtro de tipo (correção 07/07) ───────────────────────
    # Mensagens enviadas pela EQUIPE (celular, WhatsApp Web, Kommo) chegam pelo
    # evento "ao enviar" da Z-API com type diferente de ReceivedCallback.
    # O filtro de tipo vinha ANTES e as descartava → a pausa humana nunca
    # disparava e o bot atropelava atendimentos da equipe.
    if body.get("fromMe"):
        phone_fm = canon_phone(body.get("phone", "").strip())
        # ── LID no lugar do telefone (descoberta 11/07, caso Ismael) ──────────
        # O WhatsApp usa identificadores de privacidade (LID, 14+ dígitos) em
        # alguns chats. A pausa caía na chave do LID e o número real do cliente
        # ficava DESPROTEGIDO. Resolvemos pelo mapa LID→telefone aprendido nas
        # mensagens recebidas.
        digits_fm = re.sub(r"\D", "", body.get("phone", "") or "")
        if len(digits_fm) >= 14:
            mapeado = store.all_state("lid_map").get(digits_fm)
            if mapeado:
                phone_fm = canon_phone(str(mapeado))
                logger.info(f"[fromMe] LID {digits_fm} → {phone_fm} (mapeado)")
            else:
                logger.warning(
                    f"[fromMe] phone parece LID sem mapeamento: {digits_fm} — "
                    f"keys={list(body.keys())[:15]}"
                )
        text_fm  = (body.get("text") or {}).get("message", "").strip()
        tem_midia = bool((body.get("audio") or {}).get("audioUrl") or body.get("image") or body.get("video"))
        # Log de diagnóstico: prova se os eventos "enviadas por mim" estão chegando
        logger.info(
            f"[fromMe] type={body.get('type')} phone={phone_fm} "
            f"text_len={len(text_fm)} midia={tem_midia}"
        )
        if phone_fm and (text_fm or tem_midia):
            asyncio.create_task(record_outgoing_message(phone_fm, text_fm or "[mídia]"))
            return JSONResponse({"status": "recorded", "reason": "fromMe — pausa humana avaliada"})
        return JSONResponse({"status": "ignored", "reason": "fromMe sem conteúdo"})

    if body.get("type") != "ReceivedCallback":
        return JSONResponse({"status": "ignored", "reason": "not a message"})

    # Chave canônica — MESMO formato usado pelo caminho Kommo (evita conversa duplicada)
    phone = canon_phone(body.get("phone", "").strip())
    text  = (body.get("text") or {}).get("message", "").strip()
    name  = body.get("senderName", "").strip()

    # Aprende o mapa LID→telefone (usado para resolver eventos fromMe com LID)
    for _lid_field in ("lid", "senderLid", "chatLid", "participantLid"):
        _lid_raw = str(body.get(_lid_field) or "")
        _lid_digits = re.sub(r"\D", "", _lid_raw)
        if len(_lid_digits) >= 14 and phone:
            store.set_state(_lid_digits, "lid_map", phone)

            # ── Migra pausa pendente da chave-LID (caso ref-303, 12/07) ────────
            # Conversa ABERTA pela equipe: o fromMe da Jana chega ANTES de
            # existir mapa → pausa cai na chave-LID. Na 1ª resposta do cliente,
            # transferimos a pausa para o número real ANTES de processar —
            # o bot já encontra a pausa ativa e não atropela a prospecção.
            _lid_pause = float(_human_pause_until.get(_lid_digits) or 0)
            try:
                _lid_pause = max(_lid_pause, float(
                    store.all_state("pause_until").get(_lid_digits) or 0
                ))
            except (TypeError, ValueError):
                pass
            if _lid_pause > time.time():
                _human_pause_until[phone] = _lid_pause
                store.set_state(phone, "pause_until", _lid_pause)
                _human_pause_until.pop(_lid_digits, None)
                store.del_state(_lid_digits, "pause_until")
                followup.cancel(phone)
                logger.info(
                    f"[{phone}] Pausa humana migrada da chave-LID {_lid_digits} "
                    f"(conversa aberta pela equipe)"
                )
            break

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

    # Números da equipe: nenhum robô interage (EQUIPE_PHONES no Railway)
    if is_equipe_phone(phone):
        logger.info(f"[{phone}] Número da equipe — robôs não interagem")
        return JSONResponse({"status": "ignored", "reason": "equipe"})

    if _is_rate_limited(phone):
        return JSONResponse({"status": "ignored", "reason": "rate limit"})

    if _is_duplicate_message(phone, text or audio_url):
        logger.warning(f"[{phone}] Mensagem duplicada detectada — ignorando")
        return JSONResponse({"status": "ignored", "reason": "duplicate"})

    # Caminho reativo assume a posse da conversa (bloqueia proativo por 90s)
    _recent_inbound[phone] = time.time()
    if len(_recent_inbound) > 500:
        _corte = time.time() - 3600
        for _k in [k for k, v in _recent_inbound.items() if v < _corte]:
            _recent_inbound.pop(_k, None)

    # Áudio: processa direto (transcrição não se agrega). Texto: agrega pedaços.
    if audio_url:
        asyncio.create_task(process_message(phone, text, name, audio_url=audio_url, audio_mime=audio_mime))
        return JSONResponse({"status": "queued"})

    _msg_buffer.setdefault(phone, []).append(text)
    _buffer_name[phone] = name
    tarefa_anterior = _buffer_task.get(phone)
    if tarefa_anterior and not tarefa_anterior.done():
        tarefa_anterior.cancel()
    _buffer_task[phone] = asyncio.create_task(_debounced_process(phone))
    return JSONResponse({"status": "buffered", "aguardando": f"{DEBOUNCE_S}s"})


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

    # Cliente falou → zera o ciclo de follow-up automático.
    # Se há pausa humana ativa, o cliente está conversando com um CORRETOR:
    # o ciclo fica em posse do humano e nenhum toque é armado. A posse só volta
    # ao bot quando o cliente escreve com a pausa já vencida (aqui, paused=False).
    followup.record_client_activity(phone, paused=_is_human_paused(phone))
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

        # Pausa por intervenção humana (timer auto-expirável)
        # Acionada quando atendente enviou mensagem — bot fica em silêncio até a hora calculada.
        # Expira automaticamente: 4h em horário comercial | próximo dia útil 8h fora do horário.
        if _is_human_paused(phone):
            logger.info(f"[{phone}] Pausa ativa (intervenção humana) — bot em silêncio")
            return

        lead_ctx = await asyncio.to_thread(kommo.get_lead_context, phone)

        # ── CLIENTE DA CASA (contrato ganho): robôs NÃO atendem ────────────────
        # Regra do Felipe (11/07, caso Renato): cliente com negócio fechado que
        # escreve fala com HUMANO. O bot acolhe UMA vez, aciona a equipe e
        # silencia — mesmo que Gabriel/Henry tenham estado ativo na memória.
        if lead_ctx.get("cliente_ativo"):
            gabriel.set_human_mode(phone)
            henry.set_human_mode(phone)
            followup.cancel(phone)
            acolhida = (
                "Olá! 😊 Como você já é cliente da casa, quem cuida do seu retorno "
                "é diretamente nossa equipe. Já avisei aqui — te respondem em breve!"
            )
            henry.record_outgoing(phone, acolhida)
            await asyncio.to_thread(zapi.send_text, phone, acolhida)
            lead_id_ca = lead_ctx.get("id")
            if lead_id_ca:
                asyncio.create_task(asyncio.to_thread(
                    kommo.add_task, lead_id_ca,
                    f"🔔 Cliente da casa escreveu no WhatsApp: \"{text[:120]}\" — responder pessoalmente.",
                    3600,
                ))
            logger.info(f"[{phone}] Cliente da casa — acolhido 1x, equipe acionada, bots silenciados")
            return

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
                    score_ret   = gabriel.get_score(phone)
                    await asyncio.to_thread(
                        kommo.update_lead_after_gabriel, phone, history_ret, handoff_ret, funil_ret, score_ret
                    )
                    # Alerta tempo-real ao corretor (quente/visita/humano)
                    if handoff_ret in ("VISITA", "URGENTE", "SOLICITADO") or score_ret == "quente":
                        evento = handoff_ret if handoff_ret in ("VISITA", "URGENTE", "SOLICITADO") else "quente"
                        alertas.enviar(evento, lead_ctx, phone, f"Funil: {funil_ret}")
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
                score   = gabriel.get_score(phone)
                await asyncio.to_thread(
                    kommo.update_lead_after_gabriel, phone, history, handoff, funil, score
                )
                # Alerta tempo-real ao corretor (quente/visita/humano)
                if handoff in ("VISITA", "URGENTE", "SOLICITADO") or score == "quente":
                    evento = handoff if handoff in ("VISITA", "URGENTE", "SOLICITADO") else "quente"
                    alertas.enviar(evento, lead_ctx, phone, f"Funil: {funil}")
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
                    gabriel.activate, phone, funil_gab, name, lead_ctx_gab,
                    henry.get_history(phone)[-8:],   # Gabriel vê a triagem — não nasce cego
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
                # Alerta tempo-real: gente esperando HUMANO não pode virar tarefa esquecida
                if handoff in ("URGENTE", "SOLICITADO", "SUPORTE", "JURIDICO"):
                    alertas.enviar(handoff, lead_ctx, phone, "Origem: triagem do Henry")

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
    Registra mensagem enviada pelo atendente humano e ativa a pausa automática do bot.

    Se o texto for eco do próprio bot (chat() adiciona ao histórico ANTES de send_text(),
    então o fromMe ecoa algo que já está como último turno do assistente), ignora
    silenciosamente — nenhuma pausa é acionada.

    Pausa automática:
      Horário comercial (seg–sex 8h–17h) → HUMAN_PAUSE_HOURS horas (padrão: 4h)
      Fora do horário / fim de semana    → próximo dia útil às 8h Brasília
    """
    try:
        # Eco do próprio bot → ignora sem pausar nem registrar
        if _is_bot_echo(phone, text):
            logger.debug(f"[{phone}] fromMe é eco do bot — ignorado sem pausar")
            return

        # Registra no histórico do bot ativo para preservar contexto da conversa.
        # by_human=True → marca o turno como fala da EQUIPE (caso Jucy 24/07).
        if gabriel.is_active(phone) and not gabriel.is_human_mode(phone):
            gabriel.record_outgoing(phone, text, by_human=True)
        elif not henry.is_human_mode(phone):
            henry.record_outgoing(phone, text, by_human=True)

        # Ativa a pausa automática com auto-expiração
        resume_ts = _calc_resume_timestamp(time.time())
        _human_pause_until[phone] = resume_ts
        store.set_state(phone, "pause_until", resume_ts)
        # Humano assumiu → follow-up cancelado E a conversa passa a ser DELE.
        # Sem a posse, as respostas do cliente durante o atendimento humano
        # re-armavam o ciclo e o toque 1 (4h) vencia junto com a pausa (4h) —
        # o bot reabria toda conversa que um humano tinha acabado de encerrar.
        followup.cancel(phone)
        followup.mark_human_owned(phone)
        resume_dt = datetime.fromtimestamp(resume_ts, tz=_BR_TZ)
        logger.info(
            f"[{phone}] Intervenção humana detectada — bot pausado até "
            f"{resume_dt.strftime('%d/%m %H:%M')} (Brasília)"
        )

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
            asyncio.create_task(_with_activation_sem(activate_henry_for_contact(contact_id)))

    # ── Novos leads → Henry proativo ──────────────────────────────────────────
    for event in leads_body.get("add", []):
        try:
            lead_id = int(event.get("id", 0))
        except (TypeError, ValueError):
            continue
        if lead_id:
            logger.info(f"Kommo leads[add] — lead_id={lead_id}")
            asyncio.create_task(_with_activation_sem(activate_henry_for_lead(lead_id)))

    # ── Mudança de status → Gabriel proativo ──────────────────────────────────
    leads_events = leads_body.get("status", [])
    if not leads_events and not leads_body.get("add"):
        logger.info("Kommo: sem eventos de add ou status")
        return

    for event in leads_events:
        try:
            lead_id     = int(event.get("id", 0))
            pipeline_id = int(event.get("pipeline_id", 0))
            status_id   = int(event.get("status_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not lead_id or not pipeline_id:
            continue
        # ── Lead FECHADO (ganho=142 / perdido=143) → NUNCA ativar Gabriel ──────
        # Sem este guard, um fechamento em massa (ex: 248 leads em 05/07/2026)
        # dispara centenas de ativações simultâneas → OOM → crash loop do serviço.
        if status_id in (142, 143):
            logger.info(f"Lead {lead_id} fechado (status {status_id}) — Gabriel nao ativado")
            continue
        funil = PIPE_TO_FUNIL.get(pipeline_id)
        if not funil:
            logger.info(f"Pipeline {pipeline_id} nao e funil Gabriel")
            continue
        asyncio.create_task(_with_activation_sem(activate_gabriel_for_lead(lead_id, pipeline_id, funil)))


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


async def activate_henry_for_lead(lead_id: int, max_idade_h: float | None = None):
    """
    Ativa Henry proativamente quando novo lead chega no Kommo via qualquer canal
    (OLX/Canal Pro, Instagram, Facebook, formulário web — sem WhatsApp direto).

    max_idade_h: sobrepõe HENRY_MAX_LEAD_AGE_HOURS. Usado pelo resgate manual da
    Recepção (/admin/recepcao/resgatar), onde o lead é antigo DE PROPÓSITO —
    ficou parado justamente porque a ativação automática falhou.
    """
    limite_idade = HENRY_MAX_LEAD_AGE_HOURS if max_idade_h is None else max_idade_h
    try:
        await asyncio.sleep(5)   # aguarda WebConnect/KWID finalizar enriquecimento
        phone, name, lead_ctx = await asyncio.to_thread(
            kommo.get_lead_phone_and_context, lead_id
        )
        if not phone:
            logger.warning(f"Lead {lead_id} sem telefone — Henry nao ativado")
            return

        # ── Posse do caminho reativo (caso Jô, 12/07) ──────────────────────────
        # Mensagem chegou pelo Z-API há <90s → o caminho reativo é dono da
        # conversa; a saudação proativa fica dispensada (evita dupla apresentação).
        if time.time() - _recent_inbound.get(phone, 0) < 90:
            logger.info(f"[{phone}] Inbound recente via Z-API — ativação proativa dispensada")
            return

        # ── Prevenção de duplicatas (padrão Canal Pro, 11/07) ──────────────────
        # Portal cria lead novo para contato que JÁ tem lead ativo → marca como
        # duplicata (nota + tarefa de merge) e NÃO inicia novo atendimento.
        lead_existente = await asyncio.to_thread(kommo.find_lead_by_phone, phone)
        if lead_existente and lead_existente.get("id") != lead_id:
            logger.info(
                f"[{phone}] Lead {lead_id} é duplicata do lead ativo "
                f"{lead_existente['id']} — marcando e não ativando Henry"
            )
            asyncio.create_task(asyncio.to_thread(
                kommo.mark_duplicate, lead_id, lead_existente["id"]
            ))
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
        if lead_age_h > limite_idade:
            logger.info(
                f"[{phone}] Lead {lead_id} tem {lead_age_h:.1f}h — "
                f"acima do limite de {limite_idade}h para ativação proativa. Ignorando."
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
    phone = canon_phone(phone)
    henry.reset_conversation(phone)
    gabriel.reset(phone)
    _human_pause_until.pop(phone, None)
    store.del_state(phone, "pause_until")
    followup.cancel(phone)
    return {"status": "ok", "message": f"Conversa de {phone} reiniciada (pausa cancelada)"}


@app.get("/admin/demandas")
async def admin_demandas(semanal: bool = False):
    """Gera o Radar de Demandas sob demanda (para teste/consulta imediata)."""
    report = await asyncio.to_thread(demandas.build_report, semanal)
    return {"report": report}


@app.api_route("/admin/retroativo", methods=["GET", "POST"])
async def admin_retroativo(dry_run: bool = True, limit: int = 0, escopo: str = "ativos"):
    """
    Revisão silenciosa. escopo: 'ativos' (Aluguel+Avulso ativos) |
    'perdidos' (Venda perdida — inclui os 602 do fechamento em massa) |
    'recepcao' (classifica e roteia leads parados na Recepção).
    dry_run=true (padrão) só simula. Acompanhe em /admin/retroativo/status.
    """
    if escopo not in ("ativos", "todos", "perdidos", "recepcao"):
        return {"erro": "escopo deve ser: ativos | todos | perdidos | recepcao"}
    if retroativo.is_running():
        return {"status": "já em execução", "detalhes": retroativo.status()}
    asyncio.create_task(asyncio.to_thread(retroativo.run, dry_run, limit, escopo))
    return {
        "status" : "iniciado em background",
        "escopo" : escopo,
        "modo"   : "SIMULAÇÃO (nada será gravado)" if dry_run else "EXECUÇÃO REAL",
        "acompanhar": "/admin/retroativo/status",
    }


@app.api_route("/admin/retroativo/migrar", methods=["GET", "POST"])
async def admin_retroativo_migrar(batch: int = 40, dry_run: bool = True, destino: str = "demanda"):
    """
    Resgata leads 'Venda perdida' (Aluguel + Avulso) em lotes.
    destino='demanda' (padrão): devolve à etapa DEmanda do próprio funil.
    destino='cadencia': envia aos funis de Cadência por faixa de valor.
    Rode a revisão silenciosa (escopo=perdidos) ANTES, para os leads
    voltarem com cadastro completo.
    """
    if destino not in ("demanda", "cadencia"):
        return {"erro": "destino deve ser: demanda | cadencia"}
    resultado = await asyncio.to_thread(retroativo.migrar_perdidos, batch, dry_run, destino)
    return resultado


@app.get("/admin/retroativo/status")
async def admin_retroativo_status():
    return retroativo.status()


@app.get("/admin/duplicados")
async def admin_duplicados():
    """
    Leads duplicados: mesmo telefone com 2+ leads ATIVOS. Só leitura —
    o merge é feito na interface do Kommo. Demora ~1-2 min (varre contatos).
    """
    return await asyncio.to_thread(retroativo.relatorio_duplicados)


@app.api_route("/admin/recepcao/resgatar", methods=["GET", "POST"])
async def admin_recepcao_resgatar(
    batch: int = 20, dry_run: bool = True, max_dias: int = 7
):
    """
    Rede de segurança da Recepção — leads que entraram e nunca foram atendidos.

    Motivo: leads de portal (Canal Pro/OLX) chegam pelo webhook do Kommo, mas
    quando o evento leads[add] não vem (só a nota de origem), ou quando o canal
    de saída está fora do ar, o Henry nunca é ativado. O lead fica no balcão sem
    score e sem tarefa. Em 27-30/07 isso somou 33 leads em 4 dias.

    Esta varredura pega o que escapou: lead ativo na Recepção, com telefone,
    criado nos últimos `max_dias`, sem histórico de bot, sem pausa/modo humano.

    dry_run=true (padrão) apenas LISTA. Rode o dry primeiro, sempre.
    """
    def _candidatos() -> list[dict]:
        leads = retroativo._paginate_leads(
            {"filter[pipeline_id]": PIPE_RECEPCAO},
            keep=lambda ld: ld.get("status_id") not in (STATUS_GANHO, STATUS_PERDIDO),
        )
        limite = time.time() - max_dias * 86400
        return [ld for ld in leads if (ld.get("created_at") or 0) >= limite]

    candidatos = await asyncio.to_thread(_candidatos)

    elegiveis, ignorados = [], {"sem_telefone": 0, "ja_atendido": 0, "equipe": 0}
    for ld in candidatos:
        phone, nome, _ctx = await asyncio.to_thread(
            kommo.get_lead_phone_and_context, ld["id"]
        )
        if not phone:
            ignorados["sem_telefone"] += 1
            continue
        if is_equipe_phone(phone):
            ignorados["equipe"] += 1
            continue
        if (henry.get_history(phone) or gabriel.is_active(phone)
                or henry.is_human_mode(phone) or gabriel.is_human_mode(phone)
                or _is_human_paused(phone)):
            ignorados["ja_atendido"] += 1
            continue
        elegiveis.append({"lead_id": ld["id"], "nome": nome or ld.get("name"),
                          "phone": phone, "created_at": ld.get("created_at")})

    elegiveis.sort(key=lambda e: e["created_at"] or 0, reverse=True)
    lote = elegiveis[:batch]

    if dry_run:
        return {"dry_run": True, "candidatos_na_recepcao": len(candidatos),
                "elegiveis": len(elegiveis), "ignorados": ignorados,
                "lote_que_seria_ativado": lote}

    ativados = 0
    for item in lote:
        # max_idade_h generoso: estes leads são antigos justamente porque a
        # ativação automática falhou — o guard de idade não deve barrá-los.
        asyncio.create_task(_with_activation_sem(
            activate_henry_for_lead(item["lead_id"], max_idade_h=max_dias * 24)
        ))
        ativados += 1
        await asyncio.sleep(1.5)   # respiro entre ativações

    logger.info(f"[recepcao/resgatar] {ativados} leads reenviados ao Henry")
    return {"dry_run": False, "elegiveis": len(elegiveis),
            "ativados": ativados, "ignorados": ignorados, "lote": lote}


@app.api_route("/admin/retroativo/realocar", methods=["GET", "POST"])
async def admin_retroativo_realocar(batch: int = 20, dry_run: bool = True):
    """
    Leads no funil errado (Motivo da Busca × pipeline): dry_run lista os
    conflitos; modo real move em lotes (ativa Gabriel no funil certo — só
    com supervisão, janela 9h+ seg–sáb).
    """
    resultado = await asyncio.to_thread(retroativo.realocar_desalinhados, batch, dry_run)
    return resultado


@app.get("/admin/custos")
async def admin_custos():
    """Gasto de IA por robô desde o deploy do medidor (tokens, US$ e R$)."""
    import custos
    return await asyncio.to_thread(custos.resumo)


@app.get("/admin/transcripts")
async def admin_transcripts(dias: int = 7):
    """
    Conversas dos robôs dos últimos N dias (aprendizado contínuo).
    Consumido pela rotina semanal que analisa qualidade e sugere ajustes de prompt.
    """
    rows = await asyncio.to_thread(store.recent_messages, dias)
    convs: dict = {}
    for bot, ph, role, content, ts in rows:
        c = convs.setdefault(ph, {"phone": ph, "bots": set(), "msgs": []})
        c["bots"].add(bot)
        c["msgs"].append({"de": "cliente" if role == "user" else bot, "texto": (content or "")[:400]})
    dados = [
        {"phone": c["phone"], "bots": sorted(c["bots"]), "mensagens": c["msgs"][-40:]}
        for c in list(convs.values())[:80]
    ]
    return {"dias": dias, "conversas": len(dados), "dados": dados}


@app.get("/admin/estoque")
async def admin_estoque(forcar: bool = False):
    """Inventário do site (cache 45 min). forcar=true recarrega agora."""
    inv = await asyncio.to_thread(estoque.fetch_inventory, forcar)
    resumo: dict = {}
    for it in inv.values():
        chave = f"{it['finalidade']} · {it['cidade'] or '?'}"
        resumo[chave] = resumo.get(chave, 0) + 1
    return {"total": len(inv), "por_secao": resumo,
            "amostra": list(inv.values())[:8]}


@app.get("/admin/buscar")
async def admin_buscar(finalidade: str = "aluguel", tipo: str = "", cidade: str = "",
                       bairro: str = "", max_price: int = 0, dorm: int = 0):
    """Testa a busca que o Gabriel usa ao vivo (E3)."""
    res = await asyncio.to_thread(
        estoque.search, finalidade, tipo, cidade, bairro, max_price, dorm, 3
    )
    return {"encontrados": len(res), "imoveis": res}


@app.api_route("/admin/matching", methods=["GET", "POST"])
async def admin_matching(dry_run: bool = True, batch: int = 15, notificar: bool = True):
    """
    Matching DEmanda × estoque do site. dry_run=true (padrão) só lista os
    matches. Modo real: Imóveis Potenciais + aviso ao cliente (voz Gabriel,
    janela seg-sex 8-19h/sáb 8-12h, máx MATCH_MAX_DIA/dia) + tarefa corretor.
    """
    return await asyncio.to_thread(estoque.run_matching, dry_run, batch, notificar)


@app.api_route("/admin/faseb", methods=["GET", "POST"])
async def admin_faseb(dry_run: bool = True, batch: int = 0):
    """
    Fase B — revalidação ativa: Gabriel pergunta "ainda está procurando?" a leads
    com cadastro incompleto. dry_run=true mostra o lote e as mensagens sem enviar.
    Modo real: só na janela seg–sex 9-18h / sáb 9-12h, lote padrão 12.
    """
    if faseb.is_running():
        return {"status": "já em execução", "detalhes": faseb.status()}
    resultado = await asyncio.to_thread(faseb.run, dry_run, batch)
    return resultado


@app.get("/admin/faseb/status")
async def admin_faseb_status():
    return faseb.status()


@app.get("/admin/status/{phone}")
async def get_status(phone: str):
    resume_ts  = _human_pause_until.get(phone)
    pause_info = None
    if resume_ts:
        resume_dt  = datetime.fromtimestamp(resume_ts, tz=_BR_TZ)
        mins_left  = max(0, int((resume_ts - time.time()) / 60))
        pause_info = {
            "active"      : time.time() < resume_ts,
            "resume_at"   : resume_dt.strftime("%d/%m/%Y %H:%M") + " (Brasília)",
            "mins_left"   : mins_left,
        }
    return {
        "phone"          : phone,
        "henry_mode"     : henry.is_human_mode(phone),
        "gabriel_active" : gabriel.is_active(phone),
        "gabriel_funil"  : gabriel.get_funil(phone),
        "gabriel_human"  : gabriel.is_human_mode(phone),
        "henry_history"  : len(henry.get_history(phone)),
        "gabriel_history": len(gabriel.get_history(phone)),
        "human_pause"    : pause_info,
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
