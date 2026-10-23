"""
Teste do app (Streamlit AppTest) com Supabase Auth FALSO e o mesmo Postgres de teste da outra suite.
Roda so' se existir streamlit e TIAGO_TEST_ADMIN_URL (rode ANTES test_tiago_v2.py para criar o schema e os dados).
"""
import os
import sys
import types
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
ADMIN = os.environ.get("TIAGO_TEST_ADMIN_URL")
pytestmark = pytest.mark.skipif(not ADMIN, reason="sem Postgres de teste")

RAIZ = Path(__file__).resolve().parents[1]


class _User:
    def __init__(self, email):
        self.email = email


class _Auth:
    def sign_in_with_password(self, cred):
        if cred["password"] != "certa":
            raise Exception("invalid")
        return types.SimpleNamespace(user=_User(cred["email"]))

    def sign_out(self):
        _Auth.saiu = True


class _Cliente:
    auth = _Auth()


@pytest.fixture()
def app(monkeypatch):
    import supabase

    monkeypatch.setattr(supabase, "create_client", lambda *a, **k: _Cliente())
    monkeypatch.syspath_prepend(str(RAIZ))
    import streamlit as st

    st.cache_resource.clear()
    at = st_testing.AppTest.from_file(str(RAIZ / "app_tiago.py"), default_timeout=30)
    at.secrets["SUPABASE_URL"] = "https://x.supabase.co"
    at.secrets["SUPABASE_ANON_KEY"] = "fake"
    at.secrets["ANTHROPIC_API_KEY"] = "fake"
    at.secrets["DATABASE_URL"] = "postgresql://tiago_app@/" + ADMIN.split("/", 3)[3]
    return at.run()


def entrar(at, email, senha="certa"):
    at.text_input[0].set_value(email)
    at.text_input[1].set_value(senha)
    at.button[0].click()
    return at.run()


def textos(at):
    return " | ".join([e.value for e in at.error] + [e.value for e in at.caption] + [m.value for m in at.markdown])


def test_tela_inicial_pede_login(app):
    assert not app.exception
    assert "Acesso restrito" in textos(app)


def test_senha_errada(app):
    at = entrar(app, "ok@x.com", "errada")
    assert "E-mail ou senha inválidos" in textos(at)


def test_email_liberado_entra_e_ve_o_painel(app):
    at = entrar(app, "ok@x.com")
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["usuario"] == "ok@x.com" and at.session_state["papel"] == "EDITOR"


@pytest.mark.parametrize("email", ["off@x.com", "desconhecido@x.com"])
def test_email_sem_acesso_e_barrado(app, email):
    at = entrar(app, email)
    assert "não tem acesso ao TIA.go" in textos(at)
    assert "usuario" not in at.session_state
