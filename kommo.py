"""
kommo.py
========
Cliente Kommo para busca de leads por telefone, atualização de campos
e registro de notas/tarefas após handoff do Henry (bot).

IMPORTANTE: Os IDs dos campos novos serão preenchidos após rodar:
    python kommo_setup_campos.py
"""

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


# ─── IDs dos campos customizados ──────────────────────────────────────────────
# Campos existentes
F_CANAL_ORIGEM  = 1328586   # select
F_URGENCIA      = 1328582   # select
F_DORMITORIOS   = 1328592   # select
F_BAIRRO        = 1328594   # text
F_MOTIVO_BUSCA  = 1307202   # text
F_IMOVEL_ORIG   = 1312438   # text

# Campos novos — preencher após rodar kommo_setup_campos.py
F_ORCAMENTO         = None   # text   — Orçamento
F_FORMA_PAGAMENTO   = None   # select — Forma de Pagamento
F_PRE_APROVACAO     = None   # select — Pré-aprovação
F_MOTIVACAO         = None   # text   — Motivação
F_SITUACAO_ATUAL    = None   # select — Situação Atual
F_DATA_ENTRADA      = None   # text   — Data de Entrada
F_NUM_PESSOAS       = None   # text   — Número de Pessoas
F_FINALIDADE        = None   # select — Finalidade
F_IMOVEL_VENDER     = None   # select — Tem Imóvel para Vender
F_SCORE             = None   # select — Score de Qualificação

# Tenta carregar IDs do arquivo gerado pelo setup
import os, json as _json
_ids_file = os.path.join(os.path.dirname(__file__), "..", "kommo_campos_ids.json")
if os.path.exists(_ids_file):
    try:
        _ids = _json.load(open(_ids_file, encoding="utf-8"))
        F_ORCAMENTO       = _ids.get("Orçamento")
        F_FORMA_PAGAMENTO = _ids.get("Forma de Pagamento")
        F_PRE_APROVACAO   = _ids.get("Pré-aprovação")
        F_MOTIVACAO       = _ids.get("Motivação")
        F_SITUACAO_ATUAL  = _ids.get("Situação Atual")
        F_DATA_ENTRADA    = _ids.get("Data de Entrada")
        F_NUM_PESSOAS     = _ids.get("Número de Pessoas")
        F_FINALIDADE      = _ids.get("Finalidade")
        F_IMOVEL_VENDER   = _ids.get("Tem Imóvel para Vender")
        F_SCORE           = _ids.get("Score de Qualificação")
        logger.info("IDs dos campos carregados de kommo_campos_ids.json")
    except Exception as e:
        logger.warning(f"Não foi possível carregar kommo_campos_ids.json: {e}")

# ─── Pipelines e status ───────────────────────────────────────────────────────
PIPE_RECEPCAO   = 9959303
PIPE_ALUGUEL    = 11482927
PIPE_AVULSO     = 11482943

STATUS_GANHO    = 142
STATUS_PERDIDO  = 143

# Substrings para localizar pipelines dinamicamente pelo nome
_PIPE_NOME_CAPTACAO   = ["captação", "captacao", "proprietário", "proprietario"]
_PIPE_NOME_CORRETORES = ["corretor", "equipe"]

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


def get_pipe_captacao() -> int | None:
    if "captacao" not in _pipe_id_cache:
        _pipe_id_cache["captacao"] = _find_pipe_by_name(_PIPE_NOME_CAPTACAO)
    return _pipe_id_cache["captacao"]


def get_pipe_corretores() -> int | None:
    if "corretores" not in _pipe_id_cache:
        _pipe_id_cache["corretores"] = _find_pipe_by_name(_PIPE_NOME_CORRETORES)
    return _pipe_id_cache["corretores"]


def get_entry_status(pipe_id: int | None) -> int | None:
    """Retorna o primeiro status ativo (não 'Incoming leads') de um pipeline."""
    if not pipe_id:
        return None
    if pipe_id in _pipe_entry_cache:
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
        _pipe_entry_cache[pipe_id] = sid
        return sid
    except Exception as e:
        logger.error(f"Erro ao buscar status de entrada do pipeline {pipe_id}: {e}")
        return None

DORM_ENUM = {1: 1110914, 2: 1110916, 3: 1110918, 4: 1110920}

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

    def find_lead_by_phone(self, phone: str) -> dict | None:
        norm = _norm_phone(phone)
        try:
            data     = self._get("contacts", {"query": norm, "with": "leads", "limit": 5})
            contacts = data.get("_embedded", {}).get("contacts", [])
            for contact in contacts:
                leads = (contact.get("_embedded") or {}).get("leads", [])
                if not leads:
                    continue
                lead_id = sorted(leads, key=lambda l: l["id"])[-1]["id"]
                lead    = self._get(f"leads/{lead_id}", {"with": "pipeline,status,custom_fields"})
                if lead.get("status_id") not in (STATUS_GANHO, STATUS_PERDIDO):
                    return lead
        except Exception as e:
            logger.warning(f"Erro ao buscar lead {norm}: {e}")
        return None

    def get_lead_context(self, phone: str) -> dict:
        """Retorna contexto completo do lead para o prompt do Claude."""
        lead = self.find_lead_by_phone(phone)
        if not lead:
            return {}

        ctx = {"id": lead.get("id"), "name": lead.get("name", "")}

        emb    = lead.get("_embedded") or {}
        pipe   = emb.get("pipeline") or {}
        status = emb.get("status")   or {}
        ctx["pipeline"] = pipe.get("name", "")
        ctx["stage"]    = status.get("name", "")
        ctx["pipe_id"]  = lead.get("pipeline_id")

        field_map = {
            F_BAIRRO      : "bairro",
            F_MOTIVO_BUSCA: "motivo_busca",
            F_DORMITORIOS : "dormitorios",
            F_URGENCIA    : "urgencia",
            F_ORCAMENTO   : "orcamento",
            F_MOTIVACAO   : "motivacao",
            F_SITUACAO_ATUAL: "situacao_atual",
            F_FINALIDADE  : "finalidade",
            F_DATA_ENTRADA: "data_entrada",
            F_NUM_PESSOAS : "num_pessoas",
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
            if dados.get("orcamento"):
                self._patch_field(lead_id, F_ORCAMENTO, {"value": dados["orcamento"]})
            if dados.get("data_entrada"):
                self._patch_field(lead_id, F_DATA_ENTRADA, {"value": dados["data_entrada"]})

        # ── Move para o funil correto ──────────────────────────────────────────
        HANDOFF_PIPELINE = {
            "GABRIEL_ALUGUEL"  : PIPE_ALUGUEL,
            "GABRIEL_AVULSO"   : PIPE_AVULSO,
            "GABRIEL_CAPTACAO" : get_pipe_captacao(),
            "CORRETOR"         : get_pipe_corretores(),
        }
        pipe_destino = HANDOFF_PIPELINE.get(handoff_reason)
        if pipe_destino:
            status_destino = get_entry_status(pipe_destino)
            if status_destino:
                try:
                    self._patch("leads", [{
                        "id"         : lead_id,
                        "pipeline_id": pipe_destino,
                        "status_id"  : status_destino,
                    }])
                    logger.info(f"Lead {lead_id} movido para pipeline {pipe_destino}")
                    time.sleep(0.2)
                except Exception as e:
                    logger.error(f"Erro ao mover lead {lead_id} para pipeline {pipe_destino}: {e}")
            else:
                logger.warning(f"Sem status de entrada para pipeline {pipe_destino}")

        # ── Nota com resumo da triagem ─────────────────────────────────────────
        nota = self._build_note_triagem(history, handoff_reason, dados)
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
        texto_tarefa = self._texto_tarefa(handoff_reason)
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

        # Motivo — inferido pelo tipo de handoff ou pelo texto
        if handoff_reason in ("GABRIEL_ALUGUEL",):
            dados["motivo"] = "Locação"
        elif handoff_reason in ("GABRIEL_AVULSO",):
            dados["motivo"] = "Compra"
        elif handoff_reason in ("GABRIEL_CAPTACAO",):
            dados["motivo"] = "Proprietário"
        elif handoff_reason in ("CORRETOR",):
            dados["motivo"] = "Corretor parceiro"
        else:
            if re.search(r"\b(alug|loca[çc])", texto, re.IGNORECASE):
                dados["motivo"] = "Locação"
            elif re.search(r"\b(comprar?|compra|vend|adquirir)", texto, re.IGNORECASE):
                dados["motivo"] = "Compra"

        # Orçamento básico mencionado na triagem
        m = re.search(
            r"(r\$?\s*[\d.,]+\s*(?:mil|k)?(?:\s*[-–]\s*r?\$?\s*[\d.,]+\s*(?:mil|k)?)?)",
            texto, re.IGNORECASE
        )
        if m:
            dados["orcamento"] = m.group(1).strip()

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

    def _texto_tarefa(self, handoff_reason: str) -> str:
        tarefas = {
            "GABRIEL_ALUGUEL"  : "🤖 Henry: lead de LOCAÇÃO triado e movido para Aluguel. Gabriel assume a qualificação.",
            "GABRIEL_AVULSO"   : "🤖 Henry: lead de COMPRA triado e movido para Avulso. Gabriel assume a qualificação.",
            "GABRIEL_CAPTACAO" : "🤖 Henry: PROPRIETÁRIO identificado e movido para Captação. Time de captação deve contatar.",
            "CORRETOR"         : "🤖 Henry: CORRETOR PARCEIRO identificado. Time de parcerias deve contatar.",
            "URGENTE"          : "⚡ Henry: URGENTE — lead precisa de atendimento imediato!",
            "SOLICITADO"       : "🤖 Henry: cliente solicitou atendimento humano. Contatar agora.",
            "JURIDICO"         : "🤖 Henry: dúvida jurídica identificada. Encaminhar para responsável.",
        }
        return tarefas.get(handoff_reason, f"🤖 Henry: handoff — {handoff_reason}. Verificar e dar continuidade.")

    # ─── Nota de triagem ──────────────────────────────────────────────────────

    def _build_note_triagem(self, history, handoff_reason, dados) -> str:
        perfil_label = {
            "GABRIEL_ALUGUEL"  : "🏠 Locatário",
            "GABRIEL_AVULSO"   : "🏡 Comprador",
            "GABRIEL_CAPTACAO" : "🔑 Proprietário",
            "CORRETOR"         : "🤝 Corretor parceiro",
            "URGENTE"          : "⚡ Urgente",
            "SOLICITADO"       : "🙋 Solicitou humano",
            "JURIDICO"         : "⚖️ Dúvida jurídica",
        }.get(handoff_reason, f"❓ {handoff_reason}")

        linhas = [
            f"🤖 Henry (SDR) — Triagem concluída",
            f"Perfil identificado: {perfil_label}",
            "",
            "📋 DADOS COLETADOS NA TRIAGEM:",
            f"  Interesse    : {dados.get('motivo', '—')}",
            f"  Bairro       : {dados.get('bairro', '—')}",
            f"  Orçamento    : {dados.get('orcamento', '—')}",
            f"  Prazo        : {dados.get('data_entrada', '—')}",
            "",
            "ℹ️  Qualificação profunda será feita pelo Gabriel no funil de destino.",
            "",
            "─── Conversa Henry × Cliente ───",
        ]
        for msg in history[-30:]:
            role = "👤 Cliente" if msg["role"] == "user" else "🤖 Henry"
            linhas.append(f"{role}: {msg['content']}")
        return "\n".join(linhas)[:3500]
