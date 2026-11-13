# Proposta: lancador manual Meta na main

## Resultado e escopo

E viavel usar integralmente GitHub Actions e o secret Actions existente
`FB_PAGE_ACCESS_TOKEN`. Nao e necessario conhecer/copiar seu valor para
Codespaces. O workflow novo deve existir na main para workflow_dispatch.
Esta proposta NAO altera main e NAO dispara Actions ou consultas Meta.

Branch de proposta: `proposal/meta-actions-pinned-launcher`, baseada na main
`a8771390331119aef346236aab363846c9e058e9` verificada em 13/11/2026.
A branch contem o workflow novo e estes artefatos de revisao; nao e baseada
na branch de diagnostico e nao incorpora seus scripts/testes/docs na main.
Para a implantacao minima, copie SOMENTE
`.github/workflows/meta-account-diagnostic-pinned.yml` conforme instrucoes abaixo.
Nao faca merge da branch `diagnostic/account-metrics-online` nem cherry-pick
do commit de diagnostico inteiro.

## Como funciona

O lancador permite somente workflow_dispatch, com confirmacao booleana obrigatoria
false por padrao. O job aceita somente o repositorio original, refs/heads/main e
primeira tentativa. A selecao de branch do GitHub nao seleciona o codigo:
qualquer selecao diferente da main e recusada pelo job. Nao ha input de branch,
SHA, URL, caminho ou comando. Nao ha cron, push, pull_request ou workflow_call.
A concorrencia usa o mesmo grupo do diagnostico anterior; cancelamento automatico
desativado e reexecucao do mesmo run bloqueada. Cada novo disparo manual pode
fazer mais 12 consultas: nao e um limite cumulativo entre runs.

O checkout usa action fixada em SHA completo, repositorio fixo e commit
`de9d2cb3df1ed85641d30c26aeed2f83485311c7` fixo. Nao usa github.sha, uma branch
mutavel, inputs ou conteudo da main para executar Python. `path: diagnostic`
isola o checkout no runner; defaults.run.working-directory aponta para ele.
`persist-credentials: false` evita deixar o GITHUB_TOKEN na configuracao Git
para os scripts. Ha apenas contents:read no job; todas as outras permissoes
configuraveis ficam desabilitadas. GITHUB_TOKEN nao e fornecido ao Python.

Sparse-checkout sem cone materializa apenas quatro arquivos do commit revisado:
- script do diagnostico;
- teste do diagnostico;
- scripts/update_metrics.py, lido SOMENTE como texto/AST para obter o ID padrao;
- workflow antigo do diagnostico, lido SOMENTE como texto pelo teste existente.

O arquivo YAML antigo nao e chamado: fazer checkout de um workflow nao o executa.
Nenhum coletor de producao e importado ou executado. Nenhum JSON e materializado.
Alem da verificacao do SHA, os quatro arquivos tem SHA-256 literal verificado
antes de executar qualquer teste. Submodules e LFS estao desativados.
Sparse-checkout reduz os arquivos presentes no diretorio de trabalho; objetos
Git e metadados ainda existem em .git e isso NAO e uma sandbox de seguranca.

Testes e preflight usam o Python do runner em modo -I -S -B (sem caminho de
imports do diretorio corrente, configuracoes PYTHON*, site-packages ou pyc).
Nao ha instalacao de dependencias nem actions locais. Os 11 testes do commit
fixo passam antes do preflight sem rede. Falha em qualquer passo interrompe a
sequencia: nenhum continue-on-error, always() ou fallback disponibiliza o token.
Uma checagem Git apos os testes/preflight recusa alteracoes rastreadas ou
novos arquivos nao ignorados. O secret aparece uma unica vez no YAML, no env
do passo final, e nao em checkout, job, testes, argumentos ou arquivos.

O processo final usa exec e so executa o script de diagnostico revisado.
Este script faz oito sondagens Facebook, uma descoberta Instagram e tres
sondagens Instagram: 12 GETs, ou nove se nao houver conta vinculada valida.
Teto de 16 tentativas por processo, sem retry, redirects ou paginacao, com
HTTPS fixo e token no cabecalho. Logs contem nomes fixos, estados e contagem;
nao contem tokens, IDs, cursores, URLs ou respostas brutas. Nenhum dado de
conta e gravado. A janela e ontem em Brasilia e os nomes/versao da API sao
os do codigo revisado. data_present nao prova exatidao da metrica; erros por
sondagem aparecem no relatorio mesmo se o job ficar verde.

## Revisao de risco

- O script final consegue ler e usar o token integral. contents:read restringe
  o token GitHub, NAO reduz os poderes do token Meta. A seguranca depende da
  revisao do codigo fixo, alem das restricoes GET implementadas nele.
- Revisados: construcao de URLs/cabecalho, bloqueio de redirects, ausencia de
  retries/paginacao, contagem inclusive em falhas, supressao de excecoes/corpos,
  imports e leitura AST da configuracao. Os testes usam mocks, nao o token real.
- Testes e processo final usam o mesmo runner efemero. Restricao do secret ao
  ultimo passo nao torna codigo de teste malicioso seguro: ele poderia preparar
  persistencia no runner. Por isso testes tambem sao fixados e revisados, nao
  provenientes de PR, input, cache ou artefato. Nao ha promessa de sandbox.
- A action fixada de checkout e o runner hospedado pelo GitHub tambem sao parte
  da base de confianca. A imagem ubuntu-24.04 e mantida pelo GitHub, nao imutavel.
  O checkout pode executar sua limpeza pos-job, mas nao recebe o secret Meta.
- Quem pode alterar/mesclar workflows da main pode mudar o codigo que recebe
  secrets. Os guards deste YAML nao protegem contra um mantenedor que os remova.
  A resposta remota mostrou main sem protecao de branch classica; regras de
  rulesets nao foram auditadas. Revisao/protecao de alteracoes em workflows e
  recomendada como controle administrativo separado; nao foi modificada aqui.
- O mascaramento de logs do GitHub e auxiliar: nao e a garantia contra vazamento.
  O codigo nao imprime dados sensiveis. Logs de diagnostico devem continuar sem
  debug de HTTP, set -x, dump de ambiente ou corpo de respostas.
- Se o token estiver indisponivel, o script falha com mensagem fixa, sem consulta.
  A existencia do nome e seu uso foram vistos nos workflows; o valor, validade,
  escopos e eventual restricao do secret a environments nao foram consultados.
- Nao ha passos de publicacao, artefatos, JSON, backfill, commit, push ou merge.
  A ausencia de escrita e propriedade do codigo revisado e dos arquivos
  materializados, nao uma restricao de filesystem aplicada pelo runner.
- Mover/apagar a branch de diagnostico nao troca o SHA fixado. Mantenha a branch
  como referencia de auditoria para preservar sua disponibilidade. Para atualizar
  o diagnostico, revise novo commit/testes e altere SHA e hashes via novo PR.

## Testes executados nesta preparacao

`python3 -B -m unittest discover -s proposal -p test_pinned_workflow.py -v`

Tres testes de proposta passaram, sem skips nesta preparacao:
1. YAML analisado e contrato de seguranca validado.
2. Dez mutacoes perigosas rejeitadas (gatilhos, inputs arbitrarios, permissoes,
   guard, ref, credenciais persistidas, cone, continue-on-error, secret antecipado,
   execucao de coletor).
3. Integracao com Git real: checkout sparse de fixture local com os quatro
   arquivos identicos aos remotos e arquivos-isca; somente os quatro arquivos
   ficam materializados. Hashes, 11 testes originais, preflight e verificacao de
   alteracoes passam. Alteracao proposital do script e detectada por hash e Git.

A fixture local usa commit proprio; somente a comparacao HEAD e substituida
por esse SHA no teste de integracao. O workflow entregue conserva o SHA remoto.
Nenhuma chamada a Meta nem etapa com secret foi executada. O workflow nao foi
validado em runner Actions real nesta etapa; as politicas da organizacao ainda
podem exigir aprovacao de actions ou impedir execucao.

Para repetir os testes de proposta e necessario PyYAML e uma copia local do
commit revisado, indicada por META_DIAGNOSTIC_FIXTURE. Sem a fixture o teste de
integracao e explicitamente skipped; os testes estaticos continuam. O workflow
implantado nao depende de PyYAML nem dos testes de proposta: usa os testes
stdlib do commit fixo.

## Implantacao exclusivamente pelo navegador (acao posterior do usuario)

1. Abra o arquivo `.github/workflows/meta-account-diagnostic-pinned.yml` na
   branch de proposta, clique Raw e copie seu conteudo integral.
2. Volte ao repositorio na main > Add file > Create new file. Use exatamente
   `.github/workflows/meta-account-diagnostic-pinned.yml` e cole o YAML.
3. Em Commit changes, escolha criar uma NOVA branch e iniciar um pull request.
   Nao selecione commit direto na main. Use um nome novo para essa branch.
4. No PR, confirme em Files changed que ha exatamente UM arquivo adicionado,
   o novo workflow. Confira SHA completo, quatro hashes, confirmacao false,
   contents:read, persist-credentials:false e guard main. Os scripts, testes e
   documentacao do diagnostico devem continuar fora da main.
5. Depois da sua revisao e decisao de implantar, faca o merge desse PR de UM
   arquivo pelo navegador. Esta e a unica alteracao de main necessaria; nao foi
   feita pelo assistente. O merge nao executa o diagnostico, pois nao ha push.
6. Actions > Meta account diagnostic (reviewed commit) > Run workflow.
   Selecione main. Para iniciar chamadas reais, marque confirm_read_only e
   clique Run workflow. Sem marcar, o job e skipped. Somente essa acao manual
   posterior inicia o diagnostico. Nao use Re-run; inicie outro run manual se
   realmente desejar uma nova rodada. Cada rodada conta separadamente.
7. Acompanhe checkout, integridade, 11 testes, preflight e diagnostico. Leia apenas
   o relatorio sanitizado. Se faltar secret/permissao, nao adicione dumps de log.
   Nao e necessario abrir, copiar ou recadastrar o token existente.

Para retirar o lancador depois, remova SOMENTE o novo arquivo via outro PR;
nao altere os workflows existentes. O procedimento nao impede que workflows
preexistentes continuem seus proprios agendamentos habituais.

Referencias oficiais:
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow
- https://github.com/actions/checkout/blob/11bd71901bbe5b1630ceea73d27597364c9af683/README.md
