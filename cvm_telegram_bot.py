#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robô CVM → Telegram
===================

Monitora o registro de novas ofertas públicas de distribuição na CVM e envia
um alerta no Telegram sempre que aparece uma nova oferta dos tipos:

    - CRA  (Certificado de Recebíveis do Agronegócio)
    - CRI  (Certificado de Recebíveis Imobiliários)
    - Debêntures
    - Notas Comerciais (e Notas Promissórias Comerciais)

Fonte dos dados
---------------
Portal de Dados Abertos da CVM (mantido pela própria SRE):

    https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip

O arquivo contém as ofertas registradas/dispensadas (ICVM 400, RCVM 160 rito
ordinário, ICVM 555 e RCVM 160 rito automático). É atualizado pela CVM
diariamente (às vezes mais de uma vez por dia). É a fonte pública, estável e
oficial equivalente ao que aparece na tela de "Consulta de Oferta Pública" do
sistema SRE.

Observação sobre tempo real: o sistema web do SRE
(web.cvm.gov.br/sre-publico-cvm) mostra os dados em tempo quase real, mas usa
uma API interna não documentada. Este robô usa o Dados Abertos por ser uma
fonte oficial e estável — o custo é uma latência de algumas horas até ~1 dia.
Para a maioria dos usos (acompanhar o pipeline de ofertas) isso é suficiente.

Como usar
---------
    python cvm_telegram_bot.py --inspect        # baixa e mostra as colunas reais do CSV
    python cvm_telegram_bot.py --test-telegram  # envia uma mensagem de teste
    python cvm_telegram_bot.py                  # uma verificação (ideal para cron)
    python cvm_telegram_bot.py --loop           # roda continuamente
    python cvm_telegram_bot.py --dry-run        # verifica mas imprime em vez de enviar

Veja o README.md para a configuração do bot do Telegram e do agendamento.
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
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Falta a biblioteca 'requests'. Instale com:  pip install requests")


# ===================================================================
# CONFIGURAÇÃO
# ===================================================================
# Você pode definir tudo por variável de ambiente (recomendado) OU
# editar os valores padrão abaixo diretamente.

# --- Telegram ---------------------------------------------------------
# Token do bot (criado com o @BotFather) e o ID do chat de destino.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "COLE_AQUI_O_TOKEN_DO_BOT")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "COLE_AQUI_O_CHAT_ID")

# --- O que monitorar --------------------------------------------------
# Tipos de valor mobiliário de interesse. O casamento é feito sem acento
# e sem diferenciar maiúsculas/minúsculas. NÃO precisa mexer aqui a menos
# que queira incluir/excluir tipos.
#
# Acrônimos curtos (casam como palavra inteira):
KEYWORDS_PALAVRA = ["cra", "cri"]
# Termos longos (casam como trecho do texto — cobrem plural e variações):
KEYWORDS_TRECHO = [
    "certificado de recebiveis do agronegocio",
    "certificado de recebiveis imobiliario",   # cobre "imobiliário" e "imobiliários"
    "debenture",                               # cobre "debênture" e "debêntures"
    "nota comercial",
    "nota promissoria",                        # cobre "nota promissória comercial"
]

# --- Comportamento ----------------------------------------------------
# Intervalo entre verificações (segundos) no modo --loop. 3600 = 1 hora.
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "3600"))

# Só considera "nova" uma oferta cuja data de registro esteja dentro desta
# janela. Evita re-notificar histórico antigo e mantém a comparação enxuta.
JANELA_DIAS = int(os.environ.get("JANELA_DIAS", "45"))

# --- Fonte de dados / arquivos locais ---------------------------------
CVM_ZIP_URL = "https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip"

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "estado_ofertas.json"   # ofertas já notificadas
LOG_FILE = BASE_DIR / "cvm_robo.log"

# Link mostrado nas mensagens (tela pública de consulta do SRE).
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


# -------------------------------------------------------------------
# Utilitários de texto
# -------------------------------------------------------------------
def strip_accents(texto: str) -> str:
    """Remove acentos: 'Debênture' -> 'Debenture'."""
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(texto: str) -> str:
    """Normaliza para comparação: sem acento, minúsculo, espaços colapsados."""
    if texto is None:
        return ""
    t = strip_accents(str(texto)).lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def norm_header(nome: str) -> str:
    """Normaliza nome de coluna: 'Data_Registro' / 'Data Registro' -> 'dataregistro'."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(str(nome)).lower())


def html_escape(texto: str) -> str:
    """Escapa caracteres especiais para o parse_mode=HTML do Telegram."""
    return (
        str(texto)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# -------------------------------------------------------------------
# Detecção de colunas
# -------------------------------------------------------------------
# O layout exato do CSV da CVM pode variar com o tempo (a CVM já mudou
# nomes de colunas mais de uma vez). Por isso o robô NÃO usa nomes fixos:
# ele procura, no cabeçalho real, a coluna mais provável para cada campo.
COLUMN_CANDIDATES = {
    "tipo": [
        "valormobiliario", "tipovalormobiliario", "especievalormobiliario",
        "especie", "tipoativo", "tipooferta", "valoresmobiliarios",
    ],
    "emissor": [
        "nomeemissor", "emissor", "nomedoemissor", "denominacaoemissor",
        "razaosocialemissor", "nomeofertante", "ofertante", "denominacaosocial",
    ],
    "numero": [
        "numeroregistro", "numregistro", "numerodoregistro", "numeroprocesso",
        "numerooferta", "numerorequerimento", "protocolo", "numprocesso",
        "idoferta", "numero",
    ],
    "data_registro": [
        "dataregistro", "datadoregistro", "dataconcessaoregistro",
        "dataconcessao", "datadeconcessao", "datadeferimento",
    ],
    "data_inicio": ["datainiciooferta", "datainicio", "datadainiciooferta"],
    "valor": [
        "valoroferta", "valortotaloferta", "valortotal", "montante",
        "valordistribuicao", "valortotaldistribuicao", "valormobiliariooferta",
    ],
    "modalidade": ["modalidadeoferta", "modalidaderegistro", "modalidade"],
    "situacao": ["situacao", "status", "situacaooferta", "situacaoregistro"],
}


def detect_columns(header: list[str]) -> dict[str, str]:
    """
    Recebe o cabeçalho real do CSV e devolve um mapa
    {campo_logico: nome_real_da_coluna} com a melhor correspondência.
    """
    norm_map = {norm_header(h): h for h in header}
    resultado: dict[str, str] = {}
    for campo, candidatos in COLUMN_CANDIDATES.items():
        achou = None
        # 1) match exato
        for cand in candidatos:
            if cand in norm_map:
                achou = norm_map[cand]
                break
        # 2) match por "contém"
        if achou is None:
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
def linha_interessa(row: dict, cols: dict) -> bool:
    """
    Decide se a linha é de um tipo monitorado.

    Estratégia robusta: se o robô identificou a coluna de 'tipo', usa ela;
    senão (ou além disso) varre o conteúdo inteiro da linha. Como os termos
    procurados são bem específicos, o risco de falso positivo é mínimo.
    """
    # Texto da coluna de tipo, se identificada
    alvos = []
    if "tipo" in cols:
        alvos.append(row.get(cols["tipo"], ""))
    # Fallback: a linha inteira concatenada
    alvos.append(" ".join(str(v) for v in row.values()))

    for alvo in alvos:
        txt = normalize(alvo)
        if not txt:
            continue
        # acrônimos curtos: palavra inteira
        for kw in KEYWORDS_PALAVRA:
            if re.search(r"\b" + re.escape(kw) + r"\b", txt):
                return True
        # termos longos: trecho
        for kw in KEYWORDS_TRECHO:
            if kw in txt:
                return True
    return False


def tipo_detectado(row: dict, cols: dict) -> str:
    """Devolve uma etiqueta amigável do tipo, para exibir na mensagem."""
    if "tipo" in cols and row.get(cols["tipo"]):
        return str(row[cols["tipo"]]).strip()
    # tenta inferir pelo conteúdo
    txt = normalize(" ".join(str(v) for v in row.values()))
    if "agronegocio" in txt or re.search(r"\bcra\b", txt):
        return "CRA (Certificado de Recebíveis do Agronegócio)"
    if "imobiliario" in txt or re.search(r"\bcri\b", txt):
        return "CRI (Certificado de Recebíveis Imobiliários)"
    if "debenture" in txt:
        return "Debênture"
    if "nota comercial" in txt or "nota promissoria" in txt:
        return "Nota Comercial"
    return "(tipo não identificado)"


# -------------------------------------------------------------------
# Identificador único da oferta (para deduplicação)
# -------------------------------------------------------------------
def offer_id(filename: str, row: dict, cols: dict) -> str:
    """
    Gera um ID estável para a oferta. Preferência pelo número de
    registro/processo; se não houver, usa um hash do conteúdo da linha.
    """
    if "numero" in cols and row.get(cols["numero"]):
        return f"{filename}:{str(row[cols['numero']]).strip()}"
    # fallback: hash do conteúdo
    bruto = "|".join(f"{k}={v}" for k, v in sorted(row.items()))
    h = hashlib.sha1(bruto.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{filename}:hash:{h}"


# -------------------------------------------------------------------
# Datas
# -------------------------------------------------------------------
def parse_data(valor: str):
    """Tenta interpretar uma data em vários formatos comuns da CVM."""
    if not valor:
        return None
    s = str(valor).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    # ISO costuma vir como 'YYYY-MM-DD' (possivelmente com horário)
    s_data = s.split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s_data, fmt)
        except ValueError:
            continue
    return None


def data_da_oferta(row: dict, cols: dict):
    """Pega a melhor data disponível: registro > início da oferta."""
    for campo in ("data_registro", "data_inicio"):
        if campo in cols:
            d = parse_data(row.get(cols[campo], ""))
            if d:
                return d
    return None


# -------------------------------------------------------------------
# Download e leitura do ZIP da CVM
# -------------------------------------------------------------------
def baixar_zip(url: str) -> bytes:
    """Baixa o arquivo .zip da CVM. Lança exceção em caso de falha."""
    log.info("Baixando dados da CVM: %s", url)
    resp = requests.get(url, timeout=120, headers={"User-Agent": "cvm-robo/1.0"})
    resp.raise_for_status()
    log.info("Download OK (%.1f KB).", len(resp.content) / 1024)
    return resp.content


def ler_csvs_do_zip(zip_bytes: bytes):
    """
    Gera tuplas (nome_arquivo, header, lista_de_linhas) para cada .csv
    dentro do zip. Os arquivos da CVM são separados por ';' e codificados
    em ISO-8859-1 (latin-1).
    """
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
# Estado (ofertas já notificadas)
# -------------------------------------------------------------------
def carregar_estado() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                dados = json.load(fh)
            dados.setdefault("ids_notificados", [])
            return dados
        except Exception as exc:  # noqa: BLE001
            log.error("Estado corrompido (%s). Começando do zero.", exc)
    return {"ids_notificados": [], "primeira_execucao": True}


def salvar_estado(estado: dict) -> None:
    estado["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(estado, fh, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)  # gravação atômica


# -------------------------------------------------------------------
# Telegram
# -------------------------------------------------------------------
def enviar_telegram(texto: str) -> bool:
    """Envia uma mensagem. Retorna True se enviou com sucesso."""
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


def formatar_mensagem(nome_arquivo: str, row: dict, cols: dict) -> str:
    """Monta o texto (HTML) do alerta de uma oferta."""
    tipo = html_escape(tipo_detectado(row, cols))
    emissor = html_escape(row.get(cols.get("emissor", ""), "") or "(não informado)")
    numero = html_escape(row.get(cols.get("numero", ""), "") or "—")
    modalidade = html_escape(row.get(cols.get("modalidade", ""), "") or "—")
    situacao = html_escape(row.get(cols.get("situacao", ""), "") or "")

    d = data_da_oferta(row, cols)
    data_txt = d.strftime("%d/%m/%Y") if d else "—"

    valor_bruto = row.get(cols.get("valor", ""), "") or ""
    valor_txt = formatar_valor(valor_bruto)

    linhas = [
        "🆕 <b>Nova oferta registrada na CVM</b>",
        "",
        f"📄 <b>Tipo:</b> {tipo}",
        f"🏢 <b>Emissor:</b> {emissor}",
        f"🔢 <b>Registro/Processo:</b> {numero}",
        f"📅 <b>Data:</b> {data_txt}",
    ]
    if valor_txt:
        linhas.append(f"💰 <b>Valor:</b> {valor_txt}")
    if modalidade and modalidade != "—":
        linhas.append(f"📋 <b>Modalidade:</b> {modalidade}")
    if situacao:
        linhas.append(f"📌 <b>Situação:</b> {situacao}")
    linhas += [
        "",
        f"📁 <i>{html_escape(nome_arquivo)}</i>",
        f'🔗 <a href="{LINK_CONSULTA}">Consultar no sistema SRE</a>',
    ]
    return "\n".join(linhas)


def formatar_valor(bruto: str) -> str:
    """Tenta exibir o valor como moeda; se não der, devolve o texto cru."""
    s = str(bruto).strip()
    if not s or s.lower() in ("nan", "none", "null", "0"):
        return ""
    limpo = s.replace("R$", "").strip()
    # normaliza separadores: remove milhar, troca decimal por ponto
    teste = limpo
    if "," in teste and "." in teste:
        teste = teste.replace(".", "").replace(",", ".")
    elif "," in teste:
        teste = teste.replace(",", ".")
    try:
        n = float(teste)
        return "R$ " + f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except ValueError:
        return s  # devolve cru se não for número reconhecível


# -------------------------------------------------------------------
# Núcleo: uma verificação
# -------------------------------------------------------------------
def verificar(dry_run: bool = False, notificar_primeira: bool = False) -> None:
    """Executa uma verificação completa: baixa, filtra, notifica, salva estado."""
    estado = carregar_estado()
    primeira = estado.get("primeira_execucao", False) or not STATE_FILE.exists()
    ja_notificados = set(estado.get("ids_notificados", []))

    try:
        zip_bytes = baixar_zip(CVM_ZIP_URL)
    except Exception as exc:  # noqa: BLE001
        log.error("Não foi possível baixar os dados da CVM: %s", exc)
        return

    limite_data = datetime.now() - timedelta(days=JANELA_DIAS)
    candidatas = []   # (nome_arquivo, row, cols, id)
    total_linhas = 0

    for nome_arquivo, header, linhas in ler_csvs_do_zip(zip_bytes):
        cols = detect_columns(header)
        log.info("Arquivo '%s': %d linhas. Colunas detectadas: %s",
                 nome_arquivo, len(linhas), cols or "(nenhuma)")
        total_linhas += len(linhas)
        for row in linhas:
            if not linha_interessa(row, cols):
                continue
            # filtro de janela temporal (se houver data utilizável)
            d = data_da_oferta(row, cols)
            if d is not None and d < limite_data:
                continue
            oid = offer_id(nome_arquivo, row, cols)
            candidatas.append((nome_arquivo, row, cols, oid))

    log.info("Total de linhas lidas: %d | ofertas de interesse na janela: %d",
             total_linhas, len(candidatas))

    # Primeira execução: apenas semeia o estado, sem disparar notificações
    # (a menos que o usuário peça explicitamente).
    if primeira and not notificar_primeira:
        for _, _, _, oid in candidatas:
            ja_notificados.add(oid)
        estado["ids_notificados"] = sorted(ja_notificados)
        estado["primeira_execucao"] = False
        if not dry_run:
            salvar_estado(estado)
        log.info("Primeira execução: %d ofertas registradas no estado SEM "
                 "notificar. A partir de agora só as novas geram alerta.",
                 len(candidatas))
        return

    # Execuções seguintes: notifica o que for novo
    novas = [(arq, row, cols, oid) for (arq, row, cols, oid) in candidatas
             if oid not in ja_notificados]

    if not novas:
        log.info("Nenhuma oferta nova desta vez.")
        if not dry_run:
            estado["primeira_execucao"] = False
            salvar_estado(estado)
        return

    log.info("%d oferta(s) nova(s) encontrada(s).", len(novas))
    enviadas = 0
    for nome_arquivo, row, cols, oid in novas:
        msg = formatar_mensagem(nome_arquivo, row, cols)
        if dry_run:
            print("\n----- (DRY-RUN) mensagem que seria enviada -----")
            print(msg)
            print("------------------------------------------------")
            ja_notificados.add(oid)  # em dry-run marcamos para não repetir no console
            enviadas += 1
            continue
        if enviar_telegram(msg):
            ja_notificados.add(oid)
            enviadas += 1
            log.info("Alerta enviado: %s", oid)
            time.sleep(1)  # respeita o rate limit do Telegram
        else:
            log.warning("Falha ao enviar (será tentado de novo na próxima): %s", oid)

    estado["ids_notificados"] = sorted(ja_notificados)
    estado["primeira_execucao"] = False
    if not dry_run:
        salvar_estado(estado)
    log.info("Verificação concluída: %d/%d alertas enviados.", enviadas, len(novas))


# -------------------------------------------------------------------
# Modos auxiliares
# -------------------------------------------------------------------
def modo_inspect() -> None:
    """Baixa os dados e mostra o cabeçalho real + amostras. Não usa Telegram."""
    try:
        zip_bytes = baixar_zip(CVM_ZIP_URL)
    except Exception as exc:  # noqa: BLE001
        log.error("Falha no download: %s", exc)
        return
    for nome_arquivo, header, linhas in ler_csvs_do_zip(zip_bytes):
        cols = detect_columns(header)
        print("\n" + "=" * 70)
        print(f"ARQUIVO: {nome_arquivo}   ({len(linhas)} linhas)")
        print("=" * 70)
        print("Colunas (cabeçalho real do CSV):")
        for h in header:
            print(f"   - {h}")
        print("\nMapa de colunas detectado pelo robô:")
        for campo, real in cols.items():
            print(f"   {campo:<14} -> {real}")
        faltando = [c for c in COLUMN_CANDIDATES if c not in cols]
        if faltando:
            print(f"   (não detectadas: {', '.join(faltando)})")
        # mostra até 3 linhas de interesse como amostra
        amostras = [r for r in linhas if linha_interessa(r, cols)][:3]
        print(f"\nAmostras de ofertas de interesse (até 3 de "
              f"{sum(1 for r in linhas if linha_interessa(r, cols))}):")
        for r in amostras:
            print(f"   • tipo={tipo_detectado(r, cols)!r} | "
                  f"emissor={r.get(cols.get('emissor',''),'?')!r} | "
                  f"data={data_da_oferta(r, cols)}")
        if not amostras:
            print("   (nenhuma — confira se o filtro de tipo casa com este arquivo)")


def modo_test_telegram() -> None:
    """Envia uma mensagem de teste para validar token e chat_id."""
    msg = ("✅ <b>Robô CVM → Telegram</b>\n\n"
           "Teste de conexão bem-sucedido. "
           "Você receberá um alerta aqui sempre que uma nova oferta de "
           "CRA, CRI, debênture ou nota comercial for registrada na CVM.")
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
        description="Robô que avisa no Telegram sobre novas ofertas (CRA, CRI, "
                    "debêntures, notas comerciais) registradas na CVM.")
    parser.add_argument("--loop", action="store_true",
                        help="roda continuamente, verificando a cada "
                             f"{POLL_INTERVAL_SECONDS}s")
    parser.add_argument("--dry-run", action="store_true",
                        help="verifica e imprime as mensagens em vez de enviar")
    parser.add_argument("--inspect", action="store_true",
                        help="baixa os dados e mostra as colunas reais do CSV")
    parser.add_argument("--test-telegram", action="store_true",
                        help="envia uma mensagem de teste para o Telegram")
    parser.add_argument("--notify-on-first-run", action="store_true",
                        help="na primeira execução, notifica tudo que está na "
                             "janela (padrão: só semeia o estado, sem notificar)")
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
