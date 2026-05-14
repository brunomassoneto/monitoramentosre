# Robô CVM → Telegram

Robô que **te avisa no Telegram sobre ofertas públicas de distribuição na
CVM**, filtrando pelos tipos que te interessam:

- **CRA** — Certificado de Recebíveis do Agronegócio
- **CRI** — Certificado de Recebíveis Imobiliários
- **Debêntures**
- **Notas Comerciais** (e Notas Promissórias Comerciais)

O robô acompanha o **ciclo de vida** da oferta e pode enviar **dois alertas**
pela mesma oferta:

- 📥 **Novo pedido em análise** — quando o pedido de oferta entra na CVM;
- ✅ **Oferta registrada** — quando o registro é concedido (ou dispensado).

Se você só quiser os alertas de registro, basta mudar `ALERTAR_EM_ANALISE`
para `False` no topo do `cvm_telegram_bot.py`.

---

## Como funciona

A cada verificação o robô:

1. Baixa o arquivo oficial de ofertas do **Portal de Dados Abertos da CVM**
   (`oferta_distribuicao.zip`), mantido pela própria SRE;
2. Lê os CSVs e filtra as ofertas dos tipos que te interessam;
3. Classifica cada oferta em uma **fase** (em análise / registrada);
4. Compara com o que já foi visto antes (arquivo `estado_ofertas.json`);
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

> Se algum dia você quiser a versão em tempo real, dá para trocar a função
> `baixar_zip` / `ler_csvs_do_zip` por uma chamada à API interna do SRE. O
> resto do robô (filtro, deduplicação, Telegram) continua igual. Para
> descobrir o endpoint: abra a página do SRE, tecle F12 → aba *Network* →
> recarregue → veja as chamadas `XHR`.

---

## Passo a passo de instalação

### 1. Pré-requisitos

- Python 3.9 ou superior
- Instalar a dependência:

```bash
pip install -r requirements.txt
```

### 2. Criar o bot no Telegram

1. No Telegram, abra uma conversa com **@BotFather**.
2. Envie `/newbot` e siga as instruções (nome e username do bot).
3. O BotFather devolve um **token** parecido com
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Guarde.

### 3. Descobrir o seu `chat_id`

1. Envie **qualquer mensagem** para o seu bot recém-criado (procure pelo
   username dele e mande um "oi"). Isso é obrigatório — bots não podem
   iniciar conversas.
2. Acesse no navegador (troque `<TOKEN>` pelo seu token):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Procure no JSON por `"chat":{"id":...}`. Esse número é o seu `chat_id`.
   - Para receber em um **grupo**: adicione o bot ao grupo, mande uma
     mensagem lá, e repita o `getUpdates` — o `id` do grupo costuma ser
     negativo.

### 4. Configurar o robô

Você tem duas opções (escolha uma):

**Opção A — variáveis de ambiente (recomendado):**

```bash
export TELEGRAM_BOT_TOKEN="123456789:AAE..."
export TELEGRAM_CHAT_ID="987654321"
```

**Opção B — editar o arquivo:** abra `cvm_telegram_bot.py` e preencha as
linhas `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` no bloco *CONFIGURAÇÃO*.

### 5. Testar

```bash
# Confere se o Telegram está configurado certo (deve chegar uma mensagem):
python cvm_telegram_bot.py --test-telegram

# Baixa os dados da CVM e mostra as colunas reais do CSV + amostras.
# Útil para confirmar que o robô está "enxergando" as ofertas:
python cvm_telegram_bot.py --inspect
```

> **Importante sobre o `--inspect`:** a CVM já mudou nomes de colunas no
> passado. O robô detecta as colunas automaticamente, mas se o `--inspect`
> mostrar `(não detectadas: ...)` para campos importantes como `tipo` ou
> `numero`, me avise o cabeçalho que aparece — ou adicione o nome real na
> lista `COLUMN_CANDIDATES` dentro do script. O filtro de tipo funciona
> mesmo sem a coluna `tipo` (ele varre a linha inteira), então no pior caso
> você ainda recebe os alertas, só com menos detalhes na mensagem.

### 6. Primeira execução

```bash
python cvm_telegram_bot.py
```

Na **primeira vez**, o robô apenas registra as ofertas atuais no arquivo
`estado_ofertas.json` **sem enviar nada** — assim você não recebe uma enxurrada
de alertas com o histórico. A partir da segunda execução, só ofertas **novas**
geram mensagem.

> Quer receber, já na primeira execução, tudo que foi registrado nos últimos
> dias? Use `python cvm_telegram_bot.py --notify-on-first-run`.

---

## Como deixar rodando sozinho

O robô foi feito para rodar **uma verificação por vez** (modo ideal para
agendador) ou **em loop contínuo**. Escolha um modo:

### Opção 1 — Loop contínuo (mais simples)

```bash
python cvm_telegram_bot.py --loop
```

Fica rodando e verifica a cada 1 hora (configurável em `POLL_INTERVAL_SECONDS`).
Bom para deixar rodando numa máquina/servidor ligado. Para rodar em segundo
plano no Linux/Mac: `nohup python cvm_telegram_bot.py --loop &`.

### Opção 2 — cron (Linux / macOS)

Edite o crontab com `crontab -e` e adicione (verifica a cada hora, das 7h às 21h):

```cron
0 7-21 * * * cd /caminho/para/cvm-robo && /usr/bin/python3 cvm_telegram_bot.py >> cron.log 2>&1
```

Se estiver usando variáveis de ambiente, defina-as no início do crontab ou
coloque o `export` num script wrapper.

### Opção 3 — Agendador de Tarefas (Windows)

1. Abra o *Agendador de Tarefas* → *Criar Tarefa Básica*.
2. Disparador: diariamente / repetir a cada 1 hora.
3. Ação: *Iniciar um programa* →
   - Programa: `python`
   - Argumentos: `cvm_telegram_bot.py`
   - Iniciar em: a pasta do robô (ex.: `C:\cvm-robo`).

### Opção 4 — systemd (servidor Linux)

Crie `/etc/systemd/system/cvm-robo.service`:

```ini
[Unit]
Description=Robo CVM -> Telegram
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/caminho/para/cvm-robo
Environment=TELEGRAM_BOT_TOKEN=123456789:AAE...
Environment=TELEGRAM_CHAT_ID=987654321
ExecStart=/usr/bin/python3 /caminho/para/cvm-robo/cvm_telegram_bot.py --loop
Restart=on-failure
RestartSec=300

[Install]
WantedBy=multi-user.target
```

Depois: `sudo systemctl enable --now cvm-robo` e acompanhe com
`journalctl -u cvm-robo -f`.

---

## Todos os comandos

| Comando | O que faz |
|---|---|
| `python cvm_telegram_bot.py` | Uma verificação (ideal para cron/agendador) |
| `python cvm_telegram_bot.py --loop` | Roda continuamente (verifica a cada `POLL_INTERVAL_SECONDS`) |
| `python cvm_telegram_bot.py --dry-run` | Verifica, mas **imprime** as mensagens em vez de enviar |
| `python cvm_telegram_bot.py --inspect` | Baixa os dados e mostra as colunas reais do CSV |
| `python cvm_telegram_bot.py --test-telegram` | Envia uma mensagem de teste |
| `python cvm_telegram_bot.py --notify-on-first-run` | Notifica o histórico recente já na 1ª execução |

---

## Ajustes que você pode querer fazer

Tudo no bloco **CONFIGURAÇÃO** no topo de `cvm_telegram_bot.py`:

- **`POLL_INTERVAL_SECONDS`** — frequência do modo `--loop` (padrão: 3600s = 1h).
  Não adianta colocar muito baixo: a CVM atualiza o arquivo no máximo algumas
  vezes por dia.
- **`JANELA_DIAS`** — quantos dias para trás conta como "recente" (padrão: 45).
- **`KEYWORDS_PALAVRA` / `KEYWORDS_TRECHO`** — os tipos monitorados. Para
  acompanhar **só CRI**, por exemplo, deixe as listas apenas com os termos de CRI.

---

## Arquivos gerados pelo robô

- **`estado_ofertas.json`** — memória das ofertas já notificadas. **Não apague**
  (apagar faz o robô tratar tudo como "primeira execução" de novo).
- **`cvm_robo.log`** — registro de execução, útil para depurar.

---

## Problemas comuns

| Sintoma | Causa provável / solução |
|---|---|
| `--test-telegram` falha | Token ou `chat_id` errados; ou você não mandou mensagem pro bot antes de pegar o `chat_id`. |
| Não chega nenhum alerta nunca | Normal nos primeiros dias se não houve registro novo. Rode `--inspect` para confirmar que o robô vê ofertas, e `--dry-run` para ver o que ele faria. |
| `--inspect` diz "não detectadas: tipo/numero" | A CVM mudou o layout. Veja o cabeçalho impresso e acrescente os nomes reais em `COLUMN_CANDIDATES`. |
| Erro de download | A CVM pode estar fora do ar momentaneamente; o robô tenta de novo no próximo ciclo. Confirme também que a máquina tem acesso a `dados.cvm.gov.br`. |
| Recebi a mesma oferta duas vezes | Raro — acontece se a CVM revisar uma linha sem número de registro estável. Inofensivo. |

---

## Aviso

Ferramenta informativa, baseada em dados públicos da CVM. Não constitui
recomendação de investimento. Confirme sempre as informações na fonte oficial
antes de tomar qualquer decisão.
