"""
Testes do TIA.go v2.0 (dados SINTETICOS; nenhum dado real).

Parte 1 (sempre roda): montagem de SQL do db_tiago, travas, 429 do Sienge.
Parte 2 (so' roda se existir um Postgres de teste): variaveis de ambiente
  TIAGO_TEST_ADMIN_URL   url de um banco VAZIO de teste com superusuario (ex.: postgresql://postgres@/t?host=/tmp&port=54330)
  TIAGO_TEST_SCHEMA_SQL  caminho do tiago_schema_v0.2.sql (cria schema, papeis e RLS)
O teste cria os schemas gdf/egc de mentira (a trava do SQL exige), roda o SQL e entra de verdade como
tiago_app e tiago_ingest. NUNCA aponte isto para o radar-comercial.
"""
import datetime as dt
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db_tiago as D  # noqa: E402
import ingest_sienge_tiago as I  # noqa: E402


# ------------------------- parte 1: sem banco -------------------------
class PoolFalso:
    pass


def consulta(tabela="parcelas_pagar"):
    return D.Consulta(PoolFalso(), tabela)


def test_sql_basico():
    sql, p = (
        consulta().select("id, balance_amount").gt("balance_amount", 0).order("id").range(0, 999)._sql()
    )
    assert sql == 'SELECT "id", "balance_amount" FROM "tiago"."parcelas_pagar" WHERE "balance_amount" > %s ORDER BY "id" ASC LIMIT %s'
    assert p == [0, 1000]


def test_sql_range_com_offset_e_desc():
    sql, p = consulta("historico_diario_parcelas").select("*").order("capturado_em", desc=True).range(1000, 1999)._sql()
    assert 'ORDER BY "capturado_em" DESC LIMIT %s OFFSET %s' in sql
    assert p == [1000, 1000]


def test_sql_ilike_e_datas():
    sql, p = consulta("movimentos_pagamento").gte("payment_date", "2026-01-01").lt("payment_date", "2026-02-01").ilike("creditor_name", "%x%")._sql()
    assert sql.count("%s") == 3 and "ILIKE" in sql and p == ["2026-01-01", "2026-02-01", "%x%"]


def test_tabela_fora_da_lista_e_recusada():
    with pytest.raises(D.DadosErro):
        D.Consulta(PoolFalso(), "pg_shadow")
    with pytest.raises(D.DadosErro):
        D.Consulta(PoolFalso(), 'parcelas_pagar"; drop table x; --')


@pytest.mark.parametrize("ruim", ["id; drop table x", "a b", "A", '"x"', "id,(select 1)", "1abc"])
def test_coluna_invalida_e_recusada(ruim):
    with pytest.raises(D.DadosErro):
        consulta().select(ruim)
    with pytest.raises(D.DadosErro):
        consulta().eq(ruim, 1)
    with pytest.raises(D.DadosErro):
        consulta().order(ruim)


def test_select_vazio_vira_todas_as_colunas():
    assert consulta().select("")._colunas == "*" and consulta().select("*")._colunas == "*"
    with pytest.raises(D.DadosErro):
        consulta().eq("", 1)


def test_valor_malicioso_vai_como_parametro_nunca_no_texto():
    sql, p = consulta().eq("id", "1'; drop table x; --")._sql()
    assert "drop" not in sql and p == ["1'; drop table x; --"]


@pytest.mark.parametrize(
    "url,ok",
    [
        ("postgresql://tiago_app.abcd:senha@aws-0-us-east-2.pooler.supabase.com:5432/postgres", True),
        ("postgresql://tiago_app@/postgres?host=/tmp", True),
        ("postgresql://tiago_ingest.abcd:s@h:5432/postgres", False),  # papel errado
        ("postgresql://postgres.abcd:s@h:5432/postgres", False),
        ("postgresql://tiago_app.abcd:s@h:6543/postgres", False),  # transaction pooler
        ("https://x.supabase.co", False),
        ("", False),
    ],
)
def test_validar_url_app(url, ok):
    if ok:
        D.validar_url(url)
    else:
        with pytest.raises(D.DadosErro):
            D.validar_url(url)


@pytest.mark.parametrize(
    "url,ok",
    [
        ("postgresql://tiago_ingest.abcd:senha@h:5432/postgres", True),
        ("postgresql://tiago_app.abcd:senha@h:5432/postgres", False),
        ("postgresql://tiago_ingest.abcd:senha@h:6543/postgres", False),
    ],
)
def test_validar_url_ingest(url, ok):
    if ok:
        I.validar_url(url)
    else:
        with pytest.raises(ValueError):
            I.validar_url(url)


def test_conversao_de_valores_como_a_api():
    from decimal import Decimal

    assert D._converter(Decimal("10.50")) == 10.5 and isinstance(D._converter(Decimal("10.50")), float)
    assert D._converter(dt.date(2026, 10, 22)) == "2026-10-22"
    assert D._converter(dt.datetime(2026, 10, 22, 4, 0, tzinfo=dt.timezone.utc)) == "2026-10-22T04:00:00+00:00"
    assert D._converter("x") == "x" and D._converter(None) is None


class RespFalsa:
    def __init__(self, status, corpo=None):
        self.status_code = status
        self.ok = status < 400
        self._c = corpo if corpo is not None else {"data": []}

    def json(self):
        return self._c

    def raise_for_status(self):
        raise RuntimeError(f"http {self.status_code}")


def test_sienge_429_espera_e_tenta_de_novo():
    respostas = [RespFalsa(429), RespFalsa(429), RespFalsa(200, {"data": [{"billId": 1}]})]
    esperas = []
    out = I.buscar_sienge("http://x", None, I.Janela(dt.date(2026, 10, 22)), "outcome", {},
                          dormir=esperas.append, get=lambda *a, **k: respostas.pop(0))
    assert out == [{"billId": 1}] and esperas == [30, 60]


def test_sienge_429_desiste_depois_das_esperas():
    esperas = []
    with pytest.raises(RuntimeError):
        I.buscar_sienge("http://x", None, I.Janela(dt.date(2026, 10, 22)), "outcome", {},
                        dormir=esperas.append, get=lambda *a, **k: RespFalsa(429))
    assert esperas == [30, 60, 120, 240]


def test_sienge_erro_nao_imprime_corpo(capsys):
    with pytest.raises(RuntimeError):
        I.buscar_sienge("http://x", None, I.Janela(dt.date(2026, 10, 22)), "income", {},
                        get=lambda *a, **k: RespFalsa(400, {"clientMessage": "SEGREDO"}))
    assert "SEGREDO" not in capsys.readouterr().out


# ------------------------- parte 2: com Postgres de teste -------------------------
ADMIN = os.environ.get("TIAGO_TEST_ADMIN_URL")
SCHEMA_SQL = os.environ.get("TIAGO_TEST_SCHEMA_SQL")
pg = pytest.mark.skipif(not (ADMIN and SCHEMA_SQL), reason="sem Postgres de teste (ver cabecalho)")


def pagar(b, i, saldo, status="N", venc="2026-11-10"):
    return {"billId": b, "installmentId": i, "dueDate": venc, "balanceAmount": saldo,
            "authorizationStatus": status, "creditorName": f"Fornecedor Ficticio {b}",
            "payments": [{"operationTypeId": 1, "operationTypeName": "Pagamento", "netAmount": 10.0,
                          "paymentDate": "2026-10-01", "sequencialNumber": 1}] if i == 1 else [],
            "authorizations": [{"authorizationUserName": "Fulano Ficticio", "authorizationDate": "2026-10-02T10:00:00",
                                "isLastToAuthorize": True}] if status == "S" else []}


def receber(b, i, saldo, venc="2026-11-20"):
    return {"billId": b, "installmentId": i, "dueDate": venc, "balanceAmount": saldo, "clientName": f"Cliente Ficticio {b}",
            "defaulterSituation": "N",
            "receipts": [{"operationTypeId": 2, "operationTypeName": "Recebimento", "netAmount": 5.0,
                          "paymentDate": "2026-10-03", "sequencialNumber": 1, "bankMovements": [{"x": 1}]}] if i == 1 else []}


@pytest.fixture(scope="module")
def banco():
    import psycopg2

    adm = psycopg2.connect(ADMIN)
    adm.autocommit = True
    cur = adm.cursor()
    cur.execute("drop schema if exists tiago cascade; drop schema if exists gdf cascade; drop schema if exists egc cascade")
    cur.execute("create schema gdf; create schema egc")
    cur.execute(Path(SCHEMA_SQL).read_text(encoding="utf-8"))
    cur.execute("insert into tiago.acesso(email, papel, ativo) values ('ok@x.com','EDITOR',true), ('off@x.com','EDITOR',false)")
    base = ADMIN.split("?")[0].rsplit("@", 1)
    yield {"adm": adm, "ingest": "postgresql://tiago_ingest@/" + ADMIN.split("/", 3)[3],
           "app": "postgresql://tiago_app@/" + ADMIN.split("/", 3)[3]}
    adm.close()


@pg
def test_ingest_grava_e_regrava_sem_duplicar(banco):
    pg_raw, rc_raw = [pagar(1, 1, 100.0, "S"), pagar(1, 2, 50.5), pagar(2, 1, 0.0, "N")], [receber(9, 1, 300.0), receber(9, 2, 10.0)]
    plano = I.montar_plano(pg_raw, rc_raw, "2026-10-22")
    conn = I.conectar(banco["ingest"])
    assert I.gravar_tabelas(conn, plano, log=lambda *_: None) == 0
    assert I.gravar_tabelas(conn, plano, log=lambda *_: None) == 0  # de novo: nada duplica
    conn.close()
    cur = banco["adm"].cursor()
    contagens = {}
    for t in ["parcelas_pagar", "historico_diario_parcelas", "movimentos_pagamento", "aprovacoes_pagar",
              "parcelas_receber", "historico_diario_parcelas_receber", "movimentos_recebimento"]:
        cur.execute(f"select count(*) from tiago.{t}")
        contagens[t] = cur.fetchone()[0]
    assert contagens == {"parcelas_pagar": 3, "historico_diario_parcelas": 3, "movimentos_pagamento": 2, "aprovacoes_pagar": 1,
                         "parcelas_receber": 2, "historico_diario_parcelas_receber": 2, "movimentos_recebimento": 1}
    cur.execute("select bank_movements from tiago.movimentos_recebimento")
    assert cur.fetchone()[0] == [{"x": 1}]  # jsonb gravado como json


@pg
def test_regravar_atualiza_saldo(banco):
    conn = I.conectar(banco["ingest"])
    plano = I.montar_plano([pagar(1, 2, 20.0)], [], "2026-10-22")
    assert I.gravar_tabelas(conn, plano, log=lambda *_: None) == 0
    conn.close()
    cur = banco["adm"].cursor()
    cur.execute("select balance_amount from tiago.parcelas_pagar where id='1-2'")
    assert float(cur.fetchone()[0]) == 20.0
    cur.execute("select count(*) from tiago.historico_diario_parcelas where bill_id=1 and installment_id=2")
    assert cur.fetchone()[0] == 1  # mesmo dia: atualiza, nao duplica


@pg
def test_chave_repetida_no_mesmo_envio_nao_quebra(banco):
    conn = I.conectar(banco["ingest"])
    linhas = [I.linha_pagar(pagar(7, 1, 1.0)), I.linha_pagar(pagar(7, 1, 2.0))]
    assert I.upsert(conn, "parcelas_pagar", linhas) == 1
    conn.close()


@pg
def test_falha_numa_tabela_nao_impede_as_outras(banco):
    conn = I.conectar(banco["ingest"])
    ruim = pagar(50, 1, 5.0, status="X")  # viola o check authorization_status in ('S','N')
    plano = [("parcelas_pagar", [I.linha_pagar(ruim)]),
             ("parcelas_receber", [I.linha_receber(receber(60, 1, 8.0))])]
    msgs = []
    assert I.gravar_tabelas(conn, plano, log=msgs.append) == 1
    conn.close()
    cur = banco["adm"].cursor()
    cur.execute("select count(*) from tiago.parcelas_receber where id='60-1'")
    assert cur.fetchone()[0] == 1
    cur.execute("select count(*) from tiago.parcelas_pagar where id='50-1'")
    assert cur.fetchone()[0] == 0
    assert any("ERRO ao gravar parcelas_pagar" in m for m in msgs) and not any("Fornecedor" in m for m in msgs)


@pg
def test_ingest_nao_consegue_apagar(banco):
    import psycopg2

    conn = I.conectar(banco["ingest"])
    with conn.cursor() as cur, pytest.raises(psycopg2.Error):
        cur.execute("delete from tiago.parcelas_pagar")
    conn.rollback()
    conn.close()


@pg
def test_ingest_so_entra_como_tiago_ingest(banco):
    with pytest.raises(ValueError):
        I.conectar(banco["app"])


@pg
def test_app_le_pelo_shim_no_formato_da_api(banco):
    db = D.abrir_dados(banco["app"])
    linhas = db.table("parcelas_pagar").select("id, balance_amount, due_date").gt("balance_amount", 0).order("id").range(0, 999).execute().data
    assert linhas and isinstance(linhas[0]["balance_amount"], float) and isinstance(linhas[0]["due_date"], str)
    assert linhas == sorted(linhas, key=lambda r: r["id"])
    cp = db.table("movimentos_pagamento").select("id, payment_date").ilike("creditor_name", "%ficticio%").execute().data
    assert cp and cp[0]["payment_date"] == "2026-10-01"
    # historico: view resumo e ordenacao desc + limit
    h = db.table("resumo_diario_fluxo_caixa").select("*").order("capturado_em", desc=True).limit(5).execute().data
    assert h and "capturado_em" in h[0]


@pg
def test_app_paginacao_range(banco):
    db = D.abrir_dados(banco["app"])
    todas = db.table("parcelas_pagar").select("id").order("id").execute().data
    p1 = db.table("parcelas_pagar").select("id").order("id").range(0, 1).execute().data
    p2 = db.table("parcelas_pagar").select("id").order("id").range(2, 3).execute().data
    assert [r["id"] for r in p1 + p2] == [r["id"] for r in todas][: len(p1 + p2)]


@pg
def test_app_nao_consegue_escrever(banco):
    import psycopg2

    pool = D.criar_pool(banco["app"])
    conn = pool.getconn()
    with conn.cursor() as cur:
        for comando in ["insert into tiago.acesso(email) values ('x@x.com')", "delete from tiago.acesso",
                        "update tiago.parcelas_pagar set balance_amount = 0", "create table tiago.x(a int)"]:
            with pytest.raises(psycopg2.Error):
                cur.execute(comando)
            conn.rollback()
    pool.putconn(conn)


@pg
def test_app_nao_enxerga_outros_schemas(banco):
    import psycopg2

    banco["adm"].cursor().execute("create table if not exists gdf.segredo(a int)")
    pool = D.criar_pool(banco["app"])
    conn = pool.getconn()
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            cur.execute("select * from gdf.segredo")
    conn.rollback()
    pool.putconn(conn)


@pg
def test_acesso_liberado(banco):
    db = D.abrir_dados(banco["app"])
    assert db.email_liberado("ok@x.com") == "EDITOR"
    assert db.email_liberado("OK@X.COM") == "EDITOR"  # nao diferencia maiuscula
    assert db.email_liberado("off@x.com") is None  # inativo
    assert db.email_liberado("nunca@x.com") is None
    assert db.email_liberado("") is None


@pg
def test_pool_reaproveita_e_limita_conexoes(banco):
    pool = D.criar_pool(banco["app"], maximo=2)
    a, b = pool.getconn(), pool.getconn()
    pool.putconn(a)
    c = pool.getconn()
    assert c is a  # reaproveitou
    pool.putconn(b)
    pool.putconn(c)
    pool.fechar_tudo()


# ---- v2.1: registro de metricas apos a ingestao (sem banco: conexao de mentira) ----
class _CurFalso:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, q):
        self.conn.comandos.append(q)
        if self.conn.erro: raise self.conn.erro
    def fetchone(self): return (6,)


class _ConnFalsa:
    def __init__(self, erro=None): self.comandos, self.erro, self.commits, self.rollbacks = [], erro, 0, 0
    def cursor(self): return _CurFalso(self)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


def test_registrar_metricas_chama_rotina_e_confirma(capsys):
    c = _ConnFalsa()
    I.registrar_metricas(c)
    assert c.comandos == ["SELECT tiago.registrar_metricas()"] and c.commits == 1
    assert "Metricas registradas: 6" in capsys.readouterr().out


def test_registrar_metricas_tolerante_a_falha(capsys):
    c = _ConnFalsa(erro=RuntimeError("segredo-nao-deve-aparecer"))
    I.registrar_metricas(c)  # nao levanta
    saida = capsys.readouterr().out
    assert c.rollbacks == 1 and "AVISO" in saida and "segredo" not in saida
