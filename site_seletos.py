"""
site_seletos.py
===============
Busca informações de imóveis no site da Seletos pelo número de referência.

Usado pelo Gabriel para responder perguntas sobre imóveis específicos
sem precisar inventar dados ou enviar links genéricos.
"""

import re
import logging
import requests

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# Padrões de URL a tentar, em ordem
_URL_PATTERNS = [
    "https://www.seletosimoveis.com/imovel/{ref}/",
    "https://www.seletosimoveis.com/imoveis/{ref}/",
    "https://seletosimoveis.com/imovel/{ref}/",
]


def _get_html(ref: str) -> str | None:
    """Tenta buscar o HTML da página do imóvel, testando padrões de URL."""
    for pattern in _URL_PATTERNS:
        url = pattern.format(ref=ref)
        try:
            r = requests.get(url, headers=_HEADERS, timeout=8, allow_redirects=True)
            if r.ok and len(r.text) > 1000:
                logger.info(f"Imóvel #{ref} encontrado em {url}")
                return r.text
        except Exception as e:
            logger.debug(f"URL {url} falhou: {e}")
    return None


def _extract(html: str, ref: str) -> dict:
    """Extrai campos básicos do imóvel via regex no HTML."""

    def og(prop: str) -> str:
        m = re.search(rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not m:
            m = re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{prop}["\']', html, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def meta(name: str) -> str:
        m = re.search(rf'<meta[^>]+name=["\']([^"\']*{name}[^"\']*)["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        return m.group(2).strip() if m else ""

    title       = og("title") or meta("title") or ""
    description = og("description") or meta("description") or ""
    image_url   = og("image") or ""

    # Tenta extrair preço (R$ 1.500,00 ou R$1500)
    price_m = re.search(r'R\$\s*[\d.,]+', html)
    price   = price_m.group(0).strip() if price_m else ""

    # Tenta extrair quartos
    rooms_m = re.search(r'(\d)\s*quarto', html, re.IGNORECASE)
    rooms   = rooms_m.group(0) if rooms_m else ""

    # Área
    area_m = re.search(r'(\d+)\s*m[²2]', html)
    area   = f"{area_m.group(1)} m²" if area_m else ""

    # Banheiros
    bath_m = re.search(r'(\d)\s*banheiro', html, re.IGNORECASE)
    bath   = bath_m.group(0) if bath_m else ""

    # Vagas
    vaga_m = re.search(r'(\d)\s*vaga', html, re.IGNORECASE)
    vaga   = vaga_m.group(0) if vaga_m else ""

    return {
        "ref"        : ref,
        "title"      : title,
        "description": description,
        "price"      : price,
        "rooms"      : rooms,
        "area"       : area,
        "bath"       : bath,
        "vaga"       : vaga,
        "image"      : image_url,
    }


def fetch_imovel_details(ref: str) -> str:
    """
    Retorna uma string formatada com os detalhes do imóvel de referência `ref`.
    Retorna string vazia se o imóvel não for encontrado.
    """
    html = _get_html(ref)
    if not html:
        logger.warning(f"Imóvel #{ref} não encontrado no site")
        return ""

    d = _extract(html, ref)

    # Monta bloco de texto para injetar no prompt do Gabriel
    lines = [f"📋 IMÓVEL #{ref} — dados do site Seletos:"]
    if d["title"]:
        lines.append(f"• Título: {d['title']}")
    if d["price"]:
        lines.append(f"• Preço: {d['price']}")
    if d["area"]:
        lines.append(f"• Área: {d['area']}")
    if d["rooms"]:
        lines.append(f"• Quartos: {d['rooms']}")
    if d["bath"]:
        lines.append(f"• Banheiros: {d['bath']}")
    if d["vaga"]:
        lines.append(f"• Vagas: {d['vaga']}")
    if d["description"]:
        # Trunca descrição para não inflar demais o prompt
        desc = d["description"][:400].strip()
        lines.append(f"• Descrição: {desc}{'...' if len(d['description']) > 400 else ''}")

    url = _URL_PATTERNS[0].format(ref=ref)
    lines.append(f"• Link: {url}")
    lines.append("Use esses dados para responder perguntas sobre este imóvel. NÃO invente nada além do que está aqui.")

    return "\n".join(lines)


def extract_ref_from_text(text: str) -> str | None:
    """
    Extrai número de referência de imóvel de uma mensagem.
    Aceita formatos: #269, ref 269, referência 269, imóvel 269, imovel 269
    """
    m = re.search(
        r'(?:#|ref(?:er[eê]ncia)?\s*[:.]?\s*|im[oó]vel\s+)(\d{2,5})',
        text,
        re.IGNORECASE,
    )
    return m.group(1) if m else None
