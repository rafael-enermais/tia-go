"""
TIA.go - Ingestao diaria Sienge (contas a pagar + receber) -> banco radar-comercial, schema `tiago` | v2.0

O QUE MUDOU DA v1 (ingest_sienge_to_supabase.py)
  - Grava por CONEXAO DIRETA no Postgres (Session pooler, porta 5432) com o papel `tiago_ingest`
    (le/insere/atualiza em tiago.*, sem DELETE), em lotes. Antes gravava pela API do Supabase com a
    chave de servico e estourava o tempo limite ("statement timeout") em parte das rodadas.
  - Cada tabela e' gravada numa transacao propria. Se uma falhar, as OUTRAS ainda sao tentadas
    (antes, a falha em parcelas_pagar impedia o snapshot do dia) e a rodada termina com erro (codigo 1).
  - Se o Sienge responder 429 (limite de chamadas), espera e tenta de novo (30/60/120/240 s).
  - As regras de negocio (linha_pagar, linhas_pagamentos, etc.) sao as MESMAS da v1, sem alteracao.

VARIAVEIS DE AMBIENTE (GitHub: Settings > Secrets and variables > Actions; valor puro, sem aspas)
  SIENGE_BASE_URL, SIENGE_USER, SIENGE_PASSWORD   (os mesmos de antes)
  DATABASE_URL   postgresql://tiago_ingest.<ref>:<SENHA>@<host-pooler>:5432/postgres  (Session pooler)
  Localmente (sem GitHub), as variaveis tambem podem vir de um .env (que fica FORA do git).

COMO RODAR
  Instalar: pip install -r requirements-ingest.txt
  Rodar:    python ingest_sienge_tiago.py                    (grava hoje)
            python ingest_sienge_tiago.py --simular          (le o Sienge e conta; NAO grava)
            python ingest_sienge_tiago.py --data 2026-08-20  (so' teste: etiqueta de data do snapshot)

O repositorio e' PUBLICO: este script so' imprime contagens, nunca dado, credencial nem URL.
As anotacoes TODO sobre parametros do Sienge (selectionType, correctionDate, paginacao, ids por indice)
continuam valendo e estao no cabecalho de ingest_sienge_to_supabase.py (v1, mantido para consulta).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from urllib.parse import urlparse, unquote

import requests
from requests.auth import HTTPBasicAuth

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv e' opcional (no GitHub as variaveis ja vem prontas)
    pass

PAPEL_INGEST = "tiago_ingest"
SCHEMA = "tiago"
ESPERAS_429 = (30, 60, 120, 240)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--data", help="YYYY-MM-DD: etiqueta de data do snapshot (SO' teste/backfill).")
    p.add_argument("--simular", action="store_true", help="le o Sienge e conta, mas NAO grava no banco")
    return p.parse_args(argv)


def env_obrigatoria(nome: str) -> str:
    v = (os.environ.get(nome) or "").strip()
    if not v:
        print(f"ERRO de configuracao: falta a variavel {nome}.")
        sys.exit(2)
    return v


# ---- Sienge (somente leitura) ----
class Janela:
    def __init__(self, hoje: date, captura: str | None = None):
        self.hoje = hoje
        self.start = (hoje - timedelta(days=30)).isoformat()
        self.end = (hoje + timedelta(days=90)).isoformat()
        self.capturado_em = captura or hoje.isoformat()


def buscar_sienge(base, auth, janela: Janela, recurso, params_extra, esperas=ESPERAS_429, dormir=time.sleep, get=requests.get):
    url = f"{base}/bulk-data/v1/{recurso}"
    params = {
        "startDate": janela.start,
        "endDate": janela.end,
        "selectionType": "D",  # TODO: confirmar significado exato (ver v1)
        "correctionIndexerId": "0",
        "correctionDate": janela.end,
        **params_extra,
    }
    tentativa = 0
    while True:
        r = get(url, params=params, auth=auth, timeout=60)
        if r.status_code == 429 and tentativa < len(esperas):
            print(f"  Sienge pediu para esperar (429) em '{recurso}'; aguardando {esperas[tentativa]} s...")
            dormir(esperas[tentativa])
            tentativa += 1
            continue
        if not r.ok:
            # Sem imprimir o corpo (repositorio publico): so' o codigo e o recurso.
            print(f"  ERRO {r.status_code} em '{recurso}'.")
            r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data


# ---- Regras de negocio: IDENTICAS a v1 (nao alterar sem teste) ----

def linha_pagar(p):
    return {
        "id": f"{p['billId']}-{p['installmentId']}",
        "bill_id": p["billId"],
        "installment_id": p["installmentId"],
        "document_identification_id": p.get("documentIdentificationId"),
        "document_identification_name": p.get("documentIdentificationName"),
        "document_number": p.get("documentNumber"),
        "origin_id": p.get("originId"),
        "creditor_id": p.get("creditorId"),
        "creditor_name": p.get("creditorName"),
        "original_amount": p.get("originalAmount"),
        "discount_amount": p.get("discountAmount"),
        "tax_amount": p.get("taxAmount"),
        "balance_amount": p.get("balanceAmount"),
        "corrected_balance_amount": p.get("correctedBalanceAmount"),
        "indexer_id": p.get("indexerId"),
        "indexer_name": p.get("indexerName"),
        "due_date": p.get("dueDate"),
        "issue_date": p.get("issueDate"),
        "installment_base_date": p.get("installmentBaseDate"),
        "bill_date": p.get("billDate"),
        "authorization_status": p.get("authorizationStatus"),
        "consistency_status": p.get("consistencyStatus"),
        "forecast_document": p.get("forecastDocument"),
        "company_id": p.get("companyId"),
        "company_name": p.get("companyName"),
        "business_area_id": p.get("businessAreaId"),
        "business_area_name": p.get("businessAreaName"),
        "business_type_id": p.get("businessTypeId"),
        "business_type_name": p.get("businessTypeName"),
        "project_id": p.get("projectId"),
        "project_name": p.get("projectName"),
        "group_company_id": p.get("groupCompanyId"),
        "group_company_name": p.get("groupCompanyName"),
        "holding_id": p.get("holdingId"),
        "holding_name": p.get("holdingName"),
        "subsidiary_id": p.get("subsidiaryId"),
        "subsidiary_name": p.get("subsidiaryName"),
        "registered_user_id": p.get("registeredUserId"),
        "registered_by": p.get("registeredBy"),
        "registered_date": p.get("registeredDate"),
        "payments_categories": p.get("paymentsCategories"),
        "departaments_costs": p.get("departamentsCosts"),
        "buildings_costs": p.get("buildingsCosts"),
        "payments": p.get("payments"),
        "authorizations": p.get("authorizations"),
    }


def linha_receber(p):
    return {
        "id": f"{p['billId']}-{p['installmentId']}",
        "bill_id": p["billId"],
        "installment_id": p["installmentId"],
        "document_identification_id": p.get("documentIdentificationId"),
        "document_identification_name": p.get("documentIdentificationName"),
        "document_number": p.get("documentNumber"),
        "document_forecast": p.get("documentForecast"),
        "origin_id": p.get("originId"),
        "client_id": p.get("clientId"),
        "client_name": p.get("clientName"),
        "original_amount": p.get("originalAmount"),
        "discount_amount": p.get("discountAmount"),
        "tax_amount": p.get("taxAmount"),
        "balance_amount": p.get("balanceAmount"),
        "corrected_balance_amount": p.get("correctedBalanceAmount"),
        "indexer_id": p.get("indexerId"),
        "indexer_name": p.get("indexerName"),
        "due_date": p.get("dueDate"),
        "issue_date": p.get("issueDate"),
        "installment_base_date": p.get("installmentBaseDate"),
        "bill_date": p.get("billDate"),
        "periodicity_type": p.get("periodicityType"),
        "correction_type": p.get("correctionType"),
        "interest_type": p.get("interestType"),
        "interest_rate": p.get("interestRate"),
        "interest_base_date": p.get("interestBaseDate"),
        "embedded_interest_amount": p.get("embeddedInterestAmount"),
        "defaulter_situation": p.get("defaulterSituation"),
        "sub_judicie": p.get("subJudicie"),
        "main_unit": p.get("mainUnit"),
        "installment_number": p.get("installmentNumber"),
        "payment_term": p.get("paymentTerm"),
        "company_id": p.get("companyId"),
        "company_name": p.get("companyName"),
        "business_area_id": p.get("businessAreaId"),
        "business_area_name": p.get("businessAreaName"),
        "business_type_id": p.get("businessTypeId"),
        "business_type_name": p.get("businessTypeName"),
        "project_id": p.get("projectId"),
        "project_name": p.get("projectName"),
        "group_company_id": p.get("groupCompanyId"),
        "group_company_name": p.get("groupCompanyName"),
        "holding_id": p.get("holdingId"),
        "holding_name": p.get("holdingName"),
        "subsidiary_id": p.get("subsidiaryId"),
        "subsidiary_name": p.get("subsidiaryName"),
        "receipts": p.get("receipts"),
        "receipts_categories": p.get("receiptsCategories"),
    }


# operationTypeId em payments[] confirmado com dado real: 1=Pagamento,
# 3=Cancelamento, 5=Substituição, 8=Abatimento de Adiantamento, 10=Adiantamento.
# Cancelamento/Substituição NAO são saída real de caixa (Rafael confirmou:
# "dados poluídos, não deveriam somar") - excluídos aqui, na origem, pra
# ninguém (nem o chat, nem uma consulta manual) somar isso por engano.
# ATUALIZADO 31/08/2026: Abatimento de Adiantamento (8) TAMBÉM excluído -
# decisão do Rafael (dono do domínio financeiro, não é hipótese técnica
# minha pra testar): o caixa desse valor já saiu antes, no momento do
# Adiantamento (10) - "abatimento" é só o lançamento contábil que aplica o
# adiantamento já pago contra a parcela depois, não é uma NOVA saída de
# caixa. Contar os dois (Adiantamento + Abatimento de Adiantamento) como
# pagamento separado dobra a contagem do mesmo dinheiro. Rafael sinalizou
# que pode ter mais tipos assim a ajustar no futuro ("vão ser sinalizados os
# pagamentos que não entram na conta") - próxima vez que aparecer um, seguir
# o mesmo padrão: adicionar aqui, documentar o motivo, rodar o SQL de
# limpeza pro que já foi ingerido antes do fix (ver sql_v0.7.4 como modelo).
TIPOS_PAGAMENTO_REAL_EXCLUIR = {3, 5, 8}
# Em receipts[]: 2=Recebimento, 4=Reparcelamento, 7=Distrato - mesma lógica,
# Reparcelamento/Distrato não são entrada real de caixa.
TIPOS_RECEBIMENTO_REAL_EXCLUIR = {4, 7}


def linhas_pagamentos(p):
    # Ledger REAL de pagamentos ja feitos (payments[]), com data real do
    # evento (paymentDate) - nao depende de quantos dias a ingestao rodou.
    linhas = []
    for idx, pay in enumerate(p.get("payments") or []):
        if pay.get("operationTypeId") in TIPOS_PAGAMENTO_REAL_EXCLUIR:
            continue
        seq = pay.get("sequencialNumber")
        # id usa o INDICE da lista original (antes do filtro), nao o
        # sequencialNumber - confirmado com dado real que sequencialNumber
        # repete dentro do mesmo payments[] (ex.: billId 34116/installmentId 1
        # tem 3 pagamentos, todos com seq=1), o que quebrava o upsert
        # (ON CONFLICT DO UPDATE affects row a 2nd time).
        linhas.append({
            "id": f"{p['billId']}-{p['installmentId']}-{idx}",
            "bill_id": p["billId"],
            "installment_id": p["installmentId"],
            "creditor_id": p.get("creditorId"),
            "creditor_name": p.get("creditorName"),
            "operation_type_id": pay.get("operationTypeId"),
            "operation_type_name": pay.get("operationTypeName"),
            "gross_amount": pay.get("grossAmount"),
            "net_amount": pay.get("netAmount"),
            "discount_amount": pay.get("discountAmount"),
            "tax_amount": pay.get("taxAmount"),
            "interest_amount": pay.get("interestAmount"),
            "fine_amount": pay.get("fineAmount"),
            "monetary_correction_amount": pay.get("monetaryCorrectionAmount"),
            "calculation_date": pay.get("calculationDate"),
            "payment_date": pay.get("paymentDate"),
            "sequencial_number": seq,
        })
    return linhas


def linhas_recebimentos(p):
    # Ledger REAL de recebimentos ja feitos (receipts[]), mesma lógica.
    linhas = []
    for idx, rec in enumerate(p.get("receipts") or []):
        if rec.get("operationTypeId") in TIPOS_RECEBIMENTO_REAL_EXCLUIR:
            continue
        seq = rec.get("sequencialNumber")
        # mesmo problema do lado pagar - 47 de 197 parcelas reais tinham
        # sequencialNumber duplicado dentro de receipts[], usa indice da lista.
        linhas.append({
            "id": f"{p['billId']}-{p['installmentId']}-{idx}",
            "bill_id": p["billId"],
            "installment_id": p["installmentId"],
            "client_id": p.get("clientId"),
            "client_name": p.get("clientName"),
            "operation_type_id": rec.get("operationTypeId"),
            "operation_type_name": rec.get("operationTypeName"),
            "gross_amount": rec.get("grossAmount"),
            "net_amount": rec.get("netAmount"),
            "discount_amount": rec.get("discountAmount"),
            "tax_amount": rec.get("taxAmount"),
            "interest_amount": rec.get("interestAmount"),
            "embedded_interest_amount": rec.get("embeddedInterestAmount"),
            "calculation_date": rec.get("calculationDate"),
            "payment_date": rec.get("paymentDate"),
            "account_number": rec.get("accountNumber"),
            "account_type": rec.get("accountType"),
            "sequencial_number": seq,
            "bank_movements": rec.get("bankMovements"),
        })
    return linhas


def linhas_aprovacoes(p):
    # Ledger REAL de aprovações (authorizations[]) - authorizationDate é a
    # data/hora em que a [usuária] (ou quem for) aprovou de verdade, no passado.
    linhas = []
    for idx, auth in enumerate(p.get("authorizations") or []):
        linhas.append({
            "id": f"{p['billId']}-{p['installmentId']}-{idx}",
            "bill_id": p["billId"],
            "installment_id": p["installmentId"],
            "creditor_name": p.get("creditorName"),
            "authorization_user_id": auth.get("authorizationUserId"),
            "authorization_user_name": auth.get("authorizationUserName"),
            "authorization_date": auth.get("authorizationDate"),
            "is_last_to_authorize": auth.get("isLastToAuthorize"),
        })
    return linhas


# ---- Gravacao direta no Postgres ----
CHAVE_CONFLITO = {
    "parcelas_pagar": ("id",),
    "parcelas_receber": ("id",),
    "movimentos_pagamento": ("id",),
    "movimentos_recebimento": ("id",),
    "aprovacoes_pagar": ("id",),
    "historico_diario_parcelas": ("bill_id", "installment_id", "capturado_em"),
    "historico_diario_parcelas_receber": ("bill_id", "installment_id", "capturado_em"),
}
TAMANHO_LOTE = 500


def validar_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("postgresql", "postgres"):
        raise ValueError("DATABASE_URL precisa comecar com postgresql://")
    if unquote(u.username or "").split(".")[0] != PAPEL_INGEST:
        raise ValueError(f"a DATABASE_URL precisa entrar como {PAPEL_INGEST} (usuario {PAPEL_INGEST}.<ref> no pooler)")
    if u.port == 6543:
        raise ValueError("use o Session pooler (porta 5432), nao o Transaction pooler (6543)")


def conectar(url: str):
    import psycopg2

    validar_url(url)
    conn = psycopg2.connect(url, connect_timeout=30, application_name="tiago_ingest")
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")  # o pooler nao carrega 'alter role ... set search_path'
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute("SET statement_timeout = '120s'")
        cur.execute("SELECT current_user")
        usuario = cur.fetchone()[0]
    conn.commit()  # os SET precisam ser confirmados
    if usuario != PAPEL_INGEST:
        conn.close()
        raise ValueError(f"conectou como {usuario!r}; so' {PAPEL_INGEST} e' permitido")
    return conn


def _valor(v):
    from psycopg2.extras import Json

    return Json(v) if isinstance(v, (dict, list)) else v


def upsert(conn, tabela: str, linhas: list) -> int:
    """INSERT ... ON CONFLICT DO UPDATE em lotes, numa unica transacao. Devolve quantas linhas enviou.
    Mesmo efeito do upsert da API: colunas do envio sao atualizadas; as demais ficam como estao."""
    from psycopg2 import sql
    from psycopg2.extras import execute_values

    if not linhas:
        return 0
    chave = CHAVE_CONFLITO[tabela]
    colunas = list(linhas[0].keys())
    # remove chave repetida dentro do mesmo envio (o banco recusa 'afetar a mesma linha 2x')
    unicas = {}
    for r in linhas:
        unicas[tuple(r[c] for c in chave)] = r
    linhas = list(unicas.values())
    atualizar = [c for c in colunas if c not in chave]
    if atualizar:
        sets = sql.SQL(", ").join(sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in atualizar)
        acao = sql.SQL("DO UPDATE SET ") + sets
    else:
        acao = sql.SQL("DO NOTHING")
    comando = sql.SQL("INSERT INTO {t} ({cols}) VALUES %s ON CONFLICT ({k}) ").format(
        t=sql.Identifier(SCHEMA, tabela),
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in colunas),
        k=sql.SQL(", ").join(sql.Identifier(c) for c in chave),
    ) + acao
    try:
        with conn.cursor() as cur:
            for i in range(0, len(linhas), TAMANHO_LOTE):
                lote = linhas[i : i + TAMANHO_LOTE]
                execute_values(cur, comando.as_string(conn), [tuple(_valor(r[c]) for c in colunas) for r in lote], page_size=TAMANHO_LOTE)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(linhas)


def montar_historico_pagar(raw, capturado_em):
    return [
        {
            "capturado_em": capturado_em,
            "bill_id": p["billId"],
            "installment_id": p["installmentId"],
            "due_date": p["dueDate"],
            "balance_amount": p["balanceAmount"],
            "authorization_status": p["authorizationStatus"],
        }
        for p in raw
    ]


def montar_historico_receber(raw, capturado_em):
    return [
        {
            "capturado_em": capturado_em,
            "bill_id": p["billId"],
            "installment_id": p["installmentId"],
            "due_date": p["dueDate"],
            "balance_amount": p["balanceAmount"],
            "defaulter_situation": p.get("defaulterSituation"),
        }
        for p in raw
    ]


def gravar_tabelas(conn, plano, log=print) -> int:
    """plano = lista de (tabela, linhas). Cada tabela numa transacao; falha de uma nao impede as outras.
    Devolve quantas tabelas falharam."""
    falhas = 0
    for tabela, linhas in plano:
        try:
            n = upsert(conn, tabela, linhas)
            log(f"  {tabela}: {n} linhas gravadas")
        except Exception as e:
            falhas += 1
            # so' o tipo do erro e o codigo (repositorio publico)
            log(f"  ERRO ao gravar {tabela}: {type(e).__name__} {getattr(e, 'pgcode', '') or ''}")
    return falhas


def montar_plano(pagar_raw, receber_raw, capturado_em):
    plano = []
    if pagar_raw:
        plano += [
            ("parcelas_pagar", [linha_pagar(p) for p in pagar_raw]),
            ("historico_diario_parcelas", montar_historico_pagar(pagar_raw, capturado_em)),
            ("movimentos_pagamento", [l for p in pagar_raw for l in linhas_pagamentos(p)]),
            ("aprovacoes_pagar", [l for p in pagar_raw for l in linhas_aprovacoes(p)]),
        ]
    if receber_raw:
        plano += [
            ("parcelas_receber", [linha_receber(p) for p in receber_raw]),
            ("historico_diario_parcelas_receber", montar_historico_receber(receber_raw, capturado_em)),
            ("movimentos_recebimento", [l for p in receber_raw for l in linhas_recebimentos(p)]),
        ]
    return plano


def registrar_metricas(conn) -> None:
    """Calcula e registra os KPIs (tiago.registrar_metricas). Tolerante: se a rotina ainda nao existe
    ou falhar, avisa no log mas NAO derruba a ingestao (os dados ja foram gravados)."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {SCHEMA}.registrar_metricas()")
            n = cur.fetchone()[0]
        conn.commit()
        print(f"Metricas registradas: {n}")
    except Exception as e:
        conn.rollback()
        # so' o tipo do erro e o codigo (repositorio publico)
        print(f"AVISO: metricas nao registradas ({type(e).__name__} {getattr(e, 'pgcode', '') or ''}); os dados foram gravados.")


def main(argv=None) -> int:
    args = parse_args(argv)
    hoje = date.today()
    janela = Janela(hoje, args.data)
    if args.data:
        print(f"AVISO: modo backfill de teste - snapshot gravado como {janela.capturado_em} (hoje real: {hoje.isoformat()}).")

    base = env_obrigatoria("SIENGE_BASE_URL").rstrip("/")
    auth = HTTPBasicAuth(env_obrigatoria("SIENGE_USER"), env_obrigatoria("SIENGE_PASSWORD"))
    url = "" if args.simular else env_obrigatoria("DATABASE_URL")

    print(f"Janela de datas: {janela.start} a {janela.end}")
    print("Buscando contas a pagar (outcome)...")
    pagar_raw = buscar_sienge(base, auth, janela, "outcome", {"withBankMovements": "true", "withAuthorizations": "true"})
    print(f"  {len(pagar_raw)} parcelas recebidas")
    print("Buscando contas a receber (income)...")
    receber_raw = buscar_sienge(base, auth, janela, "income", {"withBankMovements": "true"})
    print(f"  {len(receber_raw)} parcelas recebidas")

    plano = montar_plano(pagar_raw, receber_raw, janela.capturado_em)
    if args.simular:
        print("SIMULACAO (nada foi gravado):")
        for tabela, linhas in plano:
            print(f"  {tabela}: {len(linhas)} linhas seriam enviadas")
        return 0

    conn = conectar(url)
    try:
        falhas = gravar_tabelas(conn, plano)
        if not falhas:
            registrar_metricas(conn)
    finally:
        conn.close()
    if falhas:
        print(f"Ingestao terminou COM ERRO em {falhas} tabela(s).")
        return 1
    print("Ingestao concluida.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
