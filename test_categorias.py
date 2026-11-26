"""
Teste da classificação de tipo (PADROES_TIPO / categorias_do_texto).

Rodar:  ./.venv/bin/python -m unittest test_categorias -v

Usa o .venv do robô (o módulo exige `requests`). Não chama a API da CVM
nem o Telegram.
"""
import unittest

from cvm_telegram_bot import categorias_do_texto

CRA = "CRA — Certificado de Recebíveis do Agronegócio"
CRI = "CRI — Certificado de Recebíveis Imobiliários"
DEB = "Debêntures"
NC = "Notas Comerciais"
OUTROS = "Outros títulos de securitização"
CPR = "CPR — Cédula de Produto Rural"
FIAGRO = "FIAGRO — Fundo de Investimento nas Cadeias Agroindustriais"
INFRA = "Fundos de Infraestrutura"
ACOES = "Ações"

# Os 16 valores crus de 'nomeValorMobiliario' do censo do --inspect de
# 26/11/2026 (2.000 ofertas mais recentes), com a categoria exata de cada um.
# Tipo fora do monitoramento de propósito (FIDC, FII, FIP, FIF, Funcine) tem
# de continuar fora. Mudou um padrão e um tipo trocou de categoria: este
# teste quebra.
CENSO_26_11_2026 = {
    "Cotas de FIDC": [],
    "Certificados de Recebíveis Imobiliários": [CRI],
    "Cotas de FII": [],
    "Debêntures": [DEB],
    "Notas Comerciais": [NC],
    "Cotas de FIP": [],
    "Cotas de FIF": [],
    "Cotas de FIAGRO": [FIAGRO],
    "Certificados de Recebíveis do Agronegócio": [CRA],
    "Outros títulos de securitização": [OUTROS],
    "Cédula de Produto Rural Financeira": [CPR],
    "Certificados de Recebíveis": [OUTROS],
    "Cotas de Fundos de Infra": [INFRA],
    "Ações": [ACOES],
    "Cotas de Funcine": [],
    "Notas Promissórias": [NC],
}


class TestNotasPromissorias(unittest.TestCase):
    """A API do SRE grafa "Notas Promissórias" (plural). O padrão antigo,
    r"\\bnota\\s+promissoria", só casava o singular: essas ofertas nunca
    geraram alerta."""

    def test_plural_como_a_api_escreve(self):
        self.assertEqual(categorias_do_texto("Notas Promissórias"), [NC])

    def test_singular(self):
        self.assertEqual(categorias_do_texto("Nota Promissória"), [NC])

    def test_sem_acento_e_maiusculas(self):
        self.assertEqual(categorias_do_texto("NOTAS PROMISSORIAS"), [NC])


class TestCensoDeTipos(unittest.TestCase):
    """Cada tipo real da API cai na categoria exata, e só nela."""

    def test_os_16_tipos_do_censo(self):
        for tipo, esperado in CENSO_26_11_2026.items():
            with self.subTest(tipo=tipo):
                self.assertEqual(categorias_do_texto(tipo), esperado)

    def test_nota_comercial_no_singular(self):
        self.assertEqual(categorias_do_texto("Nota Comercial"), [NC])


if __name__ == "__main__":
    unittest.main()
