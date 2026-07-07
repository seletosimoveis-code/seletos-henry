"""
demandas.py
===========
Radar de Demandas — relatório diário/semanal de captação orientada por demanda.

Lógica (Felipe, 07/07/2026): as abas "DEmanda | Procura de imóvel" (funis Aluguel
e Avulso) são o radar de captação. Se 30 clientes querem casa em Capim Macio,
captar uma casa em Capim Macio é fechamento quase garantido. Este módulo
transforma essas abas em inteligência acionável:

  • Varre os leads nas etapas DEmanda dos dois funis
  • Agrega por perfil: finalidade × tipo de imóvel × bairro × faixa de preço
  • Ranqueia as oportunidades de captação (mais clientes = mais prioritário)
  • Envia relatório via WhatsApp para a equipe:
      - DIÁRIO  → todo dia às 8h (Brasília), exceto domingo
      - SEMANAL → segunda às 8h (consolidado, com todos os perfis)

⛔ REGRA INVIOLÁVEL (Felipe): este módulo é 100% INTERNO. Ele NUNCA envia
mensagem a cliente — apenas ao número da equipe (DEMANDA_REPORT_PHONE).
A futura v2 (busca de imóveis de parceiros na internet) alimentará SOMENTE
este relatório interno; link de parceiro só chega ao cliente pela mão de um
humano, após validação da parceria.

Config (env):
  DEMANDA_REPORT_PHONE  → WhatsApp que recebe o relatório (ex: 5584XXXXXXXX).
                          Sem essa variável, o radar fica desativado (só log).
  DEMANDA_REPORT_HOUR   → hora do envio (padrão 8, Brasília)
"""

import os
import time
import asyncio
import logging
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from config import KOMMO_SUBDOMAIN, KOMMO_TOKEN
import store

logger = logging.getLogger(__name__)

_BR_TZ = timezone(timedelta(hours=-3))
_BASE  = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"

REPORT_PHONE = os.getenv("DEMANDA_REPORT_PHONE", "").strip()
REPORT_HOUR  = int(os.getenv("DEMANDA_REPORT_HOUR", "8"))

# Campos usados na agregação
F_TIPO_IMOVEL = 1312432
F_BAIRRO      = 1312436
F_DORM        = 1328592

_zapi = None   # injetado por start()


def _hdr():
    return {"Authorization": f"Bearer {KOMMO_TOKEN}", "Content-Type": "application/json"}


# ─── Descoberta das etapas DEmanda ────────────────────────────────────────────

def _demanda_statuses() -> list[tuple[int, int, str]]:
    """
    Retorna [(pipeline_id, status_id, 'aluguel'|'compra')] das etapas cujo nome
    contém 'demanda' nos funis Aluguel e Avulso. Descoberta dinâmica por nome —
    sobrevive a mudanças de ID.
    """
    out = []
    try:
        r = requests.get(f"{_BASE}/leads/pipelines", headers=_hdr(), timeout=15)
        r.raise_for_status()
        for p in r.json().get("_embedded", {}).get("pipelines", []):
            pnome = (p.get("name") or "").lower()
            if "aluguel" in pnome:
                finalidade = "aluguel"
            elif "avulso" in pnome:
                finalidade = "compra"
            else:
                continue
            for s in (p.get("_embedded") or {}).get("statuses", []):
                if "demanda" in (s.get("name") or "").lower():
                    out.append((p["id"], s["id"], finalidade))
    except Exception as e:
        logger.error(f"demandas: erro ao descobrir etapas DEmanda: {e}")
    return out


# ─── Coleta dos leads ─────────────────────────────────────────────────────────

def _fetch_demanda_leads() -> list[dict]:
    """Busca todos os leads nas etapas DEmanda (paginado)."""
    statuses = _demanda_statuses()
    if not statuses:
        logger.warning("demandas: nenhuma etapa DEmanda encontrada")
        return []

    params: dict = {"limit": 250, "page": 1}
    for i, (pid, sid, _) in enumerate(statuses):
        params[f"filter[statuses][{i}][pipeline_id]"] = pid
        params[f"filter[statuses][{i}][status_id]"]   = sid

    finalidade_by_pipe = {pid: fin for pid, _, fin in statuses}
    leads: list[dict] = []
    try:
        while True:
            r = requests.get(f"{_BASE}/leads", headers=_hdr(), params=params, timeout=20)
            if r.status_code == 204:
                break
            r.raise_for_status()
            page_leads = r.json().get("_embedded", {}).get("leads", [])
            if not page_leads:
                break
            for ld in page_leads:
                cf = {c.get("field_id"): c for c in (ld.get("custom_fields_values") or [])}

                def _val(fid):
                    vals = (cf.get(fid) or {}).get("values") or []
                    return str(vals[0].get("value", "")).strip() if vals else ""

                leads.append({
                    "id"        : ld.get("id"),
                    "name"      : ld.get("name") or f"Lead #{ld.get('id')}",
                    "price"     : int(ld.get("price") or 0),
                    "tipo"      : _val(F_TIPO_IMOVEL).lower(),
                    "bairro"    : _val(F_BAIRRO).title(),
                    "dorm"      : _val(F_DORM),
                    "finalidade": finalidade_by_pipe.get(ld.get("pipeline_id"), "?"),
                    "updated_at": int(ld.get("updated_at") or 0),
                })
            if len(page_leads) < 250:
                break
            params["page"] += 1
    except Exception as e:
        logger.error(f"demandas: erro ao buscar leads: {e}")
    return leads


# ─── Agregação e relatório ────────────────────────────────────────────────────

def _faixa(finalidade: str, price: int) -> str:
    if not price:
        return "valor n/i"
    if finalidade == "aluguel":
        if price <= 1500:  return "até R$1.500"
        if price <= 2500:  return "R$1.501–2.500"
        if price <= 4000:  return "R$2.501–4.000"
        return "acima de R$4.000"
    if price <= 300_000:   return "até R$300 mil"
    if price <= 600_000:   return "R$301–600 mil"
    return "acima de R$600 mil"


def build_report(semanal: bool = False) -> str:
    leads = _fetch_demanda_leads()
    hoje  = datetime.now(_BR_TZ).strftime("%d/%m/%Y")
    titulo = "📡 RADAR DE DEMANDAS — SEMANAL" if semanal else "📡 RADAR DE DEMANDAS — diário"

    if not leads:
        return f"{titulo} {hoje}\n\nNenhum lead nas abas DEmanda hoje."

    aluguel = [l for l in leads if l["finalidade"] == "aluguel"]
    compra  = [l for l in leads if l["finalidade"] == "compra"]

    grupos: dict[tuple, list[dict]] = defaultdict(list)
    sem_perfil: list[dict] = []
    for l in leads:
        if not l["tipo"] and not l["bairro"]:
            sem_perfil.append(l)
            continue
        chave = (
            l["finalidade"],
            l["tipo"] or "tipo n/i",
            l["bairro"] or "bairro n/i",
            _faixa(l["finalidade"], l["price"]),
        )
        grupos[chave].append(l)

    ranking = sorted(grupos.items(), key=lambda kv: len(kv[1]), reverse=True)
    corte   = len(ranking) if semanal else 10

    linhas = [
        f"{titulo} {hoje}",
        f"Total em DEmanda: {len(leads)} clientes (Aluguel {len(aluguel)} | Compra {len(compra)})",
        "",
        "🔥 OPORTUNIDADES DE CAPTAÇÃO (clientes esperando):",
    ]
    for i, (chave, ls) in enumerate(ranking[:corte], 1):
        fin, tipo, bairro, faixa = chave
        marcador = "🏆" if len(ls) >= 5 else ("⭐" if len(ls) >= 3 else "•")
        linhas.append(f"{marcador} {i}. {tipo.title()} · {bairro} · {fin} {faixa} — {len(ls)} cliente(s)")
        if semanal:
            nomes = ", ".join(l["name"].split("|")[0].strip() for l in ls[:6])
            linhas.append(f"      ({nomes}{'…' if len(ls) > 6 else ''})")

    if sem_perfil:
        linhas += [
            "",
            f"⚠️ {len(sem_perfil)} lead(s) sem perfil mapeado (sem tipo e bairro) — "
            "não entram no radar. Rodar retroativo/completar cadastro.",
        ]

    linhas += [
        "",
        "💡 Perfil com 3+ clientes = captação com fechamento quase garantido.",
    ]
    return "\n".join(linhas)


# ─── Agendamento ──────────────────────────────────────────────────────────────

async def _loop():
    logger.info(
        f"Radar de Demandas ativo — envio {REPORT_HOUR}h Brasília para {REPORT_PHONE[:6]}****"
    )
    while True:
        try:
            now = datetime.now(_BR_TZ)
            hoje_str = now.strftime("%Y-%m-%d")
            ja_enviado = store.all_state("demanda_report_sent").get("global") == hoje_str
            if (now.hour == REPORT_HOUR and not ja_enviado and now.weekday() != 6):
                semanal = now.weekday() == 0   # segunda-feira
                report  = await asyncio.to_thread(build_report, semanal)
                await asyncio.to_thread(_zapi.send_text, REPORT_PHONE, report)
                store.set_state("global", "demanda_report_sent", hoje_str)
                logger.info(f"Radar de Demandas: relatório {'semanal' if semanal else 'diário'} enviado")
        except Exception as e:
            logger.error(f"Radar de Demandas: erro no loop: {e}")
        await asyncio.sleep(300)   # verifica a cada 5 min


def start(zapi) -> None:
    """Chamado no startup do main.py."""
    global _zapi
    _zapi = zapi
    if not REPORT_PHONE:
        logger.info("Radar de Demandas DESATIVADO — defina DEMANDA_REPORT_PHONE no Railway")
        return
    asyncio.create_task(_loop())
