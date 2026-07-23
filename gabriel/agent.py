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
from datetime import datetime, timezone, timedelta
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, GABRIEL_MODEL, MAX_HISTORY, GABRIEL_MAX_TURNS
from gabriel.prompts import get_prompt
from site_seletos import fetch_imovel_details, extract_ref_from_text
import store

# Fuso de Brasília: UTC-3 fixo (Brasil não usa horário de verão desde 2019)
_BR_TZ = timezone(timedelta(hours=-3))
_DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

logger = logging.getLogger(__name__)

_HANDOFF_RE = re.compile(r"\[HANDOFF:\s*([^\]]+)\]", re.IGNORECASE)
_SCORE_RE   = re.compile(r"\[SCORE:\s*(quente|morno|frio)\s*\]", re.IGNORECASE)

# Estado em memória (cache de leitura; SQLite via store.py é o backup durável)
_gabriel_conversations: dict[str, list[dict]] = {}
_gabriel_funil:         dict[str, str]         = {}   # phone → funil ativo
_gabriel_mode:          set[str]               = set()  # phones com Gabriel ativo
_human_mode:            set[str]               = set()  # phones em modo humano final
_gabriel_turn_count:    dict[str, int]         = {}   # phone → nº de turnos do cliente
_gabriel_score:         dict[str, str]         = {}   # phone → score emitido no handoff


def hydrate_from_store() -> None:
    """Reconstrói o estado do Gabriel a partir do SQLite após um restart."""
    try:
        convs = store.recent_conversations("gabriel", MAX_HISTORY)
        _gabriel_conversations.update(convs)
        for phone, flag in store.all_state("gabriel_mode").items():
            if flag:
                _gabriel_mode.add(phone)
        for phone, flag in store.all_state("gabriel_human").items():
            if flag:
                _human_mode.add(phone)
        for phone, funil in store.all_state("gabriel_funil").items():
            if funil:
                _gabriel_funil[phone] = str(funil)
        for phone, turns in store.all_state("gabriel_turns").items():
            try:
                _gabriel_turn_count[phone] = int(turns)
            except (TypeError, ValueError):
                continue
        for phone, score in store.all_state("gabriel_score").items():
            if score:
                _gabriel_score[phone] = str(score)
        logger.info(
            f"Gabriel reidratado: {len(convs)} conversas, {len(_gabriel_mode)} ativos, "
            f"{len(_human_mode)} em modo humano"
        )
    except Exception as e:
        logger.error(f"Gabriel hydrate falhou (seguindo sem estado prévio): {e}")

_client = Anthropic(api_key=ANTHROPIC_API_KEY)


# Mapeamento: pipeline_id Kommo → chave do funil Gabriel
# Preenchido em main.py após descobrir os IDs dinâmicos
PIPE_TO_FUNIL: dict[int, str] = {}


class GabrielManager:

    # ─── Ativação proativa ────────────────────────────────────────────────────

    def activate(self, phone: str, funil: str, sender_name: str, lead_context: dict,
                 henry_tail: list[dict] | None = None) -> str:
        """
        Ativa Gabriel para o telefone indicado e gera a primeira mensagem proativa.
        Chamado pelo webhook do Kommo quando o lead entra no funil, ou diretamente
        no handoff do Henry (com henry_tail = últimos turnos da triagem, para o
        Gabriel NÃO nascer cego — correção do erro de 08/07: Gabriel ignorava a
        pergunta viva do cliente e recomeçava a qualificação do zero).

        funil: 'aluguel' | 'avulso' | 'captacao' | 'lancamentos' | 'investidor'
        Retorna o texto da primeira mensagem.
        """
        _gabriel_mode.add(phone)
        _gabriel_funil[phone] = funil
        _gabriel_conversations[phone] = []   # conversa fresca
        store.clear_msgs("gabriel", phone)
        store.set_state(phone, "gabriel_mode", True)
        store.set_state(phone, "gabriel_funil", funil)
        store.del_state(phone, "gabriel_human")

        logger.info(f"[{phone}] Gabriel ativado — funil: {funil}")

        system = self._build_system(funil, sender_name, lead_context)

        # Inject a "start" user turn para forçar Gabriel a gerar a 1ª mensagem
        seed = [{"role": "user", "content": "__INICIO__"}]

        # ── Conversa da triagem: Gabriel enxerga o que acabou de acontecer ─────
        # Sem isso, Gabriel nasce cego: ignora a pergunta viva do cliente,
        # inventa enquadramentos ("você quer se mudar já") e recomeça do zero.
        tail_block = ""
        ultima_msg_cliente = ""
        if henry_tail:
            convo = "\n".join(
                f"{'Cliente' if m['role'] == 'user' else 'Henry'}: {m['content']}"
                for m in henry_tail[-8:]
            )
            ultima_msg_cliente = next(
                (m["content"] for m in reversed(henry_tail) if m["role"] == "user"), ""
            )
            tail_block = (
                "\n\n══════════════════════════════════════════════\n"
                "CONVERSA IMEDIATAMENTE ANTERIOR (triagem do Henry — acabou de acontecer):\n"
                f"{convo}\n"
                "══════════════════════════════════════════════\n"
                f"⚠️ ÚLTIMA MENSAGEM DO CLIENTE: \"{ultima_msg_cliente}\"\n"
                "REGRAS DA SUA PRIMEIRA MENSAGEM:\n"
                "1. Se a última mensagem do cliente é uma PERGUNTA (ex: garantia, documentos, "
                "processo), sua primeira mensagem DEVE respondê-la diretamente com seu "
                "conhecimento — apresentação em 1 linha, resposta na sequência.\n"
                "2. NUNCA afirme coisas que o cliente não disse (ex: 'você já quer se mudar').\n"
                "3. NUNCA recomece a qualificação ignorando a conversa acima — continue DELA."
            )

        # Se Henry já coletou dados, Gabriel usa o contexto para não repetir perguntas
        _CAMPOS_HENRY = ["tipo_imovel", "dormitorios", "garagem", "orcamento", "bairro", "data_entrada", "motivo_busca"]
        dados_henry = [k for k in _CAMPOS_HENRY if lead_context.get(k)]
        if dados_henry:
            init_instruction = tail_block + (
                "\n\nSe receber '__INICIO__': o Henry já coletou dados do cliente (veja CONTEXTO DO LEAD acima). "
                "NÃO repita perguntas sobre o que já está preenchido. "
                "Envie uma mensagem de boas-vindas personalizada mencionando o que já sabe "
                "(ex: tipo de imóvel, bairro, orçamento) e pergunte APENAS o que ainda falta para a qualificação. "
                "Seja específico — mostre que você leu o perfil do Henry."
            )
        else:
            init_instruction = tail_block + (
                "\n\nSe receber '__INICIO__', envie a PRIMEIRA MENSAGEM proativa "
                "definida no seu prompt (respeitando as regras da conversa anterior, se houver)."
            )

        try:
            response = _client.messages.create(
                model      = GABRIEL_MODEL,
                max_tokens = 400,
                system     = system + init_instruction,
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
        store.append_msg("gabriel", phone, "assistant", clean)

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

        # ── Limite de turnos (anti-loop / anti-abuso) ─────────────────────────
        turn = _gabriel_turn_count.get(phone, 0) + 1
        _gabriel_turn_count[phone] = turn
        store.set_state(phone, "gabriel_turns", turn)
        if turn > GABRIEL_MAX_TURNS:
            logger.warning(f"[{phone}] Gabriel: limite de {GABRIEL_MAX_TURNS} turnos atingido — encerrando")
            self.set_human_mode(phone)
            encerramento = (
                "Já coletei todas as informações que precisava! 😊 "
                "Um corretor da Seletos vai entrar em contato em breve para dar continuidade. "
                "Até logo! 👋"
            )
            history.append({"role": "assistant", "content": encerramento})
            store.append_msg("gabriel", phone, "assistant", encerramento)
            return encerramento, "MAX_TURNS"

        history.append({"role": "user", "content": user_message})
        store.append_msg("gabriel", phone, "user", user_message)

        system = self._build_system(funil, sender_name, lead_context)

        # ── Busca detalhes do imóvel mencionado pelo cliente ──────────────────
        ref = extract_ref_from_text(user_message)
        if ref:
            imovel_info = fetch_imovel_details(ref)
            if imovel_info:
                system = system + f"\n\n{imovel_info}"
                logger.info(f"[{phone}] Imóvel #{ref} carregado para contexto Gabriel")
            else:
                logger.info(f"[{phone}] Ref #{ref} não encontrada no site — Gabriel responde sem detalhes")

        # ── E3: OFERTA AO VIVO — imóveis reais do site no contexto ────────────
        # Com tipo/bairro conhecidos, o Gabriel deixa de mandar link genérico e
        # passa a OFERTAR 2-3 imóveis específicos (o coração do modelo HeyLeo).
        try:
            from estoque import search_for_ctx, format_ofertas
            ofertas = search_for_ctx(funil, lead_context, user_message)
            if ofertas:
                system += "\n\n" + format_ofertas(ofertas)
                logger.info(
                    f"[{phone}] E3: {len(ofertas)} imóveis reais injetados "
                    f"(refs: {', '.join(o['ref'] for o in ofertas)})"
                )
        except Exception as e:
            logger.warning(f"[{phone}] E3 oferta ao vivo indisponível: {e}")

        try:
            response = _client.messages.create(
                model      = GABRIEL_MODEL,
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

        # Score de qualificação emitido junto ao handoff: [SCORE: quente|morno|frio]
        score_match = _SCORE_RE.search(raw)
        if score_match:
            _gabriel_score[phone] = score_match.group(1).lower()
            store.set_state(phone, "gabriel_score", _gabriel_score[phone])
            logger.info(f"[{phone}] Gabriel emitiu score: {_gabriel_score[phone]}")

        clean   = _HANDOFF_RE.sub("", raw)
        clean   = _SCORE_RE.sub("", clean).strip()

        history.append({"role": "assistant", "content": clean})
        store.append_msg("gabriel", phone, "assistant", clean)

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
        store.set_state(phone, "gabriel_human", True)
        store.del_state(phone, "gabriel_mode")
        logger.info(f"[{phone}] Modo humano ativado (Gabriel → corretor)")

    def get_funil(self, phone: str) -> str | None:
        return _gabriel_funil.get(phone)

    def get_score(self, phone: str) -> str | None:
        """Score de qualificação emitido pelo Gabriel no handoff ('quente'|'morno'|'frio')."""
        return _gabriel_score.get(phone)

    def get_history(self, phone: str) -> list[dict]:
        return _gabriel_conversations.get(phone, [])

    def record_outgoing(self, phone: str, text: str):
        """
        Registra mensagem enviada por humano como turno do assistente no contexto Gabriel.
        """
        history = _gabriel_conversations.setdefault(phone, [])
        history.append({"role": "assistant", "content": text})
        store.append_msg("gabriel", phone, "assistant", text)
        if len(history) > MAX_HISTORY:
            _gabriel_conversations[phone] = history[-MAX_HISTORY:]
        logger.info(f"[{phone}] Mensagem humana registrada no histórico Gabriel ({len(text)} chars)")

    def reactivate(self, phone: str, funil: str) -> None:
        """
        Reativa Gabriel para lead que retorna após conversa anterior encerrada
        (ex: restart do Railway, cliente inativo por semanas/meses).

        Diferente de activate():
        - NÃO gera mensagem proativa (o cliente já mandou mensagem)
        - NÃO chama a API do Claude aqui — o chat() processa a mensagem do cliente
        - O contexto do CRM (já carregado) supre o histórico perdido
        """
        _gabriel_mode.add(phone)
        _gabriel_funil[phone]         = funil
        _gabriel_conversations[phone] = []   # histórico limpo — CRM supre o contexto
        _gabriel_turn_count[phone]    = 0
        store.clear_msgs("gabriel", phone)
        store.set_state(phone, "gabriel_mode", True)
        store.set_state(phone, "gabriel_funil", funil)
        store.set_state(phone, "gabriel_turns", 0)
        store.del_state(phone, "gabriel_human")
        logger.info(f"[{phone}] Gabriel reativado para lead retornando — funil: {funil}")

    def reset(self, phone: str):
        _gabriel_mode.discard(phone)
        _human_mode.discard(phone)
        _gabriel_conversations.pop(phone, None)
        _gabriel_funil.pop(phone, None)
        _gabriel_turn_count.pop(phone, None)
        _gabriel_score.pop(phone, None)
        store.clear_msgs("gabriel", phone)
        store.clear_phone_state(
            phone,
            ["gabriel_mode", "gabriel_human", "gabriel_funil", "gabriel_turns", "gabriel_score"],
        )
        logger.info(f"[{phone}] Gabriel resetado")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _build_system(self, funil: str, name: str, ctx: dict) -> str:
        prompt = get_prompt(funil)
        nome_tag = f", {ctx.get('name') or name}" if (ctx.get("name") or name) else ""
        prompt = prompt.replace("{nome}", nome_tag)
        prompt = prompt.replace("{lead_context}", self._format_context(name, ctx))

        # ── Horário atual de Brasília ──────────────────────────────────────────
        try:
            now_br       = datetime.now(_BR_TZ)
            hora_str     = now_br.strftime("%H:%M")
            dia_str      = _DIAS_SEMANA[now_br.weekday()]
            is_comercial = (now_br.weekday() < 5) and (8 <= now_br.hour < 17)
            bloco_hora   = f"\n\n⏰ HORA ATUAL (Brasília): {hora_str} ({dia_str}-feira).\n"
            if not is_comercial:
                bloco_hora += (
                    "⚠️ FORA DO HORÁRIO COMERCIAL (seg–sex, 8h–17h). "
                    "NÃO diga 'o corretor vai te retornar rapidinho'. "
                    "Use: 'Nossa equipe entra em contato no próximo horário comercial (seg–sex, 8h–17h) 😊'\n"
                )
            prompt += bloco_hora
        except Exception:
            pass

        return prompt

    def _format_context(self, name: str, ctx: dict) -> str:
        if not ctx:
            return f"Nome: {name or 'Desconhecido'}\nLead novo — sem histórico no CRM."

        lines = [f"Nome: {ctx.get('name') or name or 'Desconhecido'}"]
        if ctx.get("pipeline"): lines.append(f"Funil: {ctx['pipeline']}")
        if ctx.get("stage"):    lines.append(f"Etapa: {ctx['stage']}")

        coletados = []
        mapping = {
            "motivo_busca"      : "Interesse",
            "tipo_imovel"       : "Tipo de imóvel",
            "dormitorios"       : "Quartos",
            "garagem"           : "Garagem",
            "bairro"            : "Bairro",
            "orcamento"         : "Orçamento",
            "data_entrada"      : "Prazo",
            "urgencia"          : "Urgência",
            "imovel_atual"      : "Situação atual",
            "finalidade"        : "Finalidade",
            "pre_aprovado"      : "Pré-aprovado",
            "imovel_vender"     : "Tem imóvel para vender",
            "score"             : "Score",
            "imoveis_potenciais": "Imóveis potenciais",
        }
        for key, label in mapping.items():
            val = ctx.get(key)
            if val:
                lines.append(f"{label}: {val}")
                coletados.append(label.lower())

        if coletados:
            lines.append(f"\n🚫 DADOS JÁ COLETADOS PELO HENRY — NÃO PERGUNTE NOVAMENTE: {', '.join(coletados)}.")
            lines.append("Use esses dados diretamente para personalizar o atendimento.")
            lines.append("É PROIBIDO repetir qualquer pergunta sobre esses itens — o cliente já respondeu ao Henry.")

        # Cliente retornando após período de inatividade
        if ctx.get("is_returning"):
            lines.append("")
            lines.append("🔄 CLIENTE RETORNANDO — já atendido anteriormente pela Seletos.")
            lines.append("   NÃO se apresente como se fosse o primeiro contato.")
            lines.append("   Reconheça que você lembra do perfil e pergunte como pode ajudar.")
            lines.append("   Exemplo: 'Olá [nome]! Que bom ter você de volta 😊 No que posso te ajudar hoje?'")
            lines.append("   Use os dados do CRM acima para demonstrar que lembra do perfil.")

        # Preferências comportamentais (aprendizado tipo Leo AiRM)
        prefs = ctx.get("preference_history")
        if prefs:
            lines.append("")
            lines.append("─── Histórico de Preferências (conversa anterior) ───")
            lines.append(prefs)
            lines.append("⚠️ Use este histórico para personalizar sugestões.")
            lines.append("   NÃO sugira características que estão na lista de REJEITADO/NÃO QUER.")
            lines.append("   Priorize características que estão na lista de GOSTOU/PREFERE.")

        return "\n".join(lines)
