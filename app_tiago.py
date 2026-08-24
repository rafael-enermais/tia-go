"""
TIA.go - App Streamlit: dashboard + chat lado a lado

Le' dados do Supabase (RLS + login) e usa a API da Anthropic com tool use pra
responder perguntas sobre fluxo de caixa - o modelo so' fala numero que veio
de verdade das ferramentas (consultar_totais_atuais / consultar_historico_diario).

Segredos necessarios em Settings > Secrets do Streamlit Cloud (NUNCA no .env
local, NUNCA no repositorio):

SUPABASE_URL = "https://SEU-PROJETO.supabase.co"
SUPABASE_ANON_KEY = "sua_anon_key_aqui"   # a publica - NUNCA a service_role aqui
ANTHROPIC_API_KEY = "sua_chave_aqui"

TODO: confirmar o nome exato do modelo disponivel na conta antes de rodar em
producao - ver https://docs.claude.com/en/docs/about-claude/models (o valor
abaixo e' um placeholder, nao fato confirmado contra a conta da EnerMais).
"""

import base64
import os
from datetime import date, timedelta

import streamlit as st
from supabase import create_client
import anthropic
import pandas as pd
import plotly.graph_objects as go

MODEL_ID = "claude-sonnet-4-5"  # TODO confirmar antes de ir pra produção

st.set_page_config(page_title="TIA.go", layout="wide")

LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo-enermais.png")


@st.cache_data
def logo_base64():
    with open(LOGO_PATH, "rb") as f:
        return base64.b64encode(f.read()).decode()


def render_logo(height=56):
    # Mesmo padrão do Radar/RHdados: logo oficial (navy+laranja) em silhueta
    # branca via filtro CSS, em vez de arquivo separado - funciona em qualquer
    # fundo escuro sem precisar de um PNG branco dedicado.
    st.markdown(
        f"""
        <img src="data:image/png;base64,{logo_base64()}" height="{height}"
             style="filter: brightness(0) invert(1); margin-bottom: 12px;">
        """,
        unsafe_allow_html=True,
    )


# ---------- Cliente Supabase: sempre em session_state, nunca cache_resource ----------
# (mesma regra do RHdados - cache_resource compartilharia sessão de auth entre
# usuários diferentes, risco de vazamento de dado financeiro entre Maria/Rafael)
def get_supabase():
    if "supabase_client" not in st.session_state:
        st.session_state.supabase_client = create_client(
            st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"]
        )
    return st.session_state.supabase_client


# ---------- Login ----------
def tela_login():
    render_logo(height=72)
    st.caption("Acesso restrito")
    email = st.text_input("E-mail")
    senha = st.text_input("Senha", type="password")
    if st.button("Entrar"):
        try:
            sb = get_supabase()
            resp = sb.auth.sign_in_with_password({"email": email, "password": senha})
            st.session_state.usuario = resp.user.email
            st.rerun()
        except Exception:
            st.error("E-mail ou senha inválidos")


if "usuario" not in st.session_state:
    tela_login()
    st.stop()

with st.sidebar:
    render_logo(height=56)
    st.write(f"Logado como: {st.session_state.usuario}")
    if st.button("Sair"):
        st.session_state.clear()
        st.rerun()

sb = get_supabase()


# ---------- Ferramentas que o Claude pode chamar (zero alucinação: só dado real) ----------
def consultar_totais_atuais():
    pagar = (
        sb.table("parcelas_pagar")
        .select("balance_amount, authorization_status")
        .gt("balance_amount", 0)
        .execute()
        .data
    )
    receber = (
        sb.table("parcelas_receber").select("balance_amount").gt("balance_amount", 0).execute().data
    )
    pagar_aprovado = sum(r["balance_amount"] for r in pagar if r["authorization_status"] == "S")
    pagar_nao_aprovado = sum(r["balance_amount"] for r in pagar if r["authorization_status"] == "N")
    receber_total = sum(r["balance_amount"] for r in receber)
    return {
        "pagar_aprovado": pagar_aprovado,
        "pagar_nao_aprovado": pagar_nao_aprovado,
        "pagar_total": pagar_aprovado + pagar_nao_aprovado,
        "receber_total": receber_total,
        "saldo_projetado": receber_total - (pagar_aprovado + pagar_nao_aprovado),
    }


def consultar_historico_diario(dias=30):
    resp = (
        sb.table("resumo_diario_fluxo_caixa")
        .select("*")
        .order("capturado_em", desc=True)
        .limit(dias)
        .execute()
    )
    return resp.data


def consultar_maiores_vencimentos(tipo="pagar", n=10):
    n = max(1, min(int(n), 50))
    if tipo == "receber":
        dados = (
            sb.table("parcelas_receber")
            .select("due_date, client_name, balance_amount")
            .gt("balance_amount", 0)
            .order("balance_amount", desc=True)
            .limit(n)
            .execute()
            .data
        )
    else:
        dados = (
            sb.table("parcelas_pagar")
            .select("due_date, creditor_name, balance_amount, authorization_status")
            .gt("balance_amount", 0)
            .order("balance_amount", desc=True)
            .limit(n)
            .execute()
            .data
        )
    return dados


def consultar_previsao_por_vencimento(meses=3):
    # Previsao por VENCIMENTO (due_date das parcelas ja cadastradas), nao por
    # tendencia historica - funciona mesmo com so' 1 dia de snapshot, porque
    # usa o dado atual de quando cada parcela vence, nao a evolucao dia a dia.
    meses = max(1, min(int(meses), 12))
    hoje = date.today()
    limite = (hoje + timedelta(days=31 * meses)).isoformat()
    pagar = (
        sb.table("parcelas_pagar")
        .select("due_date, balance_amount, authorization_status")
        .gt("balance_amount", 0)
        .lte("due_date", limite)
        .execute()
        .data
    )
    receber = (
        sb.table("parcelas_receber")
        .select("due_date, balance_amount")
        .gt("balance_amount", 0)
        .lte("due_date", limite)
        .execute()
        .data
    )
    buckets = {}
    for p in pagar:
        if not p.get("due_date"):
            continue
        mes = p["due_date"][:7]
        b = buckets.setdefault(mes, {"pagar_aprovado": 0, "pagar_nao_aprovado": 0, "receber_total": 0})
        if p.get("authorization_status") == "S":
            b["pagar_aprovado"] += p["balance_amount"]
        else:
            b["pagar_nao_aprovado"] += p["balance_amount"]
    for r in receber:
        if not r.get("due_date"):
            continue
        mes = r["due_date"][:7]
        b = buckets.setdefault(mes, {"pagar_aprovado": 0, "pagar_nao_aprovado": 0, "receber_total": 0})
        b["receber_total"] += r["balance_amount"]
    linhas = []
    for mes in sorted(buckets):
        b = buckets[mes]
        pagar_total = b["pagar_aprovado"] + b["pagar_nao_aprovado"]
        linhas.append({
            "mes": mes,
            "pagar_aprovado": b["pagar_aprovado"],
            "pagar_nao_aprovado": b["pagar_nao_aprovado"],
            "pagar_total": pagar_total,
            "receber_total": b["receber_total"],
            "saldo": b["receber_total"] - pagar_total,
        })
    return linhas


def consultar_movimentos(tipo="pagamento", nome=None, meses=3):
    # Ledger REAL (payments/receipts do Sienge, ja com data real do evento) -
    # nao depende de historico acumulado por nos, existe desde a 1a ingestao.
    meses = max(1, min(int(meses), 24))
    desde = (date.today() - timedelta(days=31 * meses)).isoformat()
    if tipo == "recebimento":
        tabela, campo_nome = "movimentos_recebimento", "client_name"
    else:
        tabela, campo_nome = "movimentos_pagamento", "creditor_name"
    q = (
        sb.table(tabela)
        .select(f"bill_id, installment_id, {campo_nome}, payment_date, net_amount, operation_type_name")
        .gte("payment_date", desde)
        .order("payment_date", desc=True)
    )
    if nome:
        q = q.ilike(campo_nome, f"%{nome}%")
    dados = q.execute().data
    total = sum(d["net_amount"] or 0 for d in dados)
    return {"total": total, "quantidade": len(dados), "movimentos": dados[:50]}


def consultar_aprovacoes(fornecedor=None, dias=90):
    dias = max(1, min(int(dias), 730))
    desde_dt = date.today() - timedelta(days=dias)
    q = (
        sb.table("aprovacoes_pagar")
        .select("bill_id, installment_id, creditor_name, authorization_user_name, authorization_date, is_last_to_authorize")
        .gte("authorization_date", desde_dt.isoformat())
        .order("authorization_date", desc=True)
    )
    if fornecedor:
        q = q.ilike("creditor_name", f"%{fornecedor}%")
    return q.execute().data[:50]


TOOLS = [
    {
        "name": "consultar_totais_atuais",
        "description": (
            "Retorna os totais ATUAIS de contas a pagar (aprovado/não aprovado) e a "
            "receber, somando só parcelas em aberto (saldo > 0)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "consultar_historico_diario",
        "description": (
            "Retorna o histórico diário do fluxo de caixa (últimos N dias) — como os "
            "totais de a pagar/a receber evoluíram dia a dia."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dias": {"type": "integer", "description": "Quantos dias retornar (padrão 30)"}
            },
        },
    },
    {
        "name": "consultar_maiores_vencimentos",
        "description": (
            "Lista as N parcelas em aberto de maior valor (saldo > 0), ordenadas do "
            "maior pro menor. Use quando pedirem uma lista/tabela dos maiores "
            "vencimentos, contas ou recebíveis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": ["pagar", "receber"], "description": "pagar ou receber (padrão pagar)"},
                "n": {"type": "integer", "description": "Quantas parcelas retornar (padrão 10, máx 50)"},
            },
        },
    },
    {
        "name": "consultar_previsao_por_vencimento",
        "description": (
            "Projeta o fluxo de caixa dos próximos N meses agrupando as parcelas já "
            "cadastradas pela DATA DE VENCIMENTO (não é tendência histórica — funciona "
            "mesmo com pouco histórico acumulado). Use para 'previsão', 'projeção' ou "
            "'fluxo dos próximos meses'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meses": {"type": "integer", "description": "Quantos meses pra frente (padrão 3, máx 12)"}
            },
        },
    },
    {
        "name": "consultar_movimentos",
        "description": (
            "Ledger REAL de pagamentos ou recebimentos JÁ REALIZADOS (data real do evento, "
            "segundo o Sienge - não depende de histórico acumulado por nós). Use pra "
            "'quanto já pagamos/recebemos', especialmente filtrando por fornecedor/cliente "
            "específico (parâmetro nome)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": ["pagamento", "recebimento"], "description": "pagamento ou recebimento (padrão pagamento)"},
                "nome": {"type": "string", "description": "Nome (ou parte do nome) do fornecedor/cliente pra filtrar - opcional"},
                "meses": {"type": "integer", "description": "Quantos meses pra trás olhar (padrão 3, máx 24)"},
            },
        },
    },
    {
        "name": "consultar_aprovacoes",
        "description": (
            "Histórico REAL de aprovações de contas a pagar (quem aprovou, quando) - data "
            "real do Sienge, não depende de histórico acumulado por nós."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fornecedor": {"type": "string", "description": "Nome (ou parte do nome) do fornecedor pra filtrar - opcional"},
                "dias": {"type": "integer", "description": "Quantos dias pra trás olhar (padrão 90)"},
            },
        },
    },
]

# Ferramentas cujo resultado, além de virar texto pro chat, também é plotado
# direto no dashboard (dash reage à conversa) - populam st.session_state.dash_extra.
FERRAMENTAS_VISUAIS = {
    "consultar_maiores_vencimentos",
    "consultar_previsao_por_vencimento",
    "consultar_movimentos",
    "consultar_aprovacoes",
}


def executar_ferramenta(nome, entrada):
    if nome == "consultar_totais_atuais":
        return consultar_totais_atuais()
    if nome == "consultar_historico_diario":
        return consultar_historico_diario(entrada.get("dias", 30))
    if nome == "consultar_maiores_vencimentos":
        return consultar_maiores_vencimentos(entrada.get("tipo", "pagar"), entrada.get("n", 10))
    if nome == "consultar_previsao_por_vencimento":
        return consultar_previsao_por_vencimento(entrada.get("meses", 3))
    if nome == "consultar_movimentos":
        return consultar_movimentos(entrada.get("tipo", "pagamento"), entrada.get("nome"), entrada.get("meses", 3))
    if nome == "consultar_aprovacoes":
        return consultar_aprovacoes(entrada.get("fornecedor"), entrada.get("dias", 90))
    return {"erro": "ferramenta desconhecida"}


SYSTEM_PROMPT = (
    "Você é a TIA.go, assistente financeira da EnerMais, feita pra ajudar a Maria (gerente "
    "financeira) a acompanhar e prever o fluxo de caixa. Responda só com dados que vieram "
    "de verdade das ferramentas — nunca invente número, data ou valor. Se a ferramenta não "
    "trouxer dado suficiente pra responder, diga isso claramente em vez de estimar.\n\n"
    "Guia de qual ferramenta usar (importante, escolha pela intenção real da pergunta, não "
    "só pela palavra 'histórico'):\n"
    "- 'o que já foi pago/recebido', 'histórico de pagamento/recebimento', 'quanto pagamos "
    "pro fornecedor X', 'quando recebemos do cliente Y' → consultar_movimentos. É dado REAL "
    "do Sienge (data real do evento), funciona desde o primeiro dia, NÃO precisa de "
    "histórico acumulado.\n"
    "- 'quem aprovou', 'quando foi aprovado' → consultar_aprovacoes. Mesma coisa, dado real, "
    "sem depender de acúmulo.\n"
    "- 'quanto vai vencer', 'previsão dos próximos meses', 'projeção' → "
    "consultar_previsao_por_vencimento. Usa a data de vencimento das parcelas já "
    "cadastradas, também não depende de acúmulo.\n"
    "- só use consultar_historico_diario se a pergunta for especificamente sobre a EVOLUÇÃO "
    "DO SALDO TOTAL EM ABERTO dia a dia (ex.: 'como o saldo agregado mudou nos últimos "
    "dias') — essa é a única métrica que realmente depende de dias reais se acumularem, "
    "avise isso se o histórico ainda for curto, mas SEMPRE ofereça consultar_movimentos ou "
    "consultar_previsao_por_vencimento como alternativa que já funciona.\n\n"
    "Quando usar consultar_maiores_vencimentos, consultar_previsao_por_vencimento, "
    "consultar_movimentos ou consultar_aprovacoes, o resultado também aparece como "
    "tabela/gráfico no dashboard ao lado — pode avisar a pessoa disso na resposta."
)


# ---------- Layout: dashboard + chat lado a lado ----------
col_dash, col_chat = st.columns(2)

with col_dash:
    st.subheader("Fluxo de caixa — resumo atual")
    totais = consultar_totais_atuais()
    c1, c2, c3 = st.columns(3)
    c1.metric("A pagar aprovado", f"R$ {totais['pagar_aprovado']:,.2f}")
    c2.metric("A pagar não aprovado", f"R$ {totais['pagar_nao_aprovado']:,.2f}")
    c3.metric("A receber", f"R$ {totais['receber_total']:,.2f}")
    st.metric("Saldo projetado (receber − pagar)", f"R$ {totais['saldo_projetado']:,.2f}")

    PERIODOS = {"Últimos 7 dias": 7, "Últimos 15 dias": 15, "Últimos 30 dias": 30, "Últimos 90 dias": 90, "Tudo": 3650}
    periodo_label = st.selectbox("Período do histórico", list(PERIODOS.keys()), index=2)
    hist = consultar_historico_diario(PERIODOS[periodo_label])
    if hist:
        df = pd.DataFrame(hist).sort_values("capturado_em")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["capturado_em"], y=df["pagar_total"], name="A pagar"))
        fig.add_trace(go.Scatter(x=df["capturado_em"], y=df["receber_total"], name="A receber"))
        fig.update_layout(title="Evolução diária", height=350, margin=dict(t=40))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info(
            "Ainda sem histórico pra mostrar gráfico — precisa rodar o script de "
            "ingestão diária (ingest_sienge_to_supabase.py) pelo menos 1x."
        )

    # ---- Visualização gerada pela conversa (dash reage ao que foi perguntado no chat) ----
    extra = st.session_state.get("dash_extra")
    if extra:
        st.markdown("---")
        if extra["tool"] == "consultar_maiores_vencimentos":
            tipo = extra["input"].get("tipo", "pagar")
            st.caption(f"Gerado pela conversa — maiores vencimentos ({tipo})")
            if extra["resultado"]:
                st.dataframe(pd.DataFrame(extra["resultado"]), use_container_width=True, hide_index=True)
            else:
                st.caption("Nenhuma parcela em aberto encontrada.")
        elif extra["tool"] == "consultar_previsao_por_vencimento":
            meses = extra["input"].get("meses", 3)
            st.caption(f"Gerado pela conversa — previsão por vencimento ({meses} meses)")
            if extra["resultado"]:
                df_prev = pd.DataFrame(extra["resultado"])
                fig_prev = go.Figure()
                fig_prev.add_trace(go.Bar(x=df_prev["mes"], y=df_prev["pagar_total"], name="A pagar"))
                fig_prev.add_trace(go.Bar(x=df_prev["mes"], y=df_prev["receber_total"], name="A receber"))
                fig_prev.update_layout(barmode="group", height=320, margin=dict(t=20))
                st.plotly_chart(fig_prev, use_container_width=True)
            else:
                st.caption("Nenhuma parcela com vencimento no período encontrada.")
        elif extra["tool"] == "consultar_movimentos":
            tipo = extra["input"].get("tipo", "pagamento")
            nome = extra["input"].get("nome")
            titulo = f"Gerado pela conversa — movimentos de {tipo}" + (f" ({nome})" if nome else "")
            st.caption(titulo)
            res = extra["resultado"]
            if res["movimentos"]:
                st.metric("Total no período", f"R$ {res['total']:,.2f}")
                st.dataframe(pd.DataFrame(res["movimentos"]), use_container_width=True, hide_index=True)
            else:
                st.caption("Nenhum movimento real encontrado no período.")
        elif extra["tool"] == "consultar_aprovacoes":
            st.caption("Gerado pela conversa — histórico de aprovações")
            if extra["resultado"]:
                st.dataframe(pd.DataFrame(extra["resultado"]), use_container_width=True, hide_index=True)
            else:
                st.caption("Nenhuma aprovação encontrada no período.")

with col_chat:
    st.subheader("Pergunte à TIA.go")
    if "mensagens" not in st.session_state:
        st.session_state.mensagens = []

    # Container de altura fixa: histórico rola dentro dele e o chat_input, por
    # ser chamado DENTRO desse container, fica fixo (colado) no rodapé dele -
    # é o comportamento nativo do Streamlit pra chat_input em container com
    # altura definida. Sem isso o input aparecia em fluxo normal (no topo,
    # empurrando as mensagens pra baixo) em vez de fixo embaixo.
    #
    # CSS extra: por padrão, com poucas mensagens, elas ficam no TOPO do
    # container e sobra um vão vazio grande até o input (que fica fixo no
    # rodapé) - visualmente parece "flutuando". O trecho abaixo diz pro bloco
    # de mensagens ser flex-column com justify-content:flex-end, empurrando o
    # conteúdo pra encostar no input e crescer pra CIMA conforme a conversa
    # cresce (como um chat comum). É CSS torcendo a estrutura interna do
    # Streamlit (via a classe do `key="chat_box"`) - funciona nas versões
    # atuais, mas pode precisar de ajuste se o Streamlit mudar essa estrutura
    # numa atualização futura (avisa se o efeito parar de funcionar).
    st.markdown(
        """
        <style>
        .st-key-chat_box [data-testid="stVerticalBlock"] {
            display: flex;
            flex-direction: column;
            justify-content: flex-end;
            min-height: 100%;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    chat_box = st.container(height=560, key="chat_box")
    with chat_box:
        for m in st.session_state.mensagens:
            with st.chat_message(m["role"]):
                st.markdown(m["content"] if isinstance(m["content"], str) else "(ferramenta)")
        pergunta = st.chat_input("Pergunte sobre o fluxo de caixa...")

    # Padrão de 2 fases (evita o bug de ordem visto no teste real: nunca
    # renderizar a mensagem nova "na mão" fora do loop - sempre grava no
    # session_state e recarrega, pra o loop acima (a única fonte de verdade
    # da ordem) desenhar tudo certo, com o chat_input sempre por último):
    # fase 1 - só grava a pergunta e recarrega (aparece na hora, sem esperar
    # a API); fase 2 - roda na recarga seguinte, sem pergunta nova pendente.
    if pergunta:
        st.session_state.mensagens.append({"role": "user", "content": pergunta})
        st.rerun()

    if st.session_state.mensagens and st.session_state.mensagens[-1]["role"] == "user":
        with st.spinner("TIA.go consultando..."):
            client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
            mensagens_api = [
                {"role": m["role"], "content": m["content"]}
                for m in st.session_state.mensagens
                if isinstance(m["content"], str)
            ]

            resposta = client.messages.create(
                model=MODEL_ID,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=mensagens_api,
            )

            while resposta.stop_reason == "tool_use":
                tool_uses = [b for b in resposta.content if b.type == "tool_use"]
                resultados = []
                for tu in tool_uses:
                    resultado = executar_ferramenta(tu.name, tu.input)
                    if tu.name in FERRAMENTAS_VISUAIS:
                        st.session_state.dash_extra = {
                            "tool": tu.name, "input": tu.input, "resultado": resultado
                        }
                    resultados.append(
                        {"type": "tool_result", "tool_use_id": tu.id, "content": str(resultado)}
                    )
                mensagens_api.append({"role": "assistant", "content": resposta.content})
                mensagens_api.append({"role": "user", "content": resultados})
                resposta = client.messages.create(
                    model=MODEL_ID,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=mensagens_api,
                )

            texto_final = "".join(b.text for b in resposta.content if b.type == "text")

        st.session_state.mensagens.append({"role": "assistant", "content": texto_final})
        st.rerun()
