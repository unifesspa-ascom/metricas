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
