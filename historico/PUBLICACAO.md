# Publicação do painel histórico

A interface principal usa os recursos de historico/, sem duplicar os JSONs.
Fonte aprovada: Unifesspa_RedesSociais_2024-2026.xlsx; disponibilidade até setembro de 2026.
O trimestre corrente continua selecionável, com dados parciais e n/d quando não integrado.
A data de importação não é apresentada como coleta da API.

A interface anterior foi preservada integralmente em ../painel-anterior.html.
Commit de origem: ce573c6fd8cfd48f11bb411dc5d48e6854c8b4f5
SHA-256 da interface anterior: f16ed04342b83c138303bbe04c656a83ace3eeb0328183ece3622a577f31316e

Os arquivos dados/ e os coletores/workflows existentes não integram esta alteração.
As coletas automáticas continuam independentes; esta interface não mistura esses dados ao histórico.
Importador e documentação de validação foram preservados; não houve nova importação.

Para visualizar: python -B -m http.server 8767 --bind 127.0.0.1 na raiz.
A restauração da interface anterior, se autorizada, exige apenas recolocar o backup como index.html.


## Interface institucional aprovada
A página principal utiliza historico/interface/ e reutiliza os JSONs, a marca e o CSS-base existentes.
A prévia aprovada foi promovida sem reimportação ou alteração de dados.
O painel histórico anterior continua disponível em historico/index.html, com seus scripts e estilos originais.
Ponto de recuperação completo anterior à publicação:
5109ab5af729389dcb9da7803cd60f841315d8c9 (origin/main na preparação).
O Git preserva a árvore completa dessa versão; não é necessário duplicar seus dados ou recursos.
O backup painel-anterior.html permanece disponível e inalterado.
Relatórios e exportações são gerados localmente no navegador; o texto editorial não é enviado a serviços externos.
