"""
custos.py
=========
Medidor de gasto de IA por robô (pedido do Felipe, 30/07/2026).
Cada chamada ao Claude registra tokens de entrada/saída por robô no SQLite.
Consulta: GET /admin/custos — tokens, US$ e R$ por robô, desde o deploy.

Histórico anterior ao deploy: console.anthropic.com → Usage
(Haiku ≈ Henry+auxiliares; Sonnet ≈ Gabriel).
"""

import os
import logging
import store

logger = logging.getLogger(__name__)

# US$ por MILHÃO de tokens (entrada, saída) — ajustável por env se a tabela mudar
PRECOS = {
    "haiku" : (float(os.getenv("PRECO_HAIKU_IN",  "1.0")),  float(os.getenv("PRECO_HAIKU_OUT",  "5.0"))),
    "sonnet": (float(os.getenv("PRECO_SONNET_IN", "3.0")),  float(os.getenv("PRECO_SONNET_OUT", "15.0"))),
    "opus"  : (float(os.getenv("PRECO_OPUS_IN",   "15.0")), float(os.getenv("PRECO_OPUS_OUT",   "75.0"))),
}
USD_BRL = float(os.getenv("USD_BRL", "5.50"))


def _familia(model: str) -> str:
    m = (model or "").lower()
    if "haiku" in m:
        return "haiku"
    if "opus" in m:
        return "opus"
    return "sonnet"


def registrar(robo: str, model: str, usage) -> None:
    """Acumula tokens/custo do robô. Nunca lança exceção (não pode quebrar atendimento)."""
    try:
        t_in  = int(getattr(usage, "input_tokens", 0) or 0)
        t_out = int(getattr(usage, "output_tokens", 0) or 0)
        if not (t_in or t_out):
            return
        fam = _familia(model)
        p_in, p_out = PRECOS[fam]
        usd = (t_in * p_in + t_out * p_out) / 1_000_000
        atual = store.all_state(f"custo_{robo}").get("global") or {}
        store.set_state("global", f"custo_{robo}", {
            "chamadas": int(atual.get("chamadas", 0)) + 1,
            "tokens_in" : int(atual.get("tokens_in", 0)) + t_in,
            "tokens_out": int(atual.get("tokens_out", 0)) + t_out,
            "usd": round(float(atual.get("usd", 0)) + usd, 4),
        })
    except Exception as e:
        logger.debug(f"custos: {e}")


ROBOS = ["henry", "gabriel", "enricher", "followup", "faseb", "matching"]


def resumo() -> dict:
    out, total_usd = {}, 0.0
    for r in ROBOS:
        d = store.all_state(f"custo_{r}").get("global")
        if isinstance(d, dict) and d.get("chamadas"):
            d = dict(d)
            d["brl"] = round(d.get("usd", 0) * USD_BRL, 2)
            out[r] = d
            total_usd += float(d.get("usd", 0))
    return {
        "desde": "o deploy do medidor (histórico anterior: console.anthropic.com → Usage)",
        "por_robo": out,
        "total_usd": round(total_usd, 2),
        "total_brl": round(total_usd * USD_BRL, 2),
        "cambio_usado": USD_BRL,
    }
