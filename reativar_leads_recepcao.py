"""
reativar_leads_recepcao.py
===========================
Script "Lead Resurrection" — re-engaja leads parados na Recepção do Kommo.

Estratégia (baseada na Real Brokerage):
  Quentes  (0-30 dias)  → mensagem de retomada com pergunta aberta
  Mornos   (30-90 dias) → mensagem de re-qualificacao
  Frios    (+90 dias)   → "breakup message" de despedida (alta taxa de resposta)

Uso:
    python reativar_leads_recepcao.py --segmento quentes --dry-run
    python reativar_leads_recepcao.py --segmento quentes
    python reativar_leads_recepcao.py --segmento mornos
    python reativar_leads_recepcao.py --segmento frios
    python reativar_leads_recepcao.py --segmento todos
    python reativar_leads_recepcao.py --segmento todos --limite 20

Flags:
    --dry-run    Simula sem enviar mensagens nem modificar o Kommo
    --limite N   Processa no maximo N leads (util para testar em lote pequeno)

IMPORTANTE:
    Leads que ja receberam mensagem sao marcados com a tag "reativacao enviada"
    e nao serao contatados novamente em execucoes futuras.
"""

import sys
import os
import re
import time
import logging
import argparse
import requests
from datetime import datetime, timezone

# Garante que imports do projeto funcionam
sys.path.insert(0, os.path.dirname(__file__))
from config import KOMMO_SUBDOMAIN, KOMMO_TOKEN
from zapi import ZAPIClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE           = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"
STATUS_GANHO   = 142
STATUS_PERDIDO = 143
PIPE_RECEPCAO  = 9959303
TAG_REATIVACAO = "reativacao enviada"

# Delay entre envios — nao reduza abaixo de 8s para evitar bloqueio do WhatsApp
DELAY_ENTRE_ENVIOS = 10   # segundos


# =============================================================================
# MENSAGENS POR SEGMENTO
# =============================================================================

_NOMES_GENERICOS = {"lead", "novo", "contato", "cliente", "whatsapp", "prospect", "oportunidade"}

def _primeiro_nome(nome: str) -> str:
    """Extrai primeiro nome para personalizar a mensagem.
    Retorna vazio se o nome for genérico (ex: 'Lead #31929878', 'Novo Lead')."""
    if not nome or not nome.strip():
        return ""
    primeiro = nome.strip().split()[0].capitalize()
    if primeiro.lower() in _NOMES_GENERICOS:
        return ""
    return f" {primeiro}"


def msg_quente(nome: str) -> str:
    n = _primeiro_nome(nome)
    return (
        f"Olá{n}! 👋 Sou Henry da Seletos Imóveis. "
        f"Vi que você entrou em contato conosco recentemente. "
        f"Ainda posso te ajudar com algum imóvel? "
        f"Como está sua busca? 😊"
    )


def msg_morno(nome: str) -> str:
    n = _primeiro_nome(nome)
    return (
        f"Olá{n}! Aqui é o Henry da Seletos Imóveis 😊 "
        f"Faz um tempo que não nos falamos — você ainda está "
        f"buscando um imóvel? Estou aqui para ajudar!"
    )


def msg_frio(nome: str) -> str:
    n = _primeiro_nome(nome)
    return (
        f"Olá{n}! Aqui é a Seletos Imóveis. "
        f"Para não te incomodar mais, vou encerrar seu atendimento por aqui. "
        f"Se um dia precisar de ajuda com imóveis em Natal, Parnamirim, "
        f"Assú ou Mossoró, é só chamar. Boa sorte! 🙏"
    )


# =============================================================================
# HELPERS KOMMO
# =============================================================================

def _hdr() -> dict:
    return {"Authorization": f"Bearer {KOMMO_TOKEN}", "Content-Type": "application/json"}


def _norm_phone(raw: str) -> str:
    """Retorna número com prefixo 55 (formato Z-API: 5584XXXXXXXXX)."""
    digits = re.sub(r"\D", "", raw or "")
    # Remove 55 se já tiver para normalizar, depois adiciona de volta
    if digits.startswith("55") and len(digits) > 11:
        digits = digits[2:]
    digits = digits[-11:] if len(digits) >= 10 else digits
    # Z-API exige código do país
    return "55" + digits if digits else ""


def _get(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{BASE}/{path}", headers=_hdr(), params=params or {})
    if r.status_code == 204:
        return {}
    r.raise_for_status()
    return r.json()


def _get_contact_phone_and_name(contact_id: int) -> tuple[str | None, str]:
    """Busca telefone e nome real no contato Kommo. Retorna (phone, nome)."""
    try:
        contact = _get(f"contacts/{contact_id}")
        nome    = contact.get("name", "")
        for cf in (contact.get("custom_fields_values") or []):
            if cf.get("field_code") in ("PHONE", "TEL"):
                vals = cf.get("values", [])
                if vals:
                    phone = _norm_phone(str(vals[0].get("value", "")))
                    return (phone if phone else None), nome
    except Exception as e:
        logger.warning(f"Erro ao buscar contato {contact_id}: {e}")
    return None, ""


def _has_tag(lead: dict, tag_name: str) -> bool:
    tags = lead.get("tags") or []
    return any(t.get("name", "").lower() == tag_name.lower() for t in tags)


def _add_tag(lead_id: int, tag_name: str):
    """Adiciona tag ao lead sem remover as existentes."""
    try:
        lead        = _get(f"leads/{lead_id}", {"with": "tags"})
        tags_atuais = [{"name": t["name"]} for t in (lead.get("tags") or [])]
        if not any(t["name"].lower() == tag_name.lower() for t in tags_atuais):
            tags_atuais.append({"name": tag_name})
        r = requests.patch(
            f"{BASE}/leads/{lead_id}",
            headers=_hdr(),
            json={"tags": tags_atuais},
        )
        if not r.ok:
            logger.error(f"Tag '{tag_name}' no lead {lead_id} falhou: {r.status_code} {r.text[:200]}")
        r.raise_for_status()
        logger.info(f"    Tag '{tag_name}' salva no Kommo (lead {lead_id})")
    except Exception as e:
        logger.error(f"Erro ao adicionar tag '{tag_name}' no lead {lead_id}: {e}")


def _fetch_leads_recepcao() -> list[dict]:
    """Busca todos os leads ativos na Recepção (paginado)."""
    leads = []
    page  = 1
    while True:
        try:
            data = _get("leads", {
                "filter[pipeline_id][]": PIPE_RECEPCAO,
                "with"                 : "contacts,tags",
                "page"                 : page,
                "limit"                : 250,
            })
            page_leads = data.get("_embedded", {}).get("leads", [])
            if not page_leads:
                break
            leads.extend(page_leads)
            if len(page_leads) < 250:
                break
            page += 1
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"Erro ao buscar leads página {page}: {e}")
            break
    return leads


def _idade_dias(lead: dict) -> int:
    ts     = lead.get("created_at") or 0
    criado = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (datetime.now(tz=timezone.utc) - criado).days


def _atividade_recente(lead: dict, horas: int = 24) -> bool:
    """Retorna True se o lead teve atividade nas últimas N horas (ex: atendimento humano em curso)."""
    ts = lead.get("updated_at") or 0
    if not ts:
        return False
    atualizado = datetime.fromtimestamp(ts, tz=timezone.utc)
    delta = datetime.now(tz=timezone.utc) - atualizado
    return delta.total_seconds() < horas * 3600


def _segmento(dias: int) -> str:
    if dias <= 30:
        return "quentes"
    elif dias <= 90:
        return "mornos"
    return "frios"


# =============================================================================
# EXECUCAO PRINCIPAL
# =============================================================================

def run(segmento: str, dry_run: bool, limite: int | None):
    logger.info("=" * 60)
    logger.info(f"Lead Resurrection | segmento={segmento} | dry_run={dry_run} | limite={limite or 'sem limite'}")
    logger.info("=" * 60)

    zapi  = ZAPIClient()
    leads = _fetch_leads_recepcao()

    # Filtra apenas leads ativos (nao ganho/perdido)
    ativos = [
        l for l in leads
        if l.get("status_id") not in (STATUS_GANHO, STATUS_PERDIDO)
    ]
    logger.info(f"Leads ativos na Recepcao: {len(ativos)}")

    # Classifica por segmento
    grupos: dict[str, list] = {"quentes": [], "mornos": [], "frios": []}
    for lead in ativos:
        dias = _idade_dias(lead)
        seg  = _segmento(dias)
        grupos[seg].append((lead, dias))

    logger.info(
        f"Quentes (0-30d): {len(grupos['quentes'])} | "
        f"Mornos (31-90d): {len(grupos['mornos'])} | "
        f"Frios (+90d): {len(grupos['frios'])}"
    )

    # Segmentos a processar
    alvos = list(grupos.keys()) if segmento == "todos" else [segmento]

    total_enviado = 0
    total_pulado  = 0
    total_sem_tel = 0
    total_erro    = 0

    for seg in alvos:
        lista = grupos[seg]
        logger.info(f"\n── Segmento: {seg.upper()} ({len(lista)} leads) ──")

        for lead, dias in lista:
            # Respeita limite
            if limite and total_enviado >= limite:
                logger.info(f"Limite de {limite} atingido — parando.")
                break

            lead_id   = lead["id"]
            nome_lead = lead.get("name", "")

            # Ja foi reativado?
            if _has_tag(lead, TAG_REATIVACAO):
                logger.info(f"  [PULADO] {lead_id} | {nome_lead} — ja tem tag reativacao")
                total_pulado += 1
                continue

            # Atividade recente (atendimento humano em curso) — não interromper
            if _atividade_recente(lead, horas=24):
                logger.info(f"  [PULADO] {lead_id} | {nome_lead} — atividade nas últimas 24h")
                total_pulado += 1
                continue

            # Busca telefone e nome real do contato via API
            contacts     = (lead.get("_embedded") or {}).get("contacts", [])
            phone        = None
            nome_contato = ""
            for c in contacts:
                phone, nome_contato = _get_contact_phone_and_name(c["id"])
                if phone:
                    break

            # Usa nome do contato se disponível e não genérico, senão usa nome do lead
            nome = nome_contato if nome_contato else nome_lead

            if not phone:
                logger.warning(f"  [SEM TEL] {lead_id} | {nome} — sem telefone, pulando")
                total_sem_tel += 1
                continue

            # Monta mensagem do segmento
            if seg == "quentes":
                msg = msg_quente(nome)
            elif seg == "mornos":
                msg = msg_morno(nome)
            else:
                msg = msg_frio(nome)

            prefixo = "DRY-RUN" if dry_run else "ENVIO"
            logger.info(f"  [{prefixo}] {lead_id} | {nome} | {phone} | {dias} dias")
            logger.info(f"    → {msg[:90]}{'...' if len(msg) > 90 else ''}")

            if not dry_run:
                ok = zapi.send_text(phone, msg)
                if ok:
                    _add_tag(lead_id, TAG_REATIVACAO)
                    logger.info(f"    ✓ Enviado e tag adicionada")
                    total_enviado += 1
                else:
                    logger.error(f"    ✗ Falha no envio — tag NÃO adicionada (lead {lead_id})")
                    total_erro += 1
                time.sleep(DELAY_ENTRE_ENVIOS)
            else:
                total_enviado += 1   # conta como "seria enviado"

    # Resumo final
    logger.info("\n" + "=" * 60)
    logger.info("RESUMO")
    logger.info("=" * 60)
    if dry_run:
        logger.info(f"  Seria enviado : {total_enviado}")
    else:
        logger.info(f"  Enviado       : {total_enviado}")
    logger.info(f"  Pulados       : {total_pulado}  (ja tinham tag reativacao)")
    logger.info(f"  Sem telefone  : {total_sem_tel}")
    if not dry_run:
        logger.info(f"  Erros         : {total_erro}")
    logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Lead Resurrection — Seletos Imoveis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python reativar_leads_recepcao.py --segmento quentes --dry-run
  python reativar_leads_recepcao.py --segmento quentes
  python reativar_leads_recepcao.py --segmento todos --limite 10
        """,
    )
    parser.add_argument(
        "--segmento",
        choices=["quentes", "mornos", "frios", "todos"],
        default="quentes",
        help="Segmento a processar (padrao: quentes)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula sem enviar mensagens nem modificar o Kommo",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Numero maximo de leads a processar nesta execucao",
    )
    args = parser.parse_args()
    run(args.segmento, args.dry_run, args.limite)
