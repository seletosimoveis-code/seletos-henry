"""
alertas.py
==========
Alerta em TEMPO REAL para o corretor — o elo que fecha o ciclo Real Brokerage.

Roteamento por REGIÃO (Felipe, 30/07/2026):
  • ASSÚ  → Sr. Hygino
  • GERAL → Jana (Natal, Parnamirim, Mossoró e demais)

MODO SILENCIOSO (Felipe, 30/07): entre 20h e 7h (Brasília) nenhum alerta é
disparado — eventos desse intervalo entram numa FILA PERSISTENTE (sobrevive a
restart) e são entregues às 7h, informando o horário original do contato.

Dispara no minuto (dentro da janela): lead QUENTE, pedido de VISITA, URGENTE,
cliente pediu HUMANO, cliente ativo com demanda, dúvida jurídica.
Mensagem: nome + telefone do cliente + link direto do lead no Kommo.
Sem assentos novos no Kommo (responsible_user_id entra depois, sem retrabalho).
Preparado para o futuro RODÍZIO: trocar corretor_para() por round-robin.
"""

import os
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import store

logger = logging.getLogger(__name__)

_BR_TZ = timezone(timedelta(hours=-3))

# Janela de envio (env override)
ALERTA_INICIO_H = int(os.getenv("ALERTA_INICIO_H", "7"))
ALERTA_FIM_H    = int(os.getenv("ALERTA_FIM_H", "20"))

CORRETORES = {
    "geral": {
        "nome" : os.getenv("CORRETOR_GERAL_NOME",  "Jana"),
        "phone": os.getenv("CORRETOR_GERAL_PHONE", "5584996360000"),
    },
    "assu": {
        "nome" : os.getenv("CORRETOR_ASSU_NOME",  "Sr. Hygino"),
        "phone": os.getenv("CORRETOR_ASSU_PHONE", "5584999109144"),
    },
}

_zapi = None
_subdominio = os.getenv("KOMMO_SUBDOMAIN", "seletosimoveis")

_TITULOS = {
    "VISITA"    : "🔥 QUER VISITAR — ligar AGORA",
    "QUENTE"    : "🔥 LEAD QUENTE qualificado",
    "SOLICITADO": "🙋 Cliente PEDIU atendimento humano",
    "URGENTE"   : "⚡ URGÊNCIA relatada pelo cliente",
    "SUPORTE"   : "🔧 Cliente ativo com demanda",
    "JURIDICO"  : "⚖️ Dúvida jurídica/contratual",
}


def init(zapi) -> None:
    global _zapi
    _zapi = zapi
    asyncio.create_task(_flush_loop())
    logger.info(
        f"Alertas tempo-real ativos ({ALERTA_INICIO_H}h–{ALERTA_FIM_H}h; fora disso, fila) "
        f"→ geral: {CORRETORES['geral']['nome']} | assu: {CORRETORES['assu']['nome']}"
    )


def _norm(s: str) -> str:
    troca = str.maketrans("áàâãéêíóôõúüç", "aaaaeeiooouuc")
    return (s or "").lower().translate(troca)


def _em_janela(dt: datetime) -> bool:
    return ALERTA_INICIO_H <= dt.hour < ALERTA_FIM_H


def corretor_para(ctx: dict) -> dict:
    """Assú no bairro/nome/origem → Hygino; resto → Jana. (Futuro: rodízio aqui.)"""
    texto = _norm(" ".join(str(ctx.get(k, "")) for k in
                           ("bairro", "name", "imovel_origem", "pipeline", "stage")))
    if "assu" in texto or "açu" in texto:
        return CORRETORES["assu"]
    return CORRETORES["geral"]


def _despachar(evento: str, ctx_min: dict, cliente_fone: str,
               extra: str, ts_original: float | None = None) -> None:
    """Monta e envia o alerta AGORA (uso interno — janela já verificada)."""
    chave  = "QUENTE" if evento == "quente" else evento
    titulo = _TITULOS.get(chave)
    if not titulo or not _zapi:
        return
    corretor = corretor_para(ctx_min)
    nome     = (ctx_min.get("name") or "Lead").split("|")[0].strip()
    linhas   = [titulo, f"👤 {nome} — 📱 {cliente_fone or 'sem fone'}"]
    if ts_original:
        hora = datetime.fromtimestamp(ts_original, tz=_BR_TZ).strftime("%d/%m às %H:%M")
        linhas.append(f"🌙 Cliente entrou em contato {hora} (fora do horário) — alerta segurado até as {ALERTA_INICIO_H}h.")
    if extra:
        linhas.append(f"📝 {extra}")
    if ctx_min.get("id"):
        linhas.append(f"🔗 https://{_subdominio}.kommo.com/leads/detail/{ctx_min['id']}")
    linhas.append("⏱️ Meta: responder em até 1 hora.")
    _zapi.send_text(corretor["phone"], "\n".join(linhas))
    logger.info(f"[{cliente_fone}] Alerta {chave} → {corretor['nome']}"
                + (" (da fila noturna)" if ts_original else ""))


def enviar(evento: str, lead_ctx: dict, cliente_fone: str, extra: str = "") -> None:
    """Ponto de entrada. Na janela → dispara; fora → enfileira para as 7h."""
    try:
        agora = datetime.now(_BR_TZ)
        ctx_min = {k: lead_ctx.get(k) for k in
                   ("id", "name", "bairro", "imovel_origem", "pipeline", "stage")}
        if _em_janela(agora):
            _despachar(evento, ctx_min, cliente_fone, extra)
            return
        # Fora da janela → fila persistente
        chave_fila = f"{int(time.time() * 1000)}_{cliente_fone}"
        store.set_state(chave_fila, "alerta_fila", {
            "evento": evento, "ctx": ctx_min, "fone": cliente_fone,
            "extra": extra, "ts": time.time(),
        })
        logger.info(f"[{cliente_fone}] Alerta {evento} ENFILEIRADO (madrugada) — sai às {ALERTA_INICIO_H}h")
    except Exception as e:
        logger.error(f"alerta {evento}: {e}")


async def _flush_loop() -> None:
    """A cada minuto: se estamos na janela, entrega a fila noturna."""
    while True:
        try:
            agora = datetime.now(_BR_TZ)
            if _em_janela(agora):
                fila = store.all_state("alerta_fila")
                for chave in sorted(fila.keys()):
                    item = fila[chave]
                    if isinstance(item, dict):
                        _despachar(
                            item.get("evento", ""), item.get("ctx") or {},
                            item.get("fone", ""), item.get("extra", ""),
                            ts_original=item.get("ts"),
                        )
                        await asyncio.sleep(2)   # espaça entregas
                    store.del_state(chave, "alerta_fila")
                if fila:
                    logger.info(f"Fila noturna de alertas entregue: {len(fila)} item(ns)")
        except Exception as e:
            logger.error(f"alerta flush: {e}")
        await asyncio.sleep(60)
