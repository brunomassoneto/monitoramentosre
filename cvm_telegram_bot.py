#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robô CVM → Telegram  (fonte: API do SRE)
========================================

Monitora as ofertas públicas de distribuição na CVM e publica alertas em um
canal do Telegram quando:
  - um NOVO PEDIDO de oferta entra em análise; e
  - uma oferta é REGISTRADA.

A mesma oferta pode gerar dois alertas ao longo do ciclo:
"📥 em análise"  ->  "✅ registrada".

Tipos monitorados: CRA, CRI, Debêntures e Notas Comerciais.

Fonte de dados — principal
--------------------------
API do sistema SRE (a mesma que alimenta a tela pública de consulta):

  POST  https://web.cvm.gov.br/sre-publico-cvm/rest/sitePublico/pesquisar/detalhado
        -> lista paginada de ofertas, ordenável por data (mais recentes primeiro).
  GET   https://web.cvm.gov.br/sre-publico-cvm/rest/sitePublico/pesquisar/infOferta/{id}
        -> detalhe de uma oferta (lista de pares {campoNome, valor}); é daqui
           que sai o "devedor" (risco) em CRA/CRI.

Vantagem: dados atualizados em tempo quase real (sem a defasagem de dias do
Portal de Dados Abertos).

Atenção/fragilidade: esta API é interna do site da CVM, não é documentada nem
tem contrato público de estabilidade. Pode mudar de formato ou passar a
bloquear acesso automatizado sem aviso. Por isso o robô tem uma FONTE DE
RESERVA (ver abaixo).

Modo reserva (degradado)
------------------------
Se a API do SRE falhar (timeout, bloqueio, formato inesperado), o robô NÃO
dispara alertas individuais. Ele envia, no máximo uma vez a cada 12 horas,
um aviso de "modo reserva" no Telegram — só para você saber que está
degradado. Quando o SRE voltar, as ofertas reais surgidas nesse período
são capturadas normalmente na próxima verificação via SRE.

(O motivo: o Portal de Dados Abertos da CVM contém o histórico inteiro e
usa identificadores diferentes dos do SRE; comparar com o estado salvo
gera milhares de falsos positivos. Ainda é possível inspecioná-lo via
--inspect para diagnóstico.)

Como usar
---------
    python cvm_telegram_bot.py --inspect        # baixa e analisa a estrutura da API
    python cvm_telegram_bot.py --test-telegram  # envia uma mensagem de teste
    python cvm_telegram_bot.py                  # uma verificação (cron/Actions)
    python cvm_telegram_bot.py --loop           # roda continuamente
    python cvm_telegram_bot.py --dry-run        # verifica mas imprime em vez de enviar
"""

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Falta a biblioteca 'requests'. Instale com:  pip install requests")


# ===================================================================
# CONFIGURAÇÃO
# ===================================================================

# --- Telegram ---------------------------------------------------------
# No GitHub Actions estes valores vêm dos "Secrets" — NÃO escreva o token
# aqui se o código for para um repositório.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "COLE_AQUI_O_TOKEN_DO_BOT")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "COLE_AQUI_O_CHAT_ID")
# Chat privado (DM com o bot) para avisos administrativos como "modo
# reserva" — não vai pro canal/grupo público. Se não configurado, cai no
# TELEGRAM_CHAT_ID normal (mesmo comportamento de antes).
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "") or TELEGRAM_CHAT_ID

# --- O que monitorar --------------------------------------------------
# Padrões (regex) que casam SEM acento e SEM diferenciar maiúsculas. Casam
# singular e plural. Para monitorar só um tipo, deixe só a entrada desejada.
PADROES_TIPO = {
    "CRA — Certificado de Recebíveis do Agronegócio": [
        r"\bcra\b", r"recebiveis\s+do\s+agronegocio",
    ],
    "CRI — Certificado de Recebíveis Imobiliários": [
        r"\bcri\b", r"recebiveis\s+imobiliari",
    ],
    "Debêntures": [
        r"\bdebentur",
    ],
    "Notas Comerciais": [
        r"\bnotas?\s+comerci", r"\bnota\s+promissoria",
    ],
    # "Outros títulos de securitização" — a própria string da API; também
    # inclui "Certificados de Recebíveis" sem qualificador (securitização
    # genérica, fora de CRA/CRI).
    "Outros títulos de securitização": [
        r"outros\s+titulos\s+de\s+securitiza",
        r"^certificad\w*\s+de\s+recebiveis$",
    ],
}

# Avisar também quando um pedido ENTRA EM ANÁLISE (além do alerta de registro)?
ALERTAR_EM_ANALISE = True

# Status (de statusDaOferta) que indicam pedido encerrado SEM registro —
# ignorados. Calibrado a partir dos valores reais vistos na API/CSV.
STATUS_TERMINAIS = [
    "cancel", "indefer", "desist", "arquivad", "revog",
    "negad", "extint", "caduc", "interromp", "expir",
]

# Status que indicam oferta REGISTRADA / concedida.
STATUS_REGISTRADA = ["registro concedido", "registrada", "oferta encerrada",
                     "concedido", "deferid"]

# --- Comportamento ----------------------------------------------------
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "3600"))

# Janela: só considera ofertas com data dentro dos últimos N dias.
JANELA_DIAS = int(os.environ.get("JANELA_DIAS", "45"))

# Quantas ofertas recentes buscar por verificação (a API entrega ordenado por
# data desc, então as mais novas vêm primeiro). 200 cobre vários dias de
# sobra; aumente se o robô ficar muito tempo sem rodar.
QTD_OFERTAS_VERIFICAR = int(os.environ.get("QTD_OFERTAS_VERIFICAR", "200"))

# Amplitude do "censo de tipos" do modo --inspect. Precisa ser MUITO maior
# que a verificação normal: tipo raro (poucas emissões por ano) não aparece
# olhando só para os últimos meses, e sem vê-lo não dá para escrever o
# padrão certo em PADROES_TIPO.
INSPECT_QTD = int(os.environ.get("INSPECT_QTD", "2000"))
INSPECT_DIAS = int(os.environ.get("INSPECT_DIAS", "730"))

# A API do SRE recusa/derruba conexões de forma INTERMITENTE quando acessada
# dos runners do GitHub (datacenter fora do Brasil): ~metade das tentativas
# falha no connect, mas a tentativa seguinte costuma funcionar. Por isso o
# robô tenta várias vezes antes de desistir.
SRE_TENTATIVAS = int(os.environ.get("SRE_TENTATIVAS", "5"))
# Timeout (connect, read) em segundos. Connect curto para falhar rápido e
# partir logo para a próxima tentativa; read mais folgado.
SRE_TIMEOUT = (
    int(os.environ.get("SRE_CONNECT_TIMEOUT", "15")),
    int(os.environ.get("SRE_READ_TIMEOUT", "60")),
)
# Só avisa "modo reserva" no Telegram depois de N verificações consecutivas
# falhando (uma falha isolada — comum e transitória — não gera mensagem).
LIMIAR_AVISO_RESERVA = int(os.environ.get("LIMIAR_AVISO_RESERVA", "2"))

# --- Fontes de dados --------------------------------------------------
SRE_BASE = "https://web.cvm.gov.br/sre-publico-cvm"
SRE_API = SRE_BASE + "/rest/sitePublico/pesquisar"
SRE_API_LISTA = SRE_API + "/detalhado"
SRE_API_DETALHE = SRE_API + "/infOferta/{id}"
SRE_API_PARTICIPANTES = SRE_API + "/participantes/{id}"
SRE_API_REQUERIMENTO = SRE_API + "/requerimento/{id}"
SRE_LINK_OFERTA = SRE_BASE + "/#/oferta-publica/{id}"

# Fonte de reserva (Portal de Dados Abertos).
CVM_ZIP_URL = "https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip"

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "estado_ofertas.json"
LOG_FILE = BASE_DIR / "cvm_robo.log"

# ===================================================================
# Fim da configuração.
# ===================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("cvm-robo")

_PADROES_COMPILADOS = {
    cat: [re.compile(p) for p in padroes]
    for cat, padroes in PADROES_TIPO.items()
}

FASE_ANALISE = "analise"
FASE_REGISTRADA = "registrada"

HEADERS_NAVEGADOR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://web.cvm.gov.br",
    "Referer": SRE_BASE + "/",
    "Connection": "keep-alive",
}

# Nome do campo, no detalhe (infOferta), que traz o devedor. A busca é feita
# de forma flexível (sem acento, por trecho), então variações são toleradas.
CAMPO_DEVEDOR_CONTEM = "devedores"


# -------------------------------------------------------------------
# Utilitários de texto
# -------------------------------------------------------------------
def strip_accents(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(texto) -> str:
    if texto is None:
        return ""
    t = strip_accents(str(texto)).lower()
    return re.sub(r"\s+", " ", t).strip()


def html_escape(texto) -> str:
    return (str(texto).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


# -------------------------------------------------------------------
# Classificação de tipo e fase
# -------------------------------------------------------------------
def categorias_do_texto(texto: str) -> list:
    """Categorias monitoradas que casam com o texto do tipo de valor mobiliário."""
    txt = normalize(texto)
    if not txt:
        return []
    achadas = []
    for cat, padroes in _PADROES_COMPILADOS.items():
        if any(p.search(txt) for p in padroes):
            achadas.append(cat)
    return achadas


def fase_do_status(status: str):
    """
    Classifica a fase a partir do texto de status da oferta:
      FASE_REGISTRADA / FASE_ANALISE / None (ignorar).
    """
    st = normalize(status)
    if not st:
        return None
    if any(t in st for t in STATUS_TERMINAIS):
        return None
    if any(r in st for r in STATUS_REGISTRADA):
        return FASE_REGISTRADA
    # qualquer outro status ativo (ex.: "aguardando bookbuilding",
    # "em análise") é tratado como pedido em análise.
    return FASE_ANALISE if ALERTAR_EM_ANALISE else None


# -------------------------------------------------------------------
# Datas
# -------------------------------------------------------------------
def parse_data(valor):
    if not valor:
        return None
    s = str(valor).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    s_data = s.split(" ")[0].split("T")[0]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s_data, fmt)
        except ValueError:
            continue
    return None


# -------------------------------------------------------------------
# Modelo de oferta (normalizado, independente da fonte)
# -------------------------------------------------------------------
class Oferta:
    """Representa uma oferta de forma uniforme, venha do SRE ou do fallback."""

    __slots__ = ("id", "processo", "protocolo", "tipo", "emissor",
                 "lider", "data", "status", "valor", "modalidade",
                 "rito", "categorias", "fase", "devedor", "coordenadores",
                 "campos", "fonte")

    def __init__(self):
        self.id = ""
        self.processo = ""
        self.protocolo = ""
        self.tipo = ""
        self.emissor = ""
        self.lider = ""
        self.data = None          # datetime
        self.status = ""
        self.valor = ""
        self.modalidade = ""
        self.rito = ""
        self.categorias = []
        self.fase = None
        self.devedor = ""
        self.coordenadores = []   # lista de nomes (demais coordenadores)
        # Campos detalhados do endpoint requerimento/{id}:
        # mapa de campoNome (normalizado) -> lista ordenada de valores únicos
        # por série (vazios ignorados).
        self.campos = {}
        self.fonte = "SRE"

    def chave(self) -> str:
        """ID estável para deduplicação."""
        if self.processo:
            return f"proc:{self.processo.strip()}"
        if self.id:
            return f"id:{self.id}"
        bruto = f"{self.tipo}|{self.emissor}|{self.data}"
        return "hash:" + hashlib.sha1(bruto.encode("utf-8", "replace")).hexdigest()[:16]

    def eh_cri_cra(self) -> bool:
        return any("CRA" in c or "CRI" in c for c in self.categorias)


# -------------------------------------------------------------------
# FONTE PRINCIPAL — API do SRE
# -------------------------------------------------------------------
def _nova_sessao() -> requests.Session:
    """Cria uma sessão HTTP; visita a home do SRE para obter cookie de sessão."""
    sess = requests.Session()
    sess.headers.update(HEADERS_NAVEGADOR)
    try:
        sess.get(SRE_BASE + "/", timeout=SRE_TIMEOUT)   # pega JSESSIONID, se houver
    except Exception as exc:  # noqa: BLE001
        log.warning("Não consegui visitar a home do SRE (seguindo mesmo assim): %s", exc)
    return sess


def _payload_lista(pagina: int, tamanho: int, dias: int = None) -> dict:
    """
    Monta o corpo do POST /detalhado para uma página de resultados.

    'dias' amplia a janela de busca; usado pelo censo de tipos do --inspect,
    que precisa olhar bem mais para trás que a verificação normal.
    """
    hoje = datetime.now()
    if dias is None:
        dias = max(JANELA_DIAS * 2, 120)
    de = (hoje - timedelta(days=dias)).strftime("%d/%m/%Y")
    ate = (hoje + timedelta(days=1)).strftime("%d/%m/%Y")
    periodo = {"de": de, "ate": ate}
    return {
        "periodoCriacaoProcesso": periodo,
        "periodoCriacaoRequerimento": periodo,
        "opa": False,
        "modalidade": "TODAS",
        "tipoOferta": "OFERTA_REGULAR",
        "colunaOrdenacao": "data",
        "direcaoOrdenacao": "DESC",
        "pagina": pagina,
        "tamanhoPagina": str(tamanho),
    }


def sre_listar_ofertas(sess: requests.Session, limite: int,
                       dias: int = None) -> list:
    """
    Busca as ofertas mais recentes via POST /detalhado, paginando até atingir
    'limite' registros. Devolve uma lista de dicts crus da API.
    Lança exceção se a API falhar — o chamador trata o fallback.
    """
    por_pagina = 50
    coletados = []
    pagina = 1
    while len(coletados) < limite:
        payload = _payload_lista(pagina, por_pagina, dias)
        resp = sess.post(SRE_API_LISTA, json=payload, timeout=SRE_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"SRE /detalhado devolveu HTTP {resp.status_code}")
        data = resp.json()
        registros = data.get("registros", [])
        if not registros:
            break
        coletados.extend(registros)
        total_paginas = data.get("totalPaginas", pagina)
        if pagina >= total_paginas:
            break
        pagina += 1
        time.sleep(0.5)   # educado com o servidor
    return coletados[:limite]


def sre_detalhe_devedor(sess: requests.Session, id_req: str) -> str:
    """
    Busca o detalhe da oferta (GET /infOferta/{id}) e extrai o campo do
    devedor. Devolve '' se não encontrar ou se a chamada falhar (o devedor
    é um 'extra' — sua ausência não impede o alerta).
    """
    if not id_req:
        return ""
    try:
        url = SRE_API_DETALHE.format(id=urllib.parse.quote(str(id_req)))
        resp = sess.get(url, timeout=60)
        if resp.status_code != 200:
            log.warning("infOferta/%s devolveu HTTP %s", id_req, resp.status_code)
            return ""
        itens = resp.json()
        if not isinstance(itens, list):
            return ""
        for item in itens:
            nome = normalize(item.get("campoNome", ""))
            if CAMPO_DEVEDOR_CONTEM in nome:
                return str(item.get("valor", "") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao buscar devedor da oferta %s: %s", id_req, exc)
    return ""


def sre_coordenadores(sess: requests.Session, id_req: str, lider: str) -> list:
    """
    Busca os participantes da oferta (GET /participantes/{id}) e devolve a
    lista de COORDENADORES, excluindo o coordenador líder (que já é exibido
    em separado) e sem repetições.

    Devolve [] se não encontrar ou se a chamada falhar — os coordenadores
    são um 'extra'; sua ausência não impede o alerta.
    """
    if not id_req:
        return []
    try:
        url = SRE_API_PARTICIPANTES.format(id=urllib.parse.quote(str(id_req)))
        resp = sess.get(url, timeout=60)
        if resp.status_code != 200:
            log.warning("participantes/%s devolveu HTTP %s", id_req, resp.status_code)
            return []
        itens = resp.json()
        if not isinstance(itens, list):
            return []
        lider_norm = normalize(lider)
        nomes = []
        vistos = set()
        for item in itens:
            # só interessa quem tem papel de coordenador
            if "coordenador" not in normalize(item.get("tipo", "")):
                continue
            nome = str(item.get("razaoSocial", "") or "").strip()
            if not nome:
                continue
            nome_norm = normalize(nome)
            if nome_norm == lider_norm:      # o líder já aparece em separado
                continue
            if nome_norm in vistos:          # evita repetição
                continue
            vistos.add(nome_norm)
            nomes.append(nome)
        return nomes
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao buscar coordenadores da oferta %s: %s", id_req, exc)
    return []


def sre_campos_requerimento(sess: requests.Session, id_req: str) -> dict:
    """
    Busca os campos detalhados da oferta (GET /requerimento/{id}) que ficam
    dentro de grupos[].series[].loteInicial.camposCadastrados[].

    Devolve um dict {campoNome_normalizado: [valores únicos não-vazios]}.
    Quando há mais de uma série, valores diferentes entre as séries vêm
    todos na lista, na ordem em que apareceram (sem duplicar).

    Em caso de erro, devolve {} — esses campos são "extras", a ausência
    não impede o alerta.
    """
    if not id_req:
        return {}
    try:
        url = SRE_API_REQUERIMENTO.format(id=urllib.parse.quote(str(id_req)))
        resp = sess.get(url, timeout=60)
        if resp.status_code != 200:
            log.warning("requerimento/%s devolveu HTTP %s", id_req, resp.status_code)
            return {}
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao buscar requerimento da oferta %s: %s", id_req, exc)
        return {}

    if not isinstance(data, dict):
        return {}

    acumulado: dict[str, list] = {}
    for grupo in data.get("grupos", []) or []:
        for serie in grupo.get("series", []) or []:
            lote = serie.get("loteInicial") or {}
            for c in lote.get("camposCadastrados", []) or []:
                if not c.get("visivel", True):
                    continue
                nome = normalize(c.get("campoNome", ""))
                valor = str(c.get("campoValor", "") or "").strip()
                if not nome or not valor:
                    continue
                lst = acumulado.setdefault(nome, [])
                if valor not in lst:
                    lst.append(valor)
    return acumulado


def sre_registro_para_oferta(reg: dict) -> Oferta:
    """Converte um registro cru da API /detalhado no modelo Oferta."""
    o = Oferta()
    o.fonte = "SRE"
    o.id = str(reg.get("idRequerimento", "") or "").strip()
    o.processo = str(reg.get("numeroProcesso", "") or "").strip()
    o.protocolo = str(reg.get("numeroProtocolo", "") or "").strip()
    o.tipo = str(reg.get("nomeValorMobiliario", "") or "").strip()
    o.emissor = str(reg.get("nomeEmissor", "") or "").strip()
    o.lider = str(reg.get("nomeCoordenadorLider", "") or "").strip()
    o.status = str(reg.get("statusDaOferta", "") or "").strip()
    o.modalidade = str(reg.get("tipoDeOferta", "") or "").strip()
    o.rito = str(reg.get("nomeTipoRequerimento", "") or "").strip()
    o.valor = str(reg.get("valorTotalEmReais", "") or "").strip()
    o.data = parse_data(reg.get("data"))
    o.categorias = categorias_do_texto(o.tipo)
    o.fase = fase_do_status(o.status)
    return o


def coletar_do_sre():
    """
    Coleta as ofertas de interesse via API do SRE. Devolve (lista_de_Oferta,
    sessao). Lança exceção se a API principal estiver inacessível.
    """
    sess = _nova_sessao()
    brutos = sre_listar_ofertas(sess, QTD_OFERTAS_VERIFICAR)
    log.info("SRE: %d ofertas recebidas da API.", len(brutos))
    limite_data = datetime.now() - timedelta(days=JANELA_DIAS)
    ofertas = []
    vistos = set()
    for reg in brutos:
        o = sre_registro_para_oferta(reg)
        if not o.categorias:          # não é um tipo monitorado
            continue
        if o.fase is None:            # status terminal / sem status
            continue
        if o.data is not None and o.data < limite_data:
            continue
        ch = o.chave()
        if ch in vistos:              # dedup (séries, repetições)
            continue
        vistos.add(ch)
        ofertas.append(o)
    return ofertas, sess


def coletar_do_sre_resiliente():
    """
    Chama coletar_do_sre() com várias tentativas e backoff crescente.

    A API do SRE costuma recusar a conexão de forma intermitente quando
    acessada de fora do Brasil (runners do GitHub): a falha é por tentativa
    e independente, então repetir quase sempre resolve. Cada tentativa cria
    uma sessão nova (conexão nova). Lança a última exceção se todas falharem.
    """
    ultima_exc = None
    for tentativa in range(1, SRE_TENTATIVAS + 1):
        try:
            resultado = coletar_do_sre()
            if tentativa > 1:
                log.info("SRE respondeu na tentativa %d/%d.",
                         tentativa, SRE_TENTATIVAS)
            return resultado
        except Exception as exc:  # noqa: BLE001
            ultima_exc = exc
            log.warning("SRE tentativa %d/%d falhou: %s",
                        tentativa, SRE_TENTATIVAS, exc)
            if tentativa < SRE_TENTATIVAS:
                # backoff: 3s, 6s, 9s, ... (teto de 20s)
                time.sleep(min(3 * tentativa, 20))
    raise ultima_exc


# -------------------------------------------------------------------
# FONTE DE RESERVA — Portal de Dados Abertos (.zip / CSV)
# -------------------------------------------------------------------
def coletar_do_fallback():
    """
    Coleta as ofertas do Portal de Dados Abertos (fonte estável, porém
    defasada). Usada só quando a API do SRE falha. Devolve lista de Oferta.
    """
    import csv
    log.warning("Usando a FONTE DE RESERVA (Portal de Dados Abertos).")
    resp = requests.get(CVM_ZIP_URL, timeout=120,
                         headers={"User-Agent": "cvm-robo/2.0"})
    resp.raise_for_status()
    limite_data = datetime.now() - timedelta(days=JANELA_DIAS)
    ofertas = []
    vistos = set()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for nome in zf.namelist():
            if not nome.lower().endswith(".csv"):
                continue
            with zf.open(nome) as fh:
                texto = io.TextIOWrapper(fh, encoding="latin-1", newline="")
                leitor = csv.DictReader(texto, delimiter=";")
                for row in leitor:
                    # detecção tolerante de colunas
                    def pega(*nomes):
                        for n in nomes:
                            for k, v in row.items():
                                if normalize(k).replace(" ", "") == normalize(n).replace(" ", ""):
                                    return v
                        return ""
                    tipo = pega("Tipo_Ativo", "Valor_Mobiliario")
                    cats = categorias_do_texto(tipo)
                    if not cats:
                        continue
                    o = Oferta()
                    o.fonte = "Dados Abertos (reserva)"
                    o.tipo = str(tipo or "").strip()
                    o.categorias = cats
                    o.emissor = str(pega("Nome_Emissor") or "").strip()
                    o.lider = str(pega("Nome_Lider") or "").strip()
                    o.processo = str(pega("Numero_Processo") or "").strip()
                    o.status = str(pega("Status_Requerimento") or "").strip()
                    o.valor = str(pega("Valor_Total_Registrado", "Valor_Total") or "").strip()
                    o.modalidade = str(pega("Tipo_Oferta", "Modalidade_Oferta") or "").strip()
                    o.rito = str(pega("Rito_Requerimento", "Rito_Oferta") or "").strip()
                    data_reg = pega("Data_Registro", "Data_Registro_Oferta")
                    data_ent = pega("Data_requerimento", "Data_Protocolo")
                    o.data = parse_data(data_reg) or parse_data(data_ent)
                    if parse_data(data_reg):
                        o.fase = FASE_REGISTRADA
                    else:
                        o.fase = fase_do_status(o.status) or FASE_ANALISE
                    if o.data is not None and o.data < limite_data:
                        continue
                    ch = o.chave()
                    if ch in vistos:
                        continue
                    vistos.add(ch)
                    ofertas.append(o)
    log.info("Fonte de reserva: %d ofertas de interesse na janela.", len(ofertas))
    return ofertas


# -------------------------------------------------------------------
# Estado
# -------------------------------------------------------------------
def carregar_estado() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                dados = json.load(fh)
            if "ofertas" not in dados:
                antigos = dados.get("ids_notificados", [])
                dados["ofertas"] = {oid: FASE_REGISTRADA for oid in antigos}
            dados.setdefault("ofertas", {})
            return dados
        except Exception as exc:  # noqa: BLE001
            log.error("Estado corrompido (%s). Começando do zero.", exc)
    return {"ofertas": {}, "primeira_execucao": True}


def salvar_estado(estado: dict) -> None:
    estado["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(estado, fh, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


# -------------------------------------------------------------------
# Telegram
# -------------------------------------------------------------------
def enviar_telegram(texto: str, chat_id: str = None) -> bool:
    destino = chat_id or TELEGRAM_CHAT_ID
    if (not TELEGRAM_BOT_TOKEN or "COLE_AQUI" in TELEGRAM_BOT_TOKEN
            or not destino or "COLE_AQUI" in destino):
        log.error("TELEGRAM_BOT_TOKEN / chat_id não configurados.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": destino,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, data=payload, timeout=30)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        log.error("Telegram recusou a mensagem: %s %s",
                  resp.status_code, resp.text[:300])
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("Erro ao falar com o Telegram: %s", exc)
        return False


def formatar_valor(bruto) -> str:
    s = str(bruto or "").strip()
    if not s or s.lower() in ("nan", "none", "null", "0", "0.0", "0,0", "0,0000"):
        return ""
    teste = s.replace("R$", "").strip()
    if "," in teste and "." in teste:
        teste = teste.replace(".", "").replace(",", ".")
    elif "," in teste:
        teste = teste.replace(",", ".")
    try:
        n = float(teste)
        if n <= 0:
            return ""
        return "R$ " + f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except ValueError:
        return s


def link_da_oferta(o: Oferta) -> str:
    """Link direto para a página da oferta no SRE (quando há id)."""
    if o.id:
        return SRE_LINK_OFERTA.format(id=urllib.parse.quote(str(o.id)))
    return SRE_BASE + "/#/consulta-oferta-publica"


def _campo(o: Oferta, *nomes_normalizados: str, limite: int = 350) -> str:
    """
    Tenta encontrar em o.campos um valor para qualquer um dos nomes
    normalizados informados (primeiro que existir vence). Junta valores
    de múltiplas séries com " | ", trunca textos muito longos.
    Devolve "" se nada existir.
    """
    for nome in nomes_normalizados:
        valores = o.campos.get(nome)
        if valores:
            txt = " | ".join(valores)
            if len(txt) > limite:
                txt = txt[:limite].rstrip() + "…"
            return txt
    return ""


def formatar_mensagem(o: Oferta) -> str:
    """
    Monta o texto (HTML) do alerta de uma oferta, em 3 blocos com hierarquia:

      1. Identificação: cabeçalho de status, emissor em destaque, tipo·valor,
         devedor (em CRA/CRI).
      2. Números-chave: datas (emissão→vencimento), avaliação de risco (em
         análise), remuneração (máxima e final), amortização.
      3. Contexto distributivo (em itálico, sem emoji em cada linha):
         coordenador líder, demais coordenadores, rito, modalidade·status.

    O objetivo é facilitar o scaning rápido — o que importa fica em cima,
    o contexto mais "administrativo" cai pro fim em itálico.
    """
    # --- Cabeçalho ---
    if o.fase == FASE_REGISTRADA:
        cabecalho = "✅ <b>Oferta registrada na CVM</b>"
    else:
        cabecalho = "📥 <b>Novo pedido de oferta em análise na CVM</b>"
    linhas = [cabecalho, ""]

    # --- Bloco 1: identificação ---
    if o.emissor:
        linhas.append(f"<b>{html_escape(o.emissor)}</b>")
    id_partes = []
    if o.tipo:
        id_partes.append(html_escape(o.tipo))
    valor_txt = formatar_valor(o.valor)
    if valor_txt:
        id_partes.append(valor_txt)
    if id_partes:
        linhas.append("  ·  ".join(id_partes))

    if o.eh_cri_cra() and o.devedor:
        dev = o.devedor
        if len(dev) > 500:
            dev = dev[:500].rstrip() + "…"
        linhas.append(f"🎯 <i>Devedor (risco):</i> {html_escape(dev)}")

    # --- Bloco 2: números-chave ---
    bloco_numeros: list[str] = []

    # Linha de datas: "📅 data  →  📆 vencimento" (junto se ambas existem)
    venc = _campo(o, "data de vencimento")
    if o.data and venc:
        bloco_numeros.append(
            f"📅 {o.data.strftime('%d/%m/%Y')}  →  📆 {html_escape(venc)}"
        )
    elif o.data:
        bloco_numeros.append(f"📅 {o.data.strftime('%d/%m/%Y')}")
    elif venc:
        bloco_numeros.append(f"📆 <i>Vencimento:</i> {html_escape(venc)}")

    # Avaliação de risco — só em análise
    risco = _campo(o, "avaliacao de risco", limite=300)
    if risco and o.fase == FASE_ANALISE:
        bloco_numeros.append(f"⚖️ <i>Avaliação de risco:</i> {html_escape(risco)}")

    # Remuneração: debêntures usam "máxima"; CRA/CRI usam genérica.
    # "Final (pós BB)" só faz sentido depois de registrada.
    remun_max = _campo(o, "informacoes sobre remuneracao maxima")
    remun = _campo(o, "informacoes sobre remuneracao") if not remun_max else ""
    if remun_max:
        bloco_numeros.append(
            f"💵 <i>Remuneração máxima:</i> {html_escape(remun_max)}"
        )
    elif remun:
        bloco_numeros.append(f"💵 <i>Remuneração:</i> {html_escape(remun)}")
    remun_final = _campo(o, "informacoes sobre remuneracao final (pos bookbuilding)")
    if remun_final and o.fase == FASE_REGISTRADA:
        bloco_numeros.append(
            f"💵 <i>Remuneração final (pós BB):</i> {html_escape(remun_final)}"
        )

    amort = _campo(o, "informacoes sobre amortizacao", limite=300)
    if amort:
        bloco_numeros.append(f"📉 <i>Amortização:</i> {html_escape(amort)}")

    if bloco_numeros:
        linhas.append("")
        linhas.extend(bloco_numeros)

    # --- Bloco 3: contexto distributivo (sem emoji) ---
    bloco_contexto: list[str] = []
    if o.lider:
        bloco_contexto.append(f"<i>Coordenador líder:</i> {html_escape(o.lider)}")
    if o.coordenadores:
        lista = ", ".join(o.coordenadores)
        if len(lista) > 400:
            lista = lista[:400].rstrip() + "…"
        bloco_contexto.append(
            f"<i>Demais coordenadores:</i> {html_escape(lista)}"
        )
    if o.rito:
        bloco_contexto.append(f"<i>Rito:</i> {html_escape(o.rito)}")
    mod_status = []
    if o.modalidade:
        mod_status.append(f"<i>Modalidade:</i> {html_escape(o.modalidade)}")
    if o.status:
        mod_status.append(f"<i>Status:</i> {html_escape(o.status)}")
    if mod_status:
        bloco_contexto.append("  ·  ".join(mod_status))

    if bloco_contexto:
        linhas.append("")
        linhas.extend(bloco_contexto)

    # --- Aviso defensivo de fonte reserva (não esperado em uso normal) ---
    if o.fonte != "SRE":
        linhas.append("")
        linhas.append(
            f"⚠️ <i>Dado da fonte de reserva ({html_escape(o.fonte)}) — "
            f"pode estar defasado.</i>"
        )

    # --- Footer com link ---
    linhas.append("")
    linhas.append(f'🔗 <a href="{link_da_oferta(o)}">Abrir no SRE</a>')

    return "\n".join(linhas)


# -------------------------------------------------------------------
# Núcleo: uma verificação
# -------------------------------------------------------------------
def verificar(dry_run: bool = False, notificar_primeira: bool = False) -> None:
    estado = carregar_estado()
    primeira = estado.get("primeira_execucao", False) or not STATE_FILE.exists()
    ofertas_estado = dict(estado.get("ofertas", {}))

    # Tenta SRE com várias tentativas (as conexões falham de forma
    # intermitente a partir dos runners do GitHub). Se TODAS falharem, vai
    # para modo reserva (sem chamar o fallback do Portal de Dados Abertos: a
    # comparação seria inviável por causa dos identificadores diferentes).
    try:
        ofertas, sess = coletar_do_sre_resiliente()
        usou_reserva = False
    except Exception as exc:  # noqa: BLE001
        log.error("Fonte principal (SRE) falhou após %d tentativas: %s",
                  SRE_TENTATIVAS, exc)
        ofertas, sess, usou_reserva = [], None, True

    n_analise = sum(1 for o in ofertas if o.fase == FASE_ANALISE)
    n_reg = sum(1 for o in ofertas if o.fase == FASE_REGISTRADA)
    if not usou_reserva:
        log.info("Ofertas de interesse na janela: %d (%d em análise, %d "
                 "registradas)", len(ofertas), n_analise, n_reg)

    # MODO RESERVA: a fonte de reserva (Portal de Dados Abertos) contém o
    # histórico inteiro e usa identificadores diferentes dos do SRE — então
    # comparar com o estado salvo gera milhares de falsos positivos. Para
    # evitar o flood, quando estamos no modo reserva NÃO disparamos alertas
    # individuais e NÃO alteramos o estado das ofertas. Quando a API do SRE
    # volta, as ofertas reais do período são capturadas na próxima verificação.
    #
    # Anti-ruído: uma falha ISOLADA do SRE é comum e transitória, então não
    # vale uma mensagem. Só avisamos depois de LIMIAR_AVISO_RESERVA
    # verificações CONSECUTIVAS falhando (= a CVM está realmente fora por
    # horas), e ainda assim no máximo uma vez a cada 12h.
    if usou_reserva:
        falhas = int(estado.get("falhas_consecutivas", 0) or 0) + 1
        estado["falhas_consecutivas"] = falhas
        agora = datetime.now()

        precisa_avisar = falhas >= LIMIAR_AVISO_RESERVA
        ultimo_aviso = estado.get("ultimo_aviso_reserva")
        if precisa_avisar and ultimo_aviso:
            try:
                ts = datetime.fromisoformat(ultimo_aviso)
                if (agora - ts) < timedelta(hours=12):
                    precisa_avisar = False
            except ValueError:
                pass

        if precisa_avisar:
            msg_reserva = (
                "⚠️ <b>Robô CVM em modo reserva</b>\n\n"
                "A fonte principal (API do SRE) não responde há algumas "
                "verificações seguidas. O robô está em modo degradado: "
                "<b>alertas individuais suspensos</b> até a fonte voltar.\n\n"
                "Assim que a API do SRE responder de novo, as ofertas reais "
                "do período são notificadas normalmente. Se este aviso "
                "persistir por horas, a API do SRE pode ter mudado e o robô "
                "precisa de revisão.")
            if dry_run:
                print("\n----- (DRY-RUN) [aviso modo reserva] -----")
                print(msg_reserva)
                print("-" * 40)
            elif enviar_telegram(msg_reserva, chat_id=TELEGRAM_ADMIN_CHAT_ID):
                estado["ultimo_aviso_reserva"] = agora.isoformat(timespec="seconds")

        if not dry_run:
            salvar_estado(estado)   # persiste o contador de falhas sempre
        log.info("Modo reserva ativo (%dª falha consecutiva); alertas "
                 "suspensos; estado preservado.%s", falhas,
                 "" if precisa_avisar else " (aviso suprimido)")
        return

    # SRE respondeu: zera o contador de falhas consecutivas.
    if estado.get("falhas_consecutivas"):
        log.info("SRE recuperado após %s falha(s) consecutiva(s).",
                 estado.get("falhas_consecutivas"))
    estado["falhas_consecutivas"] = 0

    # Primeira execução (só pela fonte principal): semeia o estado sem notificar.
    if primeira and not notificar_primeira:
        for o in ofertas:
            ofertas_estado[o.chave()] = o.fase
        estado["ofertas"] = ofertas_estado
        estado["primeira_execucao"] = False
        if not dry_run:
            salvar_estado(estado)
        log.info("Primeira execução: %d ofertas semeadas no estado SEM "
                 "notificar.", len(ofertas))
        return

    # Detecta novidades e mudanças de fase.
    eventos = []
    for o in ofertas:
        ch = o.chave()
        fase_anterior = ofertas_estado.get(ch)
        if fase_anterior == o.fase:
            continue
        if o.fase == FASE_ANALISE and fase_anterior == FASE_REGISTRADA:
            continue                      # não regride
        eventos.append(o)

    if not eventos:
        log.info("Nenhuma novidade desta vez.")
        if not dry_run:
            estado["ofertas"] = ofertas_estado
            estado["primeira_execucao"] = False
            salvar_estado(estado)
        return

    log.info("%d novidade(s) encontrada(s).", len(eventos))

    # Para as ofertas novas vindas do SRE, busca os detalhes extras:
    #  - coordenadores do consórcio (todas as ofertas);
    #  - campos detalhados (vencimento, remuneração, amortização, ...);
    #  - devedor (só CRA/CRI, onde faz sentido).
    if sess is not None:
        for o in eventos:
            if not o.id:
                continue
            o.coordenadores = sre_coordenadores(sess, o.id, o.lider)
            time.sleep(0.3)
            o.campos = sre_campos_requerimento(sess, o.id)
            time.sleep(0.3)
            if o.eh_cri_cra():
                o.devedor = sre_detalhe_devedor(sess, o.id)
                time.sleep(0.3)

    enviadas = 0
    for o in eventos:
        msg = formatar_mensagem(o)
        if dry_run:
            print(f"\n----- (DRY-RUN) [{o.fase}] {o.fonte} -----")
            print(msg)
            print("-" * 40)
            ofertas_estado[o.chave()] = o.fase
            enviadas += 1
            continue
        if enviar_telegram(msg):
            ofertas_estado[o.chave()] = o.fase
            enviadas += 1
            log.info("Alerta enviado [%s]: %s", o.fase, o.chave())
            time.sleep(1)
        else:
            log.warning("Falha ao enviar (será tentado de novo): %s", o.chave())

    estado["ofertas"] = ofertas_estado
    estado["primeira_execucao"] = False
    if not dry_run:
        salvar_estado(estado)
    log.info("Verificação concluída: %d/%d alertas enviados.", enviadas, len(eventos))


# -------------------------------------------------------------------
# Modos auxiliares
# -------------------------------------------------------------------
def modo_inspect() -> None:
    """Consulta a API do SRE e mostra o que o robô está enxergando."""
    print("=" * 66)
    print("INSPEÇÃO — fonte principal: API do SRE")
    print("=" * 66)
    try:
        sess = _nova_sessao()
        brutos = sre_listar_ofertas(sess, QTD_OFERTAS_VERIFICAR)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ A API do SRE falhou: {exc}")
        print("Tentando a fonte de reserva...")
        try:
            ofertas = coletar_do_fallback()
            print(f"Fonte de reserva OK: {len(ofertas)} ofertas de interesse.")
        except Exception as exc2:  # noqa: BLE001
            print(f"❌ Fonte de reserva também falhou: {exc2}")
        return

    print(f"API respondeu. {len(brutos)} ofertas recebidas.\n")
    if brutos:
        print("Campos disponíveis em cada registro da listagem:")
        for k in brutos[0].keys():
            print(f"   - {k}")

    # estatísticas
    limite_data = datetime.now() - timedelta(days=JANELA_DIAS)
    cont_total = Counter()
    cont_analise = Counter()
    cont_reg = Counter()
    status_vistos = Counter()
    exemplos = {}
    for reg in brutos:
        o = sre_registro_para_oferta(reg)
        status_vistos[o.status] += 1
        if not o.categorias:
            continue
        na_janela = o.data is not None and o.data >= limite_data
        for c in o.categorias:
            cont_total[c] += 1
            if o.fase == FASE_ANALISE and na_janela:
                cont_analise[c] += 1
                exemplos.setdefault((c, FASE_ANALISE), o)
            elif o.fase == FASE_REGISTRADA and na_janela:
                cont_reg[c] += 1
                exemplos.setdefault((c, FASE_REGISTRADA), o)

    print(f"\nValores de status encontrados:")
    for st, qtd in status_vistos.most_common(20):
        print(f"   {qtd:>5}  {st!r}")

    print(f"\nOfertas por tipo monitorado "
          f"(total | em análise na janela | registradas na janela):")
    for c in PADROES_TIPO:
        print(f"   {c}")
        print(f"      total: {cont_total[c]:>5}  |  análise: {cont_analise[c]:>4}"
              f"  |  registradas: {cont_reg[c]:>4}")

    # ---- Censo de tipos ------------------------------------------------
    # A contagem acima só enxerga o que JÁ está em PADROES_TIPO — ela é cega
    # para tipos que existem na API e não são monitorados. Este censo lista
    # os valores CRUS de 'nomeValorMobiliario', que é o texto contra o qual
    # os padrões casam. É a partir daqui que se escreve o regex de um tipo
    # novo, sem chutar a grafia.
    #
    # Varre uma janela bem mais larga que a verificação normal: tipo raro
    # (poucas emissões por ano) simplesmente não aparece nos últimos meses.
    print("\n" + "=" * 66)
    print("CENSO DE TIPOS — valores crus de 'nomeValorMobiliario'")
    print(f"(varredura ampla: até {INSPECT_QTD} ofertas nos últimos "
          f"{INSPECT_DIAS} dias)")
    print("=" * 66)
    try:
        amplos = sre_listar_ofertas(sess, INSPECT_QTD, dias=INSPECT_DIAS)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  Varredura ampla falhou ({exc}); usando a amostra normal.")
        amplos = brutos

    tipos = Counter()
    for reg in amplos:
        tipos[str(reg.get("nomeValorMobiliario", "") or "").strip()] += 1
    print(f"{len(amplos)} ofertas varridas, {len(tipos)} tipos distintos.\n")
    print(f"   {'qtd':>5}  {'monitorado':^11}  tipo")
    for tipo, qtd in tipos.most_common():
        cats = categorias_do_texto(tipo)
        marca = "sim" if cats else ">> NAO <<"
        print(f"   {qtd:>5}  {marca:^11}  {tipo!r}")

    # exemplo de mensagem (busca detalhes extras de um exemplo de cada tipo)
    print("\nExemplos de mensagem:")
    algum = False
    for c in PADROES_TIPO:
        for fase in (FASE_ANALISE, FASE_REGISTRADA):
            o = exemplos.get((c, fase))
            if o is None:
                continue
            algum = True
            if o.id:
                o.coordenadores = sre_coordenadores(sess, o.id, o.lider)
                if o.eh_cri_cra():
                    o.devedor = sre_detalhe_devedor(sess, o.id)
            print("\n" + "- " * 27)
            print(formatar_mensagem(o))
    if not algum:
        print("   (nenhuma oferta na janela agora)")


def modo_seed_silent() -> None:
    """
    Coleta as ofertas atuais via SRE e adiciona ao estado as que ainda não
    estão lá — SEM disparar notificações. Use ao introduzir uma nova
    categoria em PADROES_TIPO: marca as ofertas já existentes na janela
    como "já vistas", para não receber uma rajada histórica.
    """
    estado = carregar_estado()
    ofertas_estado = dict(estado.get("ofertas", {}))
    try:
        ofertas, _ = coletar_do_sre()
    except Exception as exc:  # noqa: BLE001
        log.error("SRE falhou: %s — abortando seed.", exc)
        return
    adicionadas = 0
    for o in ofertas:
        ch = o.chave()
        if ch not in ofertas_estado:
            ofertas_estado[ch] = o.fase
            adicionadas += 1
    estado["ofertas"] = ofertas_estado
    estado["primeira_execucao"] = False
    salvar_estado(estado)
    log.info("Seed silencioso: %d nova(s) oferta(s) marcada(s) como "
             "já-vista(s) (de %d coletadas). Nenhum alerta enviado.",
             adicionadas, len(ofertas))


def modo_test_telegram() -> None:
    msg = ("✅ <b>Robô CVM → Telegram</b>\n\n"
           "Teste de conexão bem-sucedido. Você receberá um alerta aqui "
           "quando um pedido de oferta de CRA, CRI, debênture ou nota "
           "comercial entrar em análise — e outro quando for registrada.")
    if enviar_telegram(msg):
        log.info("Mensagem de teste enviada com sucesso (canal). ✅")
    else:
        log.error("Falha ao enviar a mensagem de teste no canal. Confira os Secrets.")

    if TELEGRAM_ADMIN_CHAT_ID and TELEGRAM_ADMIN_CHAT_ID != TELEGRAM_CHAT_ID:
        msg_admin = ("✅ <b>Robô CVM → canal admin</b>\n\n"
                     "Teste de conexão com o chat administrativo bem-sucedido. "
                     "Avisos de \"modo reserva\" chegam aqui, não no canal "
                     "principal.")
        if enviar_telegram(msg_admin, chat_id=TELEGRAM_ADMIN_CHAT_ID):
            log.info("Mensagem de teste enviada com sucesso (admin). ✅")
        else:
            log.error("Falha ao enviar a mensagem de teste no admin. "
                       "Confira TELEGRAM_ADMIN_CHAT_ID e se você já mandou "
                       "/start para o bot na DM.")


# -------------------------------------------------------------------
# main
# -------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Robô que avisa no Telegram sobre ofertas (CRA, CRI, "
                    "debêntures, notas comerciais) na CVM, via API do SRE.")
    parser.add_argument("--loop", action="store_true",
                        help=f"roda continuamente, a cada {POLL_INTERVAL_SECONDS}s")
    parser.add_argument("--dry-run", action="store_true",
                        help="verifica e imprime as mensagens em vez de enviar")
    parser.add_argument("--inspect", action="store_true",
                        help="consulta a API e mostra o que o robô enxerga")
    parser.add_argument("--test-telegram", action="store_true",
                        help="envia uma mensagem de teste para o Telegram")
    parser.add_argument("--seed-silent", action="store_true",
                        help="marca as ofertas atuais como já-vistas SEM "
                             "notificar (útil ao introduzir nova categoria)")
    parser.add_argument("--notify-on-first-run", action="store_true",
                        help="na primeira execução, notifica tudo da janela")
    args = parser.parse_args()

    if args.inspect:
        modo_inspect()
        return
    if args.test_telegram:
        modo_test_telegram()
        return
    if args.seed_silent:
        modo_seed_silent()
        return

    if args.loop:
        log.info("Modo loop iniciado (intervalo: %ds).", POLL_INTERVAL_SECONDS)
        while True:
            try:
                verificar(dry_run=args.dry_run,
                          notificar_primeira=args.notify_on_first_run)
            except Exception as exc:  # noqa: BLE001
                log.exception("Erro inesperado na verificação: %s", exc)
            try:
                time.sleep(POLL_INTERVAL_SECONDS)
            except KeyboardInterrupt:
                log.info("Encerrado pelo usuário.")
                break
    else:
        verificar(dry_run=args.dry_run,
                  notificar_primeira=args.notify_on_first_run)


if __name__ == "__main__":
    main()
