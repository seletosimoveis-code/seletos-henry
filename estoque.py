"""
estoque.py
==========
E3 — Motor de Oferta (o HeyLeo da Seletos). Duas frentes:

1. OFERTA AO VIVO: o Gabriel consulta o estoque real do site durante a conversa
   e OFERECE 2-3 imóveis específicos (título, bairro, preço, ref, link) em vez
   de mandar link genérico de seção.

2. MATCHING DEmanda: cruza os leads da aba "DEmanda | Procura de imóvel" com o
   inventário do site → grava em Imóveis Potenciais + avisa o cliente (voz do
   Gabriel) + tarefa para o corretor. Aprovado pelo Felipe em 11-12/07/2026.

Fonte: páginas de listagem do site (server-rendered, validado 12/07):
  /aluguel-anual/ e /venda/ (+ /pagina/N) → hrefs /imovel/{slug}-ref-NNN/
  O slug carrega tipo, cidade, bairro, quartos, garagens e finalidade.
  Preço: página individual (maior R$ dentro da faixa da finalidade).

Segurança: notificação de match respeita janela (seg-sex 8-19h, sáb 8-12h),
pausa humana, EQUIPE_PHONES, 1 aviso por imóvel por lead (nunca repete),
máximo diário global (MATCH_MAX_DIA). Gabriel só oferta o que está no bloco.
"""

import os
import re
import time
import logging
import requests
from datetime import datetime, timezone, timedelta

from config import KOMMO_SUBDOMAIN, KOMMO_TOKEN
from kommo import is_equipe_phone
from crm_enricher import JUNK_PRICE_MAX
import store

logger = logging.getLogger(__name__)

SITE   = "https://www.seletosimoveis.com"
_BR_TZ = timezone(timedelta(hours=-3))
_BASE  = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"
_UA    = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SeletosBot/1.0"}

TTL_MIN       = int(os.getenv("ESTOQUE_TTL_MIN", "45"))
MAX_PAGES     = int(os.getenv("ESTOQUE_MAX_PAGES", "8"))
MATCH_MAX_DIA = int(os.getenv("MATCH_MAX_DIA", "20"))

F_IMOVEIS_POTENCIAIS = 1328598

_zapi   = None
_kommo  = None
_is_paused_fn = None

_inv:    dict = {"ts": 0, "items": {}}   # ref → item
_prices: dict = {}                        # ref → (preco|None, ts)

_CIDADES = ["natal", "parnamirim", "acu", "assu", "mossoro", "sao-goncalo",
            "extremoz", "macaiba", "ceara-mirim", "nisia-floresta"]
_TIPOS = ["apartamento", "casa", "terreno", "lote", "galpao", "deposito",
          "sala", "loja", "ponto", "predio", "sobrado", "flat", "kitnet",
          "studio", "cobertura", "sitio", "chacara", "comercial", "rural"]


def init(zapi, kommo, is_paused_fn) -> None:
    global _zapi, _kommo, _is_paused_fn
    _zapi, _kommo, _is_paused_fn = zapi, kommo, is_paused_fn


def _hdr():
    return {"Authorization": f"Bearer {KOMMO_TOKEN}", "Content-Type": "application/json"}


def _norm(s: str) -> str:
    s = (s or "").lower()
    troca = str.maketrans("áàâãéêíóôõúüç", "aaaaeeiooouuc")
    return s.translate(troca).strip()


# ─── Inventário ───────────────────────────────────────────────────────────────

def _parse_slug(href: str) -> dict | None:
    m = re.match(r"/imovel/([a-z0-9\-]+)-ref-(\d+)/?$", href)
    if not m:
        return None
    slug, ref = m.group(1), m.group(2)
    toks = slug.split("-")

    finalidade = "aluguel" if "aluguel" in toks else ("venda" if "venda" in toks else "")
    tipo = next((t for t in _TIPOS if t in toks), "")
    cidade, ci = "", -1
    for c in _CIDADES:
        parts = c.split("-")
        for i in range(len(toks) - len(parts) + 1):
            if toks[i:i + len(parts)] == parts:
                cidade, ci = c.replace("-", " "), i + len(parts) - 1
                break
        if cidade:
            break

    q = re.search(r"(\d+)-quartos?", slug)
    quartos = int(q.group(1)) if q else None
    g = re.search(r"(\d+)-garage", slug)
    garagens = int(g.group(1)) if g else None

    # bairro = tokens entre a cidade e o primeiro marcador (quartos/garagem/finalidade)
    stop = {"quartos", "quarto", "garagem", "garagens", "aluguel", "venda", "suite", "suites"}
    bairro_toks = []
    for t in toks[ci + 1:] if ci >= 0 else []:
        if t.isdigit() or t in stop:
            break
        bairro_toks.append(t)
    bairro = " ".join(bairro_toks)

    return {
        "ref": ref, "href": href, "link": SITE + href,
        "finalidade": finalidade, "tipo": tipo, "cidade": cidade,
        "bairro": bairro, "quartos": quartos, "garagens": garagens,
    }


def fetch_inventory(force: bool = False) -> dict:
    """Baixa/atualiza o inventário do site (cache com TTL). Retorna ref→item."""
    if not force and _inv["items"] and time.time() - _inv["ts"] < TTL_MIN * 60:
        return _inv["items"]

    items: dict = {}
    for secao in ("aluguel-anual", "venda"):
        vistos_secao = set()
        for page in range(1, MAX_PAGES + 1):
            url = f"{SITE}/{secao}/" if page == 1 else f"{SITE}/{secao}/pagina/{page}"
            try:
                r = requests.get(url, headers=_UA, timeout=20)
                if r.status_code != 200:
                    break
                hrefs = set(re.findall(r'href="(/imovel/[a-z0-9\-]+-ref-\d+/)"', r.text))
            except Exception as e:
                logger.warning(f"estoque: {url}: {e}")
                break
            novos = hrefs - vistos_secao
            if not novos:
                break
            vistos_secao |= hrefs
            for h in novos:
                item = _parse_slug(h)
                if item:
                    items[item["ref"]] = item
            time.sleep(0.3)
    if items:
        _inv["items"], _inv["ts"] = items, time.time()
        logger.info(f"estoque: inventário atualizado — {len(items)} imóveis")
    return _inv["items"] or {}


def _preco(item: dict) -> int | None:
    """Preço do imóvel (página individual; cache 24h). Maior R$ na faixa da finalidade."""
    ref = item["ref"]
    cached = _prices.get(ref)
    if cached and time.time() - cached[1] < 86400:
        return cached[0]
    preco = None
    try:
        r = requests.get(item["link"], headers=_UA, timeout=15)
        vals = []
        for p in re.findall(r"R\$\s?([\d\.]+)(?:,\d{2})?", r.text):
            try:
                vals.append(int(p.replace(".", "")))
            except ValueError:
                continue
        faixa = (300, 60_000) if item["finalidade"] == "aluguel" else (30_000, 60_000_000)
        na_faixa = [v for v in vals if faixa[0] <= v <= faixa[1]]
        preco = max(na_faixa) if na_faixa else None
    except Exception as e:
        logger.warning(f"estoque: preço ref {ref}: {e}")
    _prices[ref] = (preco, time.time())
    return preco


# ─── Busca ────────────────────────────────────────────────────────────────────

def search(finalidade: str, tipo: str = "", cidade: str = "", bairro: str = "",
           max_price: int = 0, dorm_min: int = 0, limit: int = 3) -> list[dict]:
    inv = fetch_inventory()
    tipo_n, cid_n, bai_n = _norm(tipo), _norm(cidade), _norm(bairro)
    if tipo_n in ("apto", "ap", "apartamento"):
        tipo_n = "apartamento"

    def ok(it):
        if it["finalidade"] != finalidade:
            return False
        if tipo_n and tipo_n not in _norm(it["tipo"]) and _norm(it["tipo"]) not in tipo_n:
            return False
        if cid_n and cid_n not in _norm(it["cidade"]):
            return False
        if dorm_min and it["quartos"] and it["quartos"] < dorm_min:
            return False
        return True

    cands = [it for it in inv.values() if ok(it)]
    # bairro: match forte primeiro, mas não elimina (bairros vizinhos valem)
    if bai_n:
        cands.sort(key=lambda it: 0 if (bai_n in _norm(it["bairro"]) or _norm(it["bairro"]) in bai_n) else 1)
        exatos = [it for it in cands if bai_n in _norm(it["bairro"]) or _norm(it["bairro"]) in bai_n]
        if exatos:
            cands = exatos + [it for it in cands if it not in exatos][:3]

    out = []
    for it in cands[:10]:
        p = _preco(it)
        if max_price and p and p > max_price * 1.15:
            continue
        out.append({**it, "preco": p})
        if len(out) >= limit:
            break
    return out


def _fmt_preco(p, finalidade):
    if not p:
        return "consulte"
    txt = f"R$ {p:,.0f}".replace(",", ".")
    return f"{txt}/mês" if finalidade == "aluguel" else txt


def format_ofertas(items: list[dict]) -> str:
    linhas = [
        "══════════════════════════════════════════════════",
        "IMÓVEIS REAIS DISPONÍVEIS AGORA NO SITE (fonte única de oferta):",
    ]
    for it in items:
        desc = f"{(it['tipo'] or 'imóvel').title()}"
        if it["quartos"]:
            desc += f" {it['quartos']}Q"
        desc += f" — {it['bairro'].title() or it['cidade'].title()}"
        if it["cidade"]:
            desc += f", {it['cidade'].title()}"
        linhas.append(f"• ref {it['ref']}: {desc} — {_fmt_preco(it.get('preco'), it['finalidade'])}")
        linhas.append(f"  {it['link']}")
    linhas += [
        "REGRAS DA OFERTA: apresente 2-3 destes quando o momento pedir opções — ",
        "sempre com ref, preço e link, e convide para VISITA. Se nenhum servir, ",
        "use o link de seção. É PROIBIDO citar imóvel que não esteja nesta lista.",
        "══════════════════════════════════════════════════",
    ]
    return "\n".join(linhas)


def _parse_orcamento(v) -> int:
    if not v:
        return 0
    d = re.sub(r"\D", "", str(v))
    return int(d) if d else 0


def search_for_ctx(funil: str, ctx: dict, user_msg: str = "") -> list[dict]:
    """Busca dirigida pelo contexto do lead — usada pelo Gabriel ao vivo."""
    finalidade = "aluguel" if funil == "aluguel" else (
        "venda" if funil in ("avulso", "lancamentos", "investidor") else "")
    if not finalidade:
        return []
    tipo   = ctx.get("tipo_imovel", "")
    bairro = ctx.get("bairro", "")
    if not (tipo or bairro):
        return []
    dorm = 0
    try:
        dorm = int(re.sub(r"\D", "", str(ctx.get("dormitorios") or ""))[:1] or 0)
    except ValueError:
        pass
    return search(
        finalidade, tipo=tipo, bairro=bairro,
        max_price=_parse_orcamento(ctx.get("orcamento")), dorm_min=dorm, limit=3,
    )


# ─── Matching DEmanda ─────────────────────────────────────────────────────────

def _in_window(dt: datetime) -> bool:
    wd = dt.weekday()
    if wd == 6:
        return False
    if wd == 5:
        return 8 <= dt.hour < 12
    return 8 <= dt.hour < 19


F_TIPO, F_BAIRRO, F_DORM = 1312432, 1312436, 1328592


def run_matching(dry_run: bool = True, batch: int = 15, notificar: bool = True) -> dict:
    """
    Cruza leads da DEmanda com o inventário. Modo real: grava Imóveis
    Potenciais + avisa o cliente (1 imóvel/rodada) + tarefa pro corretor.
    """
    agora = datetime.now(_BR_TZ)
    if not dry_run and notificar and not _in_window(agora):
        return {"status": "abortado", "motivo": "notificação só na janela seg-sex 8-19h / sáb 8-12h"}

    hoje = agora.strftime("%Y-%m-%d")
    ja_hoje = store.all_state("match_dia").get("global")
    enviados_hoje = int(ja_hoje.get("n", 0)) if isinstance(ja_hoje, dict) and ja_hoje.get("d") == hoje else 0

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
             "notificados": 0, "pulados_sem_perfil": 0, "pulados_estado": 0}
    propostas = []

    for ld in leads:
        if stats["avaliados"] >= batch:
            break
        lead_id = ld.get("id")
        cf = {c.get("field_id"): c for c in (ld.get("custom_fields_values") or [])}

        def val(fid):
            v = (cf.get(fid) or {}).get("values") or []
            return str(v[0].get("value", "")) if v else ""

        tipo, bairro = val(F_TIPO), val(F_BAIRRO)
        if not (tipo or bairro):
            stats["pulados_sem_perfil"] += 1
            continue
        stats["avaliados"] += 1

        price = int(ld.get("price") or 0)
        matches = search(
            fin_by_pipe.get(ld.get("pipeline_id"), "aluguel"),
            tipo=tipo, bairro=bairro,
            max_price=price if price >= JUNK_PRICE_MAX else 0, limit=3,
        )
        novos = [m for m in matches
                 if not store.all_state(f"match_{lead_id}").get(m["ref"])]
        if not novos:
            continue
        stats["com_match"] += 1
        top = novos[0]
        propostas.append(
            f"{lead_id} · {ld.get('name')} ← ref {top['ref']} "
            f"({top['tipo']} {top['bairro']}, {_fmt_preco(top.get('preco'), top['finalidade'])})"
        )

        if dry_run:
            continue

        # Imóveis Potenciais: APPEND aditivo (nunca apaga o que já existe)
        atual = val(F_IMOVEIS_POTENCIAIS)
        if top["ref"] not in atual:
            novo_txt = (atual + "\n" if atual else "") + \
                f"[match {agora.strftime('%d/%m')}] ref {top['ref']} — {top['link']}"
            try:
                requests.patch(
                    f"{_BASE}/leads/{lead_id}", headers=_hdr(), timeout=10,
                    json={"custom_fields_values": [
                        {"field_id": F_IMOVEIS_POTENCIAIS,
                         "values": [{"value": novo_txt[:2000]}]}]},
                )
            except Exception as e:
                logger.warning(f"matching: potenciais lead {lead_id}: {e}")

        _kommo.add_task(
            lead_id,
            f"🎯 MATCH: ref {top['ref']} casa com este lead — cliente será/foi avisado. Acompanhar!",
            14400,
        )

        # Notificação ao cliente (voz do Gabriel) — com todas as travas
        if notificar and enviados_hoje < MATCH_MAX_DIA:
            phone, nome, _ctx2 = _kommo.get_lead_phone_and_context(lead_id)
            if phone and not is_equipe_phone(phone) and not (_is_paused_fn and _is_paused_fn(phone)):
                primeiro = (nome or "").split("|")[0].split()[0].title() if nome else ""
                desc = f"{(top['tipo'] or 'imóvel').title()}"
                if top["quartos"]:
                    desc += f" de {top['quartos']} quartos"
                msg = (
                    f"Oi{', ' + primeiro if primeiro else ''}! Sou Gabriel da Seletos 😊 "
                    f"Acabou de bater com o seu perfil: {desc} em "
                    f"{(top['bairro'] or top['cidade']).title()} por "
                    f"{_fmt_preco(top.get('preco'), top['finalidade'])} — ref {top['ref']}:\n"
                    f"{top['link']}\n"
                    f"Quer que eu agende uma visita?"
                )
                try:
                    _zapi.send_text(phone, msg)
                    stats["notificados"] += 1
                    enviados_hoje += 1
                    time.sleep(2)
                except Exception as e:
                    logger.warning(f"matching: envio lead {lead_id}: {e}")
            else:
                stats["pulados_estado"] += 1
        store.set_state(f"match_{lead_id}", top["ref"], agora.strftime("%d/%m/%Y"))

    if not dry_run:
        store.set_state("global", "match_dia", {"d": hoje, "n": enviados_hoje})

    resultado = {
        "status": "simulação" if dry_run else "executado",
        "inventario": len(fetch_inventory()),
        **stats,
        "propostas": propostas[:40],
    }
    store.set_state("global", "matching_status", resultado)
    logger.info(f"matching: {resultado['status']} — {stats}")
    return resultado
