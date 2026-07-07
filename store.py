"""
store.py
========
Persistência de estado dos bots em SQLite — Fase 3 Alta Performance.

Motivação (incidente 05/07/2026): todo o estado vivia em RAM. Um restart do
Railway apagava conversas, pausas humanas e modos ativos — o bot "esquecia"
clientes no meio da qualificação e atropelava atendimentos humanos.

O que é persistido:
  • Histórico de conversas (Henry e Gabriel) — últimas mensagens por telefone
  • Modos: henry_human, gabriel_mode, gabriel_human
  • Funil ativo do Gabriel, contador de turnos, score emitido
  • Pausa por intervenção humana (timestamp de retomada)

Princípios:
  • DEGRADAÇÃO SEGURA: qualquer falha do banco é registrada em log e engolida —
    a persistência NUNCA pode derrubar o atendimento. Sem banco, o bot funciona
    como antes (só RAM).
  • Escrita write-through: RAM continua sendo a fonte de leitura (rápida);
    o banco é atualizado junto e usado apenas para reidratar no startup.
  • Volume Railway: monte um Volume em /data (env DATA_DIR). Sem volume o
    arquivo vive no filesystem efêmero — sobrevive a crash/restart do processo,
    mas não a redeploy.
"""

import os
import time
import json
import sqlite3
import logging
import threading

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") else ".")
DB_PATH  = os.path.join(DATA_DIR, "seletos_state.db")

# Quantas mensagens manter por (bot, telefone) no banco
KEEP_MSGS = 60
# Na reidratação, ignora conversas paradas há mais tempo que isso (o CRM supre)
HYDRATE_MAX_AGE_S = 72 * 3600

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_disabled = False


def _init() -> None:
    global _conn, _disabled
    try:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " bot TEXT NOT NULL, phone TEXT NOT NULL,"
            " role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL)"
        )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_bpt ON messages (bot, phone, id)"
        )
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS state ("
            " phone TEXT NOT NULL, key TEXT NOT NULL, value TEXT,"
            " PRIMARY KEY (phone, key))"
        )
        _conn.commit()
        logger.info(f"store: SQLite pronto em {DB_PATH}")
        if DATA_DIR == ".":
            logger.warning(
                "store: DATA_DIR aponta para o filesystem efêmero — "
                "monte um Volume no Railway em /data para persistência real entre deploys"
            )
    except Exception as e:
        _disabled = True
        logger.error(f"store: SQLite indisponível ({e}) — rodando SEM persistência")


_init()


def _exec(sql: str, params: tuple = ()) -> None:
    if _disabled or _conn is None:
        return
    try:
        with _lock:
            _conn.execute(sql, params)
            _conn.commit()
    except Exception as e:
        logger.warning(f"store: falha de escrita ignorada — {e}")


def _query(sql: str, params: tuple = ()) -> list[tuple]:
    if _disabled or _conn is None:
        return []
    try:
        with _lock:
            return _conn.execute(sql, params).fetchall()
    except Exception as e:
        logger.warning(f"store: falha de leitura ignorada — {e}")
        return []


# ─── Mensagens ────────────────────────────────────────────────────────────────

def append_msg(bot: str, phone: str, role: str, content: str) -> None:
    """Registra uma mensagem e poda o histórico antigo do mesmo telefone."""
    _exec(
        "INSERT INTO messages (bot, phone, role, content, ts) VALUES (?,?,?,?,?)",
        (bot, phone, role, content, time.time()),
    )
    _exec(
        "DELETE FROM messages WHERE bot=? AND phone=? AND id NOT IN "
        "(SELECT id FROM messages WHERE bot=? AND phone=? ORDER BY id DESC LIMIT ?)",
        (bot, phone, bot, phone, KEEP_MSGS),
    )


def clear_msgs(bot: str, phone: str) -> None:
    _exec("DELETE FROM messages WHERE bot=? AND phone=?", (bot, phone))


def recent_conversations(bot: str, limit_per_phone: int = 40) -> dict[str, list[dict]]:
    """
    Retorna {phone: [{'role','content'}, ...]} das conversas com atividade
    nas últimas HYDRATE_MAX_AGE_S. Usado só na reidratação do startup.
    """
    cutoff = time.time() - HYDRATE_MAX_AGE_S
    rows = _query(
        "SELECT phone, role, content FROM messages "
        "WHERE bot=? AND phone IN "
        " (SELECT DISTINCT phone FROM messages WHERE bot=? AND ts>?) "
        "ORDER BY id ASC",
        (bot, bot, cutoff),
    )
    convs: dict[str, list[dict]] = {}
    for phone, role, content in rows:
        convs.setdefault(phone, []).append({"role": role, "content": content})
    for phone in convs:
        convs[phone] = convs[phone][-limit_per_phone:]
    return convs


# ─── Estado (chave/valor por telefone) ────────────────────────────────────────

def set_state(phone: str, key: str, value) -> None:
    _exec(
        "INSERT INTO state (phone, key, value) VALUES (?,?,?) "
        "ON CONFLICT(phone, key) DO UPDATE SET value=excluded.value",
        (phone, key, json.dumps(value)),
    )


def del_state(phone: str, key: str) -> None:
    _exec("DELETE FROM state WHERE phone=? AND key=?", (phone, key))


def all_state(key: str) -> dict[str, object]:
    """Retorna {phone: value} para todos os telefones com essa chave."""
    out: dict[str, object] = {}
    for phone, value in _query("SELECT phone, value FROM state WHERE key=?", (key,)):
        try:
            out[phone] = json.loads(value)
        except Exception:
            continue
    return out


def clear_phone_state(phone: str, keys: list[str]) -> None:
    for k in keys:
        del_state(phone, k)
