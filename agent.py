"""
agent.py
========
Gerencia conversas com Claude (histórico por telefone, detecção de handoff).
"""

import re
import logging
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_HISTORY
from prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Detecta tag de handoff no texto do Claude
_HANDOFF_RE = re.compile(r"\[HANDOFF:\s*([^\]]+)\]", re.IGNORECASE)

# Estado em memória (em produção com muitos usuários: substituir por Redis)
_conversations: dict[str, list[dict]] = {}
_human_mode:    set[str]              = set()

_client = Anthropic(api_key=ANTHROPIC_API_KEY)


class AgentManager:

    # ─── Conversa ─────────────────────────────────────────────────────────────

    def chat(
        self,
        phone: str,
        user_message: str,
        sender_name: str,
        lead_context: dict,
    ) -> tuple[str, str | None]:
        """
        Processa mensagem do cliente e retorna (resposta_limpa, motivo_handoff_ou_None).
        """
        history = _conversations.setdefault(phone, [])
        history.append({"role": "user", "content": user_message})

        # Monta system prompt com contexto do CRM
        system = SYSTEM_PROMPT.replace(
            "{lead_context}",
            self._format_context(sender_name, lead_context),
        )

        try:
            response = _client.messages.create(
                model      = CLAUDE_MODEL,
                max_tokens = 600,
                system     = system,
                messages   = history,
            )
            raw_text = response.content[0].text
        except Exception as e:
            logger.error(f"[{phone}] Erro Claude API: {e}")
            raw_text = "Desculpe, tive uma instabilidade momentânea. Pode repetir? 🙏"

        # Detecta e remove tag de handoff
        match = _HANDOFF_RE.search(raw_text)
        handoff_reason = match.group(1).strip() if match else None
        clean_text     = _HANDOFF_RE.sub("", raw_text).strip()

        # Salva resposta no histórico
        history.append({"role": "assistant", "content": clean_text})

        # Limita tamanho do histórico
        if len(history) > MAX_HISTORY:
            _conversations[phone] = history[-MAX_HISTORY:]

        return clean_text, handoff_reason

    # ─── Estado ───────────────────────────────────────────────────────────────

    def get_history(self, phone: str) -> list[dict]:
        return _conversations.get(phone, [])

    def is_human_mode(self, phone: str) -> bool:
        return phone in _human_mode

    def set_human_mode(self, phone: str):
        _human_mode.add(phone)
        logger.info(f"[{phone}] Modo humano ativado")

    def activate(self, phone: str, sender_name: str, lead_context: dict) -> str:
        """
        Ativa Henry proativamente para leads que chegam via Kommo
        (OLX, Canal Pro, Instagram, Facebook) sem enviar WhatsApp primeiro.
        Gera a primeira mensagem de boas-vindas personalizada.
        """
        # Garante histórico limpo para este número
        _conversations[phone] = []
        _human_mode.discard(phone)

        system = SYSTEM_PROMPT.replace(
            "{lead_context}",
            self._format_context(sender_name, lead_context),
        )

        seed = [{"role": "user", "content": "[NOVO_LEAD]"}]

        try:
            response = _client.messages.create(
                model      = CLAUDE_MODEL,
                max_tokens = 300,
                system     = system + (
                    "\n\n⚠️ ATIVAÇÃO PROATIVA:\n"
                    "Escreva APENAS a mensagem de saudação que será enviada ao celular do cliente.\n"
                    "NÃO explique seu funcionamento. NÃO liste capacidades. NÃO confirme instruções.\n"
                    "Escreva como se estivesse digitando direto no WhatsApp do cliente:\n"
                    "apresente-se como Henry da Seletos, mencione o canal de origem se disponível "
                    "(ex: 'vi que você entrou em contato pelo OLX'), e pergunte como pode ajudar.\n"
                    "Máximo 2-3 linhas. Tom caloroso. Sem tags de handoff."
                ),
                messages   = seed,
            )
            raw = response.content[0].text
        except Exception as e:
            logger.error(f"[{phone}] Henry activate erro: {e}")
            raw = "Olá! 👋 Sou Henry da Seletos Imóveis. Vi que você entrou em contato conosco — como posso te ajudar hoje?"

        clean = _HANDOFF_RE.sub("", raw).strip()
        _conversations[phone].append({"role": "assistant", "content": clean})
        logger.info(f"[{phone}] Henry ativado proativamente ({len(clean)} chars)")
        return clean

    def record_outgoing(self, phone: str, text: str):
        """
        Registra mensagem enviada por humano (atendente) como turno do assistente.
        Henry aprende o que foi dito sem gerar nova resposta.
        """
        history = _conversations.setdefault(phone, [])
        history.append({"role": "assistant", "content": text})
        if len(history) > MAX_HISTORY:
            _conversations[phone] = history[-MAX_HISTORY:]
        logger.info(f"[{phone}] Mensagem humana registrada no histórico Henry ({len(text)} chars)")

    def reset_conversation(self, phone: str):
        """Reinicia a conversa de um número (ex: novo atendimento)."""
        _conversations.pop(phone, None)
        _human_mode.discard(phone)
        logger.info(f"[{phone}] Conversa reiniciada")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _format_context(self, name: str, ctx: dict) -> str:
        if not ctx:
            return f"Nome: {name or 'Desconhecido'}\nStatus: Lead novo — sem histórico no CRM."

        lines = [f"Nome: {ctx.get('name') or name or 'Desconhecido'}"]
        if ctx.get("canal_origem"): lines.append(f"Canal de origem: {ctx['canal_origem']}")
        if ctx.get("pipeline"):     lines.append(f"Funil: {ctx['pipeline']}")
        if ctx.get("stage"):        lines.append(f"Etapa: {ctx['stage']}")

        # Dados já coletados na triagem (pelo Henry)
        ja_coletado = []
        if ctx.get("motivo_busca"):
            lines.append(f"Interesse declarado: {ctx['motivo_busca']}")
            ja_coletado.append("interesse")
        if ctx.get("bairro"):
            lines.append(f"Bairro mencionado: {ctx['bairro']}")
            ja_coletado.append("bairro")
        if ctx.get("orcamento"):
            lines.append(f"Orçamento mencionado: {ctx['orcamento']}")
            ja_coletado.append("orçamento")
        if ctx.get("data_entrada"):
            lines.append(f"Prazo mencionado: {ctx['data_entrada']}")
            ja_coletado.append("prazo")

        if ja_coletado:
            lines.append(f"\nEsses dados vieram da triagem — não repita essas perguntas.")

        # Lead novo sem histórico
        if not ctx:
            lines.append("Lead novo — sem histórico no CRM.")

        return "\n".join(lines)
