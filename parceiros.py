"""
parceiros.py
============
EDUARDO E2 — Varredura de parceiros (a multiplicação de estoque).

Varre os sites das construtoras e imobiliárias parceiras aprovadas pelo
Felipe (Parcerias 12/07/2026) e cruza cada imóvel/empreendimento com a aba
DEmanda. Match → grava o LINK em Imóveis Potenciais + tarefa "🤝 validar
parceria" + entra no relatório interno.

REGRA DE OURO (blueprint Eduardo, inviolável):
  Eduardo NUNCA fala com cliente final. Link de parceiro só chega ao cliente
  pela mão de um HUMANO, após validar a parceria (50% dos honorários).
  Este módulo portanto NÃO tem nenhum caminho de envio via Z-API — de
  propósito. Não adicione um.

Estratégia técnica (sites heterogêneos, sem padrão de slug como o nosso):
  1. Para cada parceiro, visita páginas-semente (home + caminhos comuns de
     listagem) e coleta links internos que pareçam ser de imóvel/empreendimento.
  2. Em cada página de item: título (og:title/<title>), json-ld quando houver,
     preço = maior R$ dentro da faixa da finalidade (mesma heurística do E3).
  3. Classifica tipo/cidade/finalidade por tokens na URL + título.
  Inventário fica em cache no store (TTL padrão 12h) — a varredura completa
  só roda 2x/dia no máximo, com pausa entre requests (sites de parceiro
  merecem respeito).
"""

import os
import re
import json
import time
import hashlib
import logging
import requests
from datetime import datetime, timezone, timedelta

from config import KOMMO_SUBDOMAIN, KOMMO_TOKEN
import store

logger = logging.getLogger(__name__)

_BR_TZ = timezone(timedelta(hours=-3))
_BASE  = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"
_UA    = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SeletosBot/1.0"}

TTL_H          = int(os.getenv("PARCEIROS_TTL_H", "12"))
MAX_SEEDS      = int(os.getenv("PARCEIROS_MAX_SEEDS", "6"))    # páginas-semente por parceiro
MAX_ITENS_SITE = int(os.getenv("PARCEIROS_MAX_ITENS", "40"))   # itens detalhados por parceiro
SLEEP_S        = float(os.getenv("PARCEIROS_SLEEP_S", "0.6"))

F_IMOVEIS_POTENCIAIS = 1328598
F_TIPO, F_BAIRRO     = 1312432, 1312436

_kommo = None


def init(kommo) -> None:
    global _kommo
    _kommo = kommo


# ─── Parceiros aprovados (curadoria Felipe 12/07/2026) ────────────────────────
# construtora = lançamentos (VENDA); imobiliaria = carteira (venda + aluguel)

PARCEIROS = [
    {"nome": "Andrade Marinho", "site": "https://andrademarinho.com.br",          "tipo": "construtora"},
    {"nome": "Ecocil",          "site": "https://ecocil.com.br",                  "tipo": "construtora"},
    {"nome": "Moura Dubeux",    "site": "https://mouradubeux.com.br",             "tipo": "construtora"},
    {"nome": "Dois A",          "site": "https://doisa.com.br",                   "tipo": "construtora"},
    {"nome": "Colméia",         "site": "https://colmeia.com.br",                 "tipo": "construtora"},
    {"nome": "Licenge",         "site": "https://licenge.com.br",                 "tipo": "construtora"},
    {"nome": "Construfit",      "site": "https://construfitengenharia.com.br",    "tipo": "construtora"},
    {"nome": "Atlantis",        "site": "https://construtoraatlantis.com.br",     "tipo": "construtora"},
    {"nome": "Aldann",          "site": "https://aldann.com.br",                  "tipo": "construtora"},
    {"nome": "Alliance",        "site": "https://alliance.com.br",                "tipo": "construtora"},
    {"nome": "Terraz",          "site": "https://terrazincorporacoes.com.br",     "tipo": "construtora"},
    {"nome": "Constel",         "site": "https://constelempreendimentos.com.br",  "tipo": "construtora"},
    {"nome": "RN Imóveis",      "site": "https://rnimoveis.com.br",               "tipo": "imobiliaria"},
    {"nome": "NL Imóveis",      "site": "https://nlimoveis.com.br",               "tipo": "imobiliaria"},
    {"nome": "Emobi",           "site": "https://emobiimobiliaria.com.br",        "tipo": "imobiliaria"},
    {"nome": "Outlet Imóveis",  "site": "https://outletimoveisrn.com.br",         "tipo": "imobiliaria"},
    {"nome": "Habitacional",    "site": "https://habitacionalonline.com.br",      "tipo": "imobiliaria"},
    {"nome": "Natal RN Imóveis","site": "https://natalrnimoveis.com",             "tipo": "imobiliaria"},
    {"nome": "Caio Fernandes",  "site": "https://caiofernandes.com.br",           "tipo": "imobiliaria"},
]

# Caminhos comuns de listagem tentados em cada site (além da home)
_SEED_PATHS = ["", "/empreendimentos", "/empreendimentos/", "/imoveis", "/imoveis/",
               "/lancamentos", "/lancamentos/", "/venda", "/aluguel", "/portfolio"]

# Um link interno "parece imóvel/empreendimento" se a URL contém:
_ITEM_HINTS = ("imovel", "imoveis/", "empreendimento", "lancamento", "apartamento",
               "casa-", "/casa/", "residencial", "condominio", "detalhe", "property",
               "-ref-", "cod-", "codigo")

_CIDADES = ["natal", "parnamirim", "acu", "assu", "mossoro", "sao-goncalo",
            "extremoz", "macaiba", "ceara-mirim", "nisia-floresta",
            "sao goncalo", "ceara mirim", "nisia floresta"]
_TIPOS = ["apartamento", "casa", "terreno", "lote", "galpao", "sala", "loja",
          "predio", "sobrado", "flat", "kitnet", "studio", "cobertura",
          "comercial", "residencial", "condominio"]


def _norm(s: str) -> str:
    s = (s or "").lower()
    return s.translate(str.maketrans("áàâãéêíóôõúüç", "aaaaeeiooouuc")).strip()


def _get(url: str, timeout: int = 15) -> str:
    try:
        r = requests.get(url, headers=_UA, timeout=timeout, allow_redirects=True)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", "html"):
            return r.text
    except Exception as e:
        logger.debug(f"parceiros: {url}: {e}")
    return ""


def _hdr():
    return {"Authorization": f"Bearer {KOMMO_TOKEN}", "Content-Type": "application/json"}


# ─── Extração ─────────────────────────────────────────────────────────────────

def _discover_item_links(site: str) -> list[str]:
    """Links internos que parecem página de imóvel/empreendimento."""
    dominio = re.sub(r"^https?://(www\.)?", "", site).rstrip("/")
    found: list[str] = []
    seen: set = set()
    for path in _SEED_PATHS[:MAX_SEEDS + 1]:
        html = _get(site.rstrip("/") + path)
        if not html:
            continue
        for href in re.findall(r'href="([^"#?]+)"', html):
            if href.startswith("/"):
                url = site.rstrip("/") + href
            elif dominio in href and href.startswith("http"):
                url = href.split("#")[0]
            else:
                continue
            url = url.rstrip("/")
            low = _norm(url)
            if url in seen or len(url) > 200:
                continue
            # precisa parecer item E não ser página institucional
            if any(h in low for h in _ITEM_HINTS) and not any(
                    x in low for x in ("sobre", "contato", "blog", "politica",
                                       "trabalhe", "login", "wp-", ".jpg", ".png",
                                       ".pdf", "facebook", "instagram", "whatsapp")):
                seen.add(url)
                found.append(url)
        time.sleep(SLEEP_S)
        if len(found) >= MAX_ITENS_SITE * 2:
            break
    return found[:MAX_ITENS_SITE]


def _jsonld_hints(html: str) -> dict:
    """Extrai nome/preço de blocos json-ld quando existirem."""
    out = {}
    for bloco in re.findall(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', html, re.S)[:5]:
        try:
            data = json.loads(bloco.strip())
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            t = str(obj.get("@type", ""))
            if any(k in t for k in ("Residence", "Apartment", "House", "Product",
                                    "RealEstate", "Offer", "Place")):
                out.setdefault("nome", obj.get("name"))
                offers = obj.get("offers") or {}
                if isinstance(offers, dict) and offers.get("price"):
                    try:
                        out.setdefault("preco", int(float(offers["price"])))
                    except (ValueError, TypeError):
                        pass
    return out


def _parse_item(url: str, parceiro: dict) -> dict | None:
    html = _get(url)
    if not html:
        return None
    low_all = _norm(url + " " + html[:4000])

    # título
    m = (re.search(r'property="og:title" content="([^"]+)"', html)
         or re.search(r"<title>([^<]+)</title>", html))
    titulo = (m.group(1).strip() if m else url.rsplit("/", 1)[-1].replace("-", " "))[:120]

    ld = _jsonld_hints(html)
    if ld.get("nome"):
        titulo = str(ld["nome"])[:120]

    low = _norm(url + " " + titulo)
    finalidade = "aluguel" if any(k in low for k in ("aluguel", "locacao", "alugar")) \
        else "venda"
    if parceiro["tipo"] == "construtora":
        finalidade = "venda"                     # lançamento é sempre venda

    tipo   = next((t for t in _TIPOS if t in low), "")
    cidade = next((c for c in _CIDADES if c in low or c in low_all), "")

    preco = ld.get("preco")
    if not preco:
        vals = []
        for p in re.findall(r"R\$\s?([\d\.]+)(?:,\d{2})?", html):
            try:
                vals.append(int(p.replace(".", "")))
            except ValueError:
                continue
        faixa = (300, 60_000) if finalidade == "aluguel" else (60_000, 60_000_000)
        na_faixa = [v for v in vals if faixa[0] <= v <= faixa[1]]
        preco = max(na_faixa) if na_faixa else None

    return {
        "url": url, "titulo": titulo, "fonte": parceiro["nome"],
        "fonte_tipo": parceiro["tipo"], "finalidade": finalidade,
        "tipo": tipo, "cidade": cidade.replace("-", " "), "preco": preco,
        "texto_busca": low + " " + _norm(titulo),
    }


# ─── Inventário (cache no store, TTL 12h) ─────────────────────────────────────

def fetch_inventory(force: bool = False, so_parceiro: str = "") -> dict:
    """url→item de TODOS os parceiros. Cache persistente (sobrevive a deploy)."""
    cache = store.all_state("parc_inv").get("global") or {}
    if (not force and isinstance(cache, dict) and cache.get("items")
            and time.time() - float(cache.get("ts", 0)) < TTL_H * 3600):
        return cache["items"]

    items: dict = dict(cache.get("items") or {}) if isinstance(cache, dict) else {}
    filtro = _norm(so_parceiro)
    for p in PARCEIROS:
        if filtro and filtro not in _norm(p["nome"]):
            continue
        try:
            links = _discover_item_links(p["site"])
            n_novo = 0
            for url in links:
                it = _parse_item(url, p)
                time.sleep(SLEEP_S)
                if it and (it["tipo"] or it["preco"] or it["cidade"]):
                    items[url] = it
                    n_novo += 1
            logger.info(f"parceiros: {p['nome']} — {len(links)} links, {n_novo} itens úteis")
        except Exception as e:
            logger.warning(f"parceiros: varredura {p['nome']} falhou: {e}")

    store.set_state("global", "parc_inv", {"ts": time.time(), "items": items})
    return items


def cached_inventory() -> dict:
    """Só o cache — NUNCA dispara varredura (usado pelo matching/relatório,
    que não podem travar minutos varrendo 19 sites)."""
    cache = store.all_state("parc_inv").get("global") or {}
    return cache.get("items") or {} if isinstance(cache, dict) else {}


# ─── Matching × DEmanda ───────────────────────────────────────────────────────

def _score(item: dict, tipo_d: str, bairro_d: str, cidade_ok: bool) -> int:
    s = 0
    if tipo_d and (tipo_d in item["texto_busca"] or item["tipo"] in tipo_d):
        s += 2
    if bairro_d and bairro_d in item["texto_busca"]:
        s += 3
    if cidade_ok:
        s += 1
    return s


def run_matching(dry_run: bool = True, batch: int = 20) -> dict:
    """
    Cruza DEmanda × inventário de parceiros. Modo real: Imóveis Potenciais
    (append) + tarefa 🤝 para HUMANO validar. NUNCA notifica o cliente.
    """
    agora = datetime.now(_BR_TZ)
    inv = cached_inventory()
    if not inv:
        return {"status": "vazio", "detalhe": "inventário de parceiros vazio — rode /admin/parceiros?refresh=1"}

    from demandas import _demanda_statuses
    pares = _demanda_statuses()
    fin_by_pipe = {pid: ("aluguel" if fin == "aluguel" else "venda") for pid, _s, fin in pares}

    params: dict = {"limit": 250, "page": 1}
    for i, (pid, sid, _f) in enumerate(pares):
        params[f"filter[statuses][{i}][pipeline_id]"] = pid
        params[f"filter[statuses][{i}][status_id]"]   = sid
    try:
        r = requests.get(f"{_BASE}/leads", headers=_hdr(), params=params, timeout=25)
        leads = r.json().get("_embedded", {}).get("leads", []) if r.status_code == 200 else []
    except Exception as e:
        return {"status": "erro", "detalhe": str(e)}

    stats = {"demanda_total": len(leads), "avaliados": 0, "com_match": 0,
             "gravados": 0, "pulados_sem_perfil": 0}
    propostas = []

    for ld in leads:
        if stats["avaliados"] >= batch:
            break
        lead_id = ld.get("id")
        cf = {c.get("field_id"): c for c in (ld.get("custom_fields_values") or [])}

        def val(fid):
            v = (cf.get(fid) or {}).get("values") or []
            return str(v[0].get("value", "")) if v else ""

        tipo_d, bairro_d = _norm(val(F_TIPO)), _norm(val(F_BAIRRO))
        if tipo_d in ("apto", "ap"):
            tipo_d = "apartamento"
        if not (tipo_d or bairro_d):
            stats["pulados_sem_perfil"] += 1
            continue
        stats["avaliados"] += 1

        fin_d = fin_by_pipe.get(ld.get("pipeline_id"), "aluguel")
        price = int(ld.get("price") or 0)

        cands = []
        for it in inv.values():
            if it["finalidade"] != fin_d:
                continue
            if price >= 500 and it["preco"] and it["preco"] > price * 1.25:
                continue
            cidade_ok = (not it["cidade"]) or it["cidade"] in ("natal",) or \
                        (bairro_d and it["cidade"] in bairro_d)
            sc = _score(it, tipo_d, bairro_d, cidade_ok)
            if sc >= 3:                      # exige tipo+cidade ou bairro
                cands.append((sc, it))
        if not cands:
            continue
        cands.sort(key=lambda x: -x[0])
        top = cands[0][1]

        # 1 proposta por (lead, url) para sempre — nunca repete
        h = hashlib.md5(top["url"].encode()).hexdigest()[:10]
        if store.all_state(f"match_parc_{lead_id}").get(h):
            continue
        stats["com_match"] += 1

        phone, _nome, _c = _kommo.get_lead_phone_and_context(lead_id)
        link_lead = f"https://{KOMMO_SUBDOMAIN}.kommo.com/leads/detail/{lead_id}"
        preco_txt = f"R$ {top['preco']:,.0f}".replace(",", ".") if top["preco"] else "consulte"
        propostas.append(
            f"{ld.get('name')} · 📱 {phone or 'sem fone'}\n"
            f"     🤝 {top['fonte']}: {top['titulo'][:70]} ({preco_txt})\n"
            f"     🔗 lead: {link_lead}\n"
            f"     🔗 imóvel: {top['url']}"
        )

        if dry_run:
            continue

        atual = val(F_IMOVEIS_POTENCIAIS)
        if top["url"] not in atual:
            novo_txt = (atual + "\n" if atual else "") + \
                f"[🤝 parceiro {agora.strftime('%d/%m')}] {top['fonte']}: {top['url']}"
            try:
                requests.patch(
                    f"{_BASE}/leads/{lead_id}", headers=_hdr(), timeout=10,
                    json={"custom_fields_values": [
                        {"field_id": F_IMOVEIS_POTENCIAIS,
                         "values": [{"value": novo_txt[:2000]}]}]},
                )
            except Exception as e:
                logger.warning(f"parceiros: potenciais lead {lead_id}: {e}")

        _kommo.add_task(
            lead_id,
            f"🤝 MATCH DE PARCEIRO ({top['fonte']}): {top['titulo'][:60]} — "
            f"VALIDAR a parceria antes de ofertar. NUNCA enviar o link direto "
            f"ao cliente sem validação. 📱 {phone or 'sem fone'}",
            14400,
        )
        store.set_state(f"match_parc_{lead_id}", h, agora.strftime("%d/%m/%Y"))
        stats["gravados"] += 1
        time.sleep(0.2)

    resultado = {
        "status": "simulação" if dry_run else "executado",
        "inventario_parceiros": len(inv),
        **stats,
        "propostas": propostas[:40],
    }
    store.set_state("global", "parceiros_status", resultado)
    logger.info(f"parceiros: {resultado['status']} — {stats}")
    return resultado
