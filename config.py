"""
config.py
=========
Todas as variáveis de ambiente do bot.
Em desenvolvimento: crie um arquivo .env na raiz do projeto.
Em produção (Railway): configure as variáveis no dashboard.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Kommo ────────────────────────────────────────────────────────────────────
KOMMO_SUBDOMAIN = os.environ.get("KOMMO_SUBDOMAIN", "seletosimoveis")
KOMMO_TOKEN     = os.environ.get("KOMMO_TOKEN", "")

# ─── Anthropic (Claude) ───────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Henry (SDR) — modelo rápido/barato para triagem simples
CLAUDE_MODEL  = os.environ.get("CLAUDE_MODEL",  "claude-haiku-4-5-20251001")

# Gabriel (Qualificador) — modelo mais inteligente; fallback para CLAUDE_MODEL
GABRIEL_MODEL = os.environ.get("GABRIEL_MODEL", CLAUDE_MODEL)

# ─── Z-API ────────────────────────────────────────────────────────────────────
ZAPI_INSTANCE_ID  = os.environ.get("ZAPI_INSTANCE_ID", "")
ZAPI_TOKEN        = os.environ.get("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.environ.get("ZAPI_CLIENT_TOKEN", "")

# ─── OpenAI (Whisper — transcrição de áudios WhatsApp) ───────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ─── Bot ──────────────────────────────────────────────────────────────────────
# Máximo de mensagens no histórico por conversa
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "40"))
