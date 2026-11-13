# Segunda rodada: duas sondagens Facebook, sem publicacao

## Evidencia e limites

Execucao anterior: https://github.com/unifesspa-ascom/metricas/actions/runs/43440397333
Commit da execucao: 8801144c6cb82c7195f4d7dcc24080d63267668a.
Os 12 GETs terminaram: oito http_error no Facebook; discovery ok e tres
listas com dados no Instagram. As mensagens de erro e codigos HTTP nao foram
preservados pelo script antigo. Nao e possivel recupera-los desses logs.

Isso descarta, SOMENTE naquele instante, a hipotese de token totalmente ausente
ou inutilizavel para qualquer endpoint e a de indisponibilidade total do servico.
Nao comprova read_insights, pages_read_engagement, tarefa ANALYZE ou acesso
especifico a Page Insights. Nao prova valores, cobertura ou datas corretas no
Instagram. Expiracao posterior ou escopos insuficientes continuam possiveis.

## Pesquisa oficial em 13/11/2026

A referencia oficial avisa que metricas de Page Insights foram retiradas ate
15/06/2026 em todas as versoes e passam a produzir erro de metrica invalida.
Marca page_impressions_unique como depreciada acima de v25, enquanto lista
page_media_view e period=day. Ha uma diferenca entre avisos globais e marcacoes
por versao nas tabelas: nao tratamos isso como prova da causa dos oito erros.
A pesquisa nao verificou uma lista exaustiva de todas as metricas retiradas.

Fonte: https://developers.facebook.com/docs/graph-api/reference/insights/
O acesso direto retornou 429; foram consultados os trechos indexados da propria
Meta, inclusive tabelas, requisitos e parametros. Nao usamos fontes de terceiros
como prova de disponibilidade nem contornamos o bloqueio com chamadas Graph.

A referencia documenta metric e period como parametros, leitura da edge insights,
read_insights/pages_read_engagement e tarefa ANALYZE. O formato usado pelo
coletor e pelo primeiro diagnostico foi /insights/{metric}/day. Isso sugere
investigar a representacao da metrica/periodo, sem afirmar que ela e invalida:
a referencia tambem mostra leitura de metrica no caminho, mas isso nao demonstra
que a variante completa /{metric}/day seja aceita para esta versao/conta.

A tabela oficial de erros Page identifica 100 como parametro invalido, 200 como
permissao e 190 como token OAuth invalido. Codigos isolados nao distinguem toda
causa, sobretudo metrica retirada versus outro parametro invalido.
Fonte: https://developers.facebook.com/docs/graph-api/reference/page/

## Escolha das duas sondagens

Par controlado da MESMA metrica page_media_view, ja testada na primeira rodada:
1. legacy_path: reproduz /insights/page_media_view/day com period=day;
2. metric_parameter: usa /insights com metric=page_media_view e period=day.

Nao sao duas metricas diferentes. A escolha de duas representacoes de uma
metrica evita confundir troca de nome com troca de formato, sem gastar chamadas
em um controle conhecido como depreciado. Versionamento v21.0, pagina, token,
janela e periodo sao mantidos. Nao e retry: sao duas sondagens predefinidas,
executadas uma vez cada, mesmo se a primeira falhar. Nao ha terceira sondagem,
alternativa automatica, descoberta de conta, consulta de permissoes ou Instagram.
Nao ha troca de versao, equivalencia presumida ou dados manuais substitutos.

Janela: ontem em Brasilia, D ate D+1, igual ao diagnostico anterior.
Isso NAO valida a fronteira do dia usada pela Meta nem a atribuicao das series.
A metrica continuar na documentacao nao garante acesso nessa versao/conta.

## Protecoes e revisao

Teto rigido de DUAS tentativas por processo, inclusive erro HTTP ou transporte.
HTTPS e hostname fixos; ID estritamente numerico e lido por AST do valor padrao
nao sensivel de scripts/update_metrics.py no commit fixado. O coletor nunca e
importado/executado. Nenhum ID e impresso. Sem retries, redirects ou paginacao.
Token exclusivamente no cabecalho, nao na URL. Timeout 20s e limite 64 KiB
por corpo. Erro de transporte nao gera traceback ou texto da excecao.

Corpos HTTP de erro sao lidos somente em memoria e de forma limitada para
extrair error.code e error.error_subcode. Somente int genuino, nao bool/string/
float, entre 0 e 2147483647 e aceito. Todos os demais campos sao descartados:
message, type, error_data, fbtrace_id, URL, headers, IDs e valores de metricas.
Redirects nem sequer tem o corpo lido. JSON malformado/ausente nao remove o
status HTTP. Codigo/subcodigo ausente ou invalido e null, nunca zero inventado.
Saida: rotulos fixos, estado local, http_status, meta_code, meta_subcode e contador.
O processo pode retornar sucesso mesmo com ambas as sondagens falhando: leia o
relatorio, nao somente o selo verde do Actions.

Sem --execute-read-only ha somente preflight sem rede/secret. Testes offline
cobrem limite, URL fixa, par de consultas, falhas, somente campos numericos,
JSON/oversize, erros de leitura, redirects, nao paginacao, sanitizacao,
ausencia de escrita, AST sem import e secret ausente. A opcao de execucao real
so e usada nos testes com transporte simulado ou secret ausente.

O lancador separado sera adicionado por PR de UM arquivo novo, sem substituir
o lancador anterior nem workflows de producao. Checkout e action fixados por
SHA completo; sparse sem cone de script, teste e configuracao; hashes SHA-256
literais; Python -I -S -B; testes e preflight antes do secret; contents:read;
persist-credentials:false; confirmacao false por padrao; somente main,
workflow_dispatch e primeira tentativa. Nao recebe branch/SHA arbitrario.
Nao ha cron, upload de artefato, backfill, commit, push ou publicacao.

O codigo que recebe o token necessariamente pode usa-lo. contents:read limita
o GitHub, nao o token Meta. O runner e a action fixada tambem sao confiados;
mesmo runner entre testes e execucao nao e uma sandbox contra testes maliciosos.
Por isso os testes tambem sao revisados/fixados. Sparse nao e controle de egress
nem filesystem somente leitura. A ausencia de JSONs e de codigo de escrita
reduz o escopo, sem prometer uma restricao imposta pelo SO.

## Como interpretar a unica rodada

| Resultado | O que permite concluir |
| --- | --- |
| legacy falha, parametro tem dados | Evidencia forte de problema no formato antigo para esta metrica/janela; nao prova recuperacao das demais sete. |
| Ambas 400 / codigo 100 | Parametro/metrica/versao continuam possiveis; nao comprova descontinuacao. |
| Codigo 200 ou HTTP 403 | Investigar permissoes, tarefa da pagina e acesso do app; nao e prova de metrica retirada. |
| Codigo 190 | Investigar token/expiracao; sucesso anterior nao garante validade atual. |
| 429 ou 5xx | Throttling/servico possiveis; nao repetir automaticamente. |
| Ambas tem dados | Falha anterior pode ser temporal; ainda e preciso validar valores e periodo. |
| Subcodigo ausente, corpo invalido ou ambas genericas | Evidencia insuficiente; planejar outra investigacao revisada, sem tentar substituicoes cegas. |

HTTP e codigo Meta sao campos distintos (HTTP 200 pode conter error).
Duas sondagens nao prometem identificar definitivamente a causa nem quais
metricas sao recuperaveis. Nenhuma hipotese especifica Facebook foi descartada
por nova evidencia real nesta preparacao: nao houve novas chamadas.

## Execucao futura somente pelo navegador

1. Revise o commit-fonte linkado no PR do novo lancador e seu diff de um arquivo.
   Confira o SHA completo e hashes do YAML. Nao mescle a branch-fonte na main.
2. Somente apos autorizacao separada, mescle o PR do lancador. O merge nao dispara
   o diagnostico. Nao mescle automaticamente e nao execute nesta preparacao.
3. Actions > Facebook error diagnostic (two fixed probes) > Run workflow > main.
4. Marque confirm_read_only e clique Run workflow UMA vez. O secret Actions
   FB_PAGE_ACCESS_TOKEN existente sera usado; nao e necessario ver seu valor.
5. Aguarde integridade, 14 testes, preflight, verificacao de alteracoes e as duas
   sondagens. Compartilhe apenas o link da execucao. Nao use Re-run nem o
   diagnostico anterior, que gastaria mais chamadas. Cada novo run e outra rodada.
6. Avalie os codigos antes de autorizar qualquer coleta/correcao de producao.
   Valores e interpretacao temporal do Instagram continuam pendentes.

A publicacao automatica de dados/metricas.json continua suspensa pelo desenho
ja existente. Nao alteramos coletores, JSONs ou workflows de producao para esta
proposta e nao reativamos a publicacao de conta. Outros agendamentos preexistentes
nao sao modificados ou cancelados por este diagnostico.
