"""
TIA.go - Ingestao diaria: Sienge (contas a pagar + receber) -> Supabase

Roda 1x por dia (por enquanto manual, ou agendado no Agendador de Tarefas do
Windows - Fase 0, sem servidor ainda). Grava o estado ATUAL (upsert) em
parcelas_pagar/parcelas_receber e um SNAPSHOT append-only do dia em
historico_diario_parcelas(_receber) - e' esse snapshot que da vida ao
historico do fluxo de caixa (sem ele rodando todo dia, nao existe historico).

NUNCA COLOQUE CREDENCIAL DIRETO NESTE ARQUIVO. Crie um .env na mesma pasta
(confirme que esta no .gitignore, NUNCA commitar) assim:

SIENGE_BASE_URL=https://api.sienge.com.br/enermais/public/api
SIENGE_USER=usuario_dedicado_tia
SIENGE_PASSWORD=xxxx
SUPABASE_URL=https://SEU-PROJETO.supabase.co
SUPABASE_SERVICE_KEY=xxxx   # service_role - SO' usar aqui, NUNCA no app Streamlit/Secrets

Instalar:  pip install requests python-dotenv supabase
Rodar:     python ingest_sienge_to_supabase.py

Pra TESTAR o grafico/selecao de periodo sem esperar dias reais passarem, da
pra rodar varias vezes com --data simulando dias diferentes (o dado gravado
e' sempre o estado ATUAL do Sienge, so' a etiqueta de data do snapshot muda -
ou seja, os valores vao vir IGUAIS em todos os dias simulados, isso e' so'
pra testar a mecanica do grafico/selecao, nao serve como historico real):
  python ingest_sienge_to_supabase.py --data 2026-08-20
  python ingest_sienge_to_supabase.py --data 2026-08-22
  python ingest_sienge_to_supabase.py            (sem --data = hoje de verdade)
APAGAR as linhas de teste antes de usar com a Maria pra valer:
  delete from historico_diario_parcelas where capturado_em <> current_date;
  delete from historico_diario_parcelas_receber where capturado_em <> current_date;

TODO (nao confirmado ainda, revisar antes de rodar contra producao de verdade):
- 'selectionType': 'D' foi o valor visto em uso real, mas o significado exato
  nunca foi confirmado com a Sienge/Rafael - se o resultado vier estranho,
  esse e' o primeiro parametro a questionar.
- 'correctionDate': confirmado como parametro OBRIGATORIO pelo erro 400 real
  ("Required String parameter 'correctionDate' is not present"). Como
  correctionIndexerId='0' (sem indice de correcao), a hipotese e' que o valor
  da data nao afeta o calculo quando nao ha indice - usamos END (fim da
  janela) por seguranca, mas isso NAO foi confirmado contra o node de
  producao do n8n. Se os valores de correctedBalanceAmount vierem diferentes
  do esperado, e' aqui que se deve olhar primeiro.
- Paginacao: nao testamos volume grande o suficiente pra saber se o Sienge
  corta silenciosamente (a API Bulk Data tem modo assincrono por chunk pra
  volumes grandes - nao implementado aqui ainda, MVP assume que cabe numa
  chamada so, como aconteceu nos testes de hoje).
- IDs de movimentos_pagamento/movimentos_recebimento usam o INDICE da lista
  payments[]/receipts[] (nao o sequencialNumber do Sienge - confirmado com
  dado real que ele repete dentro da mesma parcela). Isso assume que o Sienge
  devolve os eventos na MESMA ORDEM em toda chamada - nao confirmado
  formalmente, e' a premissa mais simples que funciona hoje. Se algum dia um
  pagamento antigo sumir/duplicar depois de rodar de novo, e' aqui que se
  deve olhar.
"""

import argparse
import os
from datetime import date, timedelta

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--data",
        help=(
            "Data (YYYY-MM-DD) pra gravar como 'capturado_em' do snapshot, no lugar de "
            "hoje. SÓ pra teste/backfill do gráfico (o dado ainda é o estado ATUAL do "
            "Sienge, só a etiqueta de data do snapshot muda) - apaga essas linhas de "
            "teste antes de usar com a Maria pra valer, senão mistura histórico fake "
            "com real."
        ),
    )
    return p.parse_args()


_args = parse_args()

SIENGE_BASE = os.environ["SIENGE_BASE_URL"].rstrip("/")
SIENGE_AUTH = HTTPBasicAuth(os.environ["SIENGE_USER"], os.environ["SIENGE_PASSWORD"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

HOJE = date.today()
CAPTURADO_EM = _args.data or HOJE.isoformat()
if _args.data:
    print(f"AVISO: rodando em modo backfill de teste - snapshot vai ser gravado como {CAPTURADO_EM} (hoje real: {HOJE.isoformat()}).")
START = (HOJE - timedelta(days=30)).isoformat()
END = (HOJE + timedelta(days=90)).isoformat()


def buscar_sienge(recurso, params_extra):
    url = f"{SIENGE_BASE}/bulk-data/v1/{recurso}"
    params = {
        "startDate": START,
        "endDate": END,
        "selectionType": "D",  # TODO: confirmar significado exato
        "correctionIndexerId": "0",
        "correctionDate": END,  # TODO: valor assumido (sem indice=0 -> data nao deveria importar), nao confirmado com o node de producao
        **params_extra,
    }
    r = requests.get(url, params=params, auth=SIENGE_AUTH, timeout=60)
    if not r.ok:
        # Sienge devolve um corpo com "clientMessage" explicando o motivo exato
        # do 400 - sem isso o raise_for_status() so mostra o codigo, nao a causa.
        print(f"  ERRO {r.status_code} em '{recurso}' - corpo da resposta:")
        print(f"  {r.text}")
    r.raise_for_status()
    data = r.json()
    return data.get("data", data) if isinstance(data, dict) else data


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
    # data/hora em que a Maria (ou quem for) aprovou de verdade, no passado.
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


def main():
    print(f"Janela de datas: {START} a {END}")

    print("Buscando contas a pagar (outcome)...")
    pagar_raw = buscar_sienge("outcome", {"withBankMovements": "true", "withAuthorizations": "true"})
    print(f"  {len(pagar_raw)} parcelas recebidas")
    if pagar_raw:
        pagar_rows = [linha_pagar(p) for p in pagar_raw]
        supabase.table("parcelas_pagar").upsert(pagar_rows, on_conflict="id").execute()
        print("  upsert em parcelas_pagar OK")

        hist_pagar = [{
            "capturado_em": CAPTURADO_EM,
            "bill_id": p["billId"],
            "installment_id": p["installmentId"],
            "due_date": p["dueDate"],
            "balance_amount": p["balanceAmount"],
            "authorization_status": p["authorizationStatus"],
        } for p in pagar_raw]
        supabase.table("historico_diario_parcelas").upsert(
            hist_pagar, on_conflict="bill_id,installment_id,capturado_em"
        ).execute()
        print("  snapshot em historico_diario_parcelas OK")

        mov_pagamento_rows = [linha for p in pagar_raw for linha in linhas_pagamentos(p)]
        if mov_pagamento_rows:
            supabase.table("movimentos_pagamento").upsert(mov_pagamento_rows, on_conflict="id").execute()
            print(f"  upsert em movimentos_pagamento OK ({len(mov_pagamento_rows)} pagamentos reais)")

        aprov_rows = [linha for p in pagar_raw for linha in linhas_aprovacoes(p)]
        if aprov_rows:
            supabase.table("aprovacoes_pagar").upsert(aprov_rows, on_conflict="id").execute()
            print(f"  upsert em aprovacoes_pagar OK ({len(aprov_rows)} aprovações)")

    print("Buscando contas a receber (income)...")
    receber_raw = buscar_sienge("income", {"withBankMovements": "true"})
    print(f"  {len(receber_raw)} parcelas recebidas")
    if receber_raw:
        receber_rows = [linha_receber(p) for p in receber_raw]
        supabase.table("parcelas_receber").upsert(receber_rows, on_conflict="id").execute()
        print("  upsert em parcelas_receber OK")

        hist_receber = [{
            "capturado_em": CAPTURADO_EM,
            "bill_id": p["billId"],
            "installment_id": p["installmentId"],
            "due_date": p["dueDate"],
            "balance_amount": p["balanceAmount"],
            "defaulter_situation": p.get("defaulterSituation"),
        } for p in receber_raw]
        supabase.table("historico_diario_parcelas_receber").upsert(
            hist_receber, on_conflict="bill_id,installment_id,capturado_em"
        ).execute()
        print("  snapshot em historico_diario_parcelas_receber OK")

        mov_recebimento_rows = [linha for p in receber_raw for linha in linhas_recebimentos(p)]
        if mov_recebimento_rows:
            supabase.table("movimentos_recebimento").upsert(mov_recebimento_rows, on_conflict="id").execute()
            print(f"  upsert em movimentos_recebimento OK ({len(mov_recebimento_rows)} recebimentos reais)")

    print("Ingestão concluída.")


if __name__ == "__main__":
    main()
