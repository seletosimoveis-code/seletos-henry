"""
kommo.py
========
Cliente Kommo para busca de leads por telefone, atualização de campos
e registro de notas/tarefas após handoff do Henry (bot).

IDs verificados em 2026-06-26 via listar_campos.py.
"""

import os
import re
import time
import logging
import requests
from config import KOMMO_SUBDOMAIN, KOMMO_TOKEN

logger = logging.getLogger(__name__)

BASE = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"


def _hdr():
    return {"Authorization": f"Bearer {KOMMO_TOKEN}", "Content-Type": "application/json"}


def _norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("55") and len(digits) > 11:
        digits = digits[2:]
    return digits[-11:] if len(digits) >= 10 else digits


def canon_phone(raw: str) -> str:
    """
    Chave CANÔNICA de telefone BR — usada como chave de estado dos bots e para envio Z-API.
    Formato: 55 + DDD + número local SEM o 9º dígito extra (12 dígitos).

    Unifica os formatos que chegam pelos dois canais:
      Z-API : '558496078130' ou '5584996078130'
      Kommo : '+55 84 96078-130' ou '+55 84 9 9607-8130'
    Todos viram '558496078130'. Sem isso, cada canal cria uma conversa
    paralela para a mesma pessoa (bug da dupla saudação de 04/07/2026).
    """
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("55") and len(digits) > 11:
        digits = digits[2:]
    # DDD + 9 dígitos → remove o nono dígito (celular BR)
    if len(digits) == 11 and digits[2] == "9":
        digits = digits[:2] + digits[3:]
    if 10 <= len(digits) <= 11:
        return "55" + digits
    return digits


# ─── Números da equipe — NENHUM robô interage com eles ────────────────────────
# Env EQUIPE_PHONES: números separados por vírgula (com ou sem 9º dígito/DDI).
# Evita que testes internos virem "leads": sem Henry, sem Gabriel, sem follow-up,
# sem Fase B. Para testar de propósito, remova o número da variável no Railway.
EQUIPE_PHONES: set = {
    canon_phone(p) for p in os.getenv("EQUIPE_PHONES", "").split(",") if p.strip()
}


def is_equipe_phone(phone: str) -> bool:
    return bool(EQUIPE_PHONES) and canon_phone(phone) in EQUIPE_PHONES


# ─── IDs dos campos customizados ──────────────────────────────────────────────
# Campos select
F_CANAL_ORIGEM      = 1328586   # select — Canal de Origem
F_URGENCIA          = 1328582   # select — Urgência
F_DORMITORIOS       = 1328592   # select — N. de Dormitórios
F_IMOVEL_ATUAL      = 1328838   # select — Imóvel Atual
F_FINALIDADE        = 1328636   # select — Finalidade
F_IMOVEL_VENDER     = 1328840   # select — Tem Imóvel para Vender
F_PRE_APROVADO      = 1328596   # select — Pré-aprovado
F_SCORE             = 1328842   # select — Score de Qualificação
F_TIPO_IMOVEL_SEL   = 1328612   # select — Tipo de Imóvel (select)
F_FORMA_PAGAMENTO   = 1328606   # select — Forma de Pagamento

# Campos select (por funil — Fase 1: Aluguel/Compra)
F_ACEITA_ANIMAIS    = 1328602   # select — Aceita animais (aluguel)
F_TIPO_GARANTIA     = 1328604   # select — Tipo de Garantia (aluguel)

# Campos text
F_BAIRRO             = 1312436   # text — Bairros Preferência  ← CORRIGIDO (era 1328594)
F_MOTIVO_BUSCA       = 1307202   # text — Motivo da Busca
F_IMOVEL_ORIG        = 1312438   # text — Imóvel de Origem
F_TIPO_IMOVEL        = 1312432   # text — Tipo de Imóvel (texto livre)
F_IMOVEIS_POTENCIAIS = 1328598   # text — Imóveis Potenciais
F_ENTRADA_DISPONIVEL = 1328638   # text — Entrada Disponível (compra/lançamentos)

# Campos data
F_DATA_ENTRADA       = 1328600   # date — Data de Entrada (aluguel)

# ─── Enum IDs ─────────────────────────────────────────────────────────────────
# Canal de Origem
CANAL_ENUM = {
    "canal_pro"    : 1110898,
    "whatsapp"     : 1110900,
    "indicacao"    : 1110902,
    "site"         : 1110904,
    "redes_sociais": 1110906,
    "evento"       : 1110908,
    "outro"        : 1110910,
}

# N. de Dormitórios
DORM_ENUM = {
    0: 1110912,   # Kitnet/Studio
    1: 1110914,
    2: 1110916,
    3: 1110918,
    4: 1110920,   # 4+
}

# Pré-aprovado
PRE_APROVADO_ENUM = {
    "sim"        : 1110922,
    "em_processo": 1110924,
    "nao"        : 1110926,
}

# Urgência
URGENCIA_ENUM = {
    "imediato"   : 1110872,
    "curto_prazo": 1110874,
    "medio_prazo": 1110876,
    "sem_pressa" : 1110878,
}

# Imóvel Atual
IMOVEL_ATUAL_ENUM = {
    "alugado": 1111542,
    "proprio": 1111544,
    "familia": 1111546,
    "outro"  : 1111548,
}

# Tem Imóvel para Vender
IMOVEL_VENDER_ENUM = {
    "sim_vendido"    : 1111550,
    "sim_nao_vendido": 1111552,
    "nao"            : 1111554,
}

# Score de Qualificação
SCORE_ENUM = {
    "quente": 1111556,
    "morno" : 1111558,
    "frio"  : 1111560,
}

# Aceita animais (aluguel)
ANIMAIS_ENUM = {
    "sim": 1110928,
    "nao": 1110930,
}

# Tipo de Garantia (aluguel)
GARANTIA_ENUM = {
    "fiador"              : 1110932,
    "seguro_fianca"       : 1110934,
    "deposito"            : 1110936,   # caução
    "titulo_capitalizacao": 1110938,
    "a_definir"           : 1110940,
}

# Finalidade (compra/lançamentos)
FINALIDADE_ENUM = {
    "moradia"     : 1111012,
    "investimento": 1111014,
    "a_definir"   : 1111016,
}

# Tipo de Imóvel (select)
TIPO_IMOVEL_SEL_ENUM = {
    "apartamento"   : 1110966,
    "casa"          : 1110968,
    "terreno"       : 1110970,
    "comercial"     : 1110972,
    "empreendimento": 1110974,
}

# ─── Pipelines e status ───────────────────────────────────────────────────────
PIPE_RECEPCAO     = 9959303
PIPE_ALUGUEL      = 11482927
PIPE_AVULSO       = 11482943
PIPE_FORNECEDORES = 11487879   # pipeline interno — bot nunca responde a leads aqui

STATUS_GANHO    = 142
STATUS_PERDIDO  = 143

# Substrings para localizar pipelines dinamicamente pelo nome
_PIPE_NOME_CAPTACAO   = ["captação", "captacao", "proprietário", "proprietario"]
_PIPE_NOME_CORRETORES = ["corretor", "equipe"]
_PIPE_NOME_LANCAMENTOS = ["lançamento", "lancamento", "lançamentos", "lancamentos"]
_PIPE_NOME_INVESTIDOR  = ["investidor", "adjudicado"]

_pipe_entry_cache: dict  = {}
_pipe_id_cache: dict     = {}   # cache de busca por nome


def _todos_os_pipelines() -> list:
    """Busca todos os pipelines uma vez e cacheia."""
    if "all" in _pipe_id_cache:
        return _pipe_id_cache["all"]
    try:
        r = requests.get(f"{BASE}/leads/pipelines", headers=_hdr())
        r.raise_for_status()
        pipes = r.json().get("_embedded", {}).get("pipelines", [])
        _pipe_id_cache["all"] = pipes
        return pipes
    except Exception as e:
        logger.error(f"Erro ao listar pipelines: {e}")
        return []


def _find_pipe_by_name(substrings: list[str]) -> int | None:
    """Retorna o ID do primeiro pipeline cujo nome contém alguma das substrings."""
    for p in _todos_os_pipelines():
        nome = p.get("name", "").lower()
        if any(s in nome for s in substrings):
            return p["id"]
    return None


def _cache_pipe(key: str, substrings: list[str]) -> int | None:
    """Busca pipeline por nome, cacheia somente se encontrou (nunca cacheia None)."""
    if _pipe_id_cache.get(key):            # já temos um ID válido
        return _pipe_id_cache[key]
    result = _find_pipe_by_name(substrings)
    if result:
        _pipe_id_cache[key] = result
        logger.info(f"Pipeline '{key}' descoberto: id={result}")
    else:
        logger.warning(f"Pipeline '{key}' nao encontrado. Substrings: {substrings}")
    return result


def get_pipe_cadencia(faixa: str) -> int | None:
    """
    Descobre os funis de Cadência por nome (criados manualmente na UI do Kommo).
    faixa: 'ate300' | '301a600' | 'acima600' | 'locacao'

    Espera nomes contendo 'cad' + o identificador da faixa, ex:
      'Cadência | Até 300 mil', 'Cadência | 301 a 600 mil',
      'Cadência | Acima de 600 mil', 'Cadência | Locação'
    """
    _FAIXA_TERMS = {
        "ate300"  : ["até 300", "ate 300"],
        "301a600" : ["301"],
        "acima600": ["acima de 600", "acima 600", "600+"],
        "locacao" : ["locação", "locacao", "aluguel"],
    }
    terms = _FAIXA_TERMS.get(faixa)
    if not terms:
        return None
    cache_key = f"cadencia_{faixa}"
    if _pipe_id_cache.get(cache_key):
        return _pipe_id_cache[cache_key]
    for p in _todos_os_pipelines():
        nome = p.get("name", "").lower()
        if "cad" in nome and any(t in nome for t in terms):
            _pipe_id_cache[cache_key] = p["id"]
            logger.info(f"Pipeline cadência '{faixa}' descoberto: id={p['id']} ({p.get('name')})")
            return p["id"]
    logger.warning(f"Pipeline cadência '{faixa}' não encontrado — lead frio fica onde está")
    return None


def get_pipe_captacao() -> int | None:
    return _cache_pipe("captacao", _PIPE_NOME_CAPTACAO)


def get_pipe_corretores() -> int | None:
    return _cache_pipe("corretores", _PIPE_NOME_CORRETORES)


def get_pipe_lancamentos() -> int | None:
    return _cache_pipe("lancamentos", _PIPE_NOME_LANCAMENTOS)


def get_pipe_investidor() -> int | None:
    return _cache_pipe("investidor", _PIPE_NOME_INVESTIDOR)


def get_entry_status(pipe_id: int | None) -> int | None:
    """Retorna o primeiro status ativo (não 'Incoming leads') de um pipeline."""
    if not pipe_id:
        return None
    if _pipe_entry_cache.get(pipe_id):          # nunca retorna None cacheado
        return _pipe_entry_cache[pipe_id]
    try:
        r = requests.get(f"{BASE}/leads/pipelines/{pipe_id}", headers=_hdr())
        r.raise_for_status()
        statuses = r.json().get("_embedded", {}).get("statuses", [])
        ativas = sorted(
            [s for s in statuses
             if not s.get("is_finish")
             and s["id"] not in (STATUS_GANHO, STATUS_PERDIDO)
             and s["name"].strip().lower() != "incoming leads"],
            key=lambda x: x.get("sort", 0)
        )
        sid = ativas[0]["id"] if ativas else None
        if sid:
            _pipe_entry_cache[pipe_id] = sid    # só cacheia se encontrou (nunca cacheia None)
        else:
            logger.warning(f"Pipeline {pipe_id}: nenhum status de entrada encontrado — não cacheado")
        return sid
    except Exception as e:
        logger.error(f"Erro ao buscar status de entrada do pipeline {pipe_id}: {e}")
        return None

# (DORM_ENUM definido acima junto com os demais enums)

BAIRROS = [
    "Ponta Negra", "Capim Macio", "Lagoa Nova", "Petrópolis", "Tirol",
    "Alecrim", "Cidade Alta", "Ribeira", "Santos Reis", "Areia Preta",
    "Candelária", "Pitimbu", "Nova Parnamirim", "Parnamirim", "Emaús",
    "Neópolis", "Mãe Luíza", "Redinha", "Igapó", "Pajuçara",
    "Felipe Camarão", "Nazaré", "Planalto", "Quintas", "Nordeste",
    "Bom Pastor", "Cidade Nova", "Guarapes", "Potengi", "Lagoa Azul",
    "Praia do Meio", "Via Costeira", "Areia Branca",
]


class KommoClient:
    # ─── HTTP ─────────────────────────────────────────────────────────────────

    def _get(self, path, params=None):
        r = requests.get(f"{BASE}/{path}", headers=_hdr(), params=params or {})
        if r.status_code == 204:
            return {}
        r.raise_for_status()
        return r.json()

    def _patch(self, path, payload):
        r = requests.patch(f"{BASE}/{path}", headers=_hdr(), json=payload)
        if not r.ok:
            logger.error(f"PATCH /{path} falhou: {r.status_code} {r.text[:300]}")
        r.raise_for_status()
        return r.json()

    def _post(self, path, payload):
        r = requests.post(f"{BASE}/{path}", headers=_hdr(), json=payload)
        r.raise_for_status()
        return r.json()

    def _patch_field(self, lead_id: int, field_id: int | None, value_payload: dict):
        """Atualiza um campo com segurança (ignora se field_id for None)."""
        if not field_id:
            return
        try:
            self._patch(f"leads/{lead_id}", {
                "custom_fields_values": [{"field_id": field_id, "values": [value_payload]}]
            })
            time.sleep(0.1)
        except Exception as e:
            logger.error(f"Erro ao atualizar campo {field_id}: {e}")

    # ─── Busca de lead ────────────────────────────────────────────────────────

    # Pipelines internos — leads nesses pipelines são ignorados pelo bot
    _PIPELINES_INTERNOS = {
        11487871,   # Equipe | Corretores Parceiros
        11482967,   # Financeiro
        11482963,   # Manutenção
        11487879,   # Fornecedores
    }

    def find_lead_by_phone(self, phone: str) -> dict | None:
        """
        Retorna o lead ativo mais recente para o telefone.
        Ignora leads fechados (ganho/perdido) e leads em pipelines internos
        (Equipe, Financeiro, Manutenção, Fornecedores).
        """
        norm = _norm_phone(phone)
        try:
            data     = self._get("contacts", {"query": norm, "with": "leads", "limit": 5})
            contacts = data.get("_embedded", {}).get("contacts", [])
            for contact in contacts:
                leads = (contact.get("_embedded") or {}).get("leads", [])
                if not leads:
                    continue
                # Tenta do mais recente para o mais antigo
                for stub in sorted(leads, key=lambda l: l["id"], reverse=True):
                    lead = self._get(f"leads/{stub['id']}", {"with": "pipeline,status,custom_fields"})
                    # Ignora fechados
                    if lead.get("status_id") in (STATUS_GANHO, STATUS_PERDIDO):
                        continue
                    # Ignora pipelines internos
                    if lead.get("pipeline_id") in self._PIPELINES_INTERNOS:
                        logger.info(f"Lead {lead['id']} ignorado — pipeline interno {lead.get('pipeline_id')}")
                        continue
                    return lead
        except Exception as e:
            logger.warning(f"Erro ao buscar lead {norm}: {e}")
        return None

    def _find_won_lead(self, phone: str) -> dict | None:
        """Lead FECHADO COM GANHO mais recente (cliente com contrato na casa)."""
        norm = _norm_phone(phone)
        try:
            data     = self._get("contacts", {"query": norm, "with": "leads", "limit": 5})
            contacts = data.get("_embedded", {}).get("contacts", [])
            for contact in contacts:
                leads = (contact.get("_embedded") or {}).get("leads", [])
                for stub in sorted(leads, key=lambda l: l["id"], reverse=True):
                    lead = self._get(f"leads/{stub['id']}", {"with": "pipeline,status"})
                    if lead.get("status_id") == STATUS_GANHO:
                        return lead
        except Exception as e:
            logger.warning(f"_find_won_lead {norm}: {e}")
        return None

    def get_lead_context(self, phone: str) -> dict:
        """Retorna contexto completo do lead para o prompt do Claude."""
        lead = self.find_lead_by_phone(phone)
        if not lead:
            # Sem lead ativo — mas pode ser CLIENTE DA CASA (contrato ganho).
            # Caso Edileide (11/07): proprietária com captação ganha era tratada
            # como lead novo e interrogada sobre busca de imóvel.
            ganho = self._find_won_lead(phone)
            if ganho:
                pipe_nome = ((ganho.get("_embedded") or {}).get("pipeline") or {}).get("name", "")
                return {
                    "id"           : ganho.get("id"),
                    "name"         : ganho.get("name", ""),
                    "cliente_ativo": True,
                    "pipeline"     : pipe_nome,
                }
            return {}

        ctx = {"id": lead.get("id"), "name": lead.get("name", "")}

        emb    = lead.get("_embedded") or {}
        pipe   = emb.get("pipeline") or {}
        status = emb.get("status")   or {}
        ctx["pipeline"] = pipe.get("name", "")
        ctx["stage"]    = status.get("name", "")
        ctx["pipe_id"]  = lead.get("pipeline_id")

        field_map = {
            F_BAIRRO            : "bairro",
            F_MOTIVO_BUSCA      : "motivo_busca",
            F_DORMITORIOS       : "dormitorios",
            F_URGENCIA          : "urgencia",
            F_IMOVEL_ATUAL      : "imovel_atual",
            F_FINALIDADE        : "finalidade",
            F_TIPO_IMOVEL       : "tipo_imovel",
            F_IMOVEIS_POTENCIAIS: "imoveis_potenciais",
            F_PRE_APROVADO      : "pre_aprovado",
            F_IMOVEL_VENDER     : "imovel_vender",
            F_SCORE             : "score",
            F_IMOVEL_ORIG       : "imovel_origem",
        }

        for cf in (lead.get("custom_fields_values") or []):
            fid  = cf.get("field_id")
            vals = cf.get("values", [])
            if not vals or fid not in field_map:
                continue
            val = vals[0].get("value") or ""
            if not val:
                val = (vals[0].get("enum_value") or {}).get("value", "")
            if val:
                ctx[field_map[fid]] = val

        return ctx

    # ─── Move lead por motivação conhecida (Canal Pro tags) ──────────────────

    def move_lead_by_motivo(self, lead_id: int, motivo_busca: str) -> bool:
        """
        Move lead para o funil correto quando a motivação já é conhecida
        (ex: Canal Pro SELL/RENT) sem esperar a triagem completa do Henry.
        Retorna True se moveu, False caso contrário.
        """
        motivo = (motivo_busca or "").lower()
        if "locaç" in motivo or "aluguel" in motivo or "locar" in motivo:
            pipe_destino = PIPE_ALUGUEL
        elif "compra" in motivo or "comprar" in motivo:
            pipe_destino = PIPE_AVULSO
        else:
            return False  # motivação desconhecida — deixa Henry triar

        # Verifica se o lead está na Recepção antes de mover
        try:
            lead = self._get(f"leads/{lead_id}", {"with": "pipeline"})
            if lead.get("pipeline_id") != PIPE_RECEPCAO:
                logger.info(f"Lead {lead_id} já está fora da Recepção — não move")
                return False
        except Exception as e:
            logger.warning(f"Não foi possível verificar pipeline do lead {lead_id}: {e}")
            return False

        try:
            entry_status = get_entry_status(pipe_destino)
            if not entry_status:
                # Kommo IGNORA silenciosamente PATCH de pipeline_id sem status_id (200 OK sem mover)
                logger.error(f"Lead {lead_id}: status de entrada do pipeline {pipe_destino} indisponível — move abortado")
                return False
            patch_data = {"id": lead_id, "pipeline_id": pipe_destino, "status_id": entry_status}
            resp = self._patch("leads", [patch_data])
            atualizados = resp.get("_embedded", {}).get("leads", [])
            if not any(l.get("id") == lead_id for l in atualizados):
                logger.error(f"Lead {lead_id}: Kommo retornou 200 mas NÃO moveu (resp: {str(resp)[:200]})")
                return False
            logger.info(f"Lead {lead_id} auto-movido para pipeline {pipe_destino} (motivo: {motivo_busca})")
            return True
        except Exception as e:
            logger.error(f"Erro ao auto-mover lead {lead_id}: {e}")
            return False

    # ─── Pós-handoff ──────────────────────────────────────────────────────────

    def update_lead_after_bot(self, phone: str, history: list[dict], handoff_reason: str):
        """
        Executado pelo Henry após handoff (triagem SDR):
        1. Extrai dados básicos da conversa
        2. Atualiza campos de triagem no Kommo (só na Recepção)
        3. Move lead para o funil correto
        4. Adiciona nota com resumo da triagem + conversa
        5. Cria tarefa para o próximo responsável
        """
        lead = self.find_lead_by_phone(phone)
        if not lead:
            logger.warning(f"Handoff: lead não encontrado para {phone}")
            return

        lead_id = lead["id"]
        texto   = " ".join(m["content"] for m in history)
        na_recv = lead.get("pipeline_id") == PIPE_RECEPCAO

        # ── Extração básica (triagem Henry) ───────────────────────────────────
        dados = self._extrair_dados_triagem(texto, handoff_reason)

        # ── Atualiza campos de triagem (só na Recepção) ────────────────────────
        if na_recv:
            if dados.get("bairro"):
                self._patch_field(lead_id, F_BAIRRO, {"value": dados["bairro"]})
            if dados.get("motivo"):
                self._patch_field(lead_id, F_MOTIVO_BUSCA, {"value": dados["motivo"]})

        # ── Move para o funil correto ──────────────────────────────────────────
        HANDOFF_PIPELINE = {
            "GABRIEL_ALUGUEL"     : PIPE_ALUGUEL,
            "GABRIEL_AVULSO"      : PIPE_AVULSO,
            "GABRIEL_CAPTACAO"    : get_pipe_captacao(),
            "GABRIEL_LANCAMENTOS" : get_pipe_lancamentos(),
            "GABRIEL_INVESTIDOR"  : get_pipe_investidor(),
            "CORRETOR"            : get_pipe_corretores(),
            "FORNECEDOR"          : PIPE_FORNECEDORES,
        }
        pipe_destino  = HANDOFF_PIPELINE.get(handoff_reason)

        # Descoberta dinâmica falhou (cache frio/API oscilou)? Força re-descoberta.
        # Caso real 07/07: proprietário qualificado ficou preso na Recepção porque
        # get_pipe_captacao() retornou None no momento do handoff.
        if pipe_destino is None and handoff_reason in HANDOFF_PIPELINE:
            _pipe_id_cache.pop("all", None)
            _RETRY_FN = {
                "GABRIEL_CAPTACAO"   : get_pipe_captacao,
                "GABRIEL_LANCAMENTOS": get_pipe_lancamentos,
                "GABRIEL_INVESTIDOR" : get_pipe_investidor,
                "CORRETOR"           : get_pipe_corretores,
            }
            fn = _RETRY_FN.get(handoff_reason)
            if fn:
                pipe_destino = fn()
                logger.info(f"Re-descoberta do pipeline para {handoff_reason}: {pipe_destino}")

        lead_movido   = False
        if pipe_destino:
            logger.info(f"Movendo lead {lead_id} → pipeline {pipe_destino} (handoff={handoff_reason})")
            try:
                entry_status = get_entry_status(pipe_destino)
                if not entry_status:
                    entry_status = get_entry_status(pipe_destino)   # 2ª tentativa (API oscilou)
                if not entry_status:
                    # Kommo IGNORA silenciosamente PATCH de pipeline_id sem status_id (200 OK sem mover).
                    # Sem status_id válido, aborta e deixa lead_movido=False → tarefa sai com aviso ⚠️.
                    raise RuntimeError(f"status de entrada do pipeline {pipe_destino} indisponível")
                patch_data = {"id": lead_id, "pipeline_id": pipe_destino, "status_id": entry_status}
                logger.info(f"Lead {lead_id}: status_id destino = {entry_status}")
                resp = self._patch("leads", [patch_data])
                # Verificação: Kommo pode retornar 200 e descartar o item — confirma que o lead está na resposta
                atualizados = resp.get("_embedded", {}).get("leads", [])
                if not any(l.get("id") == lead_id for l in atualizados):
                    raise RuntimeError(f"Kommo retornou 200 mas NÃO moveu o lead (resp: {str(resp)[:200]})")
                lead_movido = True
                logger.info(f"Lead {lead_id} movido → pipeline {pipe_destino}. Kommo resp: {str(resp)[:120]}")
                time.sleep(0.2)

                # ── Irmãos na Recepção vão JUNTOS (correção 01/08) ─────────────
                # Duplicatas do Canal Pro faziam o move acertar o lead-irmão e
                # deixar o lead DA CONVERSA no balcão (23 tarefas "movido" com
                # lead parado). Agora nenhum lead ativo do contato fica para trás.
                try:
                    norm = _norm_phone(phone)
                    data_c = self._get("contacts", {"query": norm, "with": "leads", "limit": 3})
                    for contato in data_c.get("_embedded", {}).get("contacts", []):
                        for stub in (contato.get("_embedded") or {}).get("leads", []):
                            sid = stub.get("id")
                            if not sid or sid == lead_id:
                                continue
                            irmao = self._get(f"leads/{sid}")
                            if (irmao.get("pipeline_id") == PIPE_RECEPCAO
                                    and irmao.get("status_id") not in (STATUS_GANHO, STATUS_PERDIDO)):
                                self._patch("leads", [{
                                    "id": sid, "pipeline_id": pipe_destino,
                                    "status_id": entry_status,
                                }])
                                logger.info(f"Lead-irmão {sid} (Recepção) movido junto → {pipe_destino}")
                                time.sleep(0.2)
                except Exception as e:
                    logger.warning(f"Move de irmãos do contato falhou (não-crítico): {e}")
            except Exception as e:
                logger.error(
                    f"FALHA ao mover lead {lead_id} → pipeline {pipe_destino} "
                    f"(handoff={handoff_reason}): {e}",
                    exc_info=True,
                )
        else:
            logger.warning(
                f"Pipeline destino nao encontrado para handoff '{handoff_reason}'. "
                f"HANDOFF_PIPELINE={HANDOFF_PIPELINE}"
            )

        # ── Nota com resumo da triagem ─────────────────────────────────────────
        nota = self._build_note_triagem(history, handoff_reason, dados, lead_movido)
        try:
            self._post("leads/notes", [{
                "entity_id"  : lead_id,
                "entity_type": "leads",
                "note_type"  : "common",
                "params"     : {"text": nota},
            }])
        except Exception as e:
            logger.error(f"Erro ao adicionar nota: {e}")

        # ── Tarefa ────────────────────────────────────────────────────────────
        texto_tarefa = self._texto_tarefa(handoff_reason, lead_movido)
        urgente = handoff_reason in ("URGENTE", "SOLICITADO")
        try:
            self._post("tasks", [{
                "entity_id"    : lead_id,
                "entity_type"  : "leads",
                "task_type_id" : 1,
                "text"         : texto_tarefa,
                "complete_till": int(time.time()) + (1800 if urgente else 7200),
            }])
        except Exception as e:
            logger.error(f"Erro ao criar tarefa: {e}")

        logger.info(f"Henry handoff concluído — lead {lead_id} | motivo: {handoff_reason}")

    # ─── Extração de triagem (Henry — SDR) ───────────────────────────────────

    def _extrair_dados_triagem(self, texto: str, handoff_reason: str) -> dict:
        """Extrai apenas os dados de triagem coletados pelo Henry."""
        dados = {}

        # Bairro
        for b in BAIRROS:
            if b.lower() in texto.lower():
                dados["bairro"] = b
                break

        # Motivo — sempre derivado do tipo de handoff (nunca por regex, evita falso-positivo)
        _MOTIVO_MAP = {
            "GABRIEL_ALUGUEL"     : "Locação",
            "GABRIEL_AVULSO"      : "Compra",
            "GABRIEL_CAPTACAO"    : "Proprietário",
            "GABRIEL_LANCAMENTOS" : "Lançamento",
            "GABRIEL_INVESTIDOR"  : "Investidor",
            "CORRETOR"            : "Corretor parceiro",
            "FORNECEDOR"          : "Fornecedor / Prestador",
            "SUPORTE"             : "Cliente Ativo (Suporte)",
            "OUTRO"               : "Outro",
        }
        if handoff_reason in _MOTIVO_MAP:
            dados["motivo"] = _MOTIVO_MAP[handoff_reason]

        # Tipo de imóvel (residencial E comercial)
        tipo_m = re.search(
            r'\b(casa|apartamento|apto|studio|kitnet|loft|sobrado|cobertura|flat|'
            r'galp[ãa]o|dep[óo]sito|sala\s+comercial|loja|ponto\s+comercial|'
            r'pr[ée]dio\s+comercial|pr[ée]dio|terreno|lote|s[íi]tio|ch[áa]cara)\b',
            texto, re.IGNORECASE
        )
        if tipo_m:
            dados["tipo_imovel"] = tipo_m.group(1).lower()

        # Dormitórios / quartos
        dorm_m = re.search(r'(\d+)\s*(?:quarto|dormitório|suite|suíte)', texto, re.IGNORECASE)
        if dorm_m:
            dados["dormitorios"] = dorm_m.group(1)

        # Garagem / vaga
        if re.search(r'\bgaragem\b|\bvaga\b', texto, re.IGNORECASE):
            dados["garagem"] = "Sim"

        # Orçamento — prioridade: R$ + número
        m = re.search(
            r"(r\$?\s*[\d.,]+\s*(?:mil|k)?(?:\s*[-–]\s*r?\$?\s*[\d.,]+\s*(?:mil|k)?)?)",
            texto, re.IGNORECASE
        )
        if m:
            dados["orcamento"] = m.group(1).strip()
        else:
            # Fallback: "X mil reais" / "X mil" / "mil reais" (sem R$)
            m_mil = re.search(r'(\d+[\d.,]*)\s*mil(?:\s*reais?)?', texto, re.IGNORECASE)
            if m_mil:
                try:
                    val = float(m_mil.group(1).replace('.', '').replace(',', '.')) * 1000
                    dados["orcamento"] = f"R$ {val:,.0f}".replace(',', '.')
                except Exception:
                    dados["orcamento"] = f"{m_mil.group(1)} mil reais"
            elif re.search(r'\bmil\s+reais?\b', texto, re.IGNORECASE):
                dados["orcamento"] = "R$ 1.000,00"

        # Prazo / data de entrada
        m = re.search(
            r"((?:em\s+)?(?:janeiro|fevereiro|março|abril|maio|junho|julho|agosto|"
            r"setembro|outubro|novembro|dezembro)(?:\s+de\s+\d{4})?|"
            r"\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?|"
            r"(?:próxim[ao]\s+)?(?:semana|mês|ano)|"
            r"imediato|urgente|o\s+quanto\s+antes|já)",
            texto, re.IGNORECASE
        )
        if m:
            dados["data_entrada"] = m.group(1).strip()

        return dados

    def _texto_tarefa(self, handoff_reason: str, lead_movido: bool = True) -> str:
        sufixo_move = {
            "GABRIEL_ALUGUEL"     : "movido para Aluguel",
            "GABRIEL_AVULSO"      : "movido para Avulso",
            "GABRIEL_CAPTACAO"    : "movido para Captação",
            "GABRIEL_LANCAMENTOS" : "movido para Lançamentos",
            "GABRIEL_INVESTIDOR"  : "movido para Investidor",
            "CORRETOR"            : "movido para Corretores",
        }
        aviso_nao_movido = ""
        if handoff_reason in sufixo_move and not lead_movido:
            aviso_nao_movido = f" ⚠️ ATENÇÃO: pipeline NÃO foi movido automaticamente — mover manualmente para '{sufixo_move[handoff_reason].replace('movido para ', '')}' no Kommo."

        tarefas = {
            "GABRIEL_ALUGUEL"     : f"🤖 Henry: lead de LOCAÇÃO triado e movido para Aluguel. Gabriel assume a qualificação.{aviso_nao_movido}",
            "GABRIEL_AVULSO"      : f"🤖 Henry: lead de COMPRA triado e movido para Avulso. Gabriel assume a qualificação.{aviso_nao_movido}",
            "GABRIEL_CAPTACAO"    : f"🤖 Henry: PROPRIETÁRIO identificado. Time de captação deve contatar.{aviso_nao_movido}",
            "GABRIEL_LANCAMENTOS" : f"🤖 Henry: lead de LANÇAMENTO triado. Gabriel assume a qualificação.{aviso_nao_movido}",
            "GABRIEL_INVESTIDOR"  : f"🤖 Henry: INVESTIDOR identificado. Gabriel assume a qualificação.{aviso_nao_movido}",
            "FORNECEDOR"          : f"📦 Henry: FORNECEDOR/PRESTADOR identificado. Time administrativo deve contatar.{aviso_nao_movido}",
            "SUPORTE"             : "🏘️ Henry: CLIENTE ATIVO com demanda de suporte/manutenção. Atendimento ao cliente deve contatar.",
            "CORRETOR"            : f"🤖 Henry: CORRETOR PARCEIRO identificado. Time de parcerias deve contatar.{aviso_nao_movido}",
            "URGENTE"             : "⚡ Henry: URGENTE — lead precisa de atendimento imediato!",
            "SOLICITADO"          : "🤖 Henry: cliente solicitou atendimento humano. Contatar agora.",
            "JURIDICO"            : "🤖 Henry: dúvida jurídica identificada. Encaminhar para responsável.",
        }
        return tarefas.get(handoff_reason, f"🤖 Henry: handoff — {handoff_reason}. Verificar e dar continuidade.")

    # ─── Nota de triagem ──────────────────────────────────────────────────────

    def _build_note_triagem(self, history, handoff_reason, dados, lead_movido: bool = True) -> str:
        perfil_label = {
            "GABRIEL_ALUGUEL"  : "🏠 Locatário",
            "GABRIEL_AVULSO"   : "🏡 Comprador",
            "GABRIEL_CAPTACAO"    : "🔑 Proprietário",
            "GABRIEL_LANCAMENTOS" : "🏗️ Comprador (Lançamento)",
            "GABRIEL_INVESTIDOR"  : "📈 Investidor",
            "FORNECEDOR"          : "📦 Fornecedor / Prestador",
            "SUPORTE"             : "🏘️ Cliente ativo (suporte)",
            "CORRETOR"            : "🤝 Corretor parceiro",
            "URGENTE"          : "⚡ Urgente",
            "SOLICITADO"       : "🙋 Solicitou humano",
            "JURIDICO"         : "⚖️ Dúvida jurídica",
        }.get(handoff_reason, f"❓ {handoff_reason}")

        aviso = "" if lead_movido else "\n⚠️ ATENÇÃO: lead NÃO foi movido automaticamente — mover pipeline manualmente.\n"

        linhas = [
            f"🤖 Henry (SDR) — Triagem concluída",
            f"Perfil identificado: {perfil_label}",
            aviso,
            "📋 DADOS COLETADOS NA TRIAGEM:",
            f"  Interesse    : {dados.get('motivo', '—')}",
            f"  Bairro       : {dados.get('bairro', '—')}",
            f"  Tipo imóvel  : {dados.get('tipo_imovel', '—')}",
            f"  Dormitórios  : {dados.get('dormitorios', '—')}",
            "",
            "ℹ️  Qualificação profunda será feita pelo Gabriel no funil de destino.",
            "",
            "─── Conversa Henry × Cliente ───",
        ]
        for msg in history[-30:]:
            role = "👤 Cliente" if msg["role"] == "user" else "🤖 Henry"
            linhas.append(f"{role}: {msg['content']}")
        return "\n".join(linhas)[:3500]

    # ─── Para webhook Kommo (ativação proativa do Gabriel) ────────────────────

    def _build_ctx_from_lead(self, lead: dict) -> dict:
        """
        Constrói o contexto do lead a partir do objeto já carregado.
        Inclui extração de intenção via tags do Canal Pro (SELL/RENT).
        """
        ctx: dict = {"id": lead.get("id"), "name": lead.get("name", "")}

        emb    = lead.get("_embedded") or {}
        pipe   = emb.get("pipeline") or {}
        status = emb.get("status")   or {}
        ctx["pipeline"]   = pipe.get("name", "")
        ctx["stage"]      = status.get("name", "")
        ctx["pipe_id"]    = lead.get("pipeline_id")
        ctx["created_at"] = lead.get("created_at", 0)   # timestamp Unix — usado para guard de reativação

        field_map = {
            F_BAIRRO            : "bairro",
            F_MOTIVO_BUSCA      : "motivo_busca",
            F_DORMITORIOS       : "dormitorios",
            F_URGENCIA          : "urgencia",
            F_IMOVEL_ATUAL      : "imovel_atual",
            F_FINALIDADE        : "finalidade",
            F_TIPO_IMOVEL       : "tipo_imovel",
            F_IMOVEIS_POTENCIAIS: "imoveis_potenciais",
            F_PRE_APROVADO      : "pre_aprovado",
            F_IMOVEL_VENDER     : "imovel_vender",
            F_SCORE             : "score",
            F_IMOVEL_ORIG       : "imovel_origem",
        }
        for cf in (lead.get("custom_fields_values") or []):
            fid  = cf.get("field_id")
            vals = cf.get("values", [])
            if not vals or fid not in field_map:
                continue
            val = vals[0].get("value") or ""
            if not val:
                val = (vals[0].get("enum_value") or {}).get("value", "")
            if val:
                ctx[field_map[fid]] = val

        # Extrai intenção das tags Canal Pro/OLX (sem sobrescrever campo já preenchido)
        tags = lead.get("tags") or []
        tag_names = [t.get("name", "").upper() for t in tags if isinstance(t, dict) and t.get("name")]
        if not ctx.get("motivo_busca"):
            if "SELL" in tag_names:
                ctx["motivo_busca"] = "Compra de imóvel"
            elif "RENT" in tag_names:
                ctx["motivo_busca"] = "Locação de imóvel"

        # Canal de origem
        for tag in tag_names:
            if any(s in tag for s in ["OLX", "ZAP", "VIVAREAL", "CANAL PRO", "WEBCONNECT"]):
                ctx["canal_origem"] = "Canal Pro / Grupo OLX"
                break

        return ctx

    def extract_henry_data(self, texto: str, handoff_reason: str) -> dict:
        """
        Extrai dados básicos da conversa do Henry e retorna com as mesmas chaves
        do get_lead_context — usado para complementar o contexto do Gabriel sem
        depender de propagação do CRM.
        """
        raw = self._extrair_dados_triagem(texto, handoff_reason)
        result: dict = {}
        if raw.get("orcamento"):
            result["orcamento"] = raw["orcamento"]
        if raw.get("bairro"):
            result["bairro"] = raw["bairro"]
        if raw.get("data_entrada"):
            result["data_entrada"] = raw["data_entrada"]
        if raw.get("motivo"):
            result["motivo_busca"] = raw["motivo"]
        if raw.get("tipo_imovel"):
            result["tipo_imovel"] = raw["tipo_imovel"]
        if raw.get("dormitorios"):
            result["dormitorios"] = raw["dormitorios"]
        if raw.get("garagem"):
            result["garagem"] = raw["garagem"]
        return result

    def add_task(self, lead_id: int, texto: str, prazo_segundos: int = 86400) -> None:
        """Cria tarefa simples num lead (usada para alertas à equipe)."""
        # Kommo trunca texto no primeiro caractere de 4 bytes (emoji 🤝/🎯):
        # o campo da Liliane virou "[" em 02/08. Emojis fora do BMP são removidos.
        texto = re.sub(r"[\U00010000-\U0010FFFF]", "", texto).strip()
        try:
            self._post("tasks", [{
                "entity_id"    : lead_id,
                "entity_type"  : "leads",
                "task_type_id" : 1,
                "text"         : texto,
                "complete_till": int(time.time()) + prazo_segundos,
            }])
        except Exception as e:
            logger.error(f"add_task lead {lead_id}: {e}")

    def mark_duplicate(self, lead_novo_id: int, lead_original_id: int) -> None:
        """
        Marca lead recém-criado como duplicata de um lead ativo existente
        (padrão Canal Pro: portal cria lead novo para contato que já tem lead).
        Cria nota + tarefa para a equipe fundir na interface do Kommo.
        UMA VEZ POR LEAD — sem esta trava, eventos repetidos do Kommo geravam
        dezenas de tarefas idênticas (caso #22557290, 30 tarefas, 30/07).
        """
        import store as _store
        if _store.all_state("dup_marcado").get(str(lead_novo_id)):
            logger.info(f"Lead {lead_novo_id} já marcado como duplicata — ignorando repetição")
            return
        _store.set_state(str(lead_novo_id), "dup_marcado", lead_original_id)
        try:
            self._post("leads/notes", [{
                "entity_id"  : lead_novo_id,
                "entity_type": "leads",
                "note_type"  : "common",
                "params"     : {"text": (
                    f"🔁 DUPLICATA DETECTADA — este contato já possui o lead ativo "
                    f"#{lead_original_id}, que é onde o atendimento está acontecendo.\n"
                    f"Ação: fundir este lead no #{lead_original_id} pela interface do "
                    f"Kommo (mesclar preserva o histórico)."
                )},
            }])
            self._post("tasks", [{
                "entity_id"    : lead_novo_id,
                "entity_type"  : "leads",
                "task_type_id" : 1,
                "text"         : f"🔁 Duplicata: fundir no lead #{lead_original_id} (mesclar no Kommo)",
                "complete_till": int(time.time()) + 86400,
            }])
            logger.info(f"Lead {lead_novo_id} marcado como duplicata de {lead_original_id}")
        except Exception as e:
            logger.error(f"Erro ao marcar duplicata {lead_novo_id}: {e}")

    def mark_lead_cold(self, phone: str) -> None:
        """
        Após o 3º follow-up sem resposta:
        1. Marca Score=Frio (só se vazio)
        2. Move o lead para o funil de Cadência correto (ciclo de aquecimento
           automático de longo prazo, definido pelo Felipe em 07/07/2026):
             • Aluguel                     → Cadência | Locação
             • Compra (Avulso) ≤ 300 mil   → Cadência | Até 300 mil
             • Compra (Avulso) 301–600 mil → Cadência | 301 a 600 mil
             • Compra (Avulso) > 600 mil   → Cadência | Acima de 600 mil
             • Sem orçamento conhecido     → Cadência | 301 a 600 mil (faixa média)
             • Demais funis (captação etc.) → permanece onde está, só marca frio
        3. Registra nota explicando o esfriamento e o destino
        """
        lead = self.find_lead_by_phone(phone)
        if not lead:
            logger.info(f"[{phone}] mark_lead_cold: lead não encontrado")
            return
        lead_id = lead["id"]

        ja_tem_score = any(
            cf.get("field_id") == F_SCORE and cf.get("values")
            for cf in (lead.get("custom_fields_values") or [])
        )
        if not ja_tem_score:
            self._patch_field(lead_id, F_SCORE, {"enum_id": SCORE_ENUM["frio"]})
            logger.info(f"[{phone}] Lead {lead_id} marcado FRIO após 3 follow-ups sem resposta")

        # ── Roteia para o funil de Cadência ───────────────────────────────────
        pipe_atual = lead.get("pipeline_id")
        price      = int(lead.get("price") or 0)
        if price < 500:
            price = 0   # lixo do Canal Pro (código do lead) → trata como sem orçamento
        destino    = None
        destino_label = ""
        if pipe_atual == PIPE_ALUGUEL:
            destino, destino_label = get_pipe_cadencia("locacao"), "Cadência Locação"
        elif pipe_atual == PIPE_AVULSO:
            if price and price <= 300_000:
                destino, destino_label = get_pipe_cadencia("ate300"), "Cadência Até 300 mil"
            elif price > 600_000:
                destino, destino_label = get_pipe_cadencia("acima600"), "Cadência Acima de 600 mil"
            else:
                destino, destino_label = get_pipe_cadencia("301a600"), "Cadência 301 a 600 mil"

        lead_movido = False
        if destino:
            try:
                entry_status = get_entry_status(destino)
                if not entry_status:
                    raise RuntimeError(f"status de entrada do pipeline {destino} indisponível")
                resp = self._patch("leads", [{
                    "id": lead_id, "pipeline_id": destino, "status_id": entry_status,
                }])
                atualizados = resp.get("_embedded", {}).get("leads", [])
                if not any(l.get("id") == lead_id for l in atualizados):
                    raise RuntimeError("Kommo retornou 200 mas não moveu o lead")
                lead_movido = True
                logger.info(f"[{phone}] Lead {lead_id} → {destino_label} (pipeline {destino})")
            except Exception as e:
                logger.error(f"[{phone}] Falha ao mover lead {lead_id} → {destino_label}: {e}")

        if lead_movido:
            texto_nota = (
                f"🤖 Follow-up automático encerrado — cliente não respondeu a 3 toques "
                f"(4h / 24h / 72h). Lead esfriou durante a qualificação do bot.\n"
                f"➡️ Movido automaticamente para: {destino_label}"
                f"{f' (orçamento: R$ {price:,})'.replace(',', '.') if price else ''}.\n"
                f"O ciclo de aquecimento de longo prazo assume a partir daqui."
            )
        else:
            texto_nota = (
                "🤖 Follow-up automático encerrado — cliente não respondeu a 3 toques "
                "(4h / 24h / 72h). Lead esfriou durante a qualificação do bot.\n"
                "⚠️ Não foi movido para cadência (funil sem destino mapeado ou funil de "
                "cadência inexistente). Sugestão: mover manualmente ou tentar ligação."
            )
        try:
            self._post("leads/notes", [{
                "entity_id"  : lead_id,
                "entity_type": "leads",
                "note_type"  : "common",
                "params"     : {"text": texto_nota},
            }])
        except Exception as e:
            logger.error(f"[{phone}] Erro ao registrar nota de esfriamento: {e}")

    def get_preference_note(self, lead_id: int) -> str | None:
        """
        Busca a nota de preferências comportamentais mais recente do lead.
        Retorna o texto da nota ou None se não houver.
        Usado pelo Gabriel para personalizar sugestões com base em conversas anteriores.
        """
        try:
            r = requests.get(
                f"{BASE}/leads/{lead_id}/notes",
                headers=_hdr(),
                params={"note_type": "common", "limit": 25, "order[id]": "desc"},
                timeout=10,
            )
            r.raise_for_status()
            notes = r.json().get("_embedded", {}).get("notes", [])
            for note in notes:
                text = (note.get("params") or {}).get("text", "")
                if "🧠 PREFERÊNCIAS DO CLIENTE" in text:
                    return text
        except Exception as e:
            logger.warning(f"get_preference_note lead {lead_id}: {e}")
        return None

    def get_lead_id_for_contact(self, contact_id: int) -> int | None:
        """Retorna o lead ativo mais recente para um contact_id do Kommo."""
        try:
            contact = self._get(f"contacts/{contact_id}", {"with": "leads"})
            leads   = (contact.get("_embedded") or {}).get("leads", [])
            for stub in sorted(leads, key=lambda l: l["id"], reverse=True):
                lid = stub.get("id")
                if lid:
                    return int(lid)
        except Exception as e:
            logger.error(f"Erro ao buscar lead para contact {contact_id}: {e}")
        return None

    def get_lead_phone_and_context(self, lead_id: int) -> tuple[str | None, str, dict]:
        """
        Dado um lead_id, retorna (phone, name, lead_context).
        Usado pelo webhook do Kommo para ativar Henry e Gabriel proativamente.
        """
        try:
            lead = self._get(
                f"leads/{lead_id}",
                {"with": "contacts,pipeline,status,custom_fields,tags"},
            )
        except Exception as e:
            logger.error(f"Erro ao buscar lead {lead_id}: {e}")
            return None, "", {}

        name = lead.get("name", "")

        # Busca telefone nos contatos do lead
        phone = None
        contacts = (lead.get("_embedded") or {}).get("contacts", [])
        for contact_stub in contacts:
            try:
                contact = self._get(f"contacts/{contact_stub['id']}")
                for cf in (contact.get("custom_fields_values") or []):
                    if cf.get("field_code") in ("PHONE", "TEL"):
                        vals = cf.get("values", [])
                        if vals:
                            phone = canon_phone(str(vals[0].get("value", "")))
                            break
                if phone:
                    break
            except Exception:
                continue

        if not phone:
            logger.warning(f"Lead {lead_id} sem telefone nos contatos")
            return None, name, {}

        # Constrói contexto direto do objeto já carregado (sem 2ª chamada)
        ctx = self._build_ctx_from_lead(lead)
        return phone, name, ctx

    # ─── Pós-handoff Gabriel (qualificação concluída) ─────────────────────────

    def update_lead_after_gabriel(
        self,
        phone: str,
        history: list[dict],
        handoff_reason: str,
        funil: str | None,
        score: str | None = None,
    ):
        """
        Executado após Gabriel concluir a qualificação:
        1. Persiste o Score de Qualificação (Quente/Morno/Frio) emitido pelo Gabriel
        2. Adiciona nota com resumo da qualificação + conversa
        3. Cria tarefa para o corretor (com score em destaque)
        4. (Não move pipeline — Gabriel já está no funil correto)
        """
        lead = self.find_lead_by_phone(phone)
        if not lead:
            logger.warning(f"Gabriel handoff: lead não encontrado para {phone}")
            return

        lead_id = lead["id"]

        # ── Score de Qualificação (nunca sobrescreve se já preenchido) ────────
        if score:
            score_key = score.strip().lower()
            eid = SCORE_ENUM.get(score_key)
            ja_tem_score = any(
                cf.get("field_id") == F_SCORE and cf.get("values")
                for cf in (lead.get("custom_fields_values") or [])
            )
            if eid and not ja_tem_score:
                try:
                    self._patch_field(lead_id, F_SCORE, {"enum_id": eid})
                    logger.info(f"[{phone}] Score '{score_key}' gravado no lead {lead_id}")
                except Exception as e:
                    logger.error(f"[{phone}] Erro ao gravar score no lead {lead_id}: {e}")
            elif not eid:
                logger.warning(f"[{phone}] Score inválido do Gabriel: '{score}' — ignorado")

        # Nota de qualificação
        nota = self._build_note_gabriel(history, handoff_reason, funil)
        try:
            self._post("leads/notes", [{
                "entity_id"  : lead_id,
                "entity_type": "leads",
                "note_type"  : "common",
                "params"     : {"text": nota},
            }])
        except Exception as e:
            logger.error(f"Erro ao adicionar nota Gabriel: {e}")

        # Tarefa para corretor
        urgente = handoff_reason in ("URGENTE", "SOLICITADO")
        funil_label = {
            "aluguel"    : "LOCAÇÃO",
            "avulso"     : "COMPRA",
            "captacao"   : "CAPTAÇÃO",
            "lancamentos": "LANÇAMENTO",
            "investidor" : "INVESTIMENTO",
        }.get(funil or "", funil or "?")

        score_label = {"quente": "🔥 QUENTE", "morno": "🌡️ MORNO", "frio": "❄️ FRIO"}.get(
            (score or "").strip().lower(), ""
        )
        score_sufixo = f" Score: {score_label}." if score_label else ""

        texto_tarefa = f"🤖 Gabriel: qualificação de {funil_label} concluída.{score_sufixo} Lead pronto para o corretor fechar! ✅"
        if handoff_reason == "URGENTE":
            texto_tarefa = f"⚡ Gabriel: URGENTE — lead de {funil_label} precisa de atendimento imediato!{score_sufixo}"
        elif handoff_reason == "SOLICITADO":
            texto_tarefa = f"🙋 Gabriel: cliente de {funil_label} solicitou atendimento humano.{score_sufixo}"
        elif handoff_reason == "VISITA":
            texto_tarefa = f"🏠 Gabriel: cliente de {funil_label} QUER VISITAR!{score_sufixo} Ligar o quanto antes para agendar."

        # Lead quente ou pedido de visita → tarefa com prazo curto (30 min)
        if (score or "").strip().lower() == "quente" or handoff_reason == "VISITA":
            urgente = True

        try:
            self._post("tasks", [{
                "entity_id"    : lead_id,
                "entity_type"  : "leads",
                "task_type_id" : 1,
                "text"         : texto_tarefa,
                "complete_till": int(time.time()) + (1800 if urgente else 86400),
            }])
        except Exception as e:
            logger.error(f"Erro ao criar tarefa Gabriel: {e}")

        logger.info(f"Gabriel handoff concluído — lead {lead_id} | funil: {funil} | motivo: {handoff_reason}")

    def _build_note_gabriel(self, history: list[dict], handoff_reason: str, funil: str | None) -> str:
        funil_label = {
            "aluguel"    : "🏠 Locação",
            "avulso"     : "🏡 Compra",
            "captacao"   : "🔑 Captação",
            "lancamentos": "🏗️ Lançamento",
            "investidor" : "📈 Investimento",
        }.get(funil or "", funil or "?")

        linhas = [
            f"🤖 Gabriel (Qualificador) — Qualificação concluída",
            f"Funil: {funil_label}",
            f"Handoff: {handoff_reason}",
            "",
            "─── Conversa Gabriel × Cliente ───",
        ]
        for msg in history[-40:]:
            role = "👤 Cliente" if msg["role"] == "user" else "🤖 Gabriel"
            linhas.append(f"{role}: {msg['content']}")
        return "\n".join(linhas)[:3500]

