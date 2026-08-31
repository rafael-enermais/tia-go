"""
TIA.go - Teste do endpoint de CREDORES (Creditors) do Sienge

Objetivo unico: confirmar 3 coisas antes de eu mexer em schema/ingestao (nao
adivinhar, testar contra a API real - mesma disciplina usada em todos os
outros endpoints deste projeto):

1. O path certo do endpoint (chute educado abaixo, baseado no padrao dos
   outros 2 endpoints ja confirmados: bulk-data/v1/outcome e
   bulk-data/v1/income - o projeto Enerpix original usa um node chamado
   "HTTP Request Credores (creditors)", entao a hipotese e' que o recurso
   se chama "creditors", mas o path completo (bulk-data/v1/creditors ou
   outro) NUNCA foi confirmado pra esse projeto especifico).
2. Se a credencial DEDICADA do TIA.go (a que ja esta no seu .env, NAO a
   "Sienge API - Enerpix" usada no n8n) tem permissao pra esse endpoint -
   pode ser diferente, já vimos isso acontecer com contas a receber.
3. O formato real do JSON - meu projeto irmao (MarIA/Enerpix, n8n) ja
   confirmou os campos `cnpj`, `broker`, `employee`, mas sem ver o JSON
   completo eu nao sei o nome exato de "razao social"/"nome fantasia" nem
   se ha paginacao a considerar.

NUNCA COLOQUE USUARIO/SENHA DIRETO NESTE ARQUIVO - usa o mesmo .env que
ja existe na pasta (arquivos/.env), mesmo padrao do ingest_sienge_to_supabase.py.

Rodar (mesma pasta do .env):
    python test_endpoint_creditors.py
"""

import os
import json

from dotenv import load_dotenv
import requests
from requests.auth import HTTPBasicAuth

load_dotenv()

BASE_URL = os.environ["SIENGE_BASE_URL"].rstrip("/")
USER = os.environ["SIENGE_USER"]
PASSWORD = os.environ["SIENGE_PASSWORD"]
auth = HTTPBasicAuth(USER, PASSWORD)


def testar_creditors():
    # Path CONFIRMADO via screenshot do node "HTTP Request Credores" que
    # roda de verdade no n8n do projeto Enerpix original:
    # https://api.sienge.com.br/enermais/public/api/v1/creditors
    # Ou seja: e' API REST v1 pura, NAO bulk-data/v1 (diferente de
    # outcome/income, que sao bulk-data). Chute anterior (bulk-data/v1/creditors)
    # deu 403 - pode ter sido so o path errado, ou tambem falta de permissao
    # da credencial dedicada do TIA.go. Este teste isola a variavel path;
    # se ainda der 403/401 aqui, o problema e' permissao da credencial.
    url = f"{BASE_URL}/v1/creditors"
    r = requests.get(url, auth=auth, timeout=30)
    print("Creditors ->", r.status_code)
    if r.ok:
        data = r.json()
        registros = data.get("data", data) if isinstance(data, dict) else data
        print(f"  {len(registros)} registros recebidos.")
        if registros:
            print("  Amostra (1o item, JSON completo):")
            print(json.dumps(registros[0], indent=2, ensure_ascii=False))
            print()
            print("  Campos de nivel 1 encontrados:", sorted(registros[0].keys()))
    elif r.status_code == 404:
        print("  404 - path errado (nao necessariamente 'modulo indisponivel').")
        print("  Corpo da resposta:", r.text[:500])
    else:
        print("  Erro:", r.text[:500])


if __name__ == "__main__":
    testar_creditors()
