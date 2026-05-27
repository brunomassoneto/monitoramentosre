# Robô CVM → Telegram

Robô automatizado que monitora as ofertas públicas de distribuição na CVM e
publica alertas em um canal do Telegram. Roda sozinho na infraestrutura do
GitHub Actions, sem servidor próprio nem PC ligado.

## O que ele monitora

Ofertas dos tipos:

- **CRA** — Certificado de Recebíveis do Agronegócio
- **CRI** — Certificado de Recebíveis Imobiliários
- **Debêntures**
- **Notas Comerciais** (e Notas Promissórias Comerciais)

E acompanha o **ciclo de vida** de cada oferta — pode enviar **dois alertas**:

- 📥 **Novo pedido em análise** — quando o pedido entra na CVM;
- ✅ **Oferta registrada** — quando o registro é concedido.

Quem só quiser os alertas de registro pode mudar `ALERTAR_EM_ANALISE` para
`False` no topo do `cvm_telegram_bot.py`.

## O que vem em cada alerta

- **Tipo** da oferta
- **Emissor**
- **Devedor (risco)** — *só em CRA e CRI*, onde o emissor é a securitizadora
  e o risco de crédito real é do devedor lastro
- **Coordenador líder** e **demais coordenadores do consórcio**
- **Data**, **valor**, **rito**, **modalidade** e **situação**
- **Protocolo** e **número do processo**
- 🔗 **Link direto** para a página da oferta no sistema SRE

-----

## Como funciona

A cada execução, o robô:

1. Consulta a **API do sistema SRE** (a mesma que alimenta a tela pública de
   consulta de ofertas), pedindo as ofertas mais recentes, ordenadas por data.
1. Filtra pelos tipos monitorados e classifica cada oferta em uma **fase**
   (em análise / registrada / ignorar), a partir do status.
1. Para cada oferta **nova**, busca os detalhes extras: os coordenadores do
   consórcio e, em CRA/CRI, o devedor.
1. Compara com o que já foi notificado antes (arquivo `estado_ofertas.json`).
1. Envia um alerta no canal do Telegram quando uma oferta **aparece** ou
   **muda de fase**.

### Fonte de dados — principal: API do SRE

O robô consome a API interna do sistema SRE
(`web.cvm.gov.br/sre-publico-cvm`). Vantagem: os dados são atualizados em
tempo quase real — sem a defasagem de dias do Portal de Dados Abertos.

Endpoints usados:

- `POST .../rest/sitePublico/pesquisar/detalhado` — lista paginada de ofertas.
- `GET  .../rest/sitePublico/pesquisar/infOferta/{id}` — detalhe da oferta
  (de onde sai o devedor).
- `GET  .../rest/sitePublico/pesquisar/participantes/{id}` — participantes
  da oferta (de onde saem os coordenadores do consórcio).

**Atenção:** esta API é interna do site da CVM, não é documentada nem tem
contrato público de estabilidade. Pode mudar de formato sem aviso. Por isso
existe uma fonte de reserva (abaixo).

### Modo reserva (degradado)

Se a API do SRE falhar (timeout, bloqueio, formato inesperado), o robô
**não dispara alertas individuais** — ele envia, no máximo uma vez a cada
12 horas, um aviso de “modo reserva” no Telegram, só pra você saber que
está degradado. Quando o SRE voltar, as ofertas reais surgidas no período
são capturadas normalmente na próxima verificação via SRE.

**Por que não usar o Portal de Dados Abertos como reserva ativa?** Porque
o Portal contém o histórico inteiro de ofertas e usa identificadores
diferentes dos do SRE — comparar com o estado salvo gera milhares de
falsos positivos (flood). Ainda dá pra inspecioná-lo manualmente via
`--inspect`, mas o `verificar` não o usa para alertar.

### Robustez

- **Comparação de estado.** A cada execução o robô re-verifica as ofertas
  recentes e compara com o estado salvo. Uma oferta que escape numa execução
  é capturada na seguinte — não há “perdeu o bonde”.
- **Deduplicação.** A mesma oferta não gera alertas repetidos, nem quando
  aparece mais de uma vez na listagem.
- **Filtro por status.** Pedidos cancelados, revogados, expirados, caducados
  etc. são ignorados.
- **Filtro por janela temporal.** Só considera ofertas dos últimos 45 dias
  (configurável) — evita re-notificar histórico antigo.
- **Estado persistente.** Cada execução só envia o que mudou desde a última;
  o estado fica no cache do GitHub Actions, sobrevivendo entre execuções.

-----

## Estrutura do projeto

```
.
├── cvm_telegram_bot.py            # o robô (é o que importa)
├── requirements.txt               # dependências
├── test_motor.py                  # testes (opcional, só para rodar localmente)
├── README.md                      # este arquivo
├── RODAR_NO_GITHUB_ACTIONS.md     # guia completo de setup
└── .github/workflows/cvm-robo.yml # agendamento e execução automatizada
```

-----

## Agendamento

O robô roda automaticamente pelo GitHub Actions:

- **Dias úteis:** uma verificação a cada **30 minutos**.
- **Fins de semana:** uma verificação a cada **3 horas**.

O agendamento é definido pelos `cron` no arquivo `.github/workflows/cvm-robo.yml`.
Os horários do cron são em UTC (Brasil = UTC−3).

-----

## Modos de execução

Da aba **Actions** do repositório, **Run workflow** → escolha um modo:

|Modo             |Para quê                                                                                                                             |
|-----------------|-------------------------------------------------------------------------------------------------------------------------------------|
|`verificar`      |Verificação normal — é o que o agendamento usa. Envia alertas das ofertas novas.                                                     |
|`testar-telegram`|Manda só uma mensagem de teste, para validar token e chat_id.                                                                        |
|`inspecionar`    |Consulta a API e mostra no log o que o robô está enxergando (status, contagem por tipo, exemplos de mensagem). Útil para diagnóstico.|

Também é possível rodar localmente, para testes:

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python cvm_telegram_bot.py --inspect        # mostra o que o robô enxerga
python cvm_telegram_bot.py --test-telegram  # envia uma mensagem de teste
python cvm_telegram_bot.py --dry-run        # verifica mas imprime em vez de enviar
python cvm_telegram_bot.py                  # uma verificação real
```

-----

## Configurações no topo do `cvm_telegram_bot.py`

Tudo no bloco **CONFIGURAÇÃO**:

- **`PADROES_TIPO`** — quais tipos monitorar. Para acompanhar só CRI, por
  exemplo, deixe apenas a entrada do CRI no dicionário.
- **`ALERTAR_EM_ANALISE`** — `True` (padrão) ou `False`. Se `False`, o robô
  só envia o alerta ✅ de registro.
- **`STATUS_TERMINAIS`** / **`STATUS_REGISTRADA`** — palavras usadas para
  classificar a fase a partir do status da oferta.
- **`JANELA_DIAS`** — quantos dias para trás contam como “recente” (padrão: 45).
- **`QTD_OFERTAS_VERIFICAR`** — quantas ofertas recentes buscar por verificação
  (padrão: 200). Aumente se o robô ficar muito tempo parado.

-----

## Manutenção

- **O cache do GitHub Actions guarda o estado** (`estado_ofertas.json`) entre
  execuções. Não apague — apagar faz o robô tratar tudo como “primeira
  execução” (semeia sem notificar). Em uso normal, deixe quieto.
- **Se passar 60 dias sem nenhum commit no repositório**, o GitHub pausa o
  agendamento automático e avisa por e-mail. Para reativar, vá em Actions e
  reabilite, ou faça qualquer commit.
- **Se chegar um aviso de “modo reserva” no canal**, é sinal de que a API do
  SRE não respondeu. Os alertas individuais ficam suspensos nesse intervalo
  pra evitar flood. O aviso é repetido no máximo a cada 12h; se persistir
  por mais de algumas horas, a API do SRE pode ter mudado e o robô precisa
  de revisão (rode `inspecionar` para diagnosticar).
- **Para diagnóstico**, rode o modo `inspecionar`: ele mostra no log se a API
  respondeu, os status encontrados, a contagem por tipo e exemplos de mensagem.

-----

## Aviso

Ferramenta informativa, baseada em dados públicos da CVM. Não constitui
recomendação de investimento. Confirme sempre as informações na fonte oficial
antes de tomar qualquer decisão.