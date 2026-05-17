#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Testa o motor do robô (versão SRE) com dados simulados no formato EXATO da
API do SRE (confirmado pelo teste de acesso no GitHub Actions).
"""
import json

import cvm_telegram_bot as bot

HOJE = bot.datetime.now()


def d(dias_atras):
    return (HOJE - bot.timedelta(days=dias_atras)).strftime("%d/%m/%Y")


# Registros no formato REAL da API /detalhado
def reg(idr, tipo, emissor, lider, status, dias, valor="0,0000",
        processo=None, modalidade="PRIMARIA", rito="OPD Aut"):
    return {
        "idRequerimento": idr,
        "numeroProcesso": processo or f"SRE/{idr}/2026",
        "numeroProtocolo": f"{idr}/2026",
        "nomeValorMobiliario": tipo,
        "tipoDeOferta": modalidade,
        "statusDaOferta": status,
        "nomeEmissor": emissor,
        "cnpjEmissor": "00.000.000/0001-00",
        "nomeCoordenadorLider": lider,
        "cnpjCoordenadorLider": "11.111.111/0001-11",
        "nomeTipoRequerimento": rito,
        "valorTotalEmReais": valor,
        "data": d(dias),
        "registroAutomatico": True,
        "totalRegistros": 999,
    }


LISTA_R1 = [
    reg("26335", "Debêntures", "ECO101 RODOVIAS S.A.", "BANCO BRADESCO BBI S.A.",
        "Aguardando Bookbuilding", 2, "2.400.000.000,0000"),
    reg("26300", "Certificados de Recebíveis Imobiliários", "OPEA SECURITIZADORA S.A.",
        "ITAU BBA", "Registro Concedido", 5, "500.000.000,0000"),
    reg("26280", "Certificados de Recebíveis do Agronegócio", "ECO SECURITIZADORA",
        "BANCO BOCOM BBM", "Aguardando Bookbuilding", 3, "0,0000"),
    reg("26250", "Notas Comerciais", "PROFARMA S.A.", "CAIXA ECONOMICA",
        "Registro Concedido", 8, "200.000.000,0000"),
    # Ações -> ignora
    reg("26240", "Ações Ordinárias", "TECH S.A.", "BANCO X",
        "Registro Concedido", 4, "1.000.000.000,0000"),
    # Debênture cancelada -> ignora
    reg("26230", "Debêntures", "CANCELADA S.A.", "BANCO Y",
        "Oferta Cancelada", 3),
    # CRI antigo (fora da janela de 45 dias) -> ignora
    reg("25000", "Certificados de Recebíveis Imobiliários", "VELHA SEC S.A.",
        "BANCO Z", "Registro Concedido", 400, "100.000.000,0000"),
]

# Detalhe (infOferta) no formato real: lista de {codigo, campoNome, valor}
DETALHES = {
    "26280": [
        {"codigo": 26280, "campoNome": "Emissão nº", "valor": "12"},
        {"codigo": 26280, "campoNome": "Tipo de lastro", "valor": "Concentrado"},
        {"codigo": 26280, "campoNome": "Identificação dos devedores e coobrigados",
         "valor": "USINA SãO JOãO AÇÚCAR E ÁLCOOL S.A. (devedora)"},
        {"codigo": 26280, "campoNome": "Destinação dos recursos", "valor": "..."},
    ],
    "26300": [
        {"codigo": 26300, "campoNome": "Emissão nº", "valor": "143"},
        {"codigo": 26300, "campoNome": "Identificação dos devedores e coobrigados",
         "valor": "ALLOS S.A."},
    ],
}

# Participantes (participantes/{id}) no formato real: lista de objetos
# com idParticipante, razaoSocial, cnpjInstituicao, tipo.
PARTICIPANTES = {
    "26335": [   # debênture ECO101 — líder Bradesco + 3 coordenadores + emissor
        {"idParticipante": "1", "razaoSocial": "BANCO BRADESCO BBI S.A.",
         "cnpjInstituicao": "06.271.464/0001-19", "tipo": "COORDENADOR"},
        {"idParticipante": "2", "razaoSocial": "ITAU BBA ASSESSORIA FINANCEIRA S.A",
         "cnpjInstituicao": "04.845.753/0001-59", "tipo": "COORDENADOR"},
        {"idParticipante": "3", "razaoSocial": "BANCO SANTANDER (BRASIL) S.A.",
         "cnpjInstituicao": "90.400.888/0001-42", "tipo": "COORDENADOR"},
        {"idParticipante": "4", "razaoSocial": "XP INVESTIMENTOS CCTVM S.A.",
         "cnpjInstituicao": "02.332.886/0001-04", "tipo": "COORDENADOR"},
        {"idParticipante": "5", "razaoSocial": "ECO101 RODOVIAS S.A.",
         "cnpjInstituicao": "00.000.000/0001-00", "tipo": "EMISSOR"},
    ],
    "26300": [   # CRI com só o líder, sem demais coordenadores
        {"idParticipante": "9", "razaoSocial": "ITAU BBA",
         "cnpjInstituicao": "04.845.753/0001-59", "tipo": "COORDENADOR"},
    ],
}


# --- substitui as chamadas de rede por dados simulados --------------
class FakeSession:
    def post(self, url, json=None, timeout=None):
        return FakeResp(200, {"totalRegistros": len(_LISTA), "totalPaginas": 1,
                              "pagina": 1, "registros": _LISTA})

    def get(self, url, timeout=None):
        # extrai o id do final da URL e roteia conforme o endpoint
        idr = url.rstrip("/").split("/")[-1]
        if "/participantes/" in url:
            return FakeResp(200, PARTICIPANTES.get(idr, []))
        return FakeResp(200, DETALHES.get(idr, []))


class FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


_LISTA = LISTA_R1
bot._nova_sessao = lambda: FakeSession()

if bot.STATE_FILE.exists():
    bot.STATE_FILE.unlink()
enviadas = []
bot.enviar_telegram = lambda texto: enviadas.append(texto) or True

print("\n##### TESTE 1: parsing de um registro da API #####")
o = bot.sre_registro_para_oferta(LISTA_R1[0])
assert o.id == "26335"
assert o.processo == "SRE/26335/2026"
assert o.tipo == "Debêntures"
assert o.emissor == "ECO101 RODOVIAS S.A."
assert o.lider == "BANCO BRADESCO BBI S.A."
assert o.categorias == ["Debêntures"]
assert o.fase == bot.FASE_ANALISE     # "Aguardando Bookbuilding"
assert o.data is not None
print(f"  id={o.id} tipo={o.tipo!r} fase={o.fase} OK ✅")

print("\n##### TESTE 2: classificação de fase por status #####")
casos = [
    ("Aguardando Bookbuilding", bot.FASE_ANALISE),
    ("Registro Concedido", bot.FASE_REGISTRADA),
    ("Oferta Encerrada", bot.FASE_REGISTRADA),
    ("Oferta Cancelada", None),
    ("Registro Caducado", None),
    ("Requerimento Expirado", None),
    ("", None),
]
for status, esperado in casos:
    got = bot.fase_do_status(status)
    status_ok = "✅" if got == esperado else "❌"
    print(f"  {status_ok}  {status!r:30} -> {got} (esperado {esperado})")
    assert got == esperado
print("OK ✅")

print("\n##### TESTE 3: tipos — singular/plural e exclusões #####")
for tipo, deve in [
    ("Debêntures", True), ("Debênture", True),
    ("Certificados de Recebíveis Imobiliários", True),
    ("Certificado de Recebíveis do Agronegócio", True),
    ("Notas Comerciais", True), ("Nota Comercial", True),
    ("Ações Ordinárias", False), ("Cotas de FII", False),
]:
    got = bool(bot.categorias_do_texto(tipo))
    assert got == deve, f"falhou em {tipo!r}"
print("OK ✅")

print("\n##### TESTE 4: primeira execução semeia, não notifica #####")
bot.verificar(dry_run=False)
assert len(enviadas) == 0
estado = json.loads(bot.STATE_FILE.read_text(encoding="utf-8"))
# de interesse na janela: Deb + CRI + CRA + NC = 4 (ações/cancelada/antigo fora)
print(f"  Ofertas semeadas: {len(estado['ofertas'])} (esperado: 4)")
assert len(estado["ofertas"]) == 4
print("OK ✅")

print("\n##### TESTE 5: 2ª execução sem mudanças -> nada enviado #####")
enviadas.clear()
bot.verificar(dry_run=False)
assert len(enviadas) == 0
print("OK ✅")

print("\n##### TESTE 6: novo CRA em análise -> alerta com DEVEDOR #####")
_LISTA = LISTA_R1 + [reg("26280", "Certificados de Recebíveis do Agronegócio",
                         "ECO SECURITIZADORA", "BANCO BOCOM BBM",
                         "Aguardando Bookbuilding", 1, "0,0000",
                         processo="SRE/99999/2026")]
# obs: novo processo único para contar como nova oferta
enviadas.clear()
bot.verificar(dry_run=False)
print(f"  Mensagens: {len(enviadas)} (esperado: 1)")
assert len(enviadas) == 1
msg = enviadas[0]
assert "em análise" in msg.lower()
assert "Devedor (risco)" in msg, "CRA novo deveria trazer devedor"
assert "USINA S" in msg, "devedor real deveria aparecer"
assert "oferta-publica/26280" in msg, "link direto da oferta deveria aparecer"
print("  ---- mensagem ----")
print(msg)
print("  ------------------")
print("OK ✅")

print("\n##### TESTE 7: CRI registrado traz devedor; debênture NÃO #####")
bot.STATE_FILE.unlink(missing_ok=True)
_LISTA = LISTA_R1
enviadas.clear()
bot.verificar(dry_run=False, notificar_primeira=True)
msg_cri = [m for m in enviadas if "OPEA" in m][0]
msg_deb = [m for m in enviadas if "ECO101" in m][0]
assert "Devedor (risco)" in msg_cri and "ALLOS S.A." in msg_cri
assert "Devedor (risco)" not in msg_deb, "debênture não tem devedor separado"
assert not any("CANCELADA" in m for m in enviadas)
assert not any("VELHA SEC" in m for m in enviadas)
assert not any("TECH S.A." in m for m in enviadas)
print(f"  Alertas: {len(enviadas)} (esperado: 4)")
assert len(enviadas) == 4
print("OK ✅")

print("\n##### TESTE 8: demais coordenadores do consórcio #####")
# função isolada: a debênture 26335 tem líder Bradesco + 3 coords + 1 emissor
coords = bot.sre_coordenadores(FakeSession(), "26335", "BANCO BRADESCO BBI S.A.")
print(f"  Coordenadores (excluindo líder): {coords}")
assert "ITAU BBA ASSESSORIA FINANCEIRA S.A" in coords
assert "BANCO SANTANDER (BRASIL) S.A." in coords
assert "XP INVESTIMENTOS CCTVM S.A." in coords
assert not any("BRADESCO" in c for c in coords), "o líder não deve repetir na lista"
assert not any("ECO101" in c for c in coords), "o emissor (tipo!=COORDENADOR) deve sair"
assert len(coords) == 3
# numa oferta só com o líder, a lista de demais coordenadores fica vazia
coords_vazio = bot.sre_coordenadores(FakeSession(), "26300", "ITAU BBA")
assert coords_vazio == [], "só o líder -> sem demais coordenadores"
# e na mensagem completa, a linha deve aparecer
bot.STATE_FILE.unlink(missing_ok=True)
_LISTA = LISTA_R1
enviadas.clear()
bot.verificar(dry_run=False, notificar_primeira=True)
msg_deb = [m for m in enviadas if "ECO101" in m][0]
assert "Demais coordenadores" in msg_deb
assert "ITAU BBA" in msg_deb and "SANTANDER" in msg_deb and "XP INVESTIMENTOS" in msg_deb
print("  ---- trecho da mensagem (debênture ECO101) ----")
for ln in msg_deb.split("\n"):
    if "oordenador" in ln:
        print("   " + ln)
print("OK ✅ (demais coordenadores aparecem, líder e emissor filtrados)")

print("\n##### TESTE 9: fallback quando a API do SRE falha #####")
bot.STATE_FILE.unlink(missing_ok=True)


class SessaoQuebrada:
    def post(self, *a, **k):
        return FakeResp(403, "Forbidden")

    def get(self, *a, **k):
        return FakeResp(403, "Forbidden")


bot._nova_sessao = lambda: SessaoQuebrada()
# simula o fallback devolvendo 2 ofertas
def fake_fallback():
    o1 = bot.Oferta()
    o1.fonte = "Dados Abertos (reserva)"
    o1.tipo = "Debêntures"
    o1.categorias = ["Debêntures"]
    o1.emissor = "EMISSOR RESERVA S.A."
    o1.processo = "SRE/7777/2026"
    o1.status = "Registro Concedido"
    o1.fase = bot.FASE_REGISTRADA
    o1.data = HOJE
    return [o1]


bot.coletar_do_fallback = fake_fallback
enviadas.clear()
bot.verificar(dry_run=False, notificar_primeira=True)
# deve ter: 1 aviso de modo reserva + 1 alerta da oferta
assert any("modo reserva" in m.lower() for m in enviadas), "deve avisar modo reserva"
assert any("EMISSOR RESERVA" in m for m in enviadas)
assert any("fonte de reserva" in m.lower() for m in enviadas)
print(f"  Mensagens no modo reserva: {len(enviadas)} (1 aviso + alertas)")
print("OK ✅ (fallback funciona e avisa o usuário)")

print("\n##### TESTE 10: migração do estado antigo #####")
bot.STATE_FILE.write_text(json.dumps({
    "ids_notificados": ["proc:SRE/26300/2026"], "primeira_execucao": False
}), encoding="utf-8")
est = bot.carregar_estado()
assert est["ofertas"] == {"proc:SRE/26300/2026": bot.FASE_REGISTRADA}
print("  Estado antigo convertido. OK ✅")

print("\n##### TESTE 11: formatação de valor #####")
assert bot.formatar_valor("2.400.000.000,0000") == "R$ 2.400.000.000,00"
assert bot.formatar_valor("0,0000") == ""
assert bot.formatar_valor("") == ""
print("OK ✅")

bot.STATE_FILE.unlink(missing_ok=True)
if bot.LOG_FILE.exists():
    bot.LOG_FILE.unlink()
print("\n=========================================")
print(" TODOS OS TESTES PASSARAM ✅")
print("=========================================")
