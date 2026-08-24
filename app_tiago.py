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

import streamlit as st
from supabase import create_client
import anthropic
import pandas as pd
import plotly.graph_objects as go

MODEL_ID = "claude-sonnet-4-5"  # TODO confirmar antes de ir pra produção

st.set_page_config(page_title="TIA.go", layout="wide")


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
    st.markdown("## EnerMais")
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
]


def executar_ferramenta(nome, entrada):
    if nome == "consultar_totais_atuais":
        return consultar_totais_atuais()
    if nome == "consultar_historico_diario":
        return consultar_historico_diario(entrada.get("dias", 30))
    return {"erro": "ferramenta desconhecida"}


SYSTEM_PROMPT = (
    "Você é a TIA, assistente financeira da EnerMais, feita pra ajudar a Maria (gerente "
    "financeira) a acompanhar e prever o fluxo de caixa. Responda só com dados que vieram "
    "de verdade das ferramentas — nunca invente número, data ou valor. Se a ferramenta não "
    "trouxer dado suficiente pra responder, diga isso claramente em vez de estimar."
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

    hist = consultar_historico_diario(30)
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

with col_chat:
    st.subheader("Pergunte à TIA")
    if "mensagens" not in st.session_state:
        st.session_state.mensagens = []

    for m in st.session_state.mensagens:
        with st.chat_message(m["role"]):
            st.markdown(m["content"] if isinstance(m["content"], str) else "(ferramenta)")

    pergunta = st.chat_input("Pergunte sobre o fluxo de caixa...")
    if pergunta:
        st.session_state.mensagens.append({"role": "user", "content": pergunta})
        with st.chat_message("user"):
            st.markdown(pergunta)

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
        with st.chat_message("assistant"):
            st.markdown(texto_final)
