# Navegação de referência — identificada antes da alteração da interface

> Atualização autorizada no refinamento visual: a prévia agora usa somente as
> abas Facebook e Instagram e dois seletores compactos, Ano e Período, com
> opções decrescentes. Abre no trimestre civil atual de Brasília. Consolidado
> permanece no código, fora das abas. O mapeamento abaixo registra a referência
> original; não descreve os controles atuais da prévia.

A referência é o `index.html` existente na raiz; ele permanece inalterado.

| Responsabilidade atual | Componentes / funções | Comportamento preservado na prévia |
|---|---|---|
| Cabeçalho | `header.top`, `.wrap`, `.top-in`, `.brand` — linhas 282–296 | Marca, título e ações no topo; sem menu lateral |
| Rede social | `#tabs`, `render()` — linhas 1457–1469 | Abas horizontais **Facebook → Instagram → Consolidado**, com `role=tablist`, `aria-selected` e foco móvel |
| Estado do recorte | `st.plat`, `st.year`, `st.quarter` | Seleções de rede, ano e trimestre mantidas ao explorar conteúdos |
| Ano | `renderPainel()`, botões `.pill[data-ysel]` — linhas 1015–1017 | Botões de ano, não um seletor de datas |
| Trimestre | `.pill[data-qsel]`, `dashboardQs()`, `qRange()` — linhas 1018–1023 e 1077–1081 | Quatro botões na mesma faixa do ano; indisponíveis desabilitados, trimestre corrente identificado |
| Mudança de ano | Evento em `data-ysel` — linha 1476 | Preservar trimestre quando disponível; senão, selecionar o último disponível do ano |
| Teclado | Evento `keydown` em `#tabs` — linhas 1488–1495 | Setas, Home e End nas abas; botões de ano/trimestre com Tab e Enter/Espaço |
| Relatório | `#btnRel`, `#btnPrint` — linhas 1506–1507 | Ações no cabeçalho para alternar relatório e imprimir; sem novo menu principal |
| Celular | `.tabs` e faixa das `.pill` com `flex-wrap` | Mesmos controles e ordem, quebrando linhas; sem gaveta/hambúrguer ou filtros alternativos |

## Aplicação ao histórico

- `historico/index.html` reaproveita a organização do cabeçalho e das abas.
- `historico/app.js` conserva os atributos de seleção `data-p`, `data-ysel` e
  `data-qsel`, com funções puras para disponibilidade e troca de ano.
- Visão geral é o conteúdo do painel. Instagram e Facebook continuam sendo
  escolhidos pelas abas originais. O Consolidado mantém indicadores separados
  por rede/formato, sem somar métricas incompatíveis.
- O comparativo histórico integra o conteúdo, em bloco expansível; relatórios
  usam o botão do cabeçalho. Não são introduzidos outros menus de navegação.
- Os controles de rede/ano/trimestre permanecem no mesmo lugar também ao
  consultar o relatório, preservando o contexto selecionado.

## Estado temporal, sem inventar coleta

A planilha termina no 3º trimestre de 2026. O trimestre civil corrente pode ser
selecionado para consultar seu estado, mas **não recebe valores da API**.
Sem registros históricos nesse recorte, os indicadores são `n/d`, não zero.

O estado é calculado pelo calendário de `America/Sao_Paulo`, separadamente da
cobertura do arquivo: **Em andamento**, **Histórico** ou **Período futuro**.
São mostrados os limites civis do trimestre e o limite efetivo da referência.

**Última coleta bem-sucedida: n/d** nesta base, pois o Excel não fornece esse
registro. A data de importação é exibida separadamente e nunca é usada como
data de coleta. Sem integração autorizada, essa lacuna permanece explícita.

Um trimestre encerrado não recebe selo de cobertura completa: as conferências
validam os totais da planilha, não a ausência de lacunas na fonte original.
