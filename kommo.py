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

# Campos novos — criados por kommo_setup_campos.py em 2026-06-23
F_ORCAMENTO         = 1328828   # text   — Orçamento
F_FORMA_PAGAMENTO   = 1328606   # select — Forma de Pagamento
F_PRE_APROVACAO     = 1328836   # select — Pré-aprovação
F_MOTIVACAO         = 1328830   # text   — Motivação
F_SITUACAO_ATUAL    = 1328838   # select — Situação Atual
F_DATA_ENTRADA      = 1328832   # text   — Data de Entrada
F_NUM_PESSOAS       = 1328834   # text   — Número de Pessoas
F_FINALIDADE        = 1328636   # select — Finalidade
F_IMOVEL_VENDER     = 1328840   # select — Tem Imóvel para Vender
F_SCORE             = 1328842   # select — Score de Qualificação

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

        status_destino = get_entry_status(pipe_destino)
        if not status_destino:
            logger.warning(f"Sem status de entrada para pipeline {pipe_destino}")
            return False
        try:
            self._patch("leads", [{
                "id"         : lead_id,
                "pipeline_id": pipe_destino,
                "status_id"  : status_destino,
            }])
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
            if dados.get("orcamento"):
                self._patch_field(lead_id, F_ORCAMENTO, {"value": dados["orcamento"]})
            if dados.get("data_entrada"):
                self._patch_field(lead_id, F_DATA_ENTRADA, {"value": dados["data_entrada"]})

        # ── Move para o funil correto ──────────────────────────────────────────
        HANDOFF_PIPELINE = {
            "GABRIEL_ALUGUEL"     : PIPE_ALUGUEL,
            "GABRIEL_AVULSO"      : PIPE_AVULSO,
            "GABRIEL_CAPTACAO"    : get_pipe_captacao(),
            "GABRIEL_LANCAMENTOS" : get_pipe_lancamentos(),
            "GABRIEL_INVESTIDOR"  : get_pipe_investidor(),
            "CORRETOR"            : get_pipe_corretores(),
        }
        pipe_destino  = HANDOFF_PIPELINE.get(handoff_reason)
        lead_movido   = False
        if pipe_destino:
            status_destino = get_entry_status(pipe_destino)
            logger.info(f"Movendo lead {lead_id} → pipeline {pipe_destino}, status_destino={status_destino}")
            patch_payload: dict = {"id": lead_id, "pipeline_id": pipe_destino}
            if status_destino:
                patch_payload["status_id"] = status_destino
            try:
                self._patch("leads", [patch_payload])
                lead_movido = True
                logger.info(f"Lead {lead_id} movido para pipeline {pipe_destino}")
                time.sleep(0.2)
            except Exception as e:
                logger.error(f"Erro ao mover lead {lead_id} para pipeline {pipe_destino}: {e}")
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

        # Motivo — inferido pelo tipo de handoff ou pelo texto
        if handoff_reason in ("GABRIEL_ALUGUEL",):
            dados["motivo"] = "Locação"
        elif handoff_reason in ("GABRIEL_AVULSO",):
            dados["motivo"] = "Compra"
        elif handoff_reason in ("GABRIEL_CAPTACAO",):
            dados["motivo"] = "Proprietário"
        elif handoff_reason in ("GABRIEL_LANCAMENTOS",):
            dados["motivo"] = "Lançamento"
        elif handoff_reason in ("GABRIEL_INVESTIDOR",):
            dados["motivo"] = "Investidor"
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
            "SUPORTE"             : "🏘️ Cliente ativo (suporte)",
            "CORRETOR"         : "🤝 Corretor parceiro",
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
        ctx["pipeline"] = pipe.get("name", "")
        ctx["stage"]    = status.get("name", "")
        ctx["pipe_id"]  = lead.get("pipeline_id")

        field_map = {
            F_BAIRRO        : "bairro",
            F_MOTIVO_BUSCA  : "motivo_busca",
            F_DORMITORIOS   : "dormitorios",
            F_URGENCIA      : "urgencia",
            F_ORCAMENTO     : "orcamento",
            F_MOTIVACAO     : "motivacao",
            F_SITUACAO_ATUAL: "situacao_atual",
            F_FINALIDADE    : "finalidade",
            F_DATA_ENTRADA  : "data_entrada",
            F_NUM_PESSOAS   : "num_pessoas",
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
                            phone = _norm_phone(str(vals[0].get("value", "")))
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
    ):
        """
        Executado após Gabriel concluir a qualificação:
        1. Adiciona nota com resumo da qualificação + conversa
        2. Cria tarefa para o corretor
        3. (Não move pipeline — Gabriel já está no funil correto)
        """
        lead = self.find_lead_by_phone(phone)
        if not lead:
            logger.warning(f"Gabriel handoff: lead não encontrado para {phone}")
            return

        lead_id = lead["id"]

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

        texto_tarefa = f"🤖 Gabriel: qualificação de {funil_label} concluída. Lead pronto para o corretor fechar! ✅"
        if handoff_reason == "URGENTE":
            texto_tarefa = f"⚡ Gabriel: URGENTE — lead de {funil_label} precisa de atendimento imediato!"
        elif handoff_reason == "SOLICITADO":
            texto_tarefa = f"🙋 Gabriel: cliente de {funil_label} solicitou atendimento humano."

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

