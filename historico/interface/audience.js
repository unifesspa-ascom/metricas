(() => {
  const section = document.getElementById('audience');
  const source = new URL('../data/metricool-audience.json', document.currentScript.src);
  const days = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo'];
  const countryNames = new Intl.DisplayNames(['pt-BR'], { type: 'region' });
  const percent = new Intl.NumberFormat('pt-BR', { style: 'percent', maximumFractionDigits: 2 });

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function heatmap(hours) {
    const panel = element('article', null, 'panel audience-panel');
    const heading = element('h3', 'Melhores horários para publicar');
    heading.id = 'audience-hours-title';
    const grid = element('div', null, 'audience-grid');
    grid.tabIndex = 0;
    grid.setAttribute('role', 'region');
    grid.setAttribute('aria-labelledby', heading.id);
    const table = element('table', null, 'audience-heatmap');
    const header = table.createTHead().insertRow();
    header.append(element('th', 'Dia'));
    for (let hour = 0; hour < 24; hour++) {
      const label = element('th', hour + 'h');
      label.scope = 'col';
      header.append(label);
    }
    const body = table.createTBody();
    hours.forEach((values, day) => {
      const row = body.insertRow();
      const label = element('th', days[day]);
      label.scope = 'row';
      row.append(label);
      values.forEach((score, hour) => {
        const cell = row.insertCell();
        const description = days[day] + ', ' + hour + 'h · ' + score + ' de 100';
        cell.title = description;
        cell.append(element('span', description, 'audience-visually-hidden'));
        cell.style.setProperty('--level', score + '%');
      });
    });
    grid.append(table);
    const peaks = hours.flatMap((values, day) => values.map((score, hour) => ({ day, hour, score })))
      .sort((a, b) => b.score - a.score).slice(0, 3)
      .map(({ day, hour }) => days[day].toLowerCase() + ', ' + hour + 'h');
    panel.append(heading, element('p', 'Horário de Brasília · Intensidade relativa de 0 a 100. Tons mais escuros indicam horários mais favoráveis.', 'sub'),
      grid, element('p', 'Horários em destaque · ' + peaks.join(' · '), 'audience-peaks'));
    return panel;
  }

  function locations(network) {
    const panel = element('article', null, 'panel audience-panel');
    panel.append(element('h3', 'De onde são os seguidores'));
    for (const [title, rows] of [['Cidades', network.cities], ['Países', network.countries]]) {
      const list = element('ul', null, 'audience-locations');
      for (const [place, share] of rows) {
        const item = element('li');
        item.append(element('span', title === 'Países' ? countryNames.of(place) : place),
          element('strong', percent.format(share / 100)));
        list.append(item);
      }
      panel.append(element('h4', title), list);
    }
    return panel;
  }

  function mount(snapshot) {
    let renderedNetwork;
    function render() {
      const { network, view } = document.body.dataset;
      const data = snapshot.networks[network];
      section.hidden = view !== 'overview' || !data;
      if (section.hidden || renderedNetwork === network) return;
      renderedNetwork = network;
      const title = element('h2', 'Público e horários');
      title.id = 'audience-title';
      const date = snapshot.collectedOn.split('-').reverse().join('/');
      const sourceNote = element('p', 'Metricool · Consulta de ' + date + ' · Retrato do público e dos horários, independente do período selecionado.', 'sub');
      const panels = element('div', null, 'audience-panels');
      panels.append(heatmap(data.hours), locations(data));
      section.replaceChildren(title, sourceNote, panels);
    }
    new MutationObserver(render).observe(document.body, { attributes: true, attributeFilter: ['data-network', 'data-view'] });
    render();
  }

  fetch(source).then(response => {
    if (!response.ok) throw new Error('Dados do Metricool indisponíveis');
    return response.json();
  }).then(mount).catch(error => console.warn(error));
})();
