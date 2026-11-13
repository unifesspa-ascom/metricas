# Diagnostico controlado da Meta API

## Evidencia remota em 13/11/2026

Antes desta preparacao, a API de branches retornou somente `main`, em
`a8771390331119aef346236aab363846c9e058e9` (03:41:36 de Brasilia).
O GitHub retornou 422 / No commit found para
`020974a55c5a7002a898ac4ba5387bed1817ec88`.
A branch `fix/account-metrics-safety` nao estava publicada. A arvore completa
nao continha `scripts/diagnose_account_metrics.py` nem testes deste diagnostico.
Os coletores, dados, testes de posts/stories e quatro workflows de producao
estavam disponiveis. Esta implementacao e uma reconstrucao independente,
nao a recuperacao do trabalho local inacessivel.

Base usada: o commit remoto acima, sem incorporar alteracoes locais presumidas.
Somente quatro novos arquivos sao publicados na branch
`diagnostic/account-metrics-online`: script, testes, workflow e este documento.
Nenhum workflow existente, arquivo de dados ou main e alterado.

## Plano e protecoes

O ID da pagina e extraido por AST do valor padrao nao sensivel de `PAGE_ID`
em `scripts/update_metrics.py`, sem importar/executar o coletor e sem imprimir
esse ID. Nenhum ID e inventado. A versao v21.0 e os nomes de metricas sao
os do coletor remoto; a disponibilidade atual dessas metricas ainda precisa
ser diagnosticada. Nao ha garantia de suporte nem equivalencia entre metricas.

Janela: ontem em Brasilia, de D ate D+1, usando as mesmas datas do coletor.
12 GETs planejados: oito sondagens Facebook (seis nomes principais e dois
alternativos, sempre independentes), uma descoberta de conta Instagram
vinculada e tres sondagens Instagram (reach, accounts_engaged,
profile_links_taps). Sem conta vinculada valida, sao nove GETs e tres skips.
Nao consulta posts/stories, nao pagina, nao soma nem publica metricas.

Teto rigido de 16 tentativas por processo, inclusive erros. Sem retries e sem
seguir redirects de qualquer host. Host HTTPS e caminhos sao fixos/validados;
token apenas no cabecalho Authorization, nunca na URL. Timeout de 20 segundos
por tentativa e leitura limitada a 256 KiB. Nenhum corpo bruto, valor de metrica,
ID, cursor, URL, token ou texto de excecao e incluido na saida. A saida contem
somente nomes fixos de sondagens, estados e contagem de requisicoes.
`data_present` significa somente lista nao vazia; nao valida significado ou
integridade das metricas. Falha nao vira zero. Erros de sondagem aparecem no
relatorio mesmo quando o processo termina com sucesso.

Sem argumento explicito, o script apenas valida a configuracao, sem rede.
O workflow tem somente workflow_dispatch, confirmacao false por padrao,
contents:read, checkout sem credenciais persistidas e fixado no SHA da execucao,
sem cron, commit, push ou artefatos. Executa os testes antes de disponibilizar o
token ao unico passo real. Restringe a branch isolada e run_attempt == 1;
para repetir conscientemente, inicie outra execucao manual, nao use Re-run.
O limite e por execucao, nao um limite cumulativo entre execucoes manuais.

Foram executados 11 testes offline com respostas simuladas e o preflight sem
rede. Nao houve consultas a Meta nem acesso ao valor do secret. O nome
FB_PAGE_ACCESS_TOKEN e confirmado pelo workflow publicado; sua existencia,
validade e permissoes nao foram verificadas em endpoint de secrets.

## Execucao pelo navegador

GitHub exige que o arquivo com workflow_dispatch exista na branch padrao para
habilitar o disparo manual. Como este workflow novo fica somente na branch
isolada, o botao Run workflow pode nao estar disponivel. Nao faca merge nem
altere a branch padrao para contornar isso. Nao use workflows existentes de
coleta/backfill para este diagnostico.

Se futuramente o workflow for disponibilizado na branch padrao por uma decisao
separada e revisada: Actions > Diagnostico controlado de conta Meta > Run workflow,
selecione diagnostic/account-metrics-online, marque confirm_read_only e execute.
Ele usa o secret Actions existente FB_PAGE_ACCESS_TOKEN, sem revelar o valor.
Essa disponibilizacao na branch padrao NAO foi feita nesta tarefa.

Alternativa sem merge: abra a branch isolada no GitHub, depois Code > Codespaces
> Create codespace on diagnostic/account-metrics-online. O VS Code abre no
navegador; nao e necessario instalar nada no computador. No terminal online:

```sh
python3 -B -m unittest discover -s tests -p 'test_diagnose_account_metrics.py' -v
python3 -B scripts/diagnose_account_metrics.py
```

Confira que os 11 testes passaram e o preflight informa zero uso de rede.
O secret de Actions NAO e automaticamente disponibilizado ao Codespaces.
Para executar por Codespaces, configure conscientemente um secret de Codespaces
com o mesmo nome FB_PAGE_ACCESS_TOKEN e acesso somente a este repositorio,
na interface de configuracao do GitHub; reinicie o Codespace para recebe-lo.
Use o valor original guardado com seguranca: o GitHub nao permite recuperar o
valor do secret Actions existente. Nao imprima o ambiente e nao cole o token
em comandos, logs, chat ou arquivos. Se o valor nao estiver disponivel, pare no
preflight; nao remova as protecoes nem tente extrair o secret de Actions.
Apos revisar as protecoes e decidir iniciar a consulta real, execute UMA vez:

```sh
python3 -B scripts/diagnose_account_metrics.py --execute-read-only
```

Essa acao faz as consultas reais; ela nao foi executada nesta preparacao.
Sem valor original para um secret Codespaces, a execucao fica pendente de uma
solucao revisada para Actions na branch padrao, sem merge automatico.

Referencias:
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow
- https://docs.github.com/en/codespaces/managing-your-codespaces/managing-secrets-for-your-codespaces
