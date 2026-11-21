# Recuperacao controlada das metricas de conta Facebook

## Base remota, sem presumir o notebook

Preparacao baseada exclusivamente na main publicada
fb040ec0538f9aa7c4f324431d7b3cc23553971a.
A arvore remota e os scripts/testes/workflows foram inspecionados no GitHub.
Nao usamos nem afirmamos recuperar alteracoes da worktree local do notebook.
Os testes existentes publicados eram de posts, stories e protecao dos workflows;
esta branch adiciona contratos especificos para Insights de conta Facebook.

Evidencia real fornecida pelo usuario na segunda rodada: legacy_path HTTP 400 /
Meta 2500, metric_parameter HTTP 200 / data_present, exatamente 2 GETs.
Isso confirma o formato de parametros para page_media_view naquele contexto.
Nao comprova as outras cinco metricas, equivalencia com nomes antigos, precisao
dos valores, datas, cobertura ou disponibilidade futura. Nao houve nova chamada
a Meta durante esta preparacao.

## Inventario dos formatos

Na main inspecionada havia dois pontos executaveis com
/insights/{metric}/day:
- scripts/update_metrics.py: fetch_facebook_metrics, antigo /insights/{name}/day;
- scripts/backfill_metrics.py: fetch_fb_insights_month, mesmo formato antigo.

Ambos agora delegam ao novo scripts/facebook_account_metrics.py, que constroi
somente /{page}/insights?metric=...&period=day&since=...&until=....
A versao v21.0 e os seis nomes principais publicados foram mantidos.
Nenhum nome novo foi declarado equivalente ou aprovado.

update_posts.py, backfill_posts.py e update_stories.py usam endpoints diferentes
para posts/midias, nao esse formato diario de conta. Permanecem intactos.
Os diagnosticos antigos, em commits imutaveis referenciados pelos launchers,
continuam reproduzindo formatos antigos como evidencia/controlos. Nao foram
reescritos nem seus launchers atualizados. Nao use esses launchers para validar
a nova implementacao: continuam fixados no codigo antigo.

## O que foi corrigido e protegido

- As seis metricas sao independentes. Foram removidos os fallbacks automaticos
  para page_impressions e page_impressions_unique: falha nao autoriza trocar a
  definicao da metrica e sobrescrever uma serie com outra.
- Transporte Facebook separado, GET somente, host HTTPS fixo, token apenas no
  cabecalho, sem retries, redirects ou seguir paging.next. Maximo de SEIS
  tentativas por instancia Client, inclusive falhas, timeout 20s, corpo 256 KiB.
  Nao ha parametro que aumente o teto. Reutilizar a instancia preserva o teto.
- Janelas de coleta limitadas a 31 dias; a proxima rodada proposta usa UM dia.
  A funcao mensal foi corrigida, mas nenhum backfill e executado/autorizado.
- Nao registra excecoes, corpos, URLs, tokens, IDs, cursores ou valores brutos.
  Erros saem como categorias fixas. HTTP nao implica sucesso semantico.
- Exige envelope dict sem error, data lista, uma unica metrica com name exato,
  period=day e values lista. Uma segunda metrica ou pagina pendente invalida a
  resposta completa: nenhum resultado parcial e aceito como completo.
- Cada valor precisa ser inteiro genuino, nao bool, string, float, null, objeto
  ou negativo, dentro do intervalo de inteiros exatos do JavaScript. Zero
  explicito e valido. Ausencia de data/valor nao e substituida por zero.
- end_time precisa ser timestamp ISO real com fuso; duplicatas do mesmo instante
  sao rejeitadas. A validacao e atomica para toda a resposta da metrica.
- Foi eliminado o uso Facebook do extrator que comparava apenas os primeiros
  dez caracteres de end_time e usava o ultimo valor caso nao achasse o dia.
  O extrator legado Instagram nao foi validado/corrigido nesta etapa; seu uso
  em publicacao de conta continua bloqueado.
- O adaptador de conta Facebook agora coleta apenas Insights. Contagens de
  publicacoes/stories nao sao consultadas nem inventadas nesse caminho. Isso
  nao altera suas automacoes independentes de producao.

## Datas: regra candidata, nao uma decisao silenciosa

APPROVED_DAY_ZONE continua None. Uma resposta numericamente valida NAO gera
serie gravavel se a politica de data estiver pendente. O chamador recebe
categoria date_policy_unreviewed e serie vazia, preservando historico.

A funcao map_days testa uma regra candidata explicita: end_time e o limite
exclusivo de um dia, precisa cair a meia-noite NO FUSO ESCOLHIDO, e o dia associado
e o dia civil anterior. Nao usa end_time[:10], o fuso do computador, conversao
arbitraria para Brasilia ou subtracao fixa de 24 horas em UTC. Exige que a data
resultante esteja na janela [since, until), sem duplicatas. As fixtures testam
Brasilia e transicoes DST em Los Angeles apenas para provar o algoritmo, nao
para declarar qual fuso a Meta realmente usa para esta pagina/versao/metrica.

Uma metrica diaria agregada em outro fuso nao pode ser transformada num total
diario de Brasilia apenas mudando seu rotulo. Se a Meta usar outra fronteira,
a politica do painel precisa registra-la e ser revisada; nao sera possivel
reconstruir outro agregado diario somente desses totais.

Antes de aceitar uma politica, confirmar end_time real, fuso/fronteira do
intervalo e relacao since/until, inclusive um par de dias adjacentes. Nao
escolher automaticamente um fuso a partir do offset retornado: offset nao
e um identificador de zona nem valida a semantica do agregado.

## Historico e gravacao continuam bloqueados

update_metrics.main sem --dry-run termina ANTES de coletar e informa
account_publication_suspended. update_json bloqueia incondicionalmente qualquer
chamada nao dry-run antes de abrir ou modificar o documento. Nao existe flag
ou variavel de ambiente que libere a gravacao nesta proposta.

backfill_metrics.main termina antes de qualquer coleta ou leitura/escrita de
historico, mesmo com --dry-run. As funcoes de consulta so sao exercitadas com
mocks nos testes. Nenhum backfill, reparo historico ou publicacao foi realizado.

append_candidate e uma funcao PURA para revisar uma futura mesclagem: devolve
copia, preserva campos/series desconhecidos e todas as datas antigas; adicao
ausente nao apaga nada. Aceita zero legitimo e repeticao identica; recusa
sobrescrever data com valor diferente ou tipo historico divergente. Valida todas
as adicoes e nao modifica o original se alguma falhar. Nao grava arquivos e NAO
e um mecanismo de publicacao habilitado. Nenhuma das rotinas legadas de escrita
foi liberada com base somente nessa funcao.

O workflow de producao continua byte a byte igual: publica posts.json pelo
guard existente e isola o job de conta. Nao reativa metricas.json.
Se esta branch fosse mesclada como esta, o job account-observation legado, que
chama update_metrics sem dry-run, falharia fechado devido ao bloqueio explicito;
o job independente de posts nao depende dele. Uma adaptacao futura desse job
para observacao somente deve ser outro passo revisado, sem alterar posts/stories.
O --dry-run legado completo ainda contem consultas Instagram; nao e o launcher
controlado para a proxima rodada Facebook e nao deve ser usado para isso.

## Validacao offline

18 testes novos cobrem queries, teto de seis tentativas inclusive falhas, sem
retry/redirect/paginacao, erro sem corpo bruto, formatos/nomes/periodos, timestamps
invalidos/duplicados, valores invalidos/ausentes, zero legitimo, datas fora da
janela, politica pendente, meia-noite exclusiva, DST e virada de ano, copia
historica sem sobrescrita, validacao atomica, integracao dos dois coletores e
bloqueio de gravacoes/backfill antes de acesso ao disco ou rede.

Os testes publicados de posts/stories/workflows tambem foram executados: total
164 testes Python aprovados. Dados publicados foram usados apenas como fixtures
de leitura e permaneceram inalterados. Nenhum JSON ou workflow e incluido no
diff desta branch. Dependencias de teste foram instaladas apenas no ambiente
online de trabalho, nunca no computador do usuario nem em um workflow.

## Plano para novas consultas reais, sob autorizacao separada

NAO executar esta rodada nesta preparacao. Preparar novo launcher manual,
fixado no commit desta implementacao revisada e com hashes dos arquivos,
contents:read, checkout sem credenciais persistidas, testes/preflight antes do
secret, confirmacao false por padrao e sem publicacao/artefatos brutos.
Sera um novo PR separado; nenhum workflow existente e trocado automaticamente.

Rodada A: SEIS GETs totais, mesmo dia fechado e formato de parametros:
1. page_media_view (controle que ja respondeu, agora com validacao de estrutura);
2. page_total_media_view_unique;
3. page_post_engagements;
4. page_total_actions;
5. page_daily_follows_unique;
6. page_views_total.

Sem alternativos depreciados e sem retentativas em falhas. Cada resultado deve
informar apenas estado por metrica: HTTP/codigo numerico sanitizado se necessario,
estrutura valida, inteiro valido, presenca de zero, quantidade de amostras,
limite diario compativel e politica de data pendente/aprovada. Nao publicar valores,
IDs, corpo ou cursores. Corpos so em memoria para validacao. O runner precisara
preservar a separacao entre parse_observations e map_days; nao podera tratar
serie vazia por politica pendente como metrica sem dados.

Rodada B: depois de revisar A, aprovar regra temporal/documentacao e o significado
das metricas candidatas. Fazer no maximo DOIS GETs de page_media_view em dias
adjacentes, em instancia nova com teto explicitamente reduzido no launcher
revisado, para conferir fronteira, rotulos e cobertura entre janelas. Se forem
necessarios mais testes, outra autorizacao com numero e objetivos explicitos;
nao ampliar a mesma rodada silenciosamente. O teto do modulo e 6; o launcher B
ainda precisa impor seu teto menor de 2 e nao foi implementado nesta etapa.

Somente metricas com disponibilidade e semantica confirmadas podem entrar numa
allowlist de publicacao, com proveniencia nome/versao/fuso. Mudanca de definicao
pede migracao explicita; nunca usar nomes alternativos sob a mesma chave por
conveniencia. Ausente, erro ou sem cobertura continua ausente, nunca zero.

Em paralelo, validar separadamente Instagram: estrutura total_value versus
values, valores e janela temporal; sucesso HTTP/data_present nao e aprovacao.
Nao reativar publicacao conjunta antes dessas verificacoes.

Depois: construir candidato fora do repositorio, comparar com baseline, validar
que somente datas novas aprovadas foram acrescentadas e que historico/campos
manuais foram preservados. Nao recalcular/sobrescrever trimestres antigos ou
series com outra definicao. Divergencia bloqueia candidato. Primeira publicacao
somente manual e revisada; observar resultados antes de propor automacao.
A futura escrita exige desenho de publicacao atomica, controle de concorrencia,
checagem independente de proveniencia/datas/valores e um PR de reativacao
separado. Nada disso e habilitado nesta branch.

Referencia oficial do formato metric/period:
https://developers.facebook.com/docs/graph-api/reference/insights/
