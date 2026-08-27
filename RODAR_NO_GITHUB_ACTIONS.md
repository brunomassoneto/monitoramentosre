# Rodar o robô no GitHub Actions

Este guia coloca o robô pra rodar **sozinho, na infraestrutura do GitHub** —
a cada 30 minutos em dias úteis e a cada 3 horas nos fins de semana, sem
depender do seu computador.

A persistência do estado (a “memória” das ofertas já avisadas) é feita pelo
**cache do GitHub Actions**, então o repositório fica limpo, sem commits
automáticos.

> **Não precisa editar o código.** O `cvm_telegram_bot.py` funciona como
> está; o token e o chat_id ficam nos **Secrets** do repositório.

-----

## ⚠️ Importante: NUNCA escreva o token no arquivo

O token vai **só** nos Secrets do GitHub (Passo 5). Não cole ele dentro do
`cvm_telegram_bot.py` — se o arquivo for pro GitHub, o token fica exposto.
Deixe as linhas `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` no script
**exatamente como estão**; o workflow injeta os valores em tempo de execução.

-----

## Passo 1 — Criar o bot no Telegram

1. No Telegram, abra **@BotFather**.
1. Envie `/newbot` e siga: dê um nome e um *username* terminado em `bot`.
1. O BotFather devolve um **token** no formato `123456789:AAE-xxxxxxxxxxx`.
   **Copie e guarde** — ele só aparece uma vez (mas você pode pegá-lo de
   novo depois com `/mybots` → escolha o bot → `API Token`).

## Passo 2 — Criar o canal do Telegram

1. No app, toque no botão de **novo** (lápis/caneta) → **Novo canal**.
1. Dê nome e descrição.
1. Escolha **Canal Privado** — só quem você convidar entra; o canal não
   aparece em buscas.

## Passo 3 — Adicionar o bot como administrador do canal

Em canal, o bot **precisa ser administrador** (em grupo bastaria ser membro).
Sem isso, ele não consegue publicar.

1. Entre no canal → toque no nome dele no topo pra abrir as informações.
1. **Administradores** → **Adicionar administrador**.
1. Busque pelo *username* do seu bot e adicione.
1. Mantenha a permissão **Publicar mensagens** marcada (vem por padrão).

## Passo 4 — Pegar o `chat_id` do canal

1. No canal, publique uma mensagem qualquer (ex.: `teste`).
1. No navegador, abra (trocando `<TOKEN>` pelo token do seu bot):
   
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
1. Vai aparecer um JSON. Procure por **`"channel_post"`** — logo abaixo
   tem `"chat":{"id":-1001234567890,"title":"...",...}`.
1. O número em `"id"` — começa com **`-100`** — é o **`chat_id` do canal**.
   Copie inteiro, **com o sinal de menos**.

## Passo 5 — Subir os arquivos no GitHub

Crie um repositório (recomendo **Privado**, mas pode ser Público — o token
fica seguro nos Secrets de qualquer jeito; o que mudaria é só o código ser
visível).

A estrutura precisa ficar assim:

```
seu-repositorio/
├── cvm_telegram_bot.py
├── requirements.txt
├── README.md                          (opcional)
├── test_motor.py                      (opcional)
└── .github/
    └── workflows/
        └── cvm-robo.yml
```

Pela interface do GitHub, sem usar terminal:

1. **Add file → Upload files** → arraste `cvm_telegram_bot.py` e
   `requirements.txt` → **Commit changes**.
1. **Add file → Create new file** → no nome do arquivo digite exatamente
   `.github/workflows/cvm-robo.yml` (o GitHub cria as pastas conforme você
   digita as barras) → cole o conteúdo do `cvm-robo.yml` → **Commit changes**.

## Passo 6 — Cadastrar os Secrets

1. No repositório: **Settings → Secrets and variables → Actions**.
1. Botão **New repository secret**. Crie os secrets:
   
   |Name (exatamente assim)   |Secret (valor)                                       |Obrigatório|
   |--------------------------|-----------------------------------------------------|:---------:|
   |`TELEGRAM_BOT_TOKEN`      |o token do BotFather (ex.: `123456789:AAE...`)       |sim        |
   |`TELEGRAM_CHAT_ID`        |o `chat_id` do canal (ex.: `-1001234567890`)         |sim        |
   |`TELEGRAM_ADMIN_CHAT_ID`  |o seu `chat_id` **pessoal** na DM com o bot          |recomendado|

### Por que cadastrar o `TELEGRAM_ADMIN_CHAT_ID`

O robô manda dois tipos de mensagem: **alertas de oferta** (que interessam a
todo mundo no canal) e **avisos administrativos** de “modo reserva” — diagnóstico
de que a API da CVM não respondeu, que não é uma oferta de verdade.

Com este secret cadastrado, o aviso administrativo vai para a **sua DM privada**
com o bot e o canal fica só com alertas de oferta. **Sem ele, o robô cai de volta
no `TELEGRAM_CHAT_ID` e o aviso técnico aparece no canal, para todos.**

Para descobrir o seu `chat_id` pessoal:

1. Abra uma conversa privada com o seu bot no Telegram e mande `/start`
   (**esse passo é obrigatório** — o Telegram só deixa um bot escrever para
   quem falou com ele primeiro).
1. Abra `https://api.telegram.org/bot<TOKEN>/getUpdates` no navegador.
1. Procure por **`"message"`** (não `"channel_post"`) — abaixo vem
   `"chat":{"id":123456789,...}`. Esse número, **positivo e sem o `-100`**, é o
   seu `chat_id` pessoal.

> O modo `testar-telegram` valida os dois destinos: manda uma mensagem no canal
> e outra na DM administrativa. Se a do canal chegar e a da DM não, quase sempre
> é porque faltou o `/start` na conversa privada.

## Passo 7 — Ativar o workflow (se necessário)

Abra a aba **Actions**. Se aparecer uma tela pedindo pra habilitar
workflows, confirme. O workflow **“Robô CVM → Telegram”** deve aparecer
listado depois disso.

## Passo 8 — Testar

Antes de confiar no agendamento automático, dispare na mão:

1. Aba **Actions** → workflow **Robô CVM → Telegram** → **Run workflow**.
1. Escolha o modo **`testar-telegram`** → **Run workflow**.
1. Em ~1 minuto, a mensagem de teste deve aparecer **no canal**. Se não
   chegar, abra a execução no Actions e leia o log da etapa “Rodar o robô”:
   quase sempre é token errado, chat_id errado ou bot que não está como
   administrador do canal.

## Passo 9 — Primeira verificação real

Ainda em **Run workflow**, agora com o modo **`verificar`**.

Esta é a primeira execução de verdade. O robô **não envia alertas** — ele
apenas registra as ofertas atuais como “já vistas” no estado, pra você não
receber o histórico inteiro de uma vez. O log vai dizer algo como
*“Primeira execução: N ofertas registradas no estado SEM notificar”*.

A partir daí está tudo pronto. Quando aparecer uma oferta nova, chega no
canal.

-----

## Modos no botão “Run workflow”

|Modo             |Para quê                                                                                                                                                              |
|-----------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|`verificar`      |Verificação normal — é o que o agendamento usa. Envia alertas das ofertas novas.                                                                                      |
|`testar-telegram`|Só manda uma mensagem de teste, pra validar token e chat_id.                                                                                                          |
|`inspecionar`    |Consulta a API do SRE e mostra no log o que o robô está enxergando (status, contagem por tipo, exemplos de mensagem). Útil pra diagnóstico se algo parar de funcionar.|

-----

## Notas úteis

**Frequência:** entre 07h e 24h (BRT), o robô roda a cada 2 minutos em dias
úteis e a cada 10 minutos nos fins de semana; de madrugada fica parado.
Para mudar, edite os `cron` em
`.github/workflows/cvm-robo.yml`. Atenção que o cron do GitHub é em **UTC**
(Brasil = UTC−3).

**Atraso do agendamento:** o agendamento do GitHub Actions não é cravado
no segundo — em horários de pico pode atrasar de 5 a 20 minutos. Não é
relevante pra este robô.

**Pausa após 60 dias parado:** se o repositório ficar 60 dias sem nenhum
commit, o GitHub **desativa** o agendamento (manda e-mail avisando). Foi o
que derrubou o robô em 22/08/2026. Agora o workflow se defende sozinho: o
passo “Manter o agendamento vivo” empurra um commit vazio sempre que o
repositório passa de 50 dias parado.

Se ainda assim acontecer, a reativação é **manual** — aba **Actions** →
workflow **Robô CVM → Telegram** → menu `···` → **Enable workflow**. Fazer
um commit novo não reativa um workflow já desativado.

**Cache do estado:** o GitHub remove caches não acessados por 7 dias. Como
o robô roda todo dia, o cache é renovado continuamente. Se o robô ficar
parado mais de 7 dias, o estado é perdido — e na volta ele faz a
“primeira execução” de novo (registra tudo sem notificar). Inofensivo, mas
você perde os eventos do intervalo.

**Trocar o token ou o chat_id depois:** é só editar o valor em Settings →
Secrets and variables → Actions. Não precisa mexer em mais nada.

-----

## Problemas comuns

|Sintoma                                                     |Causa provável / solução                                                                                                                                        |
|------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------|
|`--test-telegram` falha com “chat not found”                |O bot não é administrador do canal (Passo 3), ou o `chat_id` está errado/sem o sinal de menos.                                                                  |
|A mensagem de teste cai no chat privado do bot, não no canal|O `TELEGRAM_CHAT_ID` no Secrets está com o ID errado — está com o seu ID pessoal em vez do ID do canal.                                                         |
|Não chega nenhum alerta nunca                               |Normal nos primeiros dias se a CVM não registrou nada novo. Rode `inspecionar` para confirmar que o robô vê ofertas, e `verificar` em modo manual pra ver o log.|
|`inspecionar` diz “não detectadas” pra colunas importantes  |A CVM mudou o layout do arquivo. Veja os nomes reais no log e adicione-os na lista `COLUMN_CANDIDATES` dentro do script.                                        |
|Recebi a mesma oferta duas vezes                            |Raro — pode acontecer se a CVM revisar uma linha sem número de processo estável. Inofensivo.                                                                    |