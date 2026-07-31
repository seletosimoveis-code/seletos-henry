"""
followup.py
===========
Follow-up automático — cadência anti-abandono (Fase 3 Alta Performance).

Inspirado no Leo AiRM (Real Brokerage): lead nunca é abandonado.
Cliente que silencia DURANTE a qualificação recebe até 3 toques programados:

  Toque 1 → após FOLLOWUP_1_HOURS de silêncio (padrão 4h)
  Toque 2 → FOLLOWUP_2_HOURS após o toque 1 (padrão 24h)
  Toque 3 → FOLLOWUP_3_HOURS após o toque 2 (padrão 72h) — oferece ajuda humana
  Sem resposta após o toque 3 → marca Score=Frio no CRM + nota, e PARA.

Regras anti-spam (invioláveis):
  • Máximo 3 toques por ciclo de silêncio; resposta do cliente zera o ciclo
  • Envia APENAS dentro da janela 8h–19h, segunda a sábado (Brasília)
  • NUNCA envia se: pausa humana ativa, modo humano (handoff feito),
    ou nenhum bot ativo para o telefone
  • Mensagens geradas com o contexto real da conversa (Claude Haiku),
    com fallback para templates se a API falhar

Estado persistido no SQLite (store.py), chave "fu":
  {"last_user_ts": float, "touches": int, "last_touch_ts": float, "done": bool}
"""

import os
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from anthropic import Anthropic
from config import ANTHROPIC_API_KEY
from kommo import is_equipe_phone, get_pipe_captacao
from agent import EQUIPE_TAG
import store

logger  = logging.getLogger(__name__)
_client = Anthropic(api_key=ANTHROPIC_API_KEY)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
_BR_TZ      = timezone(timedelta(hours=-3))

FOLLOWUP_ENABLED = os.getenv("FOLLOWUP_ENABLED", "1") == "1"
FOLLOWUP_1_H     = float(os.getenv("FOLLOWUP_1_HOURS", "4"))
FOLLOWUP_2_H     = float(os.getenv("FOLLOWUP_2_HOURS", "24"))
FOLLOWUP_3_H     = float(os.getenv("FOLLOWUP_3_HOURS", "72"))
WINDOW_START_H   = int(os.getenv("FOLLOWUP_WINDOW_START", "8"))
WINDOW_END_H     = int(os.getenv("FOLLOWUP_WINDOW_END", "19"))
CHECK_EVERY_S    = int(os.getenv("FOLLOWUP_CHECK_SECONDS", "300"))   # 5 min

# Referências injetadas por start() — evita import circular com main.py
_henry   = None
_gabriel = None
_kommo   = None
_zapi    = None
_is_paused_fn = None


# ─── Registro de atividade (chamado pelo main.py) ─────────────────────────────

def record_client_activity(phone: str, paused: bool = False) -> None:
    """
    Cliente falou → zera o ciclo de follow-up.

    paused=True (pausa humana ativa) → o cliente está falando COM UM CORRETOR.
    Não re-armamos o ciclo: quem responde é o humano. Caso contrário o toque 1
    vencia exatamente junto com a pausa (ambos 4h) e o bot reabria a conversa
    poucos minutos depois de o corretor encerrá-la (caso Jucy, 24/07 14:51).
    """
    try:
        if paused:
            fu = store.all_state("fu").get(phone)
            if isinstance(fu, dict) and fu.get("human_owned"):
                return
            store.set_state(phone, "fu", {
                "last_user_ts": time.time(), "touches": 0, "last_touch_ts": 0,
                "done": False, "human_owned": True,
            })
            return
        store.set_state(phone, "fu", {
            "last_user_ts": time.time(), "touches": 0, "last_touch_ts": 0,
            "done": False, "human_owned": False,
        })
    except Exception as e:
        logger.warning(f"[{phone}] followup record falhou: {e}")


def cancel(phone: str) -> None:
    """Humano assumiu / handoff / reset → follow-up deste ciclo é cancelado."""
    store.del_state(phone, "fu")


def mark_human_owned(phone: str) -> None:
    """
    Marca a conversa como propriedade do corretor humano.

    A posse só é devolvida ao bot quando o cliente escrever DEPOIS de a pausa
    humana terminar (main.py). Enquanto durar, nenhum toque automático sai —
    é o que impede o bot de reabrir um assunto que o humano já fechou.
    """
    try:
        store.set_state(phone, "fu", {
            "last_user_ts": time.time(), "touches": 0, "last_touch_ts": 0,
            "done": False, "human_owned": True,
        })
        logger.info(f"[{phone}] Conversa marcada como atendimento humano — follow-up suspenso")
    except Exception as e:
        logger.warning(f"[{phone}] mark_human_owned falhou: {e}")


# ─── Janela de envio ──────────────────────────────────────────────────────────

def _in_window(now_ts: float) -> bool:
    """
    Janela de envio (definida pelo Felipe em 06/07/2026 — horários em que ele
    consegue acompanhar e intervir se necessário):
      Seg–sex: 8h–19h  |  Sábado: 8h–12h  |  Domingo: nunca
    """
    dt = datetime.fromtimestamp(now_ts, tz=_BR_TZ)
    wd = dt.weekday()
    if wd == 6:                      # domingo
        return False
    if wd == 5:                      # sábado
        return 8 <= dt.hour < 12
    return WINDOW_START_H <= dt.hour < WINDOW_END_H   # seg–sex (padrão 8–19)


# ─── Geração da mensagem ──────────────────────────────────────────────────────

_TEMPLATES = {
    1: "Oi{nome}! Conseguiu dar uma olhada nas opções? Se quiser, filtro mais alternativas pra você 😊",
    2: "Oi{nome}! Ainda estou por aqui te ajudando na busca. Apareceu alguma dúvida ou quer que eu busque outras opções? 🏠",
    3: "Oi{nome}! Não quero te encher — se preferir, peço para um corretor da nossa equipe te ligar e resolver tudo por telefone. Que tal? 😊",
}

_FU_INSTRUCTIONS = {
    1: "Escreva UMA mensagem curta (máx 2 linhas) retomando a conversa de forma leve e útil. "
       "Referencie o que o cliente estava buscando. Termine com uma pergunta fácil de responder.",
    2: "Segundo toque: o cliente segue em silêncio. UMA mensagem curta (máx 2 linhas) oferecendo "
       "valor novo (ex: buscar outras opções, ajustar filtros). Sem tom de cobrança.",
    3: "Terceiro e ÚLTIMO toque: ofereça encaminhar para um corretor humano ligar. "
       "Deixe claro que não vai mais insistir. Tom respeitoso e caloroso. Máx 2 linhas.",
}


def _generate_message(history: list[dict], touch: int, bot_name: str) -> str:
    """Gera o toque com contexto da conversa; template como fallback."""
    try:
        linhas = []
        for m in history[-12:]:
            if m["role"] == "user":
                linhas.append(f"Cliente: {m['content']}")
            elif m["content"].strip().startswith(EQUIPE_TAG):
                # Fala de um CORRETOR humano — precisa ficar explícito, senão o
                # modelo assume que foi ele quem disse e inverte os papéis.
                texto = m["content"].strip()[len(EQUIPE_TAG):].strip()
                linhas.append(f"Corretor(a) da equipe: {texto}")
            else:
                linhas.append(f"{bot_name}: {m['content']}")
        transcript = "\n".join(linhas)
        resp = _client.messages.create(
            model      = HAIKU_MODEL,
            max_tokens = 200,
            system     = (
                f"Você é {bot_name} da Seletos Imóveis retomando contato com um cliente "
                f"que parou de responder no WhatsApp. {_FU_INSTRUCTIONS[touch]} "
                "Escreva SOMENTE a mensagem, sem aspas, sem explicações. "
                "Português brasileiro, tuteando, no máximo 1 emoji.\n"
                "REGRAS INVIOLÁVEIS:\n"
                "1. NUNCA invente imóveis, preços ou disponibilidade.\n"
                "2. NUNCA invente um motivo, acontecimento ou circunstância que "
                "não esteja LITERALMENTE escrito na conversa (chuva, trânsito, "
                "viagem, imprevisto). Se não está no texto, não existe.\n"
                "3. Preste atenção em QUEM disse cada coisa. 'Corretor(a) da "
                "equipe' é uma pessoa da Seletos, não é você e não é o cliente. "
                "Um impedimento do corretor JAMAIS pode ser atribuído ao cliente.\n"
                "4. NUNCA marque, confirme nem sugira data, horário ou endereço "
                "de visita — agendamento é exclusivo de um corretor humano. "
                "Se o assunto for visita, apenas pergunte a disponibilidade e "
                "diga que um corretor confirma.\n"
                "5. Se o cliente já disse que NÃO pode, não estará disponível ou "
                "desistiu, respeite: não insista no mesmo horário nem reafirme "
                "combinados que ele acabou de recusar.\n"
                "6. Se a conversa foi encerrada por um corretor humano ou já "
                "chegou a uma conclusão, seja breve e não reabra o assunto."
            ),
            messages   = [{"role": "user", "content": f"CONVERSA ATÉ AGORA:\n{transcript}"}],
        )
        msg = resp.content[0].text.strip()
        if 10 <= len(msg) <= 500:
            return msg
    except Exception as e:
        logger.warning(f"followup: geração LLM falhou ({e}) — usando template")
    return _TEMPLATES[touch].format(nome="")


# ─── Loop principal ───────────────────────────────────────────────────────────

def _due_hours(touches: int) -> float | None:
    return {0: FOLLOWUP_1_H, 1: FOLLOWUP_2_H, 2: FOLLOWUP_3_H}.get(touches)


async def _check_once() -> None:
    now = time.time()
    if not _in_window(now):
        return

    for phone, fu in store.all_state("fu").items():
        try:
            if is_equipe_phone(phone):
                cancel(phone)
                continue
            if not isinstance(fu, dict) or fu.get("done"):
                continue
            # Conversa em posse de um corretor humano → nenhum toque automático.
            # Só o cliente escrevendo APÓS o fim da pausa devolve a conversa ao
            # bot (main.py). Trava do caso Jucy (24/07).
            if fu.get("human_owned"):
                continue
            touches = int(fu.get("touches", 0))
            due_h   = _due_hours(touches)
            if due_h is None:
                continue
            ref_ts = float(fu.get("last_touch_ts") or 0) or float(fu.get("last_user_ts") or 0)
            if not ref_ts or (now - ref_ts) < due_h * 3600:
                continue

            # ── Elegibilidade no momento do envio ────────────────────────────
            if _is_paused_fn and _is_paused_fn(phone):
                continue

            # ── Follow-up é SÓ para locatários/compradores em busca ativa ─────
            # Regra do Felipe (11/07): proprietários e clientes com contrato
            # ganho NUNCA recebem abordagem automática — só contato humano
            # quando houver pendência. (Caso Edileide: proprietária com
            # captação ganha recebeu 2 toques como se buscasse imóvel.)
            lead_atual = await asyncio.to_thread(_kommo.find_lead_by_phone, phone)
            if not lead_atual:
                # Sem lead ATIVO (ex: contrato ganho/fechado) → não abordar
                logger.info(f"[{phone}] Follow-up cancelado — sem lead ativo (cliente fechado?)")
                cancel(phone)
                continue
            if lead_atual.get("pipeline_id") == get_pipe_captacao():
                logger.info(f"[{phone}] Follow-up cancelado — proprietário (Captação)")
                cancel(phone)
                continue
            if _gabriel.is_active(phone) and not _gabriel.is_human_mode(phone):
                bot, bot_name = _gabriel, "Gabriel"
                history = _gabriel.get_history(phone)
            elif (_henry.get_history(phone)
                  and not _henry.is_human_mode(phone)
                  and not _gabriel.is_human_mode(phone)):
                bot, bot_name = _henry, "Henry"
                history = _henry.get_history(phone)
            else:
                # Handoff feito / humano assumiu → cancela o ciclo
                cancel(phone)
                continue
            if not history:
                cancel(phone)
                continue

            touch = touches + 1
            msg = await asyncio.to_thread(_generate_message, history, touch, bot_name)

            # Registra ANTES de enviar (convenção anti-eco: o fromMe ecoado
            # precisa já estar como último turno do assistente no histórico)
            bot.record_outgoing(phone, msg)
            await asyncio.to_thread(_zapi.send_text, phone, msg)
            store.set_state(phone, "fu", {
                "last_user_ts" : fu.get("last_user_ts", now),
                "touches"      : touch,
                "last_touch_ts": now,
                "done"         : touch >= 3,
            })
            logger.info(f"[{phone}] Follow-up {touch}/3 enviado ({bot_name}): {msg[:60]}")

            # Toque 3 enviado → marca frio + move para funil de Cadência (background)
            # e SILENCIA os bots: a partir daqui o Salesbot de cadência do Kommo
            # é dono do lead; quando o cliente interagir, o alerta é para HUMANO.
            if touch >= 3:
                try:
                    _gabriel.set_human_mode(phone)
                    _henry.set_human_mode(phone)
                except Exception:
                    pass
                asyncio.create_task(asyncio.to_thread(_mark_cold_safe, phone))

            await asyncio.sleep(2)   # espaça envios (rate limit Z-API)
        except Exception as e:
            logger.error(f"[{phone}] followup erro: {e}")


def _mark_cold_safe(phone: str) -> None:
    try:
        _kommo.mark_lead_cold(phone)
    except Exception as e:
        logger.warning(f"[{phone}] mark_lead_cold falhou: {e}")


async def _loop() -> None:
    logger.info(
        f"Follow-up ativo: toques em {FOLLOWUP_1_H}h/{FOLLOWUP_2_H}h/{FOLLOWUP_3_H}h, "
        f"janela {WINDOW_START_H}h-{WINDOW_END_H}h seg-sáb, verificação a cada {CHECK_EVERY_S}s"
    )
    while True:
        try:
            await _check_once()
        except Exception as e:
            logger.error(f"followup loop erro: {e}")
        await asyncio.sleep(CHECK_EVERY_S)


def start(henry, gabriel, kommo, zapi, is_paused_fn) -> None:
    """Chamado no startup do main.py — injeta dependências e agenda o loop."""
    global _henry, _gabriel, _kommo, _zapi, _is_paused_fn
    _henry, _gabriel, _kommo, _zapi = henry, gabriel, kommo, zapi
    _is_paused_fn = is_paused_fn
    if not FOLLOWUP_ENABLED:
        logger.info("Follow-up DESATIVADO via FOLLOWUP_ENABLED=0")
        return
    asyncio.create_task(_loop())
