"""
gabriel/agent.py
================
Gerencia conversas do Gabriel — Qualificador da Seletos Imóveis.

Gabriel é ativado de duas formas:
  1. PROATIVO: Kommo webhook dispara quando lead entra no funil → Gabriel manda a 1ª mensagem
  2. REATIVO : Cliente responde → Gabriel continua a qualificação

Estado em memória por número de telefone.
"""

import re
import logging
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_HISTORY
from gabriel.prompts import get_prompt

logger = logging.getLogger(__name__)

_HANDOFF_RE = re.compile(r"\[HANDOFF:\s*([^\]]+)\]", re.IGNORECASE)

# Estado em memória: phone → conversa Gabriel
_gabriel_conversations: dict[str, list[dict]] = {}
_gabriel_funil:         dict[str, str]         = {}   # phone → funil ativo
_gabriel_mode:          set[str]               = set()  # phones com Gabriel ativo
_human_mode:            set[str]               = set()  # phones em modo humano final

_client = Anthropic(api_key=ANTHROPIC_API_KEY)


# Mapeamento: pipeline_id Kommo → chave do funil Gabriel
# Preenchido em main.py após descobrir os IDs dinâmicos
PIPE_TO_FUNIL: dict[int, str] = {}


class GabrielManager:

    # ─── Ativação proativa ────────────────────────────────────────────────────

    def activate(self, phone: str, funil: str, sender_name: str, lead_context: dict) -> str:
        """
        Ativa Gabriel para o telefone indicado e gera a primeira mensagem proativa.
        Chamado pelo webhook do Kommo quando o lead entra no funil.

        funil: 'aluguel' | 'avulso' | 'captacao' | 'lancamentos' | 'investidor'
        Retorna o texto da primeira mensagem.
        """
        _gabriel_mode.add(phone)
        _gabriel_funil[phone] = funil
        _gabriel_conversations[phone] = []   # conversa fresca

        logger.info(f"[{phone}] Gabriel ativado — funil: {funil}")

        system = self._build_system(funil, sender_name, lead_context)

        # Inject a "start" user turn para forçar Gabriel a gerar a 1ª mensagem
        seed = [{"role": "user", "content": "__INICIO__"}]

        try:
            response = _client.messages.create(
                model      = CLAUDE_MODEL,
                max_tokens = 400,
                system     = system + "\n\nSe receber '__INICIO__', envie apenas a PRIMEIRA MENSAGEM proativa definida no seu prompt, sem mais nada.",
                messages   = seed,
            )
            raw = response.content[0].text
        except Exception as e:
            logger.error(f"[{phone}] Gabriel activate erro: {e}")
            raw = "Olá! Sou Gabriel da Seletos 😊 Vou te ajudar com seu atendimento. Me conta um pouco mais sobre o que está buscando?"

        # Limpa tag de handoff (improvável na 1ª msg, mas seguro)
        clean = _HANDOFF_RE.sub("", raw).strip()

        # Salva no histórico como assistente (Gabriel iniciou)
        _gabriel_conversations[phone].append({"role": "assistant", "content": clean})

        return clean

    # ─── Resposta reativa ─────────────────────────────────────────────────────

    def chat(
        self,
        phone: str,
        user_message: str,
        sender_name: str,
        lead_context: dict,
    ) -> tuple[str, str | None]:
        """
        Processa resposta do cliente para o Gabriel.
        Retorna (resposta_limpa, handoff_reason | None).
        """
        funil   = _gabriel_funil.get(phone, "avulso")
        history = _gabriel_conversations.setdefault(phone, [])
        history.append({"role": "user", "content": user_message})

        system = self._build_system(funil, sender_name, lead_context)

        try:
            response = _client.messages.create(
                model      = CLAUDE_MODEL,
                max_tokens = 500,
                system     = system,
                messages   = history,
            )
            raw = response.content[0].text
        except Exception as e:
            logger.error(f"[{phone}] Gabriel chat erro: {e}")
            raw = "Desculpe, tive uma instabilidade. Pode repetir? 🙏"

        match  = _HANDOFF_RE.search(raw)
        handoff = match.group(1).strip() if match else None
        clean   = _HANDOFF_RE.sub("", raw).strip()

        history.append({"role": "assistant", "content": clean})

        if len(history) > MAX_HISTORY:
            _gabriel_conversations[phone] = history[-MAX_HISTORY:]

        return clean, handoff

    # ─── Estado ───────────────────────────────────────────────────────────────

    def is_active(self, phone: str) -> bool:
        """True se Gabriel está ativo para este telefone."""
        return phone in _gabriel_mode

    def is_human_mode(self, phone: str) -> bool:
        return phone in _human_mode

    def set_human_mode(self, phone: str):
        _human_mode.add(phone)
        _gabriel_mode.discard(phone)
        logger.info(f"[{phone}] Modo humano ativado (Gabriel → corretor)")

    def get_funil(self, phone: str) -> str | None:
        return _gabriel_funil.get(phone)

    def get_history(self, phone: str) -> list[dict]:
        return _gabriel_conversations.get(phone, [])

    def record_outgoing(self, phone: str, text: str):
        """
        Registra mensagem enviada por humano como turno do assistente no contexto Gabriel.
        """
        history = _gabriel_conversations.setdefault(phone, [])
        history.append({"role": "assistant", "content": text})
        if len(history) > MAX_HISTORY:
            _gabriel_conversations[phone] = history[-MAX_HISTORY:]
        logger.info(f"[{phone}] Mensagem humana registrada no histórico Gabriel ({len(text)} chars)")

    def reset(self, phone: str):
        _gabriel_mode.discard(phone)
        _human_mode.discard(phone)
        _gabriel_conversations.pop(phone, None)
        _gabriel_funil.pop(phone, None)
        logger.info(f"[{phone}] Gabriel resetado")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _build_system(self, funil: str, name: str, ctx: dict) -> str:
        prompt = get_prompt(funil)
        nome_tag = f", {ctx.get('name') or name}" if (ctx.get("name") or name) else ""
        prompt = prompt.replace("{nome}", nome_tag)
        prompt = prompt.replace("{lead_context}", self._format_context(name, ctx))
        return prompt

    def _format_context(self, name: str, ctx: dict) -> str:
        if not ctx:
            return f"Nome: {name or 'Desconhecido'}\nLead novo — sem histórico no CRM."

        lines = [f"Nome: {ctx.get('name') or name or 'Desconhecido'}"]
        if ctx.get("pipeline"): lines.append(f"Funil: {ctx['pipeline']}")
        if ctx.get("stage"):    lines.append(f"Etapa: {ctx['stage']}")

        coletados = []
        mapping = {
            "motivo_busca" : "Interesse",
            "bairro"       : "Bairro",
            "orcamento"    : "Orçamento",
            "data_entrada" : "Prazo",
            "dormitorios"  : "Quartos",
            "motivacao"    : "Motivação",
            "situacao_atual": "Situação atual",
            "finalidade"   : "Finalidade",
            "num_pessoas"  : "Nº de pessoas",
        }
        for key, label in mapping.items():
            val = ctx.get(key)
            if val:
                lines.append(f"{label}: {val}")
                coletados.append(label.lower())

        if coletados:
            lines.append(f"\n⚠️ Dados já coletados pelo Henry: {', '.join(coletados)}.")
            lines.append("Não repita essas perguntas — use-os para personalizar o atendimento.")

        return "\n".join(lines)
