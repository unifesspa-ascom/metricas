'use strict';

// No dependencies or network: execute the real inline application in a VM,
// replacing only its bootstrap with test accessors in memory.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'painel-anterior.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source); // Parse the complete production script, including bootstrap.
const bootstrap = source.lastIndexOf('\nloadData(function(){');
assert.ok(bootstrap > 0);

function app(posts = {facebook: [], instagram: []}, options = {}) {
  const elements = new Map();
  function element(id) {
    if (['chart', 'bk', 'cap', 'expl-titulo', 'btnCopy'].includes(id)) return null;
    if (!elements.has(id)) elements.set(id, {src: '', innerHTML: '', textContent: '', addEventListener() {}});
    return elements.get(id);
  }
  const sandbox = {
    document: {querySelector: () => element('logo'), getElementById: element, addEventListener() {}},
    localStorage: options.storage || {getItem() { return null; }, setItem() { throw new Error('Unexpected storage write'); }},
    addEventListener() {},
    matchMedia: () => ({addEventListener() {}}),
    fetch: options.fetch || function() { throw new Error('Network forbidden'); },
  };
  sandbox.window = sandbox;
  const exports = `
    globalThis.testAPI = {
      postMetricValid, postMetricHTML, postImpressionsHTML, postRanking, topPostsHTML, fmt, kvP, engRate, resumoTexto,
      reconstruir, aplicarLocais, renderPainel, alertasHTML, relPlat, relHTML, availabilityHTML, provenanceHTML,
      publicationInventory, publicationInventoryHTML, accountCountValid, accountPeriodStatus, accountStatusEligible, comparavelP, kpiComparavel, monthBarsHTML, tabelaMensalHTML, deltaHTML, rngP, bucketize, dailyBars, miniBars, coberturaP, autoAnalise, edGet, cleanEditorialText,
      loadStories, loadData, storiesCount, storiesTime, storiesBrtDay, storiesQuarterRange, validateStoriesDocument,
      latestStoriesMetric, storiesForQuarter, storiesSummary, storiesOperationalCoverage,
      storiesPanelHTML, storiesReportHTML, storiesDailyChartHTML, dashboardQs, refreshStoriesPanel,
      setStories(value) { STORIES = value; },
      getStoriesState() { return STORIES; },
      getStoriesRequest() { return STORIES_REQUEST; },
      setPosts(value) { POSTS = value; },
      setData(value) { DATA = value; FZP = {}; },
      setBase(value) { BASE = value; },
      getData() { return DATA; },
      setReport(year, quarter) { REL.ano = year; REL.tri = quarter; },
      setState(value) { Object.assign(st, value); },
      elementHTML(id) { return document.getElementById(id).innerHTML; },
      elementText(id) { return document.getElementById(id).textContent; }
    };
  })();`;
  vm.runInNewContext(source.slice(0, bootstrap) + exports, sandbox);
  sandbox.testAPI.setPosts(posts);
  return sandbox.testAPI;
}

const begin = '2026-01-01';
const end = '2026-03-31';
const post = (id, metrics = {}, data = '2026-02-10') => ({id, data, txt: id, ...metrics});
const ids = ranking => Array.from(ranking.top, entry => entry.id);
const plain = value => JSON.parse(JSON.stringify(value));
function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
function bodyRows(markup) {
  const body = markup.match(/<tbody>([\s\S]*?)<\/tbody>/);
  return body ? [...body[1].matchAll(/<tr>([\s\S]*?)<\/tr>/g)].map(match =>
    [...match[1].matchAll(/<td(?:\s[^>]*)?>([\s\S]*?)<\/td>/g)].map(cell => cell[1])) : [];
}

test('publication validation accepts nonnegative integers and preserves global fmt', () => {
  const api = app();
  for (const value of [0, 1, 12000]) {
    assert.equal(api.postMetricValid(value), true);
    assert.equal(api.postMetricHTML(value), api.fmt(value));
  }
  for (const value of [undefined, null, false, true, '', '0', '15', -1, 1.5, NaN, Infinity, -Infinity, {}, [], new Number(1)]) {
    assert.equal(api.postMetricValid(value), false);
    assert.match(api.postMetricHTML(value), />n\/d</);
  }
  assert.equal(api.fmt(1.6), '2');
  assert.equal(api.fmt(-1), '-1');
});

test('Instagram ranks by views alone and displays legacy impressions separately', () => {
  const api = app({instagram: [post('high-impressions', {views: 2, vis: 999}), post('high-views', {views: 50, vis: 1})], facebook: []});
  assert.deepEqual(ids(api.postRanking('instagram', begin, end)), ['high-views', 'high-impressions']);
  const markup = api.topPostsHTML('instagram', begin, end);
  assert.match(markup, /<th>Visualizações<\/th><th>Impressões \(legado\)<\/th>/);
  const rows = bodyRows(markup);
  assert.deepEqual(rows.map(row => [row[2], row[3], row[4]]),
    [['high-views', '<b>50</b>', '1'], ['high-impressions', '<b>2</b>', '999']]);
});

test('zero views is eligible, missing and invalid views never fall back to vis', () => {
  const entries = [post('zero', {views: 0}), post('old', {vis: 999999})];
  const invalid = [null, false, '10', -1, 1.5, NaN, Infinity];
  invalid.forEach((value, index) => entries.push(post('invalid-' + index, {views: value, vis: 999999})));
  const api = app({instagram: entries, facebook: []});
  const ranking = api.postRanking('instagram', begin, end);
  assert.deepEqual(ids(ranking), ['zero']);
  assert.equal(ranking.total, 9);
  assert.equal(ranking.eligible, 1);
  assert.equal(ranking.missing, 8);
  assert.equal(bodyRows(api.topPostsHTML('instagram', begin, end))[0][3], '<b>0</b>');
});

test('legacy impressions are validated independently of Instagram views', () => {
  for (const value of [undefined, null, true, '0', -1, 1.5, Infinity, NaN]) {
    const api = app({instagram: [post('eligible', {views: 1, vis: value})], facebook: []});
    assert.match(bodyRows(api.topPostsHTML('instagram', begin, end))[0][4], />n\/d</);
  }
  const api = app({instagram: [post('eligible', {views: 1, vis: 0})], facebook: []});
  assert.match(bodyRows(api.topPostsHTML('instagram', begin, end))[0][4], />n\/d</);
});

test('Facebook ranks only valid vis and labels it legacy impressions', () => {
  const api = app({facebook: [post('low', {vis: 2, views: 900}), post('high', {vis: 80}), post('zero', {vis: 0}), post('missing', {views: 999}), post('invalid', {vis: '100'})], instagram: []});
  const ranking = api.postRanking('facebook', begin, end);
  assert.deepEqual(ids(ranking), ['high', 'low', 'zero']);
  assert.equal(ranking.missing, 2);
  const markup = api.topPostsHTML('facebook', begin, end);
  assert.match(markup, /por impressões legadas/);
  assert.match(markup, /<th>Impressões \(legado\)<\/th>/);
  assert.doesNotMatch(markup, /<th>Visualizações<\/th>/);
  assert.equal(bodyRows(markup).length, 3);
});

test('all other publication columns distinguish absence from zero on both networks', () => {
  for (const network of ['facebook', 'instagram']) {
    const metrics = network === 'facebook' ? {vis: 1, alc: 0, reac: 0, com: 0, comp: 0} : {views: 1, vis: 0, alc: 0, curt: 0, com: 0, salv: 0};
    const api = app({[network]: [post('missing', network === 'facebook' ? {vis: 2} : {views: 2}), post('zero', metrics)]});
    const rows = bodyRows(api.topPostsHTML(network, begin, end));
    assert.ok(rows[0].slice(4).every(cell => cell.includes('n/d')));
    if (network === 'instagram') {
      assert.match(rows[1][4], />n\/d</); // Legacy impressions zero has no provenance.
      assert.ok(rows[1].slice(5).every(cell => cell === '0'));
    } else assert.ok(rows[1].slice(4).every(cell => cell === '0'));
  }
});

test('old Instagram posts retain coverage counts without artificial ranking rows', () => {
  const api = app({instagram: [post('old', {vis: 90}), post('old-zero', {vis: 0})], facebook: []});
  const markup = api.topPostsHTML('instagram', begin, end);
  assert.match(markup, /encontradas no período: <b>2<\/b>/);
  assert.match(markup, /Com visualizações: <b>0<\/b>/);
  assert.match(markup, /Sem visualizações: <b>2<\/b>/);
  assert.match(markup, /Exibidas: <b>0<\/b>/);
  assert.match(markup, /métrica de classificação está indisponível/);
  assert.doesNotMatch(markup, /<table/);
});

test('empty periods and missing Facebook ranking metrics keep explicit counts', () => {
  for (const network of ['instagram', 'facebook']) {
    const api = app();
    const empty = api.topPostsHTML(network, begin, end);
    assert.match(empty, /encontradas no período: <b>0<\/b>/);
    assert.match(empty, /Nenhuma publicação encontrada/);
    assert.doesNotMatch(empty, /<table/);
  }
  const api = app({facebook: [post('missing'), post('invalid', {vis: null})]});
  const markup = api.topPostsHTML('facebook', begin, end);
  assert.match(markup, /Sem impressões \(legado\): <b>2<\/b>/);
  assert.match(markup, /métrica de classificação está indisponível/);
  assert.doesNotMatch(markup, /<table/);
});

test('ten-row limit applies only after filtering eligible posts', () => {
  for (const size of [1, 4, 10, 13]) {
    const entries = Array.from({length: size}, (_, index) => post('p' + index, {views: index}));
    entries.push(post('old', {vis: 999999}));
    const api = app({instagram: entries});
    const ranking = api.postRanking('instagram', begin, end);
    assert.equal(ranking.total, size + 1);
    assert.equal(ranking.eligible, size);
    assert.equal(ranking.missing, 1);
    assert.equal(ranking.top.length, Math.min(10, size));
    const markup = api.topPostsHTML('instagram', begin, end);
    assert.equal(bodyRows(markup).length, Math.min(10, size));
    assert.match(markup, new RegExp('Exibidas: <b>' + Math.min(10, size) + '</b>'));
  }
});

test('quarter boundaries are inclusive and networks never mix', () => {
  const api = app({instagram: [post('first', {views: 2}, begin), post('last', {views: 3}, end), post('before', {views: 999}, '2025-12-31'), post('after', {views: 999}, '2026-04-01')], facebook: [post('facebook', {vis: 999, views: 999})]});
  assert.deepEqual(ids(api.postRanking('instagram', begin, end)), ['last', 'first']);
  assert.deepEqual(ids(api.postRanking('facebook', begin, end)), ['facebook']);
  assert.deepEqual(ids(api.postRanking('instagram', '2026-04-01', '2026-06-30')), ['after']);
  assert.equal(api.topPostsHTML('consolidado', begin, end), '');
  assert.equal(api.postRanking('instagram', begin, end).total, 2);
});

test('rendering does not mutate frozen POSTS or its original ordering', () => {
  const posts = freeze({instagram: [post('small', {views: 1}), post('large', {views: 9}), post('old', {vis: 50})], facebook: [post('fb', {vis: 0})]});
  const before = JSON.stringify(posts);
  const api = app(posts);
  for (const network of ['facebook', 'instagram']) {
    api.postRanking(network, begin, end);
    api.topPostsHTML(network, begin, end);
  }
  assert.equal(JSON.stringify(posts), before);
});

test('account calculations and summaries remain independent of individual metrics', () => {
  const data = freeze({series: {
    facebook: {visualizacoes: {[begin]: 100, '2026-01-02': 200}, interacoes: {[begin]: 10, '2026-01-02': 20}},
    instagram: {visualizacoes: {[begin]: 50, '2026-01-02': 70}, interacoes: {[begin]: 5, '2026-01-02': 7}, alcance: {[begin]: 10, '2026-01-02': 30}},
  }, conteudo: {2026: {instagram: [{feed: 77, stories: 9}]}}});
  const api = app();
  api.setData(data);
  const cur = {a: begin, b: '2026-01-02'};
  const ref = {a: '2025-01-01', b: '2025-01-02'};
  const indicators = () => [api.kvP('facebook', 'visualizacoes', cur.a, cur.b), api.kvP('instagram', 'visualizacoes', cur.a, cur.b), api.kvP('consolidado', 'visualizacoes', cur.a, cur.b), api.kvP('instagram', 'alcance', cur.a, cur.b), api.engRate('consolidado', cur.a, cur.b), api.resumoTexto('instagram', 2026, 1, cur, ref, ref)];
  const before = indicators();
  assert.deepEqual(before.slice(0, 5), [300, 120, 420, 20, 10]);
  const originalData = JSON.stringify(data);
  api.setPosts(freeze({instagram: [post('one', {views: 999999, vis: 88888, alc: 77777})], facebook: [post('two', {vis: 999999})]}));
  api.topPostsHTML('instagram', begin, end);
  api.topPostsHTML('facebook', begin, end);
  assert.deepEqual(indicators(), before);
  assert.equal(JSON.stringify(data), originalData);
});

test('existing escaping remains in publication text and dedicated print styles are scoped', () => {
  const api = app({instagram: [post('<script>alert(1)</script>', {views: 1})]});
  const markup = api.topPostsHTML('instagram', begin, end);
  assert.doesNotMatch(markup, /<script>/);
  assert.match(markup, /&lt;script&gt;/);
  assert.match(html, /\.post-ranking table\{min-width:760px;table-layout:fixed\}/);
  assert.match(html, /\.sheet \.post-ranking table\{min-width:0;font-size:10px\}/);
  assert.match(html, /\.sheet \.post-ranking \.tw\{overflow:visible\}/);
});

function historyFixture() {
  return {atualizado: '2099-12-31', series: {
    facebook: {visualizacoes: {'2026-07-01': 100, '2026-09-30': 200}, publicacoes: {'2026-10-01': 2}},
    instagram: {visualizacoes: {'2026-07-01': 50, '2026-09-30': 70}, alcance: {'2026-07-01': 0, '2026-07-02': 20},
      interacoes: {'2026-07-01': 3}, publicacoes: {'2026-10-01': 1}, stories: {'2026-10-01': 0}},
  }, conteudo: {2026: {instagram: [null, null, {feed: 7777777, alcance: 8888888, reacoes: 9999999, stories: 4444444}],
    facebook: [null, null, {feed: 7777777}]}}, extra: {preserved: true}};
}

test('manual content is preserved in memory but cannot affect panel or PDF', () => {
  const data = freeze(historyFixture());
  const snapshot = JSON.stringify(data);
  const api = app();
  api.setData(data);
  api.setState({plat: 'instagram', year: 2026, quarter: 3});
  api.setReport(2026, 3);
  api.renderPainel();
  const panel = api.elementHTML('app');
  const report = api.relHTML();
  for (const markup of [panel, report]) {
    assert.doesNotMatch(markup, /7[.,]?777[.,]?777|8[.,]?888[.,]?888|9[.,]?999[.,]?999|4[.,]?444[.,]?444/);
    assert.doesNotMatch(markup, /Publicações e engajamento|relatório trimestral da ASCOM \(manual\)/);
    assert.match(markup, /procedência.*não está certificada/);
  }
  const table = report.match(/<table class="rel-tbl">([\s\S]*?)<\/table>/)[1];
  assert.match(table, /Produção de conteúdo<\/td><td><b>n\/d<\/b>/);
  assert.equal(api.kvP('instagram', 'publicacoes', '2026-07-01', '2026-09-30'), null);
  assert.equal(JSON.stringify(data), snapshot);
  // Even reading DATA.conteudo would fail this independent probe.
  api.setData({...historyFixture(), get conteudo() { throw new Error('Manual source accessed'); }});
  assert.doesNotThrow(() => api.relHTML());
});

test('local metric overrides and imported archives are ignored without erasing storage', () => {
  const stored = new Map([
    ['ascom-metricas-v1', JSON.stringify({atualizado: '2099-12-31', series: {instagram: {visualizacoes: {'2026-07-01': 9999999}}}})],
    ['ascom-metricas-arquivos-v1', JSON.stringify([{plat: 'instagram', key: 'visualizacoes', nomeMetrica: 'MANUAL', rows: {'2026-07-01': 9999999}}])],
  ]);
  const before = [...stored];
  const storage = {getItem(key) { return stored.get(key) || null; },
    setItem() { throw new Error('Storage write forbidden'); }, removeItem() { throw new Error('Deletion forbidden'); }, clear() { throw new Error('Clear forbidden'); }};
  const base = freeze(historyFixture());
  const api = app(undefined, {storage});
  api.setBase(base);
  api.reconstruir();
  api.aplicarLocais();
  assert.equal(api.kvP('instagram', 'visualizacoes', '2026-07-01', '2026-07-01'), 50);
  assert.equal(api.getData().atualizado, base.atualizado);
  assert.deepEqual(plain(api.getData()), plain(base));
  assert.deepEqual([...stored], before);
});

test('legacy Instagram impression zeros are n/d without mutating data or masking views zero', () => {
  const posts = freeze({atualizado: '2026-11-12', facebook: [], instagram: [post('zero', {views: 0, vis: 0})]});
  const before = JSON.stringify(posts);
  const api = app(posts);
  const markup = api.topPostsHTML('instagram', begin, end);
  const row = bodyRows(markup)[0];
  assert.equal(row[3], '<b>0</b>');
  assert.match(row[4], />n\/d</);
  assert.match(markup, /procedência histórica por campo não está certificada/);
  assert.match(markup, /falta de comprovação/);
  assert.equal(JSON.stringify(posts), before);
  assert.equal(api.postMetricHTML(0), '0');
});

test('account availability, publication execution and document generation remain distinct', () => {
  const api = app({atualizado: '2026-11-12', facebook: [], instagram: []});
  api.setData(historyFixture());
  api.setReport(2026, 3);
  api.setState({plat: 'instagram', year: 2026, quarter: 3});
  const info = api.availabilityHTML(['instagram'], '2026-07-01', '2026-09-30', false);
  assert.match(info, /Execução declarada da coleta de publicações: <b>12\/11\/2026<\/b>/);
  assert.match(api.relHTML(), /Relatório gerado em: \d{2}\/\d{2}\/\d{4}/);
  assert.match(info, /Instagram — Visualizações<\/td><td>01\/07\/2026<\/td><td>30\/09\/2026<\/td><td>2<\/td>/);
  assert.match(info, /Instagram — Alcance<\/td><td>01\/07\/2026<\/td><td>02\/07\/2026<\/td><td>2<\/td>/);
  assert.match(info, /Instagram — Produção de conteúdo<\/td><td>n\/d<\/td><td>n\/d<\/td><td>0<\/td>/);
  assert.doesNotMatch(info, /31\/12\/2099/);
  assert.match(info, /não garante renovação de todos os insights/);
  api.renderPainel();
  assert.match(api.elementHTML('app'), /Visualização gerada em:/);
  assert.doesNotMatch(api.elementText('upd'), /2099/);
  assert.doesNotMatch(api.relHTML(), /Fonte: Meta Graph API \(coleta automática diária\)/);
  api.setPosts({facebook: [], instagram: []});
  assert.match(api.availabilityHTML(['instagram'], begin, end, false), /coleta de publicações: <b>n\/d<\/b>/);
});

test('legacy editorial instructions and unverified local analyses never enter PDF', () => {
  const marker = '[Revise e complemente este texto antes de gerar o PDF.]';
  const stored = new Map([
    ['ascom-rel-2026-3-an-instagram', 'LOCAL_ANALYSIS_777 ' + marker],
    ['ascom-rel-2026-3-final', 'LOCAL_FINAL_888 ' + marker],
  ]);
  const before = [...stored];
  const storage = {getItem(key) { return stored.get(key) || null; }, setItem() { throw new Error('Unexpected write'); }};
  const api = app(undefined, {storage});
  api.setData(historyFixture());
  api.setReport(2026, 3);
  assert.equal(api.edGet('an-instagram'), 'LOCAL_ANALYSIS_777');
  const markup = api.relHTML();
  for (const text of [marker, 'LOCAL_ANALYSIS_777', 'LOCAL_FINAL_888', 'cerca de 1%', '~1%', '~20×']) assert.ok(!markup.includes(text));
  assert.match(markup, /Leitura descritiva dos registros/);
  assert.deepEqual([...stored], before);
});

test('missing account values stay null and n/d while explicit zeros remain valid', () => {
  const api = app();
  const data = freeze({series: {facebook: {}, instagram: {visualizacoes: {[begin]: 0}, alcance: {[begin]: 0}, interacoes: {},
    cliques: {[begin]: null}, visitas: {[begin]: false}, seguidores: {[begin]: '0'}}}});
  api.setData(data);
  assert.equal(api.kvP('instagram', 'visualizacoes', begin, begin), 0);
  assert.equal(api.kvP('instagram', 'alcance', begin, begin), 0);
  assert.equal(api.kvP('instagram', 'publicacoes', begin, end), null);
  assert.equal(api.engRate('instagram', begin, end), null);
  assert.equal(api.coberturaP('instagram', 'visualizacoes', begin, begin), 1);
  const series = api.rngP('instagram', 'visualizacoes', begin, '2026-01-02');
  assert.deepEqual(plain(series), [{d: begin, v: 0, has: true}, {d: '2026-01-02', v: null, has: false}]);
  const summary = api.resumoTexto('instagram', 2026, 1, {a: begin, b: end}, {a: '2025-01-01', b: '2025-03-31'}, {a: '2025-01-01', b: '2025-03-31'});
  assert.match(summary, /Visualizações: <b>0<\/b>/);
  assert.match(summary, /Interações: <b>n\/d<\/b>/);
  assert.match(summary, /Novos seguidores: <b>n\/d<\/b>/);
  assert.match(summary, /Cliques no link: <b>n\/d<\/b>/);
  assert.doesNotMatch(summary, /NaN|-100%/);
  const allMissing = api.rngP('facebook', 'visualizacoes', begin, '2026-01-02');
  assert.match(api.dailyBars(allMissing, '#000', 720, 115), /n\/d/);
  assert.equal(api.bucketize(allMissing, 'mes')[0].v, null);
  assert.equal(api.bucketize(series, 'mes')[0].v, 0);
});

test('recorded account values remain available without certifying provenance', () => {
  const api = app();
  const dates = {};
  for (let day = 1; day <= 31; day++) dates['2026-01-' + String(day).padStart(2, '0')] = 1;
  api.setData({series: {facebook: {}, instagram: {visualizacoes: dates, interacoes: dates}}});
  assert.equal(api.kvP('instagram', 'visualizacoes', begin, '2026-01-31'), 31);
  assert.equal(api.engRate('instagram', begin, '2026-01-31'), 100);
  assert.equal(api.comparavelP('instagram', 'visualizacoes', begin, '2026-01-31'), false);
  assert.match(api.provenanceHTML(), /não está certificada por campo/);
  assert.doesNotMatch(source, /DATA\.conteudo|_relNiCount/);
});


test('account presence, semantics and certification are distinct; zeros before positives stay valid', () => {
  const api = app();
  const entries = Object.fromEntries(Array.from({length: 10}, (_, i) => ['2025-07-' + String(i + 1).padStart(2, '0'), i < 8 ? 0 : 5]));
  const data = freeze({series: {facebook: {}, instagram: {visualizacoes: entries}}});
  const before = JSON.stringify(data);
  api.setData(data);
  const status = api.accountPeriodStatus('instagram', 'visualizacoes', '2025-07-01', '2025-07-10');
  assert.deepEqual(plain(status), {present: 10, valid: 10, invalid: 0, certified: 0, days: 10, coverage: 1, certifiedCoverage: 0});
  assert.equal(api.kvP('instagram', 'visualizacoes', '2025-07-01', '2025-07-10'), 10);
  assert.equal(api.comparavelP('instagram', 'visualizacoes', '2025-07-01', '2025-07-10'), false);
  assert.equal(JSON.stringify(data), before);
});

test('coverage at 80%, below it and incomplete quarters never imply certification on either network', () => {
  const rules = app();
  // Hypothetical statuses test the policy, without certifying any stored data.
  assert.equal(rules.accountStatusEligible({invalid: 0, coverage: .8, certifiedCoverage: .8}), true);
  assert.equal(rules.accountStatusEligible({invalid: 0, coverage: .79, certifiedCoverage: .8}), false);
  assert.equal(rules.accountStatusEligible({invalid: 0, coverage: 1, certifiedCoverage: .79}), false);
  assert.equal(rules.accountStatusEligible({invalid: 1, coverage: .99, certifiedCoverage: .99}), false);
  for (const network of ['facebook', 'instagram', 'consolidado']) {
    const api = app();
    const entries = Object.fromEntries(Array.from({length: 8}, (_, i) => ['2026-01-' + String(i + 1).padStart(2, '0'), 0]));
    api.setData({series: {facebook: {visualizacoes: entries}, instagram: {visualizacoes: entries}}});
    assert.equal(api.coberturaP(network, 'visualizacoes', '2026-01-01', '2026-01-10'), .8);
    assert.equal(api.comparavelP(network, 'visualizacoes', '2026-01-01', '2026-01-10'), false);
    assert.ok(api.coberturaP(network, 'visualizacoes', '2026-01-01', '2026-01-11') < .8);
    assert.equal(api.comparavelP(network, 'visualizacoes', begin, end), false);
  }
});

test('invalid account counts become gaps and invalidate aggregates without dropping valid chart days', () => {
  const api = app();
  for (const bad of [-1, 1.5, -20]) {
    const data = freeze({series: {facebook: {}, instagram: {interacoes: {[begin]: 0, '2026-01-02': bad, '2026-01-03': 10}}}});
    api.setData(data);
    const rows = api.rngP('instagram', 'interacoes', begin, '2026-01-03');
    assert.equal(rows[0].v, 0);
    assert.equal(rows[1].v, null);
    assert.equal(rows[1].invalid, true);
    assert.equal(rows[2].v, 10);
    assert.equal(api.kvP('instagram', 'interacoes', begin, '2026-01-03'), null);
    assert.equal(api.bucketize(rows, 'mes')[0].v, null);
    const bars = api.dailyBars(rows, '#000', 720, 115);
    assert.equal((bars.match(/<rect /g) || []).length, 2);
    assert.doesNotMatch(bars, /NaN|height="-/);
    assert.equal(api.accountPeriodStatus('instagram', 'interacoes', begin, '2026-01-03').invalid, 1);
  }
});

test('real history regression: panel, summaries and PDF preserve values and charts but suppress historical deltas', () => {
  const api = app();
  const data = freeze(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'dados', 'metricas.json'), 'utf8')));
  const before = JSON.stringify(data);
  api.setData(data);
  const a = '2025-07-01', b = '2025-09-30';
  assert.equal(api.kvP('instagram', 'visualizacoes', a, b), 1588169);
  assert.equal(api.kvP('instagram', 'interacoes', a, b), null);
  const status = api.accountPeriodStatus('instagram', 'interacoes', a, b);
  assert.equal(status.invalid, 2);
  assert.equal(status.present, 92);
  assert.equal(api.comparavelP('instagram', 'visualizacoes', a, b), false);
  for (const [start, finish] of [['2025-01-01', '2025-03-31'], ['2025-04-01', '2025-06-30']]) {
    assert.equal(api.kvP('instagram', 'visualizacoes', start, finish), 0);
    assert.equal(api.kvP('instagram', 'interacoes', start, finish), null);
  }
  for (const year of [2025, 2026]) {
    api.setState({plat: 'instagram', year, quarter: 3});
    api.setReport(year, 3);
    api.renderPainel();
    const panel = api.elementHTML('app');
    const pdf = api.relPlat('instagram', year, 3, true);
    assert.match(panel, /comparação não certificada/);
    assert.match(panel, /registros disponíveis|Registros disponíveis/);
    assert.match(pdf, /sem procedência certificada/);
    assert.doesNotMatch(pdf, /class="d (?:up|dn)"/);
    assert.doesNotMatch(panel, /class="kd (?:up|dn)"/);
    const rows = bodyRows(pdf.match(/<table class="rel-tbl">[\s\S]*?<\/table>/)[0]);
    const views = rows.find(row => row[0] === 'Visualizações');
    const interactions = rows.find(row => row[0] === 'Interações');
    if (year === 2025) {
      assert.equal(views[1], '<b>1.588.169</b>');
      assert.equal(interactions[1], '<b>n/d</b>');
    } else {
      assert.equal(views[1], '<b>2.770.995</b>');
      assert.equal(interactions[1], '<b>48.795</b>');
      assert.equal(views[4], '1.588.169');
      assert.equal(interactions[4], 'n/d');
    }
    assert.match(views[3], /n\/d/);
    assert.match(views[5], /n\/d/);
    assert.match(pdf, /<svg/);
    const summary = api.resumoTexto('instagram', year, 3, {a: year+'-07-01', b: year+'-09-30'}, {a: year+'-04-01', b: year+'-06-30'}, {a: (year-1)+'-07-01', b: (year-1)+'-09-30'});
    assert.doesNotMatch(summary, /vs |mesmo valor|era /);
  }
  const monthly = api.monthBarsHTML('instagram', 2026, 3, {a: '2026-07-01', b: '2026-09-30'}, 'visualizacoes');
  assert.match(monthly, /2025: 533.404/);
  assert.equal(JSON.stringify(data), before);
});

test('partial inventory deduplicates identifiers, validates dates, excludes stories and conflicting dates without mutation', () => {
  const posts = freeze({facebook: [post('one'), post('one'), post('bad-date', {}, '2026-02-30'), post('story', {tipo: 'STORY'}), post('conflict'), post('conflict', {}, '2026-01-01'), {data: begin}, post('outside', {}, '2025-12-01')], instagram: []});
  const before = JSON.stringify(posts);
  const api = app(posts);
  const inv = api.publicationInventory('facebook', begin, end);
  assert.equal(inv.list.length, 1);
  assert.equal(inv.duplicates, 2);
  assert.equal(inv.conflicts, 1);
  assert.equal(inv.invalid, 2);
  assert.equal(inv.stories, 1);
  assert.equal(inv.first, '2025-12-01');
  const markup = api.publicationInventoryHTML(['facebook'], begin, end);
  assert.match(markup, /Facebook: 1/);
  assert.match(markup, /não representa a produção total/);
  assert.match(markup, /Cobertura histórica completa: não comprovada/);
  assert.match(markup, /limites efetivos da última execução não são armazenados/);
  assert.equal(JSON.stringify(posts), before);
});

test('empty inventory does not establish zero production and unavailable inventory stays n/d', () => {
  const api = app({facebook: [], instagram: []});
  assert.match(api.publicationInventoryHTML(['facebook'], begin, end), /Facebook: 0/);
  assert.match(api.publicationInventoryHTML(['facebook'], begin, end), /não ausência de produção/);
  api.setPosts({facebook: []});
  assert.match(api.publicationInventoryHTML(['instagram'], begin, end), /Instagram: n\/d/);
});

test('real Q3 inventory reports 7 FB and 22 IG separately from total production in panel and PDF', () => {
  const posts = freeze(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'dados', 'posts.json'), 'utf8')));
  const data = freeze(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'dados', 'metricas.json'), 'utf8')));
  const snapshot = JSON.stringify({posts, data});
  const api = app(posts);
  api.setData(data);
  api.setReport(2026, 3);
  api.setState({plat: 'consolidado', year: 2026, quarter: 3});
  api.renderPainel();
  for (const markup of [api.elementHTML('app'), api.relHTML()]) {
    assert.match(markup, /Facebook: 7/);
    assert.match(markup, /Instagram: 22/);
    assert.match(markup, /Publicações identificadas pela API — inventário parcial/);
    assert.match(markup, /não representa a produção total/i);
    assert.doesNotMatch(markup, /visualizacoes_stories/);
  }
  const report = api.relHTML();
  assert.equal((report.match(/class="note provenance"/g) || []).length, 1);
  assert.match(report, /Visualizações dos stories/);
  assert.match(report, /Produção de conteúdo<\/td><td><b>n\/d/);
  assert.equal(JSON.stringify({posts, data}), snapshot);
});

test('PDF omits entirely unavailable charts while retaining zero charts and printable table headers', () => {
  const api = app();
  api.setData({series: {facebook: {}, instagram: {visualizacoes: {'2026-07-01': null, '2026-09-30': null}}}});
  let report = api.relPlat('instagram', 2026, 3, true);
  assert.doesNotMatch(report, /<svg/);
  assert.match(report, /nenhuma série mensal disponível/);
  assert.match(report, /por dia: n\/d/);
  api.setData({series: {facebook: {}, instagram: {visualizacoes: {'2026-07-01': 0, '2026-09-30': 0}}}});
  report = api.relPlat('instagram', 2026, 3, true);
  assert.match(report, /<svg/);
  assert.match(html, /@page\{size:A4;margin:15mm 18mm\}/);
  assert.match(html, /\.sheet \.secao\.novapag\{break-before:auto\}/);
  assert.match(html, /\.sheet thead\{display:table-header-group\}/);
  assert.match(html, /\.sheet tr\{break-inside:avoid\}/);
});

test('only a complete Instagram top ten receives a dedicated print page across years and quarters', () => {
  for (const [year, quarter, date] of [[2024, 1, '2024-02-10'], [2026, 3, '2026-08-10']]) {
    for (const size of [0, 2, 9, 10, 15]) {
      const posts = freeze({instagram: Array.from({length: size}, (_, i) => post('entry-' + i, {views: i, vis: 500 - i}, date)), facebook: []});
      const snapshot = JSON.stringify(posts);
      const api = app(posts);
      api.setData({series: {facebook: {}, instagram: {visualizacoes: {[date]: 12}}}});
      const report = api.relPlat('instagram', year, quarter, true);
      const dedicated = report.includes('class="report-publications dedicated-publications"');
      assert.equal(dedicated, size >= 10);
      const publicationBlock = report.slice(report.indexOf('<div class="report-publications'));
      assert.equal(bodyRows(publicationBlock).length, Math.min(size, 10));
      assert.match(publicationBlock, /inventário parcial/);
      assert.match(publicationBlock, /Cobertura histórica completa: não comprovada/);
      assert.ok(report.indexOf('Leitura descritiva') < report.indexOf('<div class="report-publications'));
      assert.equal(JSON.stringify(posts), snapshot);
    }
  }
});

test('unavailable Instagram ranking and Facebook stay in flow, even with many inventory records', () => {
  const api = app({instagram: Array.from({length: 15}, (_, i) => post('old-' + i, {vis: 999})), facebook: Array.from({length: 15}, (_, i) => post('fb-' + i, {vis: i}))});
  api.setData({series: {facebook: {visualizacoes: {[end]: 1}}, instagram: {visualizacoes: {[end]: 1}}}});
  for (const network of ['facebook', 'instagram']) {
    const markup = api.relPlat(network, 2026, 1, true);
    assert.doesNotMatch(markup, /class="report-publications dedicated-publications"/);
    assert.ok(markup.includes(api.topPostsHTML(network, begin, end, true)));
  }
});

test('Q3 2026 has all ten Instagram ranking rows in the dedicated block without changing panel output', () => {
  const posts = freeze(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'dados', 'posts.json'), 'utf8')));
  const data = freeze(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'dados', 'metricas.json'), 'utf8')));
  const api = app(posts);
  api.setData(data);
  api.setState({plat: 'instagram', year: 2026, quarter: 3});
  api.renderPainel();
  const panel = api.elementHTML('app');
  const report = api.relPlat('instagram', 2026, 3, true);
  const block = report.slice(report.indexOf('<div class="report-publications dedicated-publications">'));
  assert.equal(bodyRows(block).length, 10);
  assert.match(block, /Instagram: 22/);
  assert.match(block, /Instagram — publicações/);
  assert.ok(block.includes(api.topPostsHTML('instagram', '2026-07-01', '2026-09-30', true)));
  api.renderPainel();
  assert.equal(api.elementHTML('app'), panel);
  assert.doesNotMatch(panel, /dedicated-publications|Como ler este relatório/);
  assert.match(html, /dedicated-publications\{break-before:page;page-break-before:always;break-inside:avoid/);
  assert.match(html, /\.sheet table\{break-inside:auto;width:100%\}/);
  assert.match(html, /\.sheet thead\{display:table-header-group\}/);
});

test('simplified report methodology preserves validity, provenance, coverage and date caveats once', () => {
  const api = app();
  api.setData(historyFixture());
  api.setReport(2026, 3);
  const report = api.relHTML();
  assert.equal((report.match(/class="report-method"/g) || []).length, 1);
  for (const text of ['80%', 'negativas ou fracionárias', 'Lacunas não são zero', 'base zero', '60 dias', 'limites efetivos', 'não está certificada por campo', 'Relatório gerado em:', 'Execução declarada', 'não representa a produção total', 'STORY', 'lifetime']) assert.ok(report.includes(text), text);
});

function storiesFixture() {
  const run = (id, minute) => ({id, api_version: 'v21.0', started_at: `2026-11-12T14:${minute}:00Z`,
    finished_at: `2026-11-12T14:${minute}:05Z`, status: 'complete', listing_status: 'complete',
    pagination_status: 'exhausted', pages_received: 1, distinct_stories_observed: 2, duplicates: 0,
    invalid_records: 0, conflicting_timestamps: 0, requests_used: 6, request_limit: 20, page_limit: 2,
    insights_status: 'complete', issues: []});
  const runs = [run('a'.repeat(32), '00'), run('b'.repeat(32), '06')];
  const evidence = (metric, value, minute) => ({status: 'confirmed', value, api_metric: metric,
    api_version: 'v21.0', response_structure: 'values', period: 'lifetime', checked_at: `2026-11-12T14:${minute}:02.000001Z`});
  const entry = (id, day, timestamp, views, reach) => ({id, timestamp, published_at: timestamp,
    publication_date_brt: day, first_seen_at: '2026-11-12T14:00:01Z', last_seen_at: '2026-11-12T14:06:01Z',
    snapshots: runs.map((r, i) => ({run_id: r.id, inventory_observation: 'complete',
      collected_at: `2026-11-12T14:${i ? '06' : '00'}:03Z`, metrics: {
        views: evidence('views', views + i * 2, i ? '06' : '00'),
        reach: evidence('reach', reach + i * 2, i ? '06' : '00')}}))});
  return {schema_version: 1, source: {provider: 'meta_graph_api', api_version: 'v21.0',
    inventory_endpoint: '/{ig-user-id}/stories', account_fingerprint: 'c'.repeat(64)}, timezone: 'America/Sao_Paulo',
    stories: {'1001': entry('1001', '2026-11-11', '2026-11-11T23:00:00Z', 10, 5),
      '1002': entry('1002', '2026-11-12', '2026-11-12T03:00:00Z', 20, 10)}, runs};
}

function missingStoryMetric(data, id, metric, status = 'unavailable', indices = [0, 1]) {
  for (const index of indices) {
    const snapshot = data.stories[id].snapshots[index];
    snapshot.metrics[metric] = {status, category: status === 'invalid' ? 'invalid_value' : 'empty_response',
      checked_at: snapshot.metrics[metric].checked_at};
    data.runs[index].status = 'partial';
    data.runs[index].insights_status = 'partial';
  }
}

test('stories contract accepts real ledger and known empty schema without inventing observations', () => {
  const api = app();
  const data = freeze(storiesFixture());
  assert.equal(api.validateStoriesDocument(data).valid, true);
  const empty = {...plain(data), stories: {}, runs: []};
  empty.source.account_fingerprint = null;
  assert.equal(api.validateStoriesDocument(empty).valid, true);
  const summary = api.storiesSummary(empty, 2026, 4);
  assert.equal(summary.observed, 0);
  assert.equal(summary.views.value, null);
  assert.equal(summary.coverage.expectedRuns, null);
  assert.ok(summary.coverage.days.every(day => day.observedStories === null && day.state === 'no_monitoring_records'));
  const real = freeze(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'dados', 'stories.json'), 'utf8')));
  assert.equal(api.validateStoriesDocument(real).valid, true);
});

test('stories count IDs once and sum latest views once; reach has coverage and individual values only', () => {
  const api = app();
  const data = freeze(storiesFixture());
  const summary = api.storiesSummary(data, 2026, 4);
  assert.equal(summary.observed, 2);
  assert.equal(summary.views.value, 34); // 12 + 22, never 10 + 12 + 20 + 22.
  assert.equal(summary.views.confirmed, 2);
  assert.equal(summary.views.total, 2);
  assert.equal(summary.reach.confirmed, 2);
  for (const field of ['value', 'sum', 'uniqueReach']) assert.equal(field in summary.reach, false);
  assert.deepEqual(Array.from(summary.stories, s => [s.views.value, s.reach.value]), [[12, 7], [22, 12]]);
});

test('stories latest confirmation follows timestamps, not snapshot order or maximum value', () => {
  const api = app();
  const data = storiesFixture();
  data.stories['1001'].snapshots[1].metrics.views.value = 3;
  data.stories['1001'].snapshots.reverse();
  assert.equal(api.validateStoriesDocument(data).valid, true);
  assert.equal(api.latestStoriesMetric(data.stories['1001'], 'views').value, 3);
  assert.equal(api.storiesSummary(data, 2026, 4).views.value, 25);
});

test('stories preserve microsecond ordering and equivalent timezone instants', () => {
  const api = app();
  assert.ok(api.storiesTime('2026-11-12T14:00:02.000002Z').key > api.storiesTime('2026-11-12T14:00:02.000001Z').key);
  assert.equal(api.storiesTime('2026-11-12T11:00:02.000001-0300').key, api.storiesTime('2026-11-12T14:00:02.000001Z').key);
  const story = storiesFixture().stories['1001'];
  story.snapshots[0].metrics.views.checked_at = '2026-11-12T14:00:02.000002Z';
  story.snapshots[1].metrics.views.checked_at = '2026-11-12T14:00:02.000001Z';
  assert.equal(api.latestStoriesMetric(story, 'views').value, 10);
});

test('stories preserve prior views after unavailable or invalid attempts, independently of reach', () => {
  const api = app();
  const data = storiesFixture();
  missingStoryMetric(data, '1001', 'views', 'unavailable', [1]);
  missingStoryMetric(data, '1002', 'views', 'invalid', [1]);
  const summary = api.storiesSummary(data, 2026, 4);
  assert.equal(summary.status, 'ready');
  assert.equal(summary.views.value, 30);
  assert.equal(summary.views.preserved, 2);
  assert.equal(summary.views.confirmed, 2);
  assert.equal(summary.coverage.partialRuns, 1);
  const latest = summary.stories[0].views;
  assert.equal(latest.checkedAt, data.stories['1001'].snapshots[0].metrics.views.checked_at);
  assert.equal(latest.lastAttempt.status, 'unavailable');
  assert.deepEqual(Array.from(summary.stories, s => s.reach.value), [7, 12]);
});

test('stories distinguish confirmed zero, unavailable, invalid and partial K/N', () => {
  const api = app();
  const zero = storiesFixture();
  for (const story of Object.values(zero.stories)) story.snapshots[1].metrics.views.value = 0;
  assert.equal(api.storiesSummary(zero, 2026, 4).views.value, 0);
  assert.equal(api.storiesSummary(zero, 2026, 4).views.state, 'confirmed');
  const partial = storiesFixture();
  missingStoryMetric(partial, '1001', 'views');
  let result = api.storiesSummary(partial, 2026, 4);
  assert.equal(result.views.value, 22);
  assert.equal(result.views.confirmed, 1);
  assert.equal(result.views.total, 2);
  assert.equal(result.views.partial, true);
  assert.equal(result.views.unavailable, 1);
  missingStoryMetric(partial, '1002', 'views');
  result = api.storiesSummary(partial, 2026, 4);
  assert.equal(result.views.value, null);
  assert.equal(result.views.state, 'absent');
  missingStoryMetric(partial, '1002', 'views', 'invalid');
  result = api.storiesSummary(partial, 2026, 4);
  assert.equal(result.views.state, 'invalid');
  assert.equal(result.views.invalid, 1);
  assert.equal(result.views.value, null);
});

test('stories filter civil quarters independently of stale account dates and UI state', () => {
  const api = app();
  api.setData({series: {instagram: {visualizacoes: {'2026-11-10': 1}}, facebook: {}}});
  api.setState({plat: 'facebook', year: 2025, quarter: 1});
  const data = storiesFixture();
  assert.equal(api.storiesForQuarter(data, 2026, 4).stories.length, 2);
  assert.equal(api.storiesForQuarter(data, 2026, 3).stories.length, 0);
  assert.deepEqual(plain(api.storiesQuarterRange(2026, 4)), {a: '2026-10-01', b: '2026-12-31'});
  assert.deepEqual(plain(api.storiesQuarterRange(2024, 1)), {a: '2024-01-01', b: '2024-03-31'});
  assert.equal(api.storiesQuarterRange(2026, 2).b, '2026-06-30');
  assert.equal(api.storiesQuarterRange(2026, 3).b, '2026-09-30');
  for (const args of [[2026, 0], [2026, 5], ['2026', 1], [2026.5, 1], [0, 1]]) {
    assert.equal(api.storiesForQuarter(data, ...args).status, 'invalid_period');
  }
});

test('stories use publication day in Sao Paulo, including quarter boundaries and historical DST', () => {
  const api = app();
  assert.equal(api.storiesBrtDay('2026-10-01T02:59:59.999999Z'), '2026-09-30');
  assert.equal(api.storiesBrtDay('2026-10-01T03:00:00Z'), '2026-10-01');
  assert.equal(api.storiesBrtDay('2026-09-30T23:59:59-03:00'), '2026-09-30');
  assert.equal(api.storiesBrtDay('2018-01-01T02:30:00Z'), '2018-01-01');
  const data = storiesFixture();
  data.stories['1001'].timestamp = data.stories['1001'].published_at = '2026-10-01T02:59:59Z';
  data.stories['1001'].publication_date_brt = '2026-09-30';
  assert.deepEqual(Array.from(api.storiesForQuarter(data, 2026, 3).stories, s => s.id), ['1001']);
  assert.deepEqual(Array.from(api.storiesForQuarter(data, 2026, 4).stories, s => s.id), ['1002']);
});

test('stories timestamp parser rejects normalized invalid dates and unsafe counts', () => {
  const api = app();
  for (const value of ['2026-02-30T10:00:00Z', '2025-02-29T10:00:00Z', '2026-11-12', '2026-11-12T10:00:00', '2026-11-12T24:00:00Z', '2026-11-12T10:00:00+24:00', null, true]) assert.equal(api.storiesTime(value), null);
  assert.ok(api.storiesTime('2024-02-29T10:00:00Z'));
  for (const value of [null, undefined, true, false, '0', -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) assert.equal(api.storiesCount(value), false);
  assert.equal(api.storiesCount(0), true);
});

test('stories contract rejects incompatible sources, broken references, duplicates and inconsistent metrics', () => {
  const api = app();
  const changes = [d => {d.schema_version = 2;}, d => {d.timezone = 'UTC';}, d => {d.source.provider = 'manual';},
    d => {d.source.account_fingerprint = null;}, d => {d.runs.push(d.runs[0]);},
    d => {d.stories['1001'].id = 'other';}, d => {d.stories['1001'].publication_date_brt = '2026-11-12';},
    d => {d.stories['1001'].snapshots[0].run_id = 'd'.repeat(32);},
    d => {d.stories['1001'].snapshots.push(d.stories['1001'].snapshots[0]);},
    d => {d.stories['1001'].snapshots[0].inventory_observation = 'partial';},
    d => {d.stories['1001'].first_seen_at = '2026-11-12T13:00:00Z';},
    d => {d.stories['1001'].snapshots[0].metrics.views.checked_at = '2026-11-12T13:00:00Z';},
    d => {d.stories['1001'].snapshots[0].metrics.views.value = -1;},
    d => {d.stories['1001'].snapshots[0].metrics.views.value = true;},
    d => {d.stories['1001'].snapshots[0].metrics.views.api_metric = 'reach';},
    d => {delete d.stories['1001'].snapshots[0].metrics.reach;},
    d => {d.runs[0].request_limit = 0;}, d => {d.runs[0].listing_status = 'partial';},
    d => {d.runs[0].distinct_stories_observed = 3;}];
  for (const change of changes) {
    const data = storiesFixture(); change(data);
    assert.equal(api.validateStoriesDocument(data).valid, false);
    const result = api.storiesSummary(data, 2026, 4);
    assert.equal(result.status, 'contract_error');
    assert.equal(result.observed, null);
    assert.equal(result.views, null);
  }
});

test('stories preserve confirmed insights from partial inventories without claiming full temporal coverage', () => {
  const api = app();
  const data = storiesFixture();
  data.runs[1].status = 'partial';data.runs[1].listing_status = 'partial';data.runs[1].pagination_status = 'pending';
  for (const story of Object.values(data.stories)) story.snapshots[1].inventory_observation = 'partial';
  const summary = api.storiesSummary(data, 2026, 4);
  assert.equal(summary.views.value, 34);
  assert.equal(summary.coverage.partialRuns, 1);
  assert.equal(summary.coverage.completeListings, 1);
  assert.equal(summary.coverage.temporalCoverage, null);
  assert.equal(summary.coverage.totalPublished, null);
});

test('stories operational coverage counts observations, not six-hour slots or full monitored days', () => {
  const api = app();
  const data = storiesFixture();
  const coverage = api.storiesOperationalCoverage(data, 2026, 4);
  assert.equal(coverage.runsInPeriod, 2);
  assert.equal(coverage.completeRuns, 2);
  assert.equal(coverage.maxGapBetweenCompleteListingsSeconds, 360);
  assert.equal(coverage.expectedRuns, null);
  assert.equal(coverage.temporalCoverage, null);
  assert.equal(coverage.totalPublished, null);
  const before = coverage.days.find(d => d.date === '2026-11-11');
  assert.equal(before.state, 'before_first_recorded_run');
  assert.equal(before.observedStories, 1); // Published before monitoring, discovered later.
  assert.equal(before.runCount, 0);
  const current = coverage.days.find(d => d.date === '2026-11-12');
  assert.equal(current.state, 'observations_recorded');
  assert.equal(current.runCount, 2);
  const later = coverage.days.find(d => d.date === '2026-11-13');
  assert.equal(later.state, 'after_last_recorded_run');
  assert.equal(later.observedStories, null);
  const old = api.storiesOperationalCoverage(data, 2025, 3);
  assert.equal(old.runsInPeriod, 0);
  assert.equal(old.maxGapBetweenCompleteListingsSeconds, null);
  assert.ok(old.days.every(d => d.observedStories === null && d.state === 'before_first_recorded_run'));
});

test('stories observation dates are grouped in BRT and remain separate from publication filters', () => {
  const api = app();
  const data = storiesFixture();
  const failed = {...plain(data.runs[0]), id: 'd'.repeat(32), started_at: '2026-11-13T02:58:00Z',
    finished_at: '2026-11-13T02:59:00Z', status: 'failed', listing_status: 'not_started', pagination_status: 'not_started',
    pages_received: 0, distinct_stories_observed: 0, requests_used: 1, insights_status: 'not_needed'};
  data.runs.push(failed);
  const coverage = api.storiesOperationalCoverage(data, 2026, 4);
  assert.equal(coverage.failedRuns, 1);
  const day = coverage.days.find(d => d.date === '2026-11-12');
  assert.equal(day.runCount, 3);
  assert.equal(day.state, 'partial_observations');
  assert.equal(api.storiesSummary(data, 2026, 4).observed, 2);
  assert.equal(coverage.lastAttemptAt, failed.finished_at);
  assert.equal(coverage.lastCompleteListingAt, data.runs[1].finished_at);
});

test('stories checked after the publication quarter remain cumulative observations, not that quarters account metrics', () => {
  const api = app();
  const data = storiesFixture();
  data.stories['1001'].timestamp = data.stories['1001'].published_at = '2026-09-30T12:00:00Z';
  data.stories['1001'].publication_date_brt = '2026-09-30';
  const summary = api.storiesSummary(data, 2026, 3);
  assert.equal(summary.observed, 1);
  assert.equal(summary.views.value, 12);
  assert.equal(summary.stories[0].views.checkedAt, data.stories['1001'].snapshots[1].metrics.views.checked_at);
  assert.equal(summary.coverage.runsInPeriod, 0);
});

test('stories summation overflow never returns a rounded or invented total', () => {
  const api = app();
  const data = storiesFixture();
  for (const story of Object.values(data.stories)) story.snapshots[1].metrics.views.value = Number.MAX_SAFE_INTEGER;
  const summary = api.storiesSummary(data, 2026, 4);
  assert.equal(summary.views.confirmed, 2);
  assert.equal(summary.views.value, null);
  assert.equal(summary.views.state, 'invalid');
});

test('stories calculations do not mutate frozen input or returned results back into it', () => {
  const api = app();
  const data = freeze(storiesFixture());
  const before = JSON.stringify(data);
  api.validateStoriesDocument(data);
  const selected = api.storiesForQuarter(data, 2026, 4);
  selected.stories[0].views.value = 999;
  const summary = api.storiesSummary(data, 2026, 4);
  summary.coverage.days[0].runCount = 999;
  assert.equal(JSON.stringify(data), before);
  assert.equal(api.storiesSummary(data, 2026, 4).views.value, 34);
});

test('optional stories loader distinguishes missing, HTTP/read errors, invalid JSON and incompatible contracts', async () => {
  const cases = [
    [() => ({ok: false, status: 404}), 'missing'], [() => ({ok: false, status: 410}), 'missing'],
    [() => ({ok: false, status: 500}), 'read_error'], [() => Promise.reject(new Error('private failure')), 'read_error'],
    [() => ({ok: true, json() { throw new SyntaxError('private body'); }}), 'json_error'],
    [() => ({ok: true, json() { return Promise.reject(new TypeError('body interrupted')); }}), 'read_error'],
    [() => ({ok: true, json: () => null}), 'contract_error'],
    [() => ({ok: true, json: () => ({...storiesFixture(), schema_version: 2})}), 'contract_error'],
  ];
  for (const [fetch, expected] of cases) {
    const result = await app(undefined, {fetch}).loadStories();
    assert.equal(result.status, expected);
    assert.equal(result.data, null);
    assert.ok(!JSON.stringify(result).includes('private'));
  }
  const data = freeze(storiesFixture());
  let requested;
  const loaded = await app(undefined, {fetch: (url, options) => { requested = [url, options.cache]; return {ok: true, json: () => data}; }}).loadStories();
  assert.equal(loaded.status, 'ready');
  assert.equal(loaded.data, data);
  assert.deepEqual(requested, ['dados/stories.json', 'no-cache']);
});

test('stories failures and pending responses never gate existing metric/post loading or alter visible rendering', async () => {
  const accounts = freeze(historyFixture());
  const posts = freeze({facebook: [], instagram: [post('sample', {views: 10}, '2026-08-01')]});
  for (const mode of ['missing', 'invalid', 'network', 'pending', 'ready']) {
    const fetch = url => {
      if (url === 'dados/metricas.json') return Promise.resolve({ok: true, json: () => accounts});
      if (url === 'dados/posts.json') return Promise.resolve({ok: true, json: () => posts});
      assert.equal(url, 'dados/stories.json');
      if (mode === 'pending') return new Promise(() => {});
      if (mode === 'missing') return Promise.resolve({ok: false, status: 404});
      if (mode === 'network') return Promise.reject(new Error('private'));
      return Promise.resolve({ok: true, json: () => mode === 'ready' ? storiesFixture() : {}});
    };
    const api = app(undefined, {fetch});
    let rendered = 0;
    await api.loadData(() => rendered++);
    assert.equal(rendered, 1);
    assert.deepEqual(plain(api.getData()), plain(accounts));
    assert.equal(api.kvP('instagram', 'visualizacoes', '2026-07-01', '2026-09-30'), 120);
    assert.equal(api.postRanking('instagram', '2026-07-01', '2026-09-30').total, 1);
    if (mode === 'pending') assert.equal(api.getStoriesState().status, 'loading');
    else assert.equal((await api.getStoriesRequest()).status, {missing: 'missing', invalid: 'contract_error', network: 'read_error', ready: 'ready'}[mode]);
    api.setState({plat: 'instagram', year: 2026, quarter: 3});
    api.renderPainel();
    const markup = api.elementHTML('app');
    assert.match(markup, /Stories do Instagram/); // Phase 2 adds an isolated screen-only section.
  }
});

test('an older optional stories response cannot overwrite a newer load result', async () => {
  let resolveOld;
  let storiesRequests = 0;
  const data = storiesFixture();
  const api = app(undefined, {fetch: url => {
    if (url === 'dados/stories.json') {
      storiesRequests++;
      if (storiesRequests === 1) return new Promise(resolve => { resolveOld = resolve; });
      return {ok: true, json: () => data};
    }
    return Promise.resolve({ok: true, json: () => url === 'dados/metricas.json' ? historyFixture() : {facebook: [], instagram: []}});
  }});
  await api.loadData(() => {});
  const oldRequest = api.getStoriesRequest();
  await api.loadData(() => {});
  await api.getStoriesRequest();
  resolveOld({ok: false, status: 404});
  await oldRequest;
  assert.equal(api.getStoriesState().status, 'ready');
  assert.equal(api.getStoriesState().data, data);
});


test('stories panel presents unique inventory, latest views and reach coverage independently', () => {
  const api=app(), data=freeze(storiesFixture());
  const before=JSON.stringify(data), markup=api.storiesPanelHTML({status:'ready',data},2026,4);
  assert.match(markup, /Stories observados<\/dt><dd>2/);
  assert.match(markup, /Visualizações registradas<\/dt><dd>34/);
  assert.match(markup, /views 2\/2/);assert.match(markup, /reach 2\/2/);
  assert.match(markup, /não produção total/);assert.match(markup, /não são visualizações ocorridas exclusivamente neste trimestre/);
  assert.match(markup, /Alcance não agregado/);
  assert.equal(JSON.stringify(data),before);
});

test('stories screen errors are distinct and do not disclose raw failure data', () => {
  const api=app();
  for(const [status,message] of Object.entries({loading:'Carregando',missing:'ausente',read_error:'Não foi possível ler',json_error:'JSON válido',contract_error:'incompatível'})){
    const markup=api.storiesPanelHTML({status,reason:'private token secret',data:null},2026,4);
    assert.match(markup,new RegExp(message));assert.match(markup,/role="status"/);
    assert.doesNotMatch(markup,/private|secret|<dd>0/);
  }
  assert.match(api.storiesPanelHTML({status:'ready',data:{}},2026,4),/incompatível/);
});

test('stories partial values retain prior confirmations and distinguish unavailable from zero', () => {
  const api=app(), data=storiesFixture();
  missingStoryMetric(data,'1001','views','unavailable',[1]);
  missingStoryMetric(data,'1002','views');
  const markup=api.storiesPanelHTML({status:'ready',data},2026,4);
  assert.match(markup,/Visualizações registradas<\/dt><dd>10/);
  assert.match(markup,/Cobertura 1\/2 · parcial/);assert.match(markup,/valor anterior preservado/);
  assert.match(markup,/Visualizações \(views\): n\/d/);
  const zero=storiesFixture();Object.values(zero.stories).forEach(s=>s.snapshots.forEach(x=>x.metrics.views.value=0));
  assert.match(api.storiesPanelHTML({status:'ready',data:zero},2026,4),/Visualizações registradas<\/dt><dd>0/);
});

test('stories empty and pre-monitoring quarters never assert zero production or zero daily gaps', () => {
  const api=app(),data=storiesFixture();
  const before=api.storiesPanelHTML({status:'ready',data},2025,3);
  assert.match(before,/anterior ao monitoramento/);assert.match(before,/Não há dados suficientes/);
  assert.match(before,/Visualizações registradas<\/dt><dd>n\/d/);
  const empty=api.storiesPanelHTML({status:'ready',data:{...data,stories:{},runs:[]}},2026,4);
  assert.match(empty,/Nenhum story observado/);assert.doesNotMatch(empty,/<rect /);
  assert.match(empty,/Pontos: sem contagem confirmada/);
});

test('stories chart uses BRT publication dates, gaps and accessible detail without IDs', () => {
  const api=app(),data=storiesFixture();
  const markup=api.storiesPanelHTML({status:'ready',data},2026,4);
  assert.match(markup,/11\/11\/2026: 1 stories identificados/);
  assert.match(markup,/12\/11\/2026: 1 stories identificados/);
  assert.match(markup,/role="img" aria-labelledby="stories-chart-title stories-chart-desc"/);
  assert.match(markup,/aria-labelledby="stories-heading"/);
  assert.match(markup,/<details class="stories-detail"><summary>Story 1/);
  assert.match(markup,/<time datetime=/);assert.match(markup,/\(Brasília\)/);
  assert.match(markup,/confirmação:/);assert.doesNotMatch(markup,/1001|1002/);
  assert.equal((markup.match(/<rect /g)||[]).length,2);
  assert.match(markup,/Observações próximas não comprovam monitoramento contínuo/);
});

test('stories stay Instagram-only and use separate screen and report presentations', () => {
  const api=app();api.setData(historyFixture());
  api.setStories({status:'ready',data:storiesFixture()});
  for(const plat of ['facebook','consolidado','instagram']){
    api.setState({plat,year:2026,quarter:4});api.renderPainel();
    const markup=api.elementHTML('app');
    assert.equal(markup.includes('id="stories-slot"'),plat==='instagram');
    if(plat==='instagram'){
      assert.match(markup,/Visualizações registradas<\/dt><dd>34/);
      assert.ok(markup.indexOf('id="stories-slot"')>markup.indexOf('id="kpi-bars"'));
    }
  }
  api.setState({quarter:3});api.renderPainel();
  assert.match(api.elementHTML('app'),/anterior ao monitoramento/);
  api.setReport(2026,4);const pdf=api.relHTML();
  assert.doesNotMatch(pdf,/id="stories-slot"|stories-panel/);
  assert.match(pdf,/id="report-stories-slot"/);
  assert.match(html,/@media print\{#stories-slot\{display:none!important\}\}/);
});

test('stories-only future quarters are selectable on Instagram without changing account ranges', () => {
  const api=app(),data=storiesFixture();
  Object.values(data.stories).forEach(story=>{
    story.timestamp=story.published_at='2027-01-01T03:00:00Z';story.publication_date_brt='2027-01-01';
    story.first_seen_at=story.first_seen_at.replace('2026-11-12','2027-01-02');
    story.last_seen_at=story.last_seen_at.replace('2026-11-12','2027-01-02');
    story.snapshots.forEach(snapshot=>{
      snapshot.collected_at=snapshot.collected_at.replace('2026-11-12','2027-01-02');
      Object.values(snapshot.metrics).forEach(m=>m.checked_at=m.checked_at.replace('2026-11-12','2027-01-02'));
    });
  });
  data.runs.forEach(run=>{run.started_at=run.started_at.replace('2026-11-12','2027-01-02');run.finished_at=run.finished_at.replace('2026-11-12','2027-01-02')});
  api.setData(historyFixture());api.setStories({status:'ready',data});api.setState({plat:'instagram',year:2027,quarter:1});
  assert.ok(api.dashboardQs().some(x=>x.y===2027&&x.q===1));api.renderPainel();
  assert.match(api.elementHTML('app'),/data-ysel="2027"/);
  assert.match(api.elementHTML('app'),/Visualizações registradas<\/dt><dd>34/);
  api.setState({plat:'facebook'});assert.ok(!api.dashboardQs().some(x=>x.y===2027));
});

test('optional asynchronous loading refreshes only stories content on the active Instagram screen', async () => {
  const api=app(undefined,{fetch:async url=>({ok:true,json:()=>url==='dados/stories.json'?storiesFixture():url==='dados/metricas.json'?historyFixture():{facebook:[],instagram:[]}})});
  api.setState({plat:'instagram',year:2026,quarter:4});
  await api.loadData(()=>api.renderPainel());await api.getStoriesRequest();
  assert.match(api.elementHTML('stories-slot'),/Visualizações registradas<\/dt><dd>34/);
});

test('stories presentation leaves account calculations, reports and source documents unchanged', () => {
  const api=app(), accounts=freeze(historyFixture()), stories=freeze(storiesFixture());
  api.setData(accounts);api.setReport(2026,3);
  const report=api.relHTML(), accountValue=api.kvP('instagram','visualizacoes','2026-07-01','2026-09-30');
  const before=JSON.stringify(stories);
  api.setStories({status:'ready',data:stories});api.setState({plat:'instagram',year:2026,quarter:4});
  api.renderPainel();
  const withoutStories = value => value.replace(/<div id="report-stories-slot">[\s\S]*?<\/section><\/div>/g,'');
  assert.equal(withoutStories(api.relHTML()),withoutStories(report));
  assert.equal(api.kvP('instagram','visualizacoes','2026-07-01','2026-09-30'),accountValue);
  assert.equal(JSON.stringify(stories),before);
  assert.match(api.elementHTML('app'),/tabindex="0" role="region" aria-label="Gráfico diário de stories/);
});

function concentrationFixture() {
  const values={};
  for(const [month,days] of [[10,31],[11,30],[12,31]]) {
    for(let day=1;day<=days;day++) values[`2026-${month}-${String(day).padStart(2,'0')}`]=month===10?10:0;
  }
  return {series:{instagram:{visualizacoes:{...values}},facebook:{visualizacoes:{...values}}}};
}
const concentrationQuarter={a:'2026-10-01',b:'2026-12-31',full:true};

test('monthly concentration never treats unavailable months as zero', () => {
  const api=app(),data=concentrationFixture();
  for(const day of Object.keys(data.series.instagram.visualizacoes))if(day<'2026-11-01')continue;else delete data.series.instagram.visualizacoes[day];
  api.setData(data);
  const before=api.kvP('instagram','visualizacoes',concentrationQuarter.a,concentrationQuarter.b);
  assert.equal(before,310);
  assert.doesNotMatch(api.alertasHTML('instagram',2026,4,concentrationQuarter),/concentra/);
  assert.equal(api.kvP('instagram','visualizacoes',concentrationQuarter.a,concentrationQuarter.b),before);
});

test('monthly concentration requires sufficient coverage in every month and preserves explicit zeros', () => {
  const api=app(),data=concentrationFixture();api.setData(data);
  assert.match(api.alertasHTML('instagram',2026,4,concentrationQuarter),/out concentra 100%/);
  // 24/30 meets 80%; confirmed zeros remain valid observations.
  for(let day=25;day<=30;day++)delete data.series.instagram.visualizacoes[`2026-11-${day}`];
  assert.match(api.alertasHTML('instagram',2026,4,concentrationQuarter),/out concentra 100%/);
  delete data.series.instagram.visualizacoes['2026-11-24'];
  assert.doesNotMatch(api.alertasHTML('instagram',2026,4,concentrationQuarter),/concentra/);
});

test('monthly concentration rejects invalid values, incomplete quarters and missing constituent networks', () => {
  const api=app(),data=concentrationFixture();api.setData(data);
  for(const invalid of [-1,null,1.5,false]) {
    data.series.instagram.visualizacoes['2026-11-01']=invalid;
    if(invalid===-1||invalid===1.5)assert.doesNotMatch(api.alertasHTML('instagram',2026,4,concentrationQuarter),/concentra/);
  }
  data.series.instagram.visualizacoes['2026-11-01']=0;
  assert.doesNotMatch(api.alertasHTML('instagram',2026,4,{...concentrationQuarter,b:'2026-10-31',full:false}),/concentra/);
  data.series.facebook.visualizacoes={};
  assert.doesNotMatch(api.alertasHTML('consolidado',2026,4,concentrationQuarter),/concentra/);
});

test('compact stories chart retains every civil day with evenly spaced bars and readable observed dates', () => {
  const api=app(),summary=api.storiesSummary(storiesFixture(),2026,4);
  const markup=api.storiesDailyChartHTML(summary.coverage);
  assert.match(markup,/viewBox="0 0 816 116"/);
  assert.equal((markup.match(/<circle /g)||[]).length,90);
  assert.equal((markup.match(/<rect /g)||[]).length,2);
  assert.match(markup,/x="368"/);assert.match(markup,/x="376"/);
  assert.match(markup,/<span>11\/11: 1<\/span>/);
  assert.match(markup,/<span>12\/11: 1<\/span>/);
  assert.doesNotMatch(markup,/rotate\(/);
  assert.match(markup,/01\/10\/2026/);assert.match(markup,/31\/12\/2026/);
  assert.match(markup,/role="img"/);assert.match(markup,/tabindex="0"/);
  assert.match(markup,/não representam zero/);
});

test('stories concise visible caveats retain methodology in native accessible disclosures', () => {
  const api=app(),markup=api.storiesPanelHTML({status:'ready',data:storiesFixture()},2026,4);
  assert.match(markup,/Inventário observado, não produção total/);
  assert.match(markup,/As visualizações são acumuladas por story, não apenas neste trimestre/);
  assert.match(markup,/<details><summary>Sobre os dados e a cobertura<\/summary>/);
  assert.match(markup,/sem somar snapshots/);
  assert.match(markup,/Observações próximas não comprovam monitoramento contínuo/);
  api.setData(historyFixture());
  const inventory=api.publicationInventoryHTML(['instagram'],'2026-07-01','2026-09-30');
  assert.match(inventory,/stories são apresentados separadamente/);
  assert.doesNotMatch(inventory,/Stories não são contabilizados/);
});

test('daily chart distinguishes explicitly supplied zero from unavailable days without inventing zero', () => {
  const api=app(),coverage=api.storiesOperationalCoverage(storiesFixture(),2026,4);
  // Presentation fixture only: the current inventory never certifies a daily zero.
  coverage.days.forEach(day=>day.observedStories=null);
  coverage.days[0].observedStories=0;
  const markup=api.storiesDailyChartHTML(coverage);
  assert.match(markup,/zero confirmado/);assert.match(markup,/fill="none" stroke="currentColor"/);
  assert.equal((markup.match(/zero confirmado/g)||[]).length,2); // marker title and wrapping date label
  assert.equal((markup.match(/sem contagem confirmada<\/title>/g)||[]).length,91);
  assert.doesNotMatch(markup,/NaN|Infinity|<rect /);
});

test('PDF stories uses the same quarterly summary and never aggregates reach or snapshots', () => {
  const api=app(),data=freeze(storiesFixture()),before=JSON.stringify(data);
  const markup=api.storiesReportHTML({status:'ready',data},2026,4);
  assert.match(markup,/Stories observados: <b>2<\/b>/);
  assert.match(markup,/Visualizações registradas: <b>34<\/b> · cobertura 2\/2/);
  assert.match(markup,/views 2\/2; reach 2\/2/);
  assert.match(markup,/01\/10\/2026 a 31\/12\/2026/);
  assert.match(markup,/Publicação em Brasília/);
  assert.match(markup,/não comprova a produção total/);
  assert.match(markup,/sem somar snapshots/);
  assert.match(markup,/Reach não é somado como alcance único/);
  assert.doesNotMatch(markup,/noprint|<svg|1001|1002/);
  assert.equal(JSON.stringify(data),before);
});

test('PDF stories preserves previous confirmations, partial coverage and legitimate zero', () => {
  const api=app(),data=storiesFixture();
  missingStoryMetric(data,'1001','views','unavailable',[1]);
  missingStoryMetric(data,'1002','views');
  missingStoryMetric(data,'1002','reach');
  const markup=api.storiesReportHTML({status:'ready',data},2026,4);
  assert.match(markup,/Visualizações registradas: <b>10<\/b> · cobertura 1\/2 \(parcial\)/);
  assert.match(markup,/views 1\/2; reach 1\/2/);
  assert.match(markup,/1 valores de views preservados/);
  const zeros=storiesFixture();Object.values(zeros.stories).forEach(story=>story.snapshots.forEach(snapshot=>snapshot.metrics.views.value=0));
  assert.match(api.storiesReportHTML({status:'ready',data:zeros},2026,4),/Visualizações registradas: <b>0<\/b> · cobertura 2\/2/);
  const absent=storiesFixture();
  missingStoryMetric(absent,'1001','views');missingStoryMetric(absent,'1002','views');
  const unavailable=api.storiesReportHTML({status:'ready',data:absent},2026,4);
  assert.match(unavailable,/Stories observados: <b>2/);
  assert.match(unavailable,/Visualizações registradas: <b>n\/d<\/b> · cobertura 0\/2 \(parcial\)/);
});

test('PDF stories distinguishes empty, unmonitored, invalid and unavailable inventory', () => {
  const api=app(),data=storiesFixture();
  for(const status of ['missing','read_error','json_error','contract_error','loading']){
    const markup=api.storiesReportHTML({status,reason:'secret',data:null},2026,4);
    assert.match(markup,/Indicadores de stories: n\/d/);
    assert.doesNotMatch(markup,/secret|<b>0<\/b>/);
  }
  assert.match(api.storiesReportHTML({status:'ready',data:{}},2026,4),/incompatível/);
  const old=api.storiesReportHTML({status:'ready',data},2025,3);
  assert.match(old,/anterior ao monitoramento/);
  assert.match(old,/Não há dados suficientes/);
  assert.match(old,/Visualizações registradas: <b>n\/d/);
  const empty=api.storiesReportHTML({status:'ready',data:{...data,stories:{},runs:[]}},2026,4);
  assert.match(empty,/Stories observados: <b>0/);
  assert.match(empty,/isso não comprova ausência/);
});

test('PDF keeps stories before Instagram rankings without changing Facebook or dedicated page rules', () => {
  const posts={facebook:[],instagram:Array.from({length:10},(_,i)=>post('p'+i,{views:i+1},'2026-11-12'))};
  const api=app(posts),accounts=historyFixture();
  accounts.series.instagram.visualizacoes['2026-12-31']=1;
  api.setData(accounts);api.setStories({status:'ready',data:storiesFixture()});
  const instagram=api.relPlat('instagram',2026,4,false);
  assert.ok(instagram.indexOf('report-stories-slot')<instagram.indexOf('class="report-publications dedicated-publications"'));
  assert.match(instagram,/Visualizações registradas: <b>34/);
  assert.doesNotMatch(api.relPlat('facebook',2026,4,true),/report-stories/);
  assert.match(api.relPlat('instagram',2026,3,false),/Período anterior ao monitoramento/);
  assert.match(html,/@page\{size:A4;margin:15mm 18mm\}/);
  assert.match(html,/\.report-publications\.dedicated-publications\{break-before:page/);
  assert.match(html,/\.sheet thead\{display:table-header-group\}/);
  assert.match(html,/\.sheet \.report-stories\{[^}]*break-inside:avoid/);
});

test('late optional loading updates only the report stories slot using the report filters', async () => {
  let finish;
  const api=app(undefined,{fetch:url=>url==='dados/stories.json'?new Promise(resolve=>{finish=resolve}):Promise.resolve({ok:true,json:()=>url==='dados/metricas.json'?historyFixture():{facebook:[],instagram:[]}})});
  api.setReport(2026,4);api.setState({view:'rel',plat:'facebook',year:2025,quarter:1});
  await api.loadData(()=>{});
  finish({ok:true,json:()=>storiesFixture()});await api.getStoriesRequest();
  assert.match(api.elementHTML('report-stories-slot'),/Visualizações registradas: <b>34/);
  assert.match(api.elementHTML('report-stories-slot'),/01\/10\/2026 a 31\/12\/2026/);
});

test('pre-monitoring presentation is unknown on both surfaces without changing inventory calculations', () => {
  const api=app(),data=freeze(storiesFixture()),state={status:'ready',data};
  const explanation='Período anterior ao monitoramento. Não há dados suficientes para determinar a quantidade de stories publicados.';
  const panel=api.storiesPanelHTML(state,2026,3),report=api.storiesReportHTML(state,2026,3);
  for(const markup of [panel,report]) {
    assert.ok(markup.includes(explanation));
    assert.doesNotMatch(markup,/(?:views |reach |[Cc]obertura )0\/0/);
    assert.match(markup,/views n\/d/);assert.match(markup,/reach n\/d/);
  }
  assert.match(panel,/Stories observados<\/dt><dd>n\/d/);
  assert.match(report,/Stories observados: <b>n\/d/);
  assert.equal(api.storiesSummary(data,2026,3).observed,0); // underlying count remains untouched
});

test('stories published before monitoring remain visible with confirmed zeros and coverage', () => {
  const api=app(),data=storiesFixture(),story=data.stories['1001'];
  story.timestamp=story.published_at='2026-09-30T12:00:00Z';story.publication_date_brt='2026-09-30';
  story.snapshots.forEach(snapshot=>{snapshot.metrics.views.value=0;snapshot.metrics.reach.value=0});
  const panel=api.storiesPanelHTML({status:'ready',data},2026,3),report=api.storiesReportHTML({status:'ready',data},2026,3);
  for(const markup of [panel,report]) {
    assert.doesNotMatch(markup,/Não há dados suficientes/);
    assert.match(markup,/views 1\/1/);assert.match(markup,/reach 1\/1/);
  }
  assert.match(panel,/Stories observados<\/dt><dd>1/);
  assert.match(panel,/Visualizações registradas<\/dt><dd>0/);
  assert.match(report,/Stories observados: <b>1/);
  assert.match(report,/Visualizações registradas: <b>0/);
});

test('empty inventory without known monitoring start and missing files remain distinct', () => {
  const api=app(),data={...storiesFixture(),stories:{},runs:[]};
  for(const markup of [api.storiesPanelHTML({status:'ready',data},2026,3),api.storiesReportHTML({status:'ready',data},2026,3)]) {
    assert.doesNotMatch(markup,/Período anterior ao monitoramento/);
    assert.match(markup,/Nenhum story observado/);
    assert.match(markup,/0\/0/);
  }
  assert.match(api.storiesReportHTML({status:'missing'},2026,3),/Arquivo de stories ausente/);
  assert.doesNotMatch(api.storiesReportHTML({status:'missing'},2026,3),/Stories observados: <b>0/);
});
