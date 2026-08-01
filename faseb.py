"""
faseb.py
========
Fase B do Retroativo — Revalidação ativa pelo Gabriel.

Pedido do Felipe (07/07/2026): leads sem entrevista completa recebem uma
mensagem curta do Gabriel validando o interesse ("ainda está procurando?").
Se o cliente responder, o Gabriel emenda a entrevista normal (reactivate +
contexto do CRM) e completa os campos. Se silenciar, o follow-up automático
assume (3 toques) e, no fim, o ciclo de cadência.

Alvo: leads ATIVOS de Aluguel e Avulso com campos-núcleo vazios, EXCETO:
  • etapas DEmanda (o robô CAD1/cadência do Kommo já é dono delas)
  • etapas de "cliente ativo"
  • leads com pausa humana, Gabriel já ativo ou em modo humano
  • leads tocados nas últimas 48h (equipe pode estar atuando)
  • leads que JÁ receberam revalidação (nunca repetir — store faseb_sent)

Regras de disparo (Felipe):
  • Só após 9h — janela: seg–sex 9h–18h, sáb 9h–12h, dom nunca
  • Lotes pequenos (padrão 12/execução) priorizando os mais RECENTES
  • Execução manual via /admin/faseb (dry_run padrão) — sem loop automático
    até o Felipe validar os primeiros lotes (FASEB_AUTO=1 liga o diário)
"""

import os
import time
import asyncio
import logging
import requests
from datetime import datetime, timezone, timedelta

from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, KOMMO_SUBDOMAIN, KOMMO_TOKEN
from kommo import PIPE_ALUGUEL, PIPE_AVULSO, is_equipe_phone
from crm_enricher import JUNK_PRICE_MAX
import store
import followup

logger  = logging.getLogger(__name__)
_client = Anthropic(api_key=ANTHROPIC_API_KEY)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
_BR_TZ      = timezone(timedelta(hours=-3))
_BASE       = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"

FASEB_BATCH = int(os.getenv("FASEB_BATCH", "12"))
FASEB_AUTO  = os.getenv("FASEB_AUTO", "0") == "1"

STATUS_GANHO, STATUS_PERDIDO = 142, 143
F_TIPO, F_BAIRRO, F_DORM, F_URG = 1312432, 1312436, 1328592, 1328582

_running = False
_gabriel = None
_henry   = None
_kommo   = None
_zapi    = None
_is_paused_fn = None


def init(gabriel, henry, kommo, zapi, is_paused_fn) -> None:
    global _gabriel, _henry, _kommo, _zapi, _is_paused_fn
    _gabriel, _henry, _kommo, _zapi = gabriel, henry, kommo, zapi
    _is_paused_fn = is_paused_fn
    if FASEB_AUTO:
        asyncio.create_task(_auto_loop())
        logger.info("Fase B: modo automático diário LIGADO (FASEB_AUTO=1)")


def is_running() -> bool:
    return _running


def status() -> dict:
    st = store.all_state("faseb_status").get("global")
    return st if isinstance(st, dict) else {"status": "nunca executado"}


def _hdr():
    return {"Authorization": f"Bearer {KOMMO_TOKEN}", "Content-Type": "application/json"}


def _in_window(dt: datetime) -> bool:
    wd = dt.weekday()
    if wd == 6:
        return False
    if wd == 5:
        return 9 <= dt.hour < 12
    return 9 <= dt.hour < 18


# ─── Seleção de candidatos ────────────────────────────────────────────────────

def _statuses_excluidos() -> set:
    """IDs de etapas DEmanda / cliente ativo (o bot não deve mexer nelas)."""
    out = set()
    try:
        r = requests.get(f"{_BASE}/leads/pipelines", headers=_hdr(), timeout=15)
        r.raise_for_status()
        for p in r.json().get("_embedded", {}).get("pipelines", []):
            for s in (p.get("_embedded") or {}).get("statuses", []):
                nome = (s.get("name") or "").lower()
                if "demanda" in nome or "cliente ativo" in nome:
                    out.add(s["id"])
    except Exception as e:
        logger.error(f"faseb: erro ao mapear etapas excluídas: {e}")
    return out


def _candidatos(batch: int) -> list[dict]:
    excluidos = _statuses_excluidos()
    corte_48h = time.time() - 48 * 3600
    ja_enviados = set(store.all_state("faseb_sent").keys())

    cands = []
    for pipe in (PIPE_ALUGUEL, PIPE_AVULSO):
        page = 1
        while page <= 4:
            try:
                r = requests.get(
                    f"{_BASE}/leads", headers=_hdr(),
                    params={"filter[pipeline_id]": pipe, "limit": 250, "page": page},
                    timeout=20,
                )
                if r.status_code == 204:
                    break
                r.raise_for_status()
                leads = r.json().get("_embedded", {}).get("leads", [])
            except Exception as e:
                logger.error(f"faseb: listagem pipe {pipe} p{page}: {e}")
                break
            if not leads:
                break
            for ld in leads:
                if ld.get("status_id") in (STATUS_GANHO, STATUS_PERDIDO):
                    continue
                if ld.get("status_id") in excluidos:
                    continue
                if str(ld.get("id")) in ja_enviados:
                    continue
                if int(ld.get("updated_at") or 0) > corte_48h:
                    continue   # equipe pode estar atuando — não atropela
                filled = set()
                for cf in (ld.get("custom_fields_values") or []):
                    vals = cf.get("values") or []
                    if vals and (vals[0].get("value") or vals[0].get("enum_id")):
                        filled.add(cf.get("field_id"))
                core_ok  = all(f in filled for f in (F_TIPO, F_BAIRRO, F_DORM, F_URG))
                price_ok = int(ld.get("price") or 0) >= JUNK_PRICE_MAX
                if core_ok and price_ok:
                    continue   # cadastro completo — não precisa revalidar
                cands.append(ld)
            if len(leads) < 250:
                break
            page += 1

    # Priorização por recência (mais recente responde mais)
    cands.sort(key=lambda l: int(l.get("updated_at") or 0), reverse=True)
    return cands[:batch]


# ─── Mensagem de revalidação ──────────────────────────────────────────────────

def _mensagem_revalidacao(nome: str, funil: str, ctx: dict) -> str:
    sabemos = ", ".join(
        f"{k}: {ctx[k]}" for k in ("tipo_imovel", "bairro", "orcamento") if ctx.get(k)
    ) or "nenhum detalhe registrado"
    objetivo = "alugar" if funil == "aluguel" else "comprar"
    try:
        resp = _client.messages.create(
            model      = HAIKU_MODEL,
            max_tokens = 200,
            system     = (
                "Você é Gabriel, especialista da Seletos Imóveis, retomando contato com um "
                "lead antigo pelo WhatsApp para validar se ele ainda procura imóvel. "
                "Escreva UMA mensagem de no máximo 3 linhas: cumprimente pelo nome (se houver), "
                f"mencione que ele buscou a Seletos para {objetivo} um imóvel, cite algum detalhe "
                "conhecido se existir, e pergunte se a busca continua. Termine pedindo uma resposta "
                "simples. Tom caloroso, 1 emoji, tuteando. NUNCA invente dados. "
                "Escreva SOMENTE a mensagem."
            ),
            messages   = [{"role": "user", "content":
                          f"Nome: {nome or 'não informado'} | O que sabemos: {sabemos}"}],
        )
        msg = resp.content[0].text.strip()
        import custos
        custos.registrar("faseb", HAIKU_MODEL, resp.usage)
        if 15 <= len(msg) <= 400:
            return msg
    except Exception as e:
        logger.warning(f"faseb: geração falhou ({e}) — usando template")
    primeiro = (nome or "").split()[0].title() if nome else ""
    saud = f"Oi{', ' + primeiro if primeiro else ''}! "
    return (saud + f"Sou Gabriel da Seletos Imóveis 😊 Você buscou a gente para {objetivo} "
            "um imóvel. Sua busca ainda continua? Me responde aqui que eu te ajudo!")


# ─── Execução ─────────────────────────────────────────────────────────────────

def run(dry_run: bool = True, batch: int = 0) -> dict:
    global _running
    if _running:
        return {"status": "já em execução"}
    agora = datetime.now(_BR_TZ)
    if not dry_run and not _in_window(agora):
        return {"status": "abortado",
                "motivo": "revalidação só na janela seg–sex 9-18h / sáb 9-12h"}

    _running = True
    batch = batch or FASEB_BATCH
    stats = {"candidatos": 0, "enviados": 0, "sem_telefone": 0, "pulados_estado": 0}
    lista = []
    try:
        cands = _candidatos(batch * 2)   # margem para os pulados
        stats["candidatos"] = len(cands)

        for ld in cands:
            if stats["enviados"] >= batch:
                break
            lead_id = ld.get("id")
            try:
                phone, nome, ctx = _kommo.get_lead_phone_and_context(lead_id)
                if not phone:
                    stats["sem_telefone"] += 1
                    continue
                if is_equipe_phone(phone):
                    stats["pulados_estado"] += 1
                    continue
                if (_is_paused_fn(phone) or _gabriel.is_active(phone)
                        or _gabriel.is_human_mode(phone) or _henry.is_human_mode(phone)):
                    stats["pulados_estado"] += 1
                    continue

                funil = "aluguel" if ld.get("pipeline_id") == PIPE_ALUGUEL else "avulso"
                msg = _mensagem_revalidacao(nome or ld.get("name", ""), funil, ctx)
                lista.append(f"{lead_id} · {ld.get('name')} → {msg[:80]}")

                if not dry_run:
                    ctx["is_returning"] = True
                    _gabriel.reactivate(phone, funil)
                    _gabriel.record_outgoing(phone, msg)   # anti-eco: antes do envio
                    _zapi.send_text(phone, msg)
                    store.set_state(str(lead_id), "faseb_sent",
                                    datetime.now(_BR_TZ).strftime("%d/%m/%Y %H:%M"))
                    followup.record_client_activity(phone)  # arma os 3 toques se silenciar
                    stats["enviados"] += 1
                    time.sleep(3)   # espaçamento entre disparos
                else:
                    stats["enviados"] += 1
            except Exception as e:
                logger.error(f"faseb: lead {lead_id}: {e}")
    finally:
        _running = False

    resultado = {
        "status" : "simulação" if dry_run else "executado",
        "janela" : agora.strftime("%d/%m %H:%M"),
        **stats,
        "mensagens": lista[:30],
    }
    store.set_state("global", "faseb_status", resultado)
    logger.info(f"faseb: {resultado['status']} — {stats}")

    if _zapi and not dry_run:
        try:
            from demandas import REPORT_PHONE
            if REPORT_PHONE:
                _zapi.send_text(
                    REPORT_PHONE,
                    f"🔁 FASE B — lote enviado: {stats['enviados']} revalidações "
                    f"(candidatos: {stats['candidatos']}). Respostas caem no Gabriel."
                )
        except Exception:
            pass
    return resultado


async def _auto_loop():
    """Um lote por dia útil, na primeira hora da janela (se FASEB_AUTO=1)."""
    while True:
        try:
            agora = datetime.now(_BR_TZ)
            hoje  = agora.strftime("%Y-%m-%d")
            ja    = store.all_state("faseb_auto_dia").get("global") == hoje
            if _in_window(agora) and agora.hour >= 9 and not ja:
                await asyncio.to_thread(run, False, 0)
                store.set_state("global", "faseb_auto_dia", hoje)
        except Exception as e:
            logger.error(f"faseb auto: {e}")
        await asyncio.sleep(600)
