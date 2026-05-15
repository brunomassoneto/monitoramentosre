#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robô CVM → Telegram
===================

Monitora ofertas públicas de distribuição na CVM e avisa no Telegram quando:
  - um NOVO PEDIDO de oferta entra em análise; e
  - uma oferta é REGISTRADA (ou tem o registro dispensado).

Ou seja, a mesma oferta pode gerar dois alertas ao longo do seu ciclo:
"📥 em análise"  ->  "✅ registrada".

Tipos monitorados:
    - CRA  (Certificado de Recebíveis do Agronegócio)
    - CRI  (Certificado de Recebíveis Imobiliários)
    - Debêntures
    - Notas Comerciais (e Notas Promissórias Comerciais)

Fonte dos dados
---------------
Portal de Dados Abertos da CVM (mantido pela própria SRE):

    https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip

O .zip contém dois arquivos, e o robô lê os dois:
  - oferta_distribuicao.csv  -> ofertas ICVM 400, RCVM 160 rito ordinário,
                                ICVM 555 e ICVM 476 encerradas (histórico longo).
  - oferta_resolucao_160.csv -> ofertas em rito automático (Resolução CVM 160);
                                este arquivo traz também os pedidos AINDA EM
                                ANÁLISE (coluna de status), por isso é a
                                principal fonte dos alertas "em análise".

É atualizado pela CVM diariamente (às vezes mais de uma vez por dia). É a fonte
pública, oficial e estável equivalente à tela de "Consulta de Oferta Pública"
do sistema SRE. O custo é uma latência de algumas horas até ~1 dia.

Como usar
---------
    python cvm_telegram_bot.py --inspect        # baixa e analisa a estrutura do CSV
    python cvm_telegram_bot.py --test-telegram  # envia uma mensagem de teste
    python cvm_telegram_bot.py                  # uma verificação (ideal para cron/Actions)
    python cvm_telegram_bot.py --loop           # roda continuamente
    python cvm_telegram_bot.py --dry-run        # verifica mas imprime em vez de enviar

Veja o README.md / RODAR_NO_GITHUB_ACTIONS.md para a configuração.
"""

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
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

# --- O que monitorar --------------------------------------------------
# Padrões (regex) que casam SEM acento e SEM diferenciar maiúsculas. Foram
# feitos para casar singular E plural ("debênture"/"debêntures",
# "nota comercial"/"notas comerciais"). Para monitorar só um tipo, deixe
# apenas a entrada desejada.
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
}

# Avisar também quando um pedido ENTRA EM ANÁLISE (além do alerta de
# registro)? True = dois alertas por oferta ao longo do ciclo.
ALERTAR_EM_ANALISE = True

# Palavras que, no status do pedido, indicam que ele NÃO está mais ativo
# (foi negado/cancelado/expirou/etc.) — esses pedidos não geram nem
# acompanham alerta. Lista calibrada a partir dos valores reais de
# 'Status_Requerimento' vistos no --inspect.
STATUS_TERMINAIS = [
    "cancel", "indefer", "desist", "arquivad", "revog",
    "negad", "extint", "caduc", "interromp", "expir",
]

# --- Comportamento ----------------------------------------------------
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "3600"))

# Só considera "nova" uma oferta cuja data de referência (registro, dispensa
# ou entrada do pedido) esteja dentro desta janela. Evita re-notificar
# histórico antigo (o arquivo da CVM tem ofertas desde 1990).
JANELA_DIAS = int(os.environ.get("JANELA_DIAS", "45"))

# --- Fonte de dados / arquivos locais ---------------------------------
CVM_ZIP_URL = "https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip"

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "estado_ofertas.json"
LOG_FILE = BASE_DIR / "cvm_robo.log"

LINK_CONSULTA = "https://web.cvm.gov.br/sre-publico-cvm/#/consulta-oferta-publica"

# ===================================================================
# Fim da configuração — daqui pra baixo é o motor do robô.
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
    categoria: [re.compile(p) for p in padroes]
    for categoria, padroes in PADROES_TIPO.items()
}

FASE_ANALISE = "analise"
FASE_REGISTRADA = "registrada"


# -------------------------------------------------------------------
# Utilitários de texto
# -------------------------------------------------------------------
def strip_accents(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(texto: str) -> str:
    if texto is None:
        return ""
    t = strip_accents(str(texto)).lower()
    return re.sub(r"\s+", " ", t).strip()


def norm_header(nome: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(str(nome)).lower())


def html_escape(texto: str) -> str:
    return (str(texto).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


# -------------------------------------------------------------------
# Detecção de colunas
# -------------------------------------------------------------------
# O layout do CSV da CVM pode variar com o tempo e os DOIS arquivos do zip
# têm layouts diferentes. O robô procura, no cabeçalho real de cada arquivo,
# a coluna mais provável para cada campo lógico.
COLUMN_CANDIDATES = {
    "tipo": [
        "tipoativo", "valormobiliario", "tipovalormobiliario",
        "especievalormobiliario", "especieativo", "especie",
        "valoresmobiliarios", "tipooferta",
    ],
    "emissor": [
        "nomeemissor", "emissor", "nomedoemissor", "denominacaoemissor",
        "razaosocialemissor", "nomeofertante", "ofertante",
    ],
    # identificador único (dedup): o número de processo é o mais estável
    "numero": [
        "numeroprocesso", "numprocesso", "processosei", "processo",
        "numeroregistrooferta", "numeroregistro", "numerorequerimento",
        "protocolo", "numero",
    ],
    "numero_registro": [
        "numeroregistrooferta", "numeroregistro", "numerorequerimento",
    ],
    "data_registro": [
        "dataregistrooferta", "dataregistro", "datadoregistro",
        "dataconcessaoregistro", "dataconcessao", "datadeconcessao",
    ],
    "data_dispensa": [
        "datadispensaoferta", "datadispensa", "datadispensaregistro",
    ],
    # data em que o pedido entrou na CVM (usada para a fase "em análise")
    "data_entrada": [
        "datarequerimento", "dataprotocolo", "dataaberturaprocesso",
        "dataabertura",
    ],
    "valor": [
        "valortotalregistrado", "valortotaloferta", "valortotal",
        "valoroferta", "valordistribuicao", "montante",
    ],
    "rito": ["ritorequerimento", "ritooferta", "rito"],
    "modalidade": ["modalidadeoferta", "modalidaderegistro", "modalidade",
                   "tipooferta"],
    "situacao": [
        "statusrequerimento", "situacaooferta", "situacaoregistro",
        "situacao", "status",
    ],
    "lider": ["nomelider", "lider", "coordenadorlider"],
    # categoria/grupo do coordenador líder (ex.: "COORDENADOR PLENO").
    # OBS.: o conjunto de dados público da CVM NÃO traz a lista completa dos
    # demais coordenadores/consórcio — só o líder e a categoria dele.
    "grupo_coordenador": ["grupocoordenador"],
    # Em CRA/CRI o emissor é a securitizadora; o RISCO da operação é do
    # devedor lastro. Esta coluna costuma trazer essa informação.
    "devedor": [
        "identificacaodevedorescoobrigados", "identificacaodevedores",
        "devedorescoobrigados", "devedores", "devedor",
    ],
    "publico_alvo": ["publicoalvo", "publico"],
}

# Para o --inspect: descobre colunas relacionadas a coordenadores.
PALAVRAS_COORDENADOR = ["coordenador", "lider", "vendedor", "consorcio",
                        "distribuidor", "intermediar", "underwriter"]


def detect_columns(header: list) -> dict:
    """Mapa {campo_logico: nome_real_da_coluna} com a melhor correspondência."""
    norm_map = {norm_header(h): h for h in header}
    resultado = {}
    for campo, candidatos in COLUMN_CANDIDATES.items():
        achou = None
        for cand in candidatos:                       # 1) match exato
            if cand in norm_map:
                achou = norm_map[cand]
                break
        if achou is None:                             # 2) match por "contém"
            for cand in candidatos:
                for nh, original in norm_map.items():
                    if cand in nh:
                        achou = original
                        break
                if achou:
                    break
        if achou:
            resultado[campo] = achou
    return resultado


# -------------------------------------------------------------------
# Casamento de tipo de oferta
# -------------------------------------------------------------------
def categorias_da_linha(row: dict, cols: dict) -> list:
    """Categorias monitoradas que casam com a linha. Lista vazia = não interessa.

    Quando a coluna de TIPO foi detectada (caso normal nos dois arquivos da
    CVM), o casamento usa SÓ ela — assim evita falso positivo, como um
    "Cotas de FII" cujo emissor se chama "...RECEBÍVEIS IMOBILIÁRIOS...".
    Só cai na varredura da linha inteira se a coluna de tipo não for achada.
    """
    if "tipo" in cols:
        textos = [normalize(row.get(cols["tipo"], ""))]
    else:
        textos = [normalize(" ".join(str(v) for v in row.values()))]
    encontradas = []
    for categoria, padroes in _PADROES_COMPILADOS.items():
        for txt in textos:
            if txt and any(p.search(txt) for p in padroes):
                encontradas.append(categoria)
                break
    return encontradas


def linha_interessa(row: dict, cols: dict) -> bool:
    return bool(categorias_da_linha(row, cols))


def tipo_detectado(row: dict, cols: dict) -> str:
    if "tipo" in cols and row.get(cols["tipo"]):
        return str(row[cols["tipo"]]).strip()
    cats = categorias_da_linha(row, cols)
    return " / ".join(cats) if cats else "(tipo não identificado)"


# -------------------------------------------------------------------
# Identificador único da oferta
# -------------------------------------------------------------------
def offer_id(row: dict, cols: dict) -> str:
    """
    ID estável da oferta. Usa o número de processo (estável e comum aos dois
    arquivos -> a mesma oferta não é notificada duas vezes pela mesma fase).
    O ID NÃO inclui o nome do arquivo de propósito.
    """
    if "numero" in cols and row.get(cols["numero"]):
        return f"proc:{str(row[cols['numero']]).strip()}"
    bruto = "|".join(f"{k}={v}" for k, v in sorted(row.items()))
    h = hashlib.sha1(bruto.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"hash:{h}"


# -------------------------------------------------------------------
# Datas
# -------------------------------------------------------------------
def parse_data(valor: str):
    if not valor:
        return None
    s = str(valor).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    s_data = s.split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s_data, fmt)
        except ValueError:
            continue
    return None


def data_de_registro(row: dict, cols: dict):
    """Data de registro (ou de dispensa de registro). None se não houver."""
    for campo in ("data_registro", "data_dispensa"):
        if campo in cols:
            d = parse_data(row.get(cols[campo], ""))
            if d:
                return d
    return None


def data_de_entrada(row: dict, cols: dict):
    """Data em que o pedido entrou na CVM. None se não houver."""
    if "data_entrada" in cols:
        return parse_data(row.get(cols["data_entrada"], ""))
    return None


# -------------------------------------------------------------------
# Fase da oferta (em análise / registrada / ignorar)
# -------------------------------------------------------------------
def status_e_terminal(row: dict, cols: dict) -> bool:
    """True se o status indica pedido encerrado sem registro (negado/cancelado…)."""
    st = normalize(_campo(row, cols, "situacao"))
    return any(palavra in st for palavra in STATUS_TERMINAIS)


def fase_da_oferta(row: dict, cols: dict):
    """
    Devolve a fase atual da oferta:
      - FASE_REGISTRADA : tem data de registro/dispensa
      - FASE_ANALISE    : pedido ativo, ainda sem registro
      - None            : ignorar (pedido encerrado sem registro, ou
                          linha sem informação suficiente)
    """
    if data_de_registro(row, cols) is not None:
        return FASE_REGISTRADA
    if not ALERTAR_EM_ANALISE:
        return None
    if status_e_terminal(row, cols):
        return None
    # sem registro e não-terminal: considera "em análise" se há algum sinal
    # de pedido ativo (um status preenchido ou uma data de entrada).
    tem_status = bool(_campo(row, cols, "situacao"))
    if tem_status or data_de_entrada(row, cols) is not None:
        return FASE_ANALISE
    return None


def data_de_referencia(row: dict, cols: dict, fase: str):
    """Data usada para o filtro de janela, conforme a fase."""
    if fase == FASE_REGISTRADA:
        return data_de_registro(row, cols)
    return data_de_entrada(row, cols)


# -------------------------------------------------------------------
# Download e leitura do ZIP da CVM
# -------------------------------------------------------------------
def baixar_zip(url: str) -> bytes:
    log.info("Baixando dados da CVM: %s", url)
    resp = requests.get(url, timeout=120, headers={"User-Agent": "cvm-robo/1.0"})
    resp.raise_for_status()
    log.info("Download OK (%.1f KB).", len(resp.content) / 1024)
    return resp.content


def ler_csvs_do_zip(zip_bytes: bytes):
    """Gera (nome_arquivo, header, linhas) para cada .csv do zip (sep ';', latin-1)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        nomes_csv = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not nomes_csv:
            log.warning("Nenhum .csv encontrado dentro do zip.")
        for nome in nomes_csv:
            try:
                with zf.open(nome) as fh:
                    texto = io.TextIOWrapper(fh, encoding="latin-1", newline="")
                    leitor = csv.DictReader(texto, delimiter=";")
                    header = leitor.fieldnames or []
                    linhas = [dict(r) for r in leitor]
                yield nome, header, linhas
            except Exception as exc:  # noqa: BLE001
                log.error("Falha ao ler '%s' do zip: %s", nome, exc)


# -------------------------------------------------------------------
# Estado (fase já notificada de cada oferta)
# -------------------------------------------------------------------
def carregar_estado() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                dados = json.load(fh)
            # migração do formato antigo {"ids_notificados": [...]}
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
# Telegram / formatação
# -------------------------------------------------------------------
def enviar_telegram(texto: str) -> bool:
    if (not TELEGRAM_BOT_TOKEN or "COLE_AQUI" in TELEGRAM_BOT_TOKEN
            or not TELEGRAM_CHAT_ID or "COLE_AQUI" in TELEGRAM_CHAT_ID):
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID não configurados.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, data=payload, timeout=30)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        log.error("Telegram recusou a mensagem: %s %s", resp.status_code, resp.text[:300])
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("Erro ao falar com o Telegram: %s", exc)
        return False


def _campo(row: dict, cols: dict, chave: str) -> str:
    if chave not in cols:
        return ""
    return str(row.get(cols[chave], "") or "").strip()


def formatar_valor(bruto: str) -> str:
    s = str(bruto).strip()
    if not s or s.lower() in ("nan", "none", "null", "0", "0.0", "0,0"):
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


def formatar_mensagem(row: dict, cols: dict, fase: str) -> str:
    """Monta o texto (HTML) do alerta, conforme a fase da oferta."""
    tipo = html_escape(tipo_detectado(row, cols))
    emissor = html_escape(_campo(row, cols, "emissor") or "(não informado)")
    lider = html_escape(_campo(row, cols, "lider"))
    grupo_coord = html_escape(_campo(row, cols, "grupo_coordenador"))
    devedor = html_escape(_campo(row, cols, "devedor"))
    processo = html_escape(_campo(row, cols, "numero"))
    nro_registro = html_escape(_campo(row, cols, "numero_registro"))
    rito = html_escape(_campo(row, cols, "rito"))
    modalidade = html_escape(_campo(row, cols, "modalidade"))
    situacao = html_escape(_campo(row, cols, "situacao"))
    publico = html_escape(_campo(row, cols, "publico_alvo"))
    valor_txt = formatar_valor(_campo(row, cols, "valor"))

    if fase == FASE_REGISTRADA:
        cabecalho = "✅ <b>Oferta registrada na CVM</b>"
        d = data_de_referencia(row, cols, fase)
        rotulo_data = "Data de registro"
    else:
        cabecalho = "📥 <b>Novo pedido de oferta em análise na CVM</b>"
        d = data_de_referencia(row, cols, fase)
        rotulo_data = "Data de entrada"
    data_txt = d.strftime("%d/%m/%Y") if d else "—"

    linhas = [
        cabecalho,
        "",
        f"📄 <b>Tipo:</b> {tipo}",
        f"🏢 <b>Emissor:</b> {emissor}",
    ]
    # Devedor (risco da operação) só faz sentido em CRA/CRI — em debênture
    # e nota comercial o próprio emissor já é o devedor.
    categorias = categorias_da_linha(row, cols)
    eh_cri_cra = any("CRA" in c or "CRI" in c for c in categorias)
    if eh_cri_cra and devedor:
        linhas.append(f"🎯 <b>Devedor (risco):</b> {devedor}")
    if lider:
        linhas.append(f"🏦 <b>Coordenador líder:</b> {lider}")
    if grupo_coord:
        linhas.append(f"🏷️ <b>Categoria do coordenador:</b> {grupo_coord}")
    linhas.append(f"📅 <b>{rotulo_data}:</b> {data_txt}")
    if valor_txt:
        linhas.append(f"💰 <b>Valor:</b> {valor_txt}")
    if rito:
        linhas.append(f"📊 <b>Rito:</b> {rito}")
    if modalidade:
        linhas.append(f"📋 <b>Modalidade:</b> {modalidade}")
    if publico:
        linhas.append(f"👥 <b>Público-alvo:</b> {publico}")
    if situacao:
        linhas.append(f"📌 <b>Situação:</b> {situacao}")
    if nro_registro:
        linhas.append(f"🔢 <b>Nº registro/requerimento:</b> {nro_registro}")
    if processo:
        linhas.append(f"🗂️ <b>Processo:</b> {processo}")
    linhas += ["", f'🔗 <a href="{LINK_CONSULTA}">Consultar no sistema SRE</a>']
    return "\n".join(linhas)


# -------------------------------------------------------------------
# Núcleo: uma verificação
# -------------------------------------------------------------------
def coletar_candidatas(zip_bytes: bytes):
    """
    Lê os CSVs e devolve um dict {offer_id: (row, cols, fase)} com as ofertas
    de interesse, na janela, deduplicadas (mesma oferta = 1 entrada; se houver
    linhas em fases diferentes, prevalece 'registrada').
    """
    limite_data = datetime.now() - timedelta(days=JANELA_DIAS)
    melhor = {}
    total_linhas = 0

    for nome_arquivo, header, linhas in ler_csvs_do_zip(zip_bytes):
        cols = detect_columns(header)
        chaves = {k: cols[k] for k in ("tipo", "numero", "data_registro",
                                       "data_entrada", "situacao") if k in cols}
        log.info("Arquivo '%s': %d linhas. Colunas-chave: %s",
                 nome_arquivo, len(linhas), chaves)
        total_linhas += len(linhas)
        for row in linhas:
            if not linha_interessa(row, cols):
                continue
            fase = fase_da_oferta(row, cols)
            if fase is None:
                continue
            dref = data_de_referencia(row, cols, fase)
            if dref is not None:
                if dref < limite_data:
                    continue
            else:
                # registrada sempre tem data; só 'analise' pode cair aqui.
                # se o arquivo TEM coluna de entrada mas veio vazia, ignora.
                if "data_entrada" in cols:
                    continue
            oid = offer_id(row, cols)
            atual = melhor.get(oid)
            if atual is None:
                melhor[oid] = (row, cols, fase)
            elif atual[2] == FASE_ANALISE and fase == FASE_REGISTRADA:
                melhor[oid] = (row, cols, fase)   # registrada prevalece
    return melhor, total_linhas


def verificar(dry_run: bool = False, notificar_primeira: bool = False) -> None:
    estado = carregar_estado()
    primeira = estado.get("primeira_execucao", False) or not STATE_FILE.exists()
    ofertas_estado = dict(estado.get("ofertas", {}))   # oid -> fase já notificada

    try:
        zip_bytes = baixar_zip(CVM_ZIP_URL)
    except Exception as exc:  # noqa: BLE001
        log.error("Não foi possível baixar os dados da CVM: %s", exc)
        return

    candidatas, total_linhas = coletar_candidatas(zip_bytes)
    n_analise = sum(1 for _, _, f in candidatas.values() if f == FASE_ANALISE)
    n_reg = sum(1 for _, _, f in candidatas.values() if f == FASE_REGISTRADA)
    log.info("Linhas lidas: %d | ofertas de interesse na janela: %d "
             "(%d em análise, %d registradas)",
             total_linhas, len(candidatas), n_analise, n_reg)

    # Primeira execução: apenas semeia o estado, sem disparar notificações.
    if primeira and not notificar_primeira:
        for oid, (_, _, fase) in candidatas.items():
            ofertas_estado[oid] = fase
        estado["ofertas"] = ofertas_estado
        estado["primeira_execucao"] = False
        if not dry_run:
            salvar_estado(estado)
        log.info("Primeira execução: %d ofertas registradas no estado SEM "
                 "notificar. A partir de agora só as novidades geram alerta.",
                 len(candidatas))
        return

    # Detecta novidades e mudanças de fase.
    eventos = []   # (row, cols, oid, fase)
    for oid, (row, cols, fase) in candidatas.items():
        fase_anterior = ofertas_estado.get(oid)
        if fase_anterior == fase:
            continue                                  # nada mudou
        if fase == FASE_ANALISE and fase_anterior == FASE_REGISTRADA:
            continue                                  # não volta atrás
        eventos.append((row, cols, oid, fase))

    if not eventos:
        log.info("Nenhuma novidade desta vez.")
        if not dry_run:
            estado["ofertas"] = ofertas_estado
            estado["primeira_execucao"] = False
            salvar_estado(estado)
        return

    log.info("%d novidade(s) encontrada(s).", len(eventos))
    enviadas = 0
    for row, cols, oid, fase in eventos:
        msg = formatar_mensagem(row, cols, fase)
        if dry_run:
            print(f"\n----- (DRY-RUN) [{fase}] -----")
            print(msg)
            print("------------------------------")
            ofertas_estado[oid] = fase
            enviadas += 1
            continue
        if enviar_telegram(msg):
            ofertas_estado[oid] = fase
            enviadas += 1
            log.info("Alerta enviado [%s]: %s", fase, oid)
            time.sleep(1)
        else:
            log.warning("Falha ao enviar (será tentado de novo): %s", oid)

    estado["ofertas"] = ofertas_estado
    estado["primeira_execucao"] = False
    if not dry_run:
        salvar_estado(estado)
    log.info("Verificação concluída: %d/%d alertas enviados.", enviadas, len(eventos))


# -------------------------------------------------------------------
# Modos auxiliares
# -------------------------------------------------------------------
def modo_inspect() -> None:
    """Baixa os dados e analisa a estrutura dos CSVs. Não usa o Telegram."""
    try:
        zip_bytes = baixar_zip(CVM_ZIP_URL)
    except Exception as exc:  # noqa: BLE001
        log.error("Falha no download: %s", exc)
        return
    limite_data = datetime.now() - timedelta(days=JANELA_DIAS)

    for nome_arquivo, header, linhas in ler_csvs_do_zip(zip_bytes):
        cols = detect_columns(header)
        print("\n" + "=" * 72)
        print(f"ARQUIVO: {nome_arquivo}   ({len(linhas)} linhas)")
        print("=" * 72)

        print("Mapa de colunas detectado pelo robô:")
        for campo in COLUMN_CANDIDATES:
            print(f"   {campo:<16} -> {cols.get(campo, '(não detectada)')}")

        # Descoberta de TODAS as colunas ligadas a coordenadores/distribuidores
        achadas = [h for h in header
                   if any(p in norm_header(h) for p in PALAVRAS_COORDENADOR)]
        print("\nColunas relacionadas a COORDENADORES encontradas no arquivo:")
        if achadas:
            for h in achadas:
                exemplo = ""
                for row in linhas:
                    v = str(row.get(h, "") or "").strip()
                    if v:
                        exemplo = v
                        break
                print(f"   - {h}   (ex.: {exemplo[:70]!r})")
        else:
            print("   (nenhuma)")

        # Valores únicos da coluna de STATUS / SITUAÇÃO
        if "situacao" in cols:
            valores = Counter(str(r.get(cols["situacao"], "") or "").strip()
                              for r in linhas)
            print(f"\nValores da coluna de status ('{cols['situacao']}') "
                  f"— ocorrências:")
            for val, qtd in valores.most_common(25):
                print(f"   {qtd:>7}  {val!r}")
        else:
            print("\nColuna de status: (não detectada neste arquivo)")

        # Valores únicos da coluna de RITO
        if "rito" in cols:
            valores = Counter(str(r.get(cols["rito"], "") or "").strip()
                              for r in linhas)
            print(f"\nValores da coluna de rito ('{cols['rito']}'):")
            for val, qtd in valores.most_common(10):
                print(f"   {qtd:>7}  {val!r}")

        # Coluna de DEVEDOR (risco em CRA/CRI): mostra se vem preenchida
        # na prática e dá amostras reais.
        if "devedor" in cols:
            preench = 0
            amostras_cri_cra = []
            for row in linhas:
                v = str(row.get(cols["devedor"], "") or "").strip()
                if v:
                    preench += 1
                    cats = categorias_da_linha(row, cols)
                    if (any("CRA" in c or "CRI" in c for c in cats)
                            and len(amostras_cri_cra) < 8):
                        amostras_cri_cra.append((row.get(cols.get("emissor", ""), "?"),
                                                 v))
            print(f"\nColuna de devedor ('{cols['devedor']}'): {preench}/{len(linhas)} "
                  f"linhas preenchidas")
            if amostras_cri_cra:
                print("Amostras de devedor em CRA/CRI:")
                for emissor, dev in amostras_cri_cra:
                    dev_curto = dev[:90] + ("…" if len(dev) > 90 else "")
                    print(f"   emissor: {str(emissor)[:40]!r:42}  ->  devedor: {dev_curto!r}")
            else:
                print("   (sem amostras de CRA/CRI com devedor preenchido neste arquivo)")
        else:
            print("\nColuna de devedor: (não detectada neste arquivo — esperado para "
                  "oferta_distribuicao.csv)")

        # Contagem por tipo monitorado e por fase
        cont_total = Counter()
        cont_analise = Counter()
        cont_reg = Counter()
        exemplos = {}   # (categoria, fase) -> (row, cols)
        for row in linhas:
            cats = categorias_da_linha(row, cols)
            if not cats:
                continue
            fase = fase_da_oferta(row, cols)
            dref = data_de_referencia(row, cols, fase) if fase else None
            na_janela = dref is not None and dref >= limite_data
            for c in cats:
                cont_total[c] += 1
                if fase == FASE_ANALISE and na_janela:
                    cont_analise[c] += 1
                    exemplos.setdefault((c, FASE_ANALISE), (row, cols))
                elif fase == FASE_REGISTRADA and na_janela:
                    cont_reg[c] += 1
                    exemplos.setdefault((c, FASE_REGISTRADA), (row, cols))
        print(f"\nOfertas por tipo monitorado "
              f"(total no arquivo | em análise na janela | registradas na janela):")
        for c in PADROES_TIPO:
            print(f"   {c}")
            print(f"      total: {cont_total[c]:>6}   |   "
                  f"em análise: {cont_analise[c]:>4}   |   "
                  f"registradas: {cont_reg[c]:>4}")

        # Exemplos de mensagem
        print("\nExemplos de mensagem que seriam enviadas:")
        algum = False
        for c in PADROES_TIPO:
            for fase in (FASE_ANALISE, FASE_REGISTRADA):
                ex = exemplos.get((c, fase))
                if ex is None:
                    continue
                algum = True
                row, rcols = ex
                print("\n" + "- " * 26)
                print(formatar_mensagem(row, rcols, fase))
        if not algum:
            print("   (nenhuma oferta na janela agora — normal dependendo do dia)")


def modo_test_telegram() -> None:
    msg = ("✅ <b>Robô CVM → Telegram</b>\n\n"
           "Teste de conexão bem-sucedido. Você receberá um alerta aqui "
           "quando um pedido de oferta de CRA, CRI, debênture ou nota "
           "comercial entrar em análise — e outro quando for registrada.")
    if enviar_telegram(msg):
        log.info("Mensagem de teste enviada com sucesso. ✅")
    else:
        log.error("Não foi possível enviar a mensagem de teste. "
                  "Confira TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.")


# -------------------------------------------------------------------
# main
# -------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Robô que avisa no Telegram sobre pedidos em análise e "
                    "ofertas registradas (CRA, CRI, debêntures, notas "
                    "comerciais) na CVM.")
    parser.add_argument("--loop", action="store_true",
                        help=f"roda continuamente, a cada {POLL_INTERVAL_SECONDS}s")
    parser.add_argument("--dry-run", action="store_true",
                        help="verifica e imprime as mensagens em vez de enviar")
    parser.add_argument("--inspect", action="store_true",
                        help="baixa os dados e analisa a estrutura dos CSVs")
    parser.add_argument("--test-telegram", action="store_true",
                        help="envia uma mensagem de teste para o Telegram")
    parser.add_argument("--notify-on-first-run", action="store_true",
                        help="na primeira execução, notifica tudo que está na "
                             "janela (padrão: só semeia o estado)")
    args = parser.parse_args()

    if args.inspect:
        modo_inspect()
        return
    if args.test_telegram:
        modo_test_telegram()
        return

    if args.loop:
        log.info("Modo loop iniciado (intervalo: %ds). Ctrl+C para parar.",
                 POLL_INTERVAL_SECONDS)
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
