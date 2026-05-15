# Robô CVM → Telegram

Robô automatizado que monitora as ofertas públicas de distribuição na CVM e
publica alertas em um canal do Telegram. Roda sozinho na infraestrutura do
GitHub Actions, sem servidor próprio nem PC ligado.

## O que ele monitora

Ofertas dos tipos:

- **CRA** — Certificado de Recebíveis do Agronegócio
- **CRI** — Certificado de Recebíveis Imobiliários
- **Debêntures** (incluindo debêntures simples)
- **Notas Comerciais** (e Notas Promissórias Comerciais)

E acompanha o **ciclo de vida** de cada oferta — pode enviar **dois alertas**:

- 📥 **Novo pedido em análise** — quando o pedido entra na CVM;
- ✅ **Oferta registrada** — quando o registro é concedido (ou dispensado).

Quem só quiser os alertas de registro pode mudar `ALERTAR_EM_ANALISE` para
`False` no topo do `cvm_telegram_bot.py`.

## Como é a mensagem

Cada alerta traz:

- **Tipo** da oferta (lido da coluna oficial da CVM)
- **Emissor**
- **Devedor (risco)** — *só em CRA e CRI*, onde o emissor é a securitizadora
  e o risco real é do devedor lastro
- **Coordenador líder** e **categoria do coordenador**
- **Data** (de entrada, se em análise; de registro, se registrada)
- **Valor** (quando informado)
- **Rito**, **modalidade**, **público-alvo** e **situação**
- **Número de registro/requerimento** e **número do processo**
- 🔗 **Link de busca no Google** pelo número de processo, que costuma cair
  direto na página da oferta (anúncio de início, prospecto, ou tela do SRE)

---

## Como funciona

A cada execução, o robô:

1. Baixa o arquivo oficial de ofertas do **Portal de Dados Abertos da CVM**
   (`oferta_distribuicao.zip`), mantido pela própria SRE.
2. Lê os dois CSVs do zip (`oferta_distribuicao.csv` e `oferta_resolucao_160.csv`),
   detectando as colunas automaticamente.
3. Filtra pelas ofertas dos tipos monitorados e classifica cada uma em uma
   **fase** (em análise / registrada / ignorar).
4. Compara com o que já foi notificado antes (arquivo `estado_ofertas.json`).
5. Envia um alerta quando uma oferta **aparece** ou **muda de fase**.

### Sobre a fonte de dados

O sistema web do SRE (`web.cvm.gov.br/sre-publico-cvm`) mostra os dados quase
em tempo real, mas usa uma API interna **não documentada e instável** — não é
confiável para automação de longo prazo.

Este robô usa o **Portal de Dados Abertos** (`dados.cvm.gov.br`), que é a fonte
**oficial, pública e estável** com exatamente as mesmas ofertas. O arquivo é
atualizado pela CVM **diariamente** (às vezes mais de uma vez por dia). Na
prática: você recebe o alerta com algumas horas de defasagem em vez de
segundos — o que é adequado para acompanhar o pipeline de ofertas.

### Robustez

- **Detecta as colunas automaticamente.** A CVM já mudou nomes de colunas no
  passado. O robô usa correspondência por palavra-chave, então sobrevive a
  pequenas alterações.
- **Deduplica entre arquivos e séries.** A mesma oferta pode aparecer em mais
  de uma linha (várias séries) ou nos dois CSVs — o robô notifica uma vez só.
- **Filtro por status.** Pedidos cancelados, revogados, expirados, caducados
  etc. são ignorados (não viram alerta nem ficam pendurados aguardando).
- **Filtro por janela temporal.** Só considera ofertas com data dentro dos
  últimos 45 dias (configurável) — evita re-notificar histórico antigo.
- **Estado persistente.** Cada execução só envia o que mudou desde a última;
  o estado fica em cache, sobrevive entre execuções.

---

## Estrutura do projeto

```
.
├── cvm_telegram_bot.py            # o robô (é o que importa)
├── requirements.txt               # dependências
├── test_motor.py                  # testes (opcional, só local)
├── README.md                      # este arquivo
├── RODAR_NO_GITHUB_ACTIONS.md     # guia completo de setup
└── .github/workflows/cvm-robo.yml # agendamento e execução automatizada
```

---

## Setup rápido

Em alto nível, o setup é:

1. **Criar um bot no Telegram** com o `@BotFather` → guardar o token.
2. **Criar um canal do Telegram** (privado) → adicionar o bot **como
   administrador** → obter o `chat_id` do canal.
3. **Subir os arquivos** num repositório do GitHub.
4. **Cadastrar dois secrets** no repositório:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. **Disparar** uma vez na aba Actions (modo `testar-telegram`) para validar,
   depois (modo `verificar`) para semear o estado inicial sem disparar
   alertas de histórico.

O passo a passo detalhado, com cliques na interface do GitHub e tudo, está
em **`RODAR_NO_GITHUB_ACTIONS.md`**.

---

## Modos de execução

Da aba **Actions** do repositório, **Run workflow** → escolha um modo:

| Modo | Para quê |
|---|---|
| `verificar` | Verificação normal — é o que o agendamento usa. Envia alertas das ofertas novas. |
| `testar-telegram` | Manda só uma mensagem de teste, para validar token e chat_id. |
| `inspecionar` | Baixa os dados e analisa a estrutura dos CSVs no log. Útil pra diagnóstico se algo parar de funcionar. |

Também dá pra rodar localmente (para testes), com:

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python cvm_telegram_bot.py --inspect        # analisa a estrutura do CSV
python cvm_telegram_bot.py --test-telegram  # envia uma mensagem de teste
python cvm_telegram_bot.py --dry-run        # verifica mas imprime em vez de enviar
python cvm_telegram_bot.py                  # uma verificação real
```

---

## Configurações no topo do `cvm_telegram_bot.py`

Tudo no bloco **CONFIGURAÇÃO**:

- **`PADROES_TIPO`** — quais tipos monitorar. Para acompanhar só CRI, por
  exemplo, deixe apenas a entrada do CRI no dicionário.
- **`ALERTAR_EM_ANALISE`** — `True` (padrão) ou `False`. Se `False`, o robô
  só envia o alerta ✅ de registro.
- **`STATUS_TERMINAIS`** — palavras que indicam pedido encerrado sem registro
  (cancelado, expirado, revogado etc.). Esses são ignorados.
- **`JANELA_DIAS`** — quantos dias para trás contam como "recente" (padrão: 45).
- **`POLL_INTERVAL_SECONDS`** — frequência do modo `--loop` (irrelevante no
  GitHub Actions, que controla o agendamento pelo arquivo de workflow).

---

## Manutenção

- **O cache do GitHub Actions guarda o estado** (`estado_ofertas.json`) entre
  execuções. Não apague — apagar faz o robô tratar tudo como "primeira
  execução" de novo (semeia sem notificar). Em geral, deixa quieto.
- **Se passar 60 dias sem nenhum commit no repositório**, o GitHub pausa o
  agendamento e avisa por e-mail. Pra reativar, basta voltar na aba Actions
  e reabilitar; ou fazer qualquer commit ocasional.
- **Se os alertas pararem de chegar e você quiser investigar**, rode o modo
  `inspecionar` — ele mostra no log o que o robô está vendo na CVM (linhas
  lidas, colunas detectadas, contagem por tipo, exemplos de mensagem).

---

## Aviso

Ferramenta informativa, baseada em dados públicos da CVM. Não constitui
recomendação de investimento. Confirme sempre as informações na fonte oficial
antes de tomar qualquer decisão.
