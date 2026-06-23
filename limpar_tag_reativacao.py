"""
limpar_tag_reativacao.py
========================
Remove a tag "reativacao enviada" de todos os leads da Recepcao
que a receberam incorretamente (quando o envio Z-API falhou mas
a tag foi adicionada mesmo assim).

Uso:
    python limpar_tag_reativacao.py --dry-run   # mostra o que seria feito
    python limpar_tag_reativacao.py             # remove de verdade
"""

import sys
import os
import re
import time
import argparse
import requests

sys.path.insert(0, os.path.dirname(__file__))
from config import KOMMO_SUBDOMAIN, KOMMO_TOKEN

BASE          = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"
PIPE_RECEPCAO = 9959303
TAG_ALVO      = "reativacao enviada"


def _hdr():
    return {"Authorization": f"Bearer {KOMMO_TOKEN}", "Content-Type": "application/json"}


def _get(path, params=None):
    r = requests.get(f"{BASE}/{path}", headers=_hdr(), params=params or {})
    if r.status_code == 204:
        return {}
    r.raise_for_status()
    return r.json()


def _fetch_leads_com_tag():
    leads = []
    page  = 1
    while True:
        try:
            data = _get("leads", {
                "filter[pipeline_id][]": PIPE_RECEPCAO,
                "with"                 : "tags",
                "page"                 : page,
                "limit"                : 250,
            })
            batch = data.get("_embedded", {}).get("leads", [])
            if not batch:
                break
            for lead in batch:
                tags = lead.get("tags") or []
                if any(t.get("name", "").lower() == TAG_ALVO.lower() for t in tags):
                    leads.append(lead)
            if len(batch) < 250:
                break
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"Erro ao buscar página {page}: {e}")
            break
    return leads


def _remove_tag(lead_id: int, tags_atuais: list):
    novas_tags = [t for t in tags_atuais if t.get("name", "").lower() != TAG_ALVO.lower()]
    r = requests.patch(
        f"{BASE}/leads",
        headers=_hdr(),
        json=[{"id": lead_id, "tags": novas_tags}],
    )
    r.raise_for_status()


def run(dry_run: bool):
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Buscando leads com tag '{TAG_ALVO}'...")
    leads = _fetch_leads_com_tag()
    print(f"Encontrados: {len(leads)} leads\n")

    ok    = 0
    erros = 0
    for lead in leads:
        lid  = lead["id"]
        nome = lead.get("name", "")
        tags = [{"name": t["name"]} for t in (lead.get("tags") or [])]
        print(f"  {'[DRY-RUN] ' if dry_run else ''}Removendo tag de lead {lid} | {nome}")
        if not dry_run:
            try:
                _remove_tag(lid, tags)
                print(f"    ✓ Tag removida")
                ok += 1
                time.sleep(0.5)
            except Exception as e:
                print(f"    ✗ Erro: {e}")
                erros += 1
        else:
            ok += 1

    print(f"\n{'Seria removida' if dry_run else 'Removida'}: {ok} | Erros: {erros}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.dry_run)
