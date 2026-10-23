"""
TIA.go - leitura direta no banco (schema `tiago` do radar-comercial) | v2.0

POR QUE EXISTE
  Antes o app lia os dados pela API do Supabase (PostgREST), que tem tempo limite por pedido.
  Agora le DIRETO no Postgres (Session pooler, porta 5432) com o papel `tiago_app`, que so'
  consegue SELECT no schema `tiago`. O login (e-mail e senha) continua sendo do Supabase Auth.

COMO USA (o resto do app quase nao muda)
  db = abrir_dados()                       # pool compartilhado e so'-leitura
  db.table("parcelas_pagar").select("id, balance_amount").gt("balance_amount", 0).order("id").range(0, 999).execute().data
  -> mesma "frase" que o app ja usava com supabase-py; aqui ela vira SQL parametrizado.

SEGURANCA
  - Nomes de tabela e coluna passam por lista/regex (nunca texto livre dentro do SQL).
  - Valores vao sempre como parametro (%s), nunca concatenados.
  - Conexao em modo SOMENTE LEITURA, schema fixado a cada conexao e limite de tempo por consulta.
  - Nunca imprime a URL nem a senha.

Os valores voltam no mesmo formato que a API devolvia: numero = float/int, data/hora = texto ISO.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from urllib.parse import urlparse, unquote

TABELAS_PERMITIDAS = {
    "parcelas_pagar",
    "parcelas_receber",
    "aprovacoes_pagar",
    "movimentos_pagamento",
    "movimentos_recebimento",
    "historico_diario_parcelas",
    "historico_diario_parcelas_receber",
    "resumo_diario_fluxo_caixa",
    "acesso",
}
SCHEMA = "tiago"
PAPEL_APP = "tiago_app"
_COLUNA = re.compile(r"^[a-z_][a-z0-9_]*$")
TEMPO_LIMITE_MS = 20000  # 20 s por consulta (a API antiga dava 8 s)


class DadosErro(Exception):
    pass


def _coluna(nome: str) -> str:
    nome = (nome or "").strip()
    if not _COLUNA.match(nome):
        raise DadosErro(f"nome de coluna invalido: {nome!r}")
    return f'"{nome}"'


def _converter(v):
    """Mesmo formato que o PostgREST devolvia em JSON."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime):
        return v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return v


class Resposta:
    def __init__(self, data):
        self.data = data


class Consulta:
    """Monta um SELECT simples. So' o que o app usa: select, gt/gte/lt/lte/eq, ilike, order, range, limit."""

    def __init__(self, pool, tabela: str):
        if tabela not in TABELAS_PERMITIDAS:
            raise DadosErro(f"tabela nao permitida: {tabela!r}")
        self._pool = pool
        self._tabela = tabela
        self._colunas = "*"
        self._filtros: list[tuple[str, str, object]] = []
        self._ordem: list[tuple[str, bool]] = []
        self._limite = None
        self._offset = None

    def select(self, colunas: str = "*"):
        colunas = (colunas or "*").strip()
        if colunas == "*":
            self._colunas = "*"
        else:
            self._colunas = ", ".join(_coluna(c) for c in colunas.split(","))
        return self

    def _filtro(self, op, coluna, valor):
        self._filtros.append((_coluna(coluna), op, valor))
        return self

    def eq(self, coluna, valor):
        return self._filtro("=", coluna, valor)

    def gt(self, coluna, valor):
        return self._filtro(">", coluna, valor)

    def gte(self, coluna, valor):
        return self._filtro(">=", coluna, valor)

    def lt(self, coluna, valor):
        return self._filtro("<", coluna, valor)

    def lte(self, coluna, valor):
        return self._filtro("<=", coluna, valor)

    def ilike(self, coluna, padrao):
        return self._filtro("ILIKE", coluna, padrao)

    def order(self, coluna, desc: bool = False):
        self._ordem.append((_coluna(coluna), bool(desc)))
        return self

    def limit(self, n: int):
        self._limite = int(n)
        return self

    def range(self, inicio: int, fim: int):
        inicio, fim = int(inicio), int(fim)
        self._offset = inicio
        self._limite = fim - inicio + 1
        return self

    def _sql(self):
        sql = f'SELECT {self._colunas} FROM "{SCHEMA}"."{self._tabela}"'
        params: list = []
        if self._filtros:
            partes = []
            for col, op, val in self._filtros:
                partes.append(f"{col} {op} %s")
                params.append(val)
            sql += " WHERE " + " AND ".join(partes)
        if self._ordem:
            sql += " ORDER BY " + ", ".join(f"{c} {'DESC' if d else 'ASC'}" for c, d in self._ordem)
        if self._limite is not None:
            sql += " LIMIT %s"
            params.append(self._limite)
        if self._offset:
            sql += " OFFSET %s"
            params.append(self._offset)
        return sql, params

    def execute(self) -> Resposta:
        sql, params = self._sql()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                nomes = [d.name for d in cur.description]
                linhas = [
                    {n: _converter(v) for n, v in zip(nomes, row)} for row in cur.fetchall()
                ]
            conn.rollback()  # fim da leitura (nao deixa transacao aberta no pooler)
            return Resposta(linhas)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._pool.putconn(conn)


class Dados:
    def __init__(self, pool):
        self._pool = pool

    def table(self, nome: str) -> Consulta:
        return Consulta(self._pool, nome)

    def email_liberado(self, email: str):
        """Devolve o papel (EDITOR/VISUALIZADOR) se o e-mail esta ativo em tiago.acesso; senao None.
        Necessario porque o login do Supabase e' compartilhado entre os apps do radar-comercial."""
        email = (email or "").strip().lower()
        if not email:
            return None
        linhas = self.table("acesso").select("email, papel, ativo").eq("email", email).execute().data
        for r in linhas:
            if r.get("ativo"):
                return r.get("papel")
        return None


def validar_url(url: str) -> None:
    """Recusa URL errada ANTES de conectar (sem imprimir nada sensivel)."""
    u = urlparse(url or "")
    if u.scheme not in ("postgresql", "postgres"):
        raise DadosErro("DATABASE_URL precisa comecar com postgresql://")
    usuario = unquote(u.username or "")
    if usuario.split(".")[0] != PAPEL_APP:
        raise DadosErro(f"a URL precisa entrar como {PAPEL_APP} (usuario {PAPEL_APP}.<ref> no pooler)")
    if u.port == 6543:
        raise DadosErro("use o Session pooler (porta 5432), nao o Transaction pooler (6543)")


class _Pool:
    """Pool minimo e a prova de erro (sem depender de detalhes internos do psycopg2)."""

    def __init__(self, conectar, maximo: int):
        import queue
        import threading

        self._conectar = conectar
        self._max = maximo
        self._livres: "queue.LifoQueue" = queue.LifoQueue()
        self._abertas = 0
        self._trava = threading.Lock()

    def getconn(self):
        import queue

        while True:
            try:
                conn = self._livres.get_nowait()
            except queue.Empty:
                with self._trava:
                    if self._abertas < self._max:
                        self._abertas += 1
                        criar = True
                    else:
                        criar = False
                if criar:
                    try:
                        return self._conectar()
                    except Exception:
                        with self._trava:
                            self._abertas -= 1
                        raise
                try:
                    conn = self._livres.get(timeout=30)
                except queue.Empty:
                    raise DadosErro("sem conexao livre no banco (tente de novo em instantes)")
            if getattr(conn, "closed", 1):
                with self._trava:
                    self._abertas -= 1
                continue
            return conn

    def putconn(self, conn):
        if getattr(conn, "closed", 1):
            with self._trava:
                self._abertas -= 1
            return
        self._livres.put(conn)

    def fechar_tudo(self):
        while not self._livres.empty():
            try:
                self._livres.get_nowait().close()
            except Exception:
                pass
        self._abertas = 0


def criar_pool(url: str, maximo: int = 3):
    """Pool pequeno (o papel tiago_app tem limite de 5 conexoes no banco)."""
    import psycopg2

    validar_url(url)

    def conectar():
        conn = psycopg2.connect(url, connect_timeout=15, application_name="tiago_app")
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")  # o pooler nao carrega 'alter role ... set search_path'
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(f"SET statement_timeout = {int(TEMPO_LIMITE_MS)}")
            cur.execute("SELECT current_user")
            usuario = cur.fetchone()[0]
        conn.commit()  # SET precisa ser confirmado, senao o rollback desfaz
        if usuario != PAPEL_APP:
            conn.close()
            raise DadosErro(f"conectou como {usuario!r}; so' {PAPEL_APP} e' permitido")
        return conn

    return _Pool(conectar, maximo)


def abrir_dados(url: str | None = None) -> Dados:
    """Uso no app: db = abrir_dados()  (le st.secrets['DATABASE_URL'])."""
    if url is None:
        import streamlit as st

        url = st.secrets["DATABASE_URL"]
    return Dados(criar_pool(url))
