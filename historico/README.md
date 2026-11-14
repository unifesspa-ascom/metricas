# Observatório histórico — versão de teste

Interface estática independente do painel atual. Usa somente o arquivo oficial
`Unifesspa_RedesSociais_2024-2026.xlsx`. Não lê os JSONs da API, não chama a Meta
e não altera coletores ou workflows.

## Visualizar

Na raiz do projeto, execute:

```powershell
python -B -m http.server 8765 --bind 127.0.0.1
```

Abra **http://127.0.0.1:8765/historico/**. A versão existente continua em
http://127.0.0.1:8765/. Use HTTP; abrir o HTML diretamente pelo explorador de
arquivos bloqueia a leitura dos JSONs em alguns navegadores.

Não há build, serviços pagos, CDN, fonte remota ou dependência de licença
pessoal. A interface funciona em GitHub Pages sob o caminho `historico/`, mas
não foi publicada. O servidor local escuta apenas em loopback.

## Atualizar a importação

O importador requer Python 3.10+ e `openpyxl` 3.x (já disponível no ambiente).
Se necessário, instale a dependência de importação com `python -m pip install openpyxl`.

```powershell
python -B -X utf8 scripts/import_historical_excel.py --input "$env:USERPROFILE\Downloads\Unifesspa_RedesSociais_2024-2026.xlsx"
```

Os arquivos são gerados em `historico/data/`; o relatório legível fica em
`historico/VALIDACAO.md`. A planilha original é somente lida. O importador
recusa usar `dados/` como destino. Saída 2 sinaliza divergências ou quarentena,
com os resultados preservados para revisão; saída 0 não certifica origem API.

## Contrato e critérios

- `manifest.json`: fonte, SHA-256, períodos, metodologia e conferências.
- `facebook.json`, `instagram-posts.json`, `instagram-stories.json`: registros
  individuais, IDs como strings, links, formato, métricas e proveniência por célula.
- `summary.json`: valores calculados, valores declarados e cobertura por métrica.
- `validation.json`: todas as 240 comparações e ocorrências da importação.
- `quarantine.json`: versões conflitantes e linhas que exigem revisão.

Datas textuais desta planilha foram identificadas como **MM/DD/YYYY HH:mm**, a
partir de exemplos não ambíguos, e conferidas contra o trimestre declarado.
Nenhum fuso foi presumido. IDs textuais longos permanecem exatos. Duplicatas
idênticas são consolidadas com as linhas de origem; conflitos são separados
para revisão. URLs iguais com IDs distintos são preservadas e sinalizadas.

Zero é um valor informado na planilha; vazio vira `null`/`n/d`. Não há fallback
entre visualizações e impressões. Alcance somado por publicação não é alcance
único de conta. Não se calculam taxas de engajamento ou consolidações de
indicadores incompatíveis. Valores individuais podem ser acumulados desde a
publicação, sem comprovar em qual período ocorreram todas as visualizações.

O comparativo anual usa trimestres comuns — normalmente Q1–Q3 — para não
comparar 2026 parcial com 2024/2025 completos. A área Relatórios permite CSV e
impressão A4 / PDF pelo navegador. Links antigos de stories podem ter expirado.

## Testes offline

### Exploração editorial

O Instagram abre em **Todas as postagens**. A quantidade reúne feed e stories
por ID único; filtros de conteúdo e formato não alteram os documentos de origem.
Collab é uma característica independente do formato e só é identificada por
campos explícitos. Esta base não oferece essa identificação.

Visualizações, curtidas e demais métricas permanecem separadas entre feed e
stories, inclusive nos gráficos, comparativos e rankings. O alcance é exibido
por postagem em “Ver métricas”, sem cálculo de alcance único do período.

O ranking de interações calcula contagens absolutas com todos os componentes
válidos: feed = curtidas + comentários + compartilhamentos + salvamentos;
stories = curtidas + respostas; Facebook = reações + comentários +
compartilhamentos. Um componente ausente torna a contagem indisponível.
O campo original `engagement` não é reinterpretado nem usado como taxa.

O gráfico mensal mantém os três meses do trimestre e parte de zero. Meses sem
registros ficam n/d. Métricas acumuladas são agrupadas pelo mês da publicação,
sem atribuir sua ocorrência àquele mês. O comparativo fica aberto e usa o mesmo
trimestre em 2024, 2025 e 2026; percentuais ficam indisponíveis enquanto a
comparabilidade da cobertura não estiver comprovada.

O executivo mostra três destaques por grupo. Em Todas as postagens, seus cards
compactam a contagem de feed e stories sem somar suas visualizações.

A navegação mantém as abas Facebook e Instagram e usa seletores compactos
de Ano e Período, ordenados do mais recente ao mais antigo. A abertura usa
o trimestre civil atual em Brasília. Consolidado fica temporariamente fora
da navegação, com o código preservado. Não há menu lateral.
O comparativo histórico está em um bloco expansível do conteúdo. O cabeçalho
oferece relatórios executivo e detalhado, preservando o período e a rede.
O executivo usa três destaques por visualizações e descreve somente valores
dos registros; o detalhado mantém a conferência e a metodologia da fonte.
Imprimir / PDF imprime a visualização selecionada, com margens A4 preservadas.

O trimestre civil corrente permanece selecionável mesmo sem dados na fonte.
Nesse caso, mostra “Em andamento · Dados parciais” e indicadores n/d.
A última coleta bem-sucedida é n/d: a planilha não fornece essa informação;
a data de importação não é uma data de coleta. Trimestres encerrados recebem
o rótulo “Histórico”, sem declaração de cobertura completa.

```powershell
python -B -X utf8 -m unittest discover -s tests -p test_historical_import.py
node --test tests/test_historical_dashboard.cjs
```

Nenhum desses testes executa coletores ou utiliza a Meta API. A nova interface
não muda os filtros, a impressão ou os cálculos do painel anterior.
