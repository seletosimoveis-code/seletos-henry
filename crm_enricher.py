"""
crm_enricher.py
===============
Enriquecimento silencioso do CRM — inspirado no Leo AiRM (The Real Brokerage).

Como funciona:
  Após qualquer handoff (Henry → Gabriel ou Gabriel → humano), Claude Haiku
  analisa o transcript completo e preenche APENAS os campos vazios do lead no Kommo.
  Também detecta preferências positivas/negativas e as salva como nota estruturada.

  Na próxima ativação do Gabriel, ele lê essas preferências e as usa para
  personalizar sugestões — como o feed do Instagram aprende do que você curte.

Princípios de segurança (imutáveis):
  • NUNCA sobrescreve campo já preenchido no CRM
  • NUNCA inventa dados — extrai apenas o que está EXPLÍCITO na conversa
  • Roda em background (asyncio.to_thread) — não bloqueia resposta ao cliente
  • Token Kommo sempre lido de variável de ambiente (nunca hardcoded)
"""

import json
import logging
import time
import requests
from datetime import datetime, timezone, timedelta
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, KOMMO_SUBDOMAIN, KOMMO_TOKEN
from kommo import (
    F_BAIRRO, F_DORMITORIOS, F_TIPO_IMOVEL, F_URGENCIA,
    F_FORMA_PAGAMENTO, F_IMOVEIS_POTENCIAIS,
    DORM_ENUM, URGENCIA_ENUM,
)

logger  = logging.getLogger(__name__)
_client = Anthropic(api_key=ANTHROPIC_API_KEY)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
_BR_TZ      = timezone(timedelta(hours=-3))
_BASE       = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"


def _hdr():
    return {"Authorization": f"Bearer {KOMMO_TOKEN}", "Content-Type": "application/json"}


# =============================================================================
# PROMPT DE EXTRAÇÃO
# =============================================================================

_EXTRACTION_PROMPT = """\
Você analisa conversas de atendimento imobiliário e extrai dados estruturados.

REGRAS ABSOLUTAS:
1. Extraia APENAS o que o CLIENTE disse explicitamente — nunca o que o bot perguntou
2. Se não tiver certeza sobre um dado, deixe null — melhor null do que dado incorreto
3. Retorne SOMENTE o JSON abaixo, sem texto antes ou depois

CONVERSA:
{transcript}

Retorne exatamente este JSON preenchido:
{{
  "tipo_imovel": null,
  "dormitorios": null,
  "bairro": null,
  "urgencia": null,
  "forma_pagamento": null,
  "preferencias_pos": [],
  "preferencias_neg": []
}}

Guia de preenchimento:
- tipo_imovel: "casa" | "apartamento" | "studio" | "kitnet" | "loft" | "sobrado" (null se não mencionou)
- dormitorios: número inteiro como string "0" (kitnet/studio), "1", "2", "3", "4" (4+ quartos)
  Se cliente disse "2 ou 3", use "2". Kitnet/studio/loft sem quartos → "0"
- bairro: nome exato do bairro mencionado pelo cliente (não pelo bot)
- urgencia: prazo para entrada — escolha UMA opção ou null:
  "imediato" = precisa agora / dentro de 30 dias / urgente
  "curto_prazo" = 1 a 3 meses
  "medio_prazo" = 3 a 6 meses
  "sem_pressa" = sem prazo definido / mais de 6 meses
- forma_pagamento: "Financiamento" | "À vista" | "FGTS" | "Misto" (null se não mencionou)
- preferencias_pos: lista de características que o cliente DEMONSTROU GOSTAR ou EXIGIU
  Exemplos: ["piscina", "andar alto", "2 vagas de garagem", "área de lazer", "varanda"]
  Inclua garagem/vaga AQUI se o cliente mencionou como requisito
- preferencias_neg: lista de características que o cliente REJEITOU ou NÃO QUER
  Exemplos: ["térreo", "sem elevador", "bairro Alecrim (muito longe)", "sem vaga"]

IMPORTANTE: preferencias_pos e preferencias_neg são baseadas EXCLUSIVAMENTE em reações
explícitas do cliente (ex: "não quero", "muito longe", "preciso de", "gostei de", "prefiro não").
Não inclua suposições ou inferências.
"""


# =============================================================================
# HELPERS KOMMO
# =============================================================================

def _fetch_filled_fields(lead_id: int) -> set:
    """
    Retorna set de field_ids que já estão preenchidos no lead.
    Faz uma única chamada GET para evitar sobrescrever dados existentes.
    """
    try:
        r = requests.get(
            f"{_BASE}/leads/{lead_id}",
            headers=_hdr(),
            params={"with": "custom_fields"},
            timeout=10,
        )
        r.raise_for_status()
        filled: set = set()
        for cf in (r.json().get("custom_fields_values") or []):
            fid  = cf.get("field_id")
            vals = cf.get("values", [])
            if not vals or not fid:
                continue
            v   = vals[0].get("value") or ""
            eid = vals[0].get("enum_id")
            if v or eid:
                filled.add(fid)
        return filled
    except Exception as e:
        logger.error(f"CRM enricher: erro ao buscar campos de lead {lead_id}: {e}")
        return set()


def _patch_lead(lead_id: int, fields_payload: list) -> bool:
    """PATCH único com todos os campos novos (minimiza chamadas à API)."""
    if not fields_payload:
        return True
    try:
        r = requests.patch(
            f"{_BASE}/leads/{lead_id}",
            headers=_hdr(),
            json={"custom_fields_values": fields_payload},
            timeout=10,
        )
        if r.ok:
            labels = [str(f.get("field_id")) for f in fields_payload]
            logger.info(
                f"CRM enricher: PATCH lead {lead_id} OK — "
                f"{len(fields_payload)} campos: {', '.join(labels)}"
            )
            return True
        logger.warning(f"CRM enricher: PATCH falhou {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"CRM enricher: erro no PATCH lead {lead_id}: {e}")
        return False


def _post_note(lead_id: int, text: str) -> bool:
    """Adiciona nota ao lead."""
    try:
        r = requests.post(
            f"{_BASE}/leads/notes",
            headers=_hdr(),
            json=[{
                "entity_id"  : lead_id,
                "entity_type": "leads",
                "note_type"  : "common",
                "params"     : {"text": text},
            }],
            timeout=10,
        )
        return r.ok
    except Exception as e:
        logger.error(f"CRM enricher: erro ao postar nota lead {lead_id}: {e}")
        return False


# =============================================================================
# EXTRAÇÃO VIA LLM
# =============================================================================

def _format_transcript(henry_history: list[dict], gabriel_history: list[dict]) -> str:
    """Formata as conversas do Henry e Gabriel em um transcript legível."""
    lines = []
    if henry_history:
        lines.append("=== TRIAGEM (Henry — SDR) ===")
        for m in henry_history:
            label = "Cliente" if m["role"] == "user" else "Henry"
            lines.append(f"{label}: {m['content']}")
    if gabriel_history:
        lines.append("\n=== QUALIFICAÇÃO (Gabriel) ===")
        for m in gabriel_history:
            label = "Cliente" if m["role"] == "user" else "Gabriel"
            lines.append(f"{label}: {m['content']}")
    return "\n".join(lines)


def _extract_via_llm(transcript: str) -> dict:
    """
    Claude Haiku analisa o transcript e retorna dados estruturados.
    Usa Haiku (mais rápido/barato) pois extração é tarefa de baixa complexidade.
    """
    try:
        prompt = _EXTRACTION_PROMPT.replace("{transcript}", transcript[:5000])
        resp = _client.messages.create(
            model      = HAIKU_MODEL,
            max_tokens = 700,
            messages   = [{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()

        # Remove bloco markdown se o modelo insistir em retornar
        if "```" in raw:
            parts = raw.split("```")
            raw   = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json\n"):
                raw = raw[5:]
            elif raw.startswith("json"):
                raw = raw[4:]

        return json.loads(raw.strip())

    except json.JSONDecodeError as e:
        logger.warning(f"CRM enricher: JSON inválido do LLM — {e}")
    except Exception as e:
        logger.error(f"CRM enricher: LLM erro — {e}")
    return {}


# =============================================================================
# PONTO DE ENTRADA PRINCIPAL
# =============================================================================

def enrich_lead_crm(
    phone: str,
    lead_id: int,
    henry_history: list[dict],
    gabriel_history: list[dict],
):
    """
    Enriquece silenciosamente os campos do lead no Kommo após uma conversa.

    Chamado em duas situações:
      1. Após Gabriel fazer handoff → passa henry_history + gabriel_history
      2. Após Henry fazer handoff não-Gabriel (SUPORTE, CORRETOR, etc.) → só henry_history

    Fluxo:
      • Busca campos já preenchidos (1 GET)
      • LLM extrai dados novos da conversa
      • Preenche apenas campos vazios (1 PATCH)
      • Salva nota de preferências comportamentais (1 POST) se houver sinais
    """
    if not lead_id:
        logger.warning(f"[{phone}] CRM enricher: lead_id ausente — abortando")
        return

    transcript = _format_transcript(henry_history, gabriel_history)
    if len(transcript.strip()) < 50:
        logger.info(f"[{phone}] CRM enricher: conversa muito curta — nada a extrair")
        return

    logger.info(
        f"[{phone}] CRM enricher iniciando para lead {lead_id} "
        f"({len(transcript)} chars, Henry={len(henry_history)} msgs, Gabriel={len(gabriel_history)} msgs)"
    )

    # 1. Campos já preenchidos (não serão sobrescritos)
    filled_ids = _fetch_filled_fields(lead_id)

    # 2. Extração via LLM
    extracted = _extract_via_llm(transcript)
    if not extracted:
        return

    logger.info(
        f"[{phone}] CRM enricher extraiu: "
        f"{json.dumps({k: v for k, v in extracted.items() if v and k not in ('preferencias_pos', 'preferencias_neg')}, ensure_ascii=False)}"
    )

    # 3. Monta payload PATCH com apenas os campos vazios
    fields_payload = []

    # Bairro (text)
    if extracted.get("bairro") and F_BAIRRO not in filled_ids:
        fields_payload.append({
            "field_id": F_BAIRRO,
            "values"  : [{"value": extracted["bairro"]}],
        })

    # Tipo de Imóvel (text)
    if extracted.get("tipo_imovel") and F_TIPO_IMOVEL not in filled_ids:
        fields_payload.append({
            "field_id": F_TIPO_IMOVEL,
            "values"  : [{"value": extracted["tipo_imovel"]}],
        })

    # Forma de Pagamento (select)
    _PAGAMENTO_MAP = {
        "financiamento": "Financiamento",
        "à vista"      : "À vista",
        "a vista"      : "À vista",
        "fgts"         : "FGTS",
        "misto"        : "Misto",
    }
    if extracted.get("forma_pagamento") and F_FORMA_PAGAMENTO not in filled_ids:
        pag = extracted["forma_pagamento"].lower()
        pag_val = _PAGAMENTO_MAP.get(pag) or extracted["forma_pagamento"]
        fields_payload.append({
            "field_id": F_FORMA_PAGAMENTO,
            "values"  : [{"value": pag_val}],
        })

    # Urgência (select — usa URGENCIA_ENUM)
    if extracted.get("urgencia") and F_URGENCIA not in filled_ids:
        eid = URGENCIA_ENUM.get(extracted["urgencia"])
        if eid:
            fields_payload.append({
                "field_id": F_URGENCIA,
                "values"  : [{"enum_id": eid}],
            })

    # Dormitórios (select — usa DORM_ENUM; inclui 0 = kitnet/studio)
    if extracted.get("dormitorios") is not None and F_DORMITORIOS not in filled_ids:
        try:
            d_raw = str(extracted["dormitorios"]).split("-")[0].strip()
            d     = max(0, min(int(d_raw), 4))
            eid   = DORM_ENUM.get(d)
            if eid:
                fields_payload.append({
                    "field_id": F_DORMITORIOS,
                    "values"  : [{"enum_id": eid}],
                })
        except Exception:
            pass

    # 4. PATCH único (minimiza chamadas à API)
    if fields_payload:
        _patch_lead(lead_id, fields_payload)
    else:
        logger.info(f"[{phone}] CRM enricher: todos os campos já estavam preenchidos")

    # 5. Preferências comportamentais → nota no Kommo
    #    Estas notas são lidas pelo Gabriel na próxima conversa (aprendizado comportamental)
    prefs_pos = [p for p in (extracted.get("preferencias_pos") or []) if p]
    prefs_neg = [n for n in (extracted.get("preferencias_neg") or []) if n]

    if prefs_pos or prefs_neg:
        now_str   = datetime.now(_BR_TZ).strftime("%d/%m/%Y %H:%M")
        nota_linhas = [f"🧠 PREFERÊNCIAS DO CLIENTE — {now_str}"]

        if prefs_pos:
            nota_linhas.append("✅ Gostou / Prefere:")
            for p in prefs_pos:
                nota_linhas.append(f"   + {p}")

        if prefs_neg:
            nota_linhas.append("❌ Não quer / Rejeitou:")
            for n in prefs_neg:
                nota_linhas.append(f"   - {n}")

        nota_linhas += [
            "",
            "📌 Detectado automaticamente pela IA — usar para personalizar próximas sugestões.",
            "   Gabriel lê este histórico ao ser ativado para não sugerir o que o cliente rejeitou.",
        ]

        if _post_note(lead_id, "\n".join(nota_linhas)):
            logger.info(
                f"[{phone}] CRM enricher: nota de preferências salva "
                f"(+{len(prefs_pos)} pos, -{len(prefs_neg)} neg)"
            )

    logger.info(f"[{phone}] CRM enricher concluído — lead {lead_id}")
