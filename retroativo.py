"""
retroativo.py
=============
Revisão silenciosa (Retroativo Fase A) — enriquecimento em massa de leads antigos.

O que faz (pedido do Felipe, 07/07/2026):
  Varre TODOS os leads ativos dos funis Aluguel e Avulso, lê as notas/históricos
  de conversa que já existem no Kommo, extrai dados via Claude Haiku e preenche
  os campos vazios do cadastro (+ orçamento no price). 100% silencioso — nenhuma
  mensagem é enviada a cliente. Leads sem histórico útil viram a lista de
  candidatos da Fase B (revalidação ativa pelo Gabriel).

Segurança:
  • NUNCA sobrescreve campo preenchido (herda regras do crm_enricher)
  • price < JUNK_PRICE_MAX (lixo do Canal Pro) é tratado como vazio
  • dry_run=True (padrão): só SIMULA e reporta — nada é gravado
  • Uma execução por vez; progresso e resultado ficam no store

Uso (endpoints em main.py):
  GET /admin/retroativo?dry_run=true          → simula e reporta
  GET /admin/retroativo?dry_run=false         → executa de verdade
  GET /admin/retroativo/status                → progresso/resultado
"""

import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta

from config import KOMMO_SUBDOMAIN, KOMMO_TOKEN
from kommo import (
    PIPE_ALUGUEL, PIPE_AVULSO, PIPE_RECEPCAO,
    get_pipe_cadencia, get_pipe_captacao, get_entry_status,
    canon_phone,
)
from crm_enricher import (
    _extract_via_llm, _patch_lead, build_fields_payload,
    maybe_set_price, JUNK_PRICE_MAX,
)
import store

logger = logging.getLogger(__name__)

_BR_TZ = timezone(timedelta(hours=-3))
_BASE  = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"

STATUS_GANHO   = 142
STATUS_PERDIDO = 143

# Campos-núcleo: lead com todos preenchidos + price válido é pulado
F_TIPO   = 1312432
F_BAIRRO = 1312436
F_DORM   = 1328592
F_URG    = 1328582

_running = False
_zapi    = None


def init(zapi) -> None:
    global _zapi
    _zapi = zapi


def is_running() -> bool:
    return _running


def status() -> dict:
    st = store.all_state("retroativo_status").get("global")
    return st if isinstance(st, dict) else {"status": "nunca executado"}


def _hdr():
    return {"Authorization": f"Bearer {KOMMO_TOKEN}", "Content-Type": "application/json"}


def _set_status(**kw):
    st = status()
    if not isinstance(st, dict):
        st = {}
    st.update(kw)
    store.set_state("global", "retroativo_status", st)


# ─── Coleta ───────────────────────────────────────────────────────────────────

def _paginate_leads(params_base: dict, keep) -> list[dict]:
    leads, page = [], 1
    while True:
        try:
            r = requests.get(
                f"{_BASE}/leads", headers=_hdr(),
                params={**params_base, "limit": 250, "page": page},
                timeout=20,
            )
            if r.status_code == 204:
                break
            r.raise_for_status()
            batch = r.json().get("_embedded", {}).get("leads", [])
        except Exception as e:
            logger.error(f"retroativo: erro ao listar leads (p{page}): {e}")
            break
        if not batch:
            break
        leads.extend(ld for ld in batch if keep(ld))
        if len(batch) < 250:
            break
        page += 1
    return leads


def _pipes_de_cliente() -> list[int]:
    """
    Todos os pipelines de CLIENTE (exclui internos: Recepção — tem escopo próprio —,
    Equipe/Corretores, Financeiro, Manutenção, Fornecedores).
    """
    EXCLUIR = ("recep", "equipe", "corretor", "financeiro", "manuten", "fornecedor")
    pipes = []
    try:
        r = requests.get(f"{_BASE}/leads/pipelines", headers=_hdr(), timeout=15)
        r.raise_for_status()
        for p in r.json().get("_embedded", {}).get("pipelines", []):
            nome = (p.get("name") or "").lower()
            if any(t in nome for t in EXCLUIR):
                continue
            pipes.append(p["id"])
    except Exception as e:
        logger.error(f"retroativo: erro ao listar pipelines de cliente: {e}")
        pipes = [PIPE_ALUGUEL, PIPE_AVULSO]   # fallback mínimo
    return pipes


def _fetch_leads(escopo: str = "ativos") -> list[dict]:
    """
    escopo:
      'ativos'   → leads ativos de Aluguel + Avulso
      'todos'    → leads ativos de TODOS os funis de cliente (Aluguel, Avulso,
                   Captação, Lançamentos, Investidor, Cadências, Avaliação etc.)
      'perdidos' → leads em 'Venda perdida' (143) de Aluguel + Avulso
                   (inclui os 602 fechados em massa pela automação de 05/07)
      'recepcao' → leads ativos parados na Recepção (balcão/entrada)
    """
    if escopo == "todos":
        leads = []
        for pipe in _pipes_de_cliente():
            leads += _paginate_leads(
                {"filter[pipeline_id]": pipe},
                keep=lambda ld: ld.get("status_id") not in (STATUS_GANHO, STATUS_PERDIDO),
            )
        return leads

    if escopo == "perdidos":
        leads = []
        for pipe in (PIPE_ALUGUEL, PIPE_AVULSO):
            leads += _paginate_leads(
                {
                    "filter[statuses][0][pipeline_id]": pipe,
                    "filter[statuses][0][status_id]"  : STATUS_PERDIDO,
                },
                keep=lambda ld: True,
            )
        return leads

    if escopo == "recepcao":
        return _paginate_leads(
            {"filter[pipeline_id]": PIPE_RECEPCAO},
            keep=lambda ld: ld.get("status_id") not in (STATUS_GANHO, STATUS_PERDIDO),
        )

    leads = []
    for pipe in (PIPE_ALUGUEL, PIPE_AVULSO):
        leads += _paginate_leads(
            {"filter[pipeline_id]": pipe},
            keep=lambda ld: ld.get("status_id") not in (STATUS_GANHO, STATUS_PERDIDO),
        )
    return leads


def _precisa_revisao(lead: dict) -> tuple[bool, set]:
    """(precisa?, set de field_ids já preenchidos)."""
    filled = set()
    for cf in (lead.get("custom_fields_values") or []):
        vals = cf.get("values") or []
        if vals and (vals[0].get("value") or vals[0].get("enum_id")):
            filled.add(cf.get("field_id"))
    price_ok = int(lead.get("price") or 0) >= JUNK_PRICE_MAX
    core_ok  = all(f in filled for f in (F_TIPO, F_BAIRRO, F_DORM, F_URG))
    return not (core_ok and price_ok), filled


def _transcript_from_notes(lead_id: int) -> str:
    """Concatena o texto das notas do lead (histórico de conversas dos bots)."""
    try:
        r = requests.get(
            f"{_BASE}/leads/{lead_id}/notes", headers=_hdr(),
            params={"limit": 100, "order[id]": "asc"},
            timeout=15,
        )
        if r.status_code == 204:
            return ""
        r.raise_for_status()
        textos = []
        for note in r.json().get("_embedded", {}).get("notes", []):
            t = (note.get("params") or {}).get("text", "")
            if t and len(t) > 30:
                textos.append(t)
        full = "\n\n".join(textos)
        return full[-6000:]   # últimas conversas pesam mais
    except Exception as e:
        logger.warning(f"retroativo: notas do lead {lead_id}: {e}")
        return ""


# ─── Movimentação segura de lead ──────────────────────────────────────────────

def _move_lead(lead_id: int, pipe_destino: int, status_id: int | None = None) -> bool:
    """
    Move lead para o pipeline destino (padrão verificado com status_id explícito).
    status_id=None → usa a etapa de entrada do pipeline.
    """
    try:
        alvo = status_id or get_entry_status(pipe_destino)
        if not alvo:
            raise RuntimeError(f"status destino do pipeline {pipe_destino} indisponível")
        r = requests.patch(
            f"{_BASE}/leads", headers=_hdr(),
            json=[{"id": lead_id, "pipeline_id": pipe_destino, "status_id": alvo}],
            timeout=15,
        )
        r.raise_for_status()
        atualizados = r.json().get("_embedded", {}).get("leads", [])
        return any(l.get("id") == lead_id for l in atualizados)
    except Exception as e:
        logger.error(f"retroativo: falha ao mover lead {lead_id} → {pipe_destino}: {e}")
        return False


F_MOTIVO = 1307202   # Motivo da Busca (preenchido pelo Henry)

_INTERESSE_DESTINO = {
    "alugar" : ("aluguel",  lambda: PIPE_ALUGUEL),
    "comprar": ("avulso",   lambda: PIPE_AVULSO),
    "vender" : ("captacao", get_pipe_captacao),
}


def _sugestao_funil(lead: dict, extracted: dict) -> tuple[str, int | None]:
    """('aluguel'|'avulso'|'captacao'|'', pipe_id|None) para leads da Recepção."""
    motivo = ""
    for cf in (lead.get("custom_fields_values") or []):
        if cf.get("field_id") == F_MOTIVO and cf.get("values"):
            motivo = str(cf["values"][0].get("value", "")).lower()
            break
    if "loca" in motivo or "alug" in motivo:
        return "aluguel", PIPE_ALUGUEL
    if "compra" in motivo or "venda" in motivo and "propriet" not in motivo:
        return "avulso", PIPE_AVULSO
    if "propriet" in motivo or "capta" in motivo:
        return "captacao", get_pipe_captacao()

    interesse = str(extracted.get("interesse") or "").lower()
    if interesse in _INTERESSE_DESTINO:
        nome, pipe_fn = _INTERESSE_DESTINO[interesse]
        return nome, pipe_fn()
    return "", None


# ─── Execução ─────────────────────────────────────────────────────────────────

def run(dry_run: bool = True, limit: int = 0, escopo: str = "ativos") -> None:
    """Roda a revisão silenciosa. Chamado em thread pelo main.py."""
    global _running
    if _running:
        logger.warning("retroativo: já em execução — ignorado")
        return

    # Recepção em modo real move leads → ativa Gabriel proativo (mensagens!).
    # Regra do Felipe: contato ativo só depois das 9h, e em lotes.
    if escopo == "recepcao" and not dry_run:
        agora = datetime.now(_BR_TZ)
        if agora.hour < 9 or agora.weekday() == 6:
            _set_status(status="abortado",
                        motivo="Recepção em modo real só após 9h (seg–sáb) — regra de contato ativo")
            logger.warning("retroativo recepção real abortado — fora da janela")
            return
        if not limit:
            limit = 15   # lote padrão de segurança

    _running = True
    inicio = time.time()
    modo   = f"{escopo.upper()} — " + ("SIMULAÇÃO (dry-run)" if dry_run else "EXECUÇÃO REAL")
    logger.info(f"retroativo: iniciando — {modo}")
    _set_status(status="rodando", modo=modo, inicio=datetime.now(_BR_TZ).strftime("%d/%m %H:%M"))

    stats = {
        "varridos": 0, "ja_completos": 0, "sem_historico": 0,
        "enriquecidos": 0, "campos_gravados": 0, "prices_gravados": 0,
        "erros": 0, "movidos": 0,
    }
    candidatos_fase_b: list = []
    amostras: list = []
    sugestoes = {"aluguel": [], "avulso": [], "captacao": []}

    try:
        leads = _fetch_leads(escopo)
        if limit:
            leads = leads[:limit]
        _set_status(total=len(leads))

        for i, lead in enumerate(leads, 1):
            stats["varridos"] += 1
            lead_id = lead.get("id")
            nome    = lead.get("name") or f"#{lead_id}"
            try:
                precisa, filled = _precisa_revisao(lead)
                if not precisa and escopo != "recepcao":
                    stats["ja_completos"] += 1
                    continue

                transcript = _transcript_from_notes(lead_id)
                if len(transcript.strip()) < 80:
                    # Recepção: mesmo sem conversa, o campo Motivo (Henry/portal)
                    # pode bastar para rotear o lead ao funil certo
                    if escopo == "recepcao":
                        nome_funil, pipe_destino = _sugestao_funil(lead, {})
                        if nome_funil and pipe_destino:
                            sugestoes[nome_funil].append(f"{lead_id} · {nome}")
                            if not dry_run and _move_lead(lead_id, pipe_destino):
                                stats["movidos"] += 1
                                time.sleep(1.5)
                            continue
                    stats["sem_historico"] += 1
                    candidatos_fase_b.append(f"{lead_id} · {nome}")
                    continue

                extracted = _extract_via_llm(transcript)
                if not extracted:
                    stats["erros"] += 1
                    continue

                # ── Recepção: classifica e (modo real) move para o funil certo ─
                if escopo == "recepcao":
                    nome_funil, pipe_destino = _sugestao_funil(lead, extracted)
                    if nome_funil and pipe_destino:
                        sugestoes[nome_funil].append(f"{lead_id} · {nome}")
                        if not dry_run and _move_lead(lead_id, pipe_destino):
                            stats["movidos"] += 1
                            time.sleep(1.5)   # espaça ativações do Gabriel
                    else:
                        candidatos_fase_b.append(f"{lead_id} · {nome}")

                payload = build_fields_payload(extracted, filled, include_data_entrada=False)
                price_atual = int(lead.get("price") or 0)
                vai_gravar_price = bool(
                    extracted.get("orcamento") and price_atual < JUNK_PRICE_MAX
                )

                if payload or vai_gravar_price:
                    stats["enriquecidos"]    += 1
                    stats["campos_gravados"] += len(payload)
                    if vai_gravar_price:
                        stats["prices_gravados"] += 1
                    if len(amostras) < 12:
                        resumo = {k: v for k, v in extracted.items()
                                  if v and k not in ("preferencias_pos", "preferencias_neg")}
                        amostras.append(f"{lead_id} · {nome}: {json.dumps(resumo, ensure_ascii=False)[:150]}")
                    if not dry_run:
                        _patch_lead(lead_id, payload)
                        maybe_set_price(lead_id, extracted, price_atual, phone=str(lead_id))
                else:
                    stats["sem_historico"] += 1
                    candidatos_fase_b.append(f"{lead_id} · {nome}")

                time.sleep(0.35)   # gentileza com as APIs
            except Exception as e:
                stats["erros"] += 1
                logger.error(f"retroativo: lead {lead_id}: {e}")

            if i % 25 == 0:
                _set_status(progresso=f"{i}/{len(leads)}", **stats)

    finally:
        _running = False

    dur = int((time.time() - inicio) / 60)
    linhas = [
        f"🧹 REVISÃO SILENCIOSA — {modo} concluída ({dur} min)",
        f"Varridos: {stats['varridos']} leads",
        f"• Já completos: {stats['ja_completos']}",
        f"• Enriquecidos{' (simulado)' if dry_run else ''}: {stats['enriquecidos']} "
        f"→ {stats['campos_gravados']} campos + {stats['prices_gravados']} orçamentos",
        f"• Sem histórico útil (candidatos Fase B): {stats['sem_historico']}",
        f"• Erros: {stats['erros']}",
    ]
    if escopo == "recepcao":
        linhas += [
            "",
            "📍 CLASSIFICAÇÃO DA RECEPÇÃO:",
            f"• Aptos para Aluguel: {len(sugestoes['aluguel'])}",
            f"• Aptos para Avulso (compra): {len(sugestoes['avulso'])}",
            f"• Aptos para Captação: {len(sugestoes['captacao'])}",
            f"• Sem dados → Fase B (Gabriel revalida): {len(candidatos_fase_b)}",
            f"• Movidos nesta execução: {stats['movidos']}",
        ]
    linhas += [
        "",
        "Amostras do que foi extraído:" if amostras else "",
        *[f"  - {a}" for a in amostras],
        "",
        ("👉 Rode com dry_run=false para gravar/mover de verdade."
         if dry_run else "✅ Concluído. Confira o radar: /admin/demandas"),
    ]
    relatorio = "\n".join(linhas)
    _set_status(status="concluído", relatorio=relatorio,
                candidatos_fase_b=candidatos_fase_b[:150],
                sugestoes={k: v[:80] for k, v in sugestoes.items()},
                **stats)
    logger.info(f"retroativo: {relatorio}")

    if _zapi:
        try:
            from demandas import REPORT_PHONE
            if REPORT_PHONE:
                _zapi.send_text(REPORT_PHONE, relatorio[:3500])
        except Exception as e:
            logger.warning(f"retroativo: envio do relatório falhou: {e}")


# ─── Migração dos perdidos → funis de Cadência (em lotes) ─────────────────────

def migrar_perdidos(batch: int = 40, dry_run: bool = True, destino: str = "demanda") -> dict:
    """
    Resgata leads 'Venda perdida' (Aluguel/Avulso), em lotes.

    destino='demanda' (padrão — decisão Felipe 07/07): devolve cada lead para a
      etapa "DEmanda | Procura de imóvel" DO SEU PRÓPRIO funil (Aluguel e Avulso).
      Desfaz o estrago da automação de 480h e reativa o radar de captação.
      ⚠️ PRÉ-REQUISITO: a automação 480h da DEmanda precisa estar DESATIVADA,
      senão os leads devolvidos morrem de novo em 20 dias.
      ⚠️ A DEmanda tem o robô CAD1 (dispara ~1 dia após entrada) → lotes
      obrigatórios para não gerar rajada de mensagens.

    destino='cadencia': envia para os funis de Cadência por faixa
      (Aluguel → Locação; Avulso → ≤300k / 301–600k / >600k pelo price).
    """
    agora = datetime.now(_BR_TZ)
    if not dry_run and (agora.hour < 9 or agora.weekday() == 6):
        return {"status": "abortado", "motivo": "migração real só após 9h (seg–sáb)"}

    # Etapas DEmanda dos dois funis (descoberta dinâmica por nome)
    demanda_por_pipe: dict[int, int] = {}
    if destino == "demanda":
        from demandas import _demanda_statuses
        demanda_por_pipe = {pid: sid for pid, sid, _fin in _demanda_statuses()}
        if not demanda_por_pipe:
            return {"status": "abortado", "motivo": "etapas DEmanda não encontradas"}

    leads = _fetch_leads("perdidos")
    stats = {"perdidos_totais": len(leads), "no_lote": 0, "movidos": 0,
             "sem_funil_destino": 0, "erros": 0}
    destinos: dict = {}

    for lead in leads[:batch]:
        stats["no_lote"] += 1
        lead_id = lead.get("id")
        pipe    = lead.get("pipeline_id")
        price   = int(lead.get("price") or 0)
        if price < JUNK_PRICE_MAX:
            price = 0

        if destino == "demanda":
            status_alvo = demanda_por_pipe.get(pipe)
            if not status_alvo:
                stats["sem_funil_destino"] += 1
                continue
            label = ("DEmanda Aluguel" if pipe == PIPE_ALUGUEL else "DEmanda Avulso")
            destinos[label] = destinos.get(label, 0) + 1
            if not dry_run:
                if _move_lead(lead_id, pipe, status_id=status_alvo):
                    stats["movidos"] += 1
                    time.sleep(1.0)
                else:
                    stats["erros"] += 1
            continue

        # destino == 'cadencia'
        if pipe == PIPE_ALUGUEL:
            pipe_dest, label = get_pipe_cadencia("locacao"), "Cadência Locação"
        else:
            if price and price <= 300_000:
                pipe_dest, label = get_pipe_cadencia("ate300"), "Cadência Até 300"
            elif price > 600_000:
                pipe_dest, label = get_pipe_cadencia("acima600"), "Cadência Acima 600"
            else:
                pipe_dest, label = get_pipe_cadencia("301a600"), "Cadência 301-600"

        if not pipe_dest:
            stats["sem_funil_destino"] += 1
            continue

        destinos[label] = destinos.get(label, 0) + 1
        if not dry_run:
            if _move_lead(lead_id, pipe_dest):
                stats["movidos"] += 1
                time.sleep(1.0)   # espaça entradas na cadência
            else:
                stats["erros"] += 1

    resultado = {
        "status"  : "simulação" if dry_run else "executado",
        "destinos": destinos,
        **stats,
        "restantes_apos_lote": max(0, stats["perdidos_totais"] - batch),
        "dica": ("Funis de cadência ausentes não recebem leads — crie-os na UI "
                 "com os nomes do blueprint." if stats["sem_funil_destino"] else
                 "Rode novamente para o próximo lote."),
    }
    store.set_state("global", "retroativo_migracao", resultado)
    logger.info(f"retroativo migração: {resultado}")
    return resultado


# ─── Relatório de leads DUPLICADOS (mesmo telefone, 2+ leads ativos) ──────────

def relatorio_duplicados() -> dict:
    """
    Agrupa leads ATIVOS por telefone do contato e lista os grupos com 2+ leads.
    Duplicata infla o Radar de Demandas e polui os lotes de segunda-feira.
    SÓ LEITURA — o merge é feito na interface do Kommo (preserva histórico).
    """
    # 1. Mapa de leads ativos (funis de cliente + Recepção) com nome do funil
    nomes_pipes: dict[int, str] = {}
    try:
        r = requests.get(f"{_BASE}/leads/pipelines", headers=_hdr(), timeout=15)
        r.raise_for_status()
        for p in r.json().get("_embedded", {}).get("pipelines", []):
            nomes_pipes[p["id"]] = p.get("name", str(p["id"]))
    except Exception as e:
        logger.error(f"duplicados: pipelines: {e}")

    pipes_alvo = set(_pipes_de_cliente()) | {PIPE_RECEPCAO}
    lead_info: dict[int, dict] = {}
    for pipe in pipes_alvo:
        for ld in _paginate_leads(
            {"filter[pipeline_id]": pipe},
            keep=lambda l: l.get("status_id") not in (STATUS_GANHO, STATUS_PERDIDO),
        ):
            lead_info[ld["id"]] = {
                "lead_id": ld["id"],
                "nome"   : ld.get("name") or f"#{ld['id']}",
                "funil"  : nomes_pipes.get(ld.get("pipeline_id"), "?"),
            }

    # 2. Varre contatos e agrupa leads ativos por telefone canônico
    por_fone: dict[str, set] = {}
    page = 1
    while page <= 30:
        try:
            r = requests.get(
                f"{_BASE}/contacts", headers=_hdr(),
                params={"limit": 250, "page": page, "with": "leads"},
                timeout=25,
            )
            if r.status_code == 204:
                break
            r.raise_for_status()
            contatos = r.json().get("_embedded", {}).get("contacts", [])
        except Exception as e:
            logger.error(f"duplicados: contatos p{page}: {e}")
            break
        if not contatos:
            break
        for c in contatos:
            fone = ""
            for cf in (c.get("custom_fields_values") or []):
                if cf.get("field_code") in ("PHONE", "TEL") and cf.get("values"):
                    fone = canon_phone(str(cf["values"][0].get("value", "")))
                    break
            if not fone:
                continue
            ativos = {
                s["id"] for s in ((c.get("_embedded") or {}).get("leads") or [])
                if s.get("id") in lead_info
            }
            if ativos:
                por_fone.setdefault(fone, set()).update(ativos)
        if len(contatos) < 250:
            break
        page += 1

    # 3. Grupos com 2+ leads ativos = duplicados
    grupos = []
    for fone, ids in por_fone.items():
        if len(ids) >= 2:
            grupos.append({
                "telefone": fone,
                "qtd"     : len(ids),
                "leads"   : sorted(
                    (lead_info[i] for i in ids), key=lambda x: x["lead_id"]
                ),
            })
    grupos.sort(key=lambda g: g["qtd"], reverse=True)

    resultado = {
        "leads_ativos_varridos" : len(lead_info),
        "telefones_com_lead"    : len(por_fone),
        "grupos_duplicados"     : len(grupos),
        "leads_envolvidos"      : sum(g["qtd"] for g in grupos),
        "como_resolver"         : "Merge pela interface do Kommo (preserva histórico/notas). "
                                  "Mantenha o lead mais avançado no funil; funda os demais nele.",
        "grupos"                : grupos[:80],
    }
    store.set_state("global", "retroativo_duplicados", resultado)
    logger.info(
        f"duplicados: {len(grupos)} grupos, {resultado['leads_envolvidos']} leads envolvidos"
    )
    return resultado


# ─── Realocação: leads no funil ERRADO (Motivo da Busca × pipeline) ───────────

def realocar_desalinhados(batch: int = 20, dry_run: bool = True) -> dict:
    """
    Detecta e corrige leads ativos cujo Motivo da Busca (preenchido pela revisão
    silenciosa) conflita com o funil onde estão. Exemplo real: Rafael #30602358,
    quer COMPRAR ponto comercial (R$ 1,5M) mas está no funil Aluguel.

      Aluguel + motivo Compra        → move para Avulso
      Avulso  + motivo Locação       → move para Aluguel
      Aluguel/Avulso + Proprietário  → move para Captação

    ⚠️ Mover ativa o Gabriel PROATIVAMENTE no funil novo (mensagem ao cliente!)
    → modo real só na janela 9h+ seg–sáb, em lotes, com supervisão (segunda-feira).
    dry_run lista os conflitos para validação humana.
    """
    agora = datetime.now(_BR_TZ)
    if not dry_run and (agora.hour < 9 or agora.weekday() == 6):
        return {"status": "abortado", "motivo": "realocação real só após 9h (seg–sáb)"}

    conflitos = []
    for lead in _fetch_leads("ativos"):
        motivo = ""
        for cf in (lead.get("custom_fields_values") or []):
            if cf.get("field_id") == F_MOTIVO and cf.get("values"):
                motivo = str(cf["values"][0].get("value", "")).lower()
                break
        if not motivo:
            continue
        pipe = lead.get("pipeline_id")
        destino, destino_label = None, ""
        if "propriet" in motivo or "capta" in motivo:
            destino, destino_label = get_pipe_captacao(), "Captação"
        elif pipe == PIPE_ALUGUEL and "compra" in motivo:
            destino, destino_label = PIPE_AVULSO, "Avulso (compra)"
        elif pipe == PIPE_AVULSO and ("loca" in motivo or "alug" in motivo):
            destino, destino_label = PIPE_ALUGUEL, "Aluguel"
        if destino and destino != pipe:
            conflitos.append({
                "lead_id": lead.get("id"),
                "nome"   : lead.get("name"),
                "de"     : "Aluguel" if pipe == PIPE_ALUGUEL else "Avulso",
                "para"   : destino_label,
                "motivo" : motivo,
                "_pipe"  : destino,
            })

    movidos, erros = 0, 0
    if not dry_run:
        for c in conflitos[:batch]:
            if _move_lead(c["lead_id"], c["_pipe"]):
                movidos += 1
                time.sleep(2)   # espaça ativações do Gabriel
            else:
                erros += 1

    resultado = {
        "status"          : "simulação" if dry_run else "executado",
        "conflitos_totais": len(conflitos),
        "movidos"         : movidos,
        "erros"           : erros,
        "lista"           : [{k: v for k, v in c.items() if k != "_pipe"}
                             for c in conflitos[:60]],
    }
    store.set_state("global", "retroativo_realocacao", resultado)
    logger.info(f"retroativo realocação: {resultado['status']} — "
                f"{len(conflitos)} conflitos, {movidos} movidos")
    return resultado
