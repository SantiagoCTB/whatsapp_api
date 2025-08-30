document.addEventListener('DOMContentLoaded', () => {
  const REFRESH_INTERVAL = 60000;
  let chartTotales, chartDiario, chartHora, chartTablero, chartTopNumeros, chartPalabras, chartRoles, chartTipos, chartTiposDiarios;
  const commonOptions = {
    animation: { duration: 1000 },
    interaction: { mode: 'nearest', intersect: false }
  };
  const menuToggle = document.getElementById('menu-toggle');
  const sidebar = document.querySelector('.sidebar');
  if (menuToggle && sidebar) {
    menuToggle.addEventListener('click', () => {
      sidebar.classList.toggle('open');
    });
  }

  const startInput = document.getElementById('fechaInicio');
  const endInput = document.getElementById('fechaFin');
  const limitInput = document.getElementById('limit');
  const filtersToggle = document.getElementById('filters-toggle');
  const filtersPanel = document.querySelector('.filters-panel');
  const applyFilters = document.getElementById('apply-filters');
  const filtroRol = document.getElementById('filtroRol');
  const filtroNumero = document.getElementById('filtroNumero');
  const clearFilters = document.getElementById('clear-filters');
  const tipoCliente = document.getElementById('tipoCliente');
  const tipoBot = document.getElementById('tipoBot');
  const tipoAsesor = document.getElementById('tipoAsesor');

  if (filtersToggle && filtersPanel) {
    filtersToggle.addEventListener('click', () => {
      filtersPanel.classList.toggle('open');
    });
  }

  if (applyFilters && filtersPanel) {
    applyFilters.addEventListener('click', () => {
      cargarDatos();
      filtersPanel.classList.remove('open');
    });
  }

  if (clearFilters) {
    clearFilters.addEventListener('click', () => {
      if (startInput) startInput.value = '';
      if (endInput) endInput.value = '';
      if (limitInput) limitInput.value = '10';
      if (filtroRol) filtroRol.selectedIndex = 0;
      if (filtroNumero) filtroNumero.selectedIndex = 0;
      [tipoCliente, tipoBot, tipoAsesor].forEach(cb => {
        if (cb) cb.checked = true;
      });
      cargarDatos();
    });
  }

  function populateFilters() {
    if (filtroRol) {
      fetch('/datos_roles')
        .then(res => res.json())
        .then(data => {
          filtroRol.innerHTML = '<option value="">Todos</option>';
          data.forEach(item => {
            const opt = document.createElement('option');
            opt.value = item.rol;
            opt.textContent = item.rol;
            filtroRol.appendChild(opt);
          });
        });
    }
    if (filtroNumero) {
      fetch('/datos_tablero')
        .then(res => res.json())
        .then(data => {
          filtroNumero.innerHTML = '<option value="">Todos</option>';
          data.forEach(item => {
            const opt = document.createElement('option');
            opt.value = item.numero;
            opt.textContent = item.numero;
            filtroNumero.appendChild(opt);
          });
        });
    }
  }

  function buildQuery() {
    const params = new URLSearchParams();
    if (startInput.value) params.append('start', startInput.value);
    if (endInput.value) params.append('end', endInput.value);
    if (limitInput && limitInput.value) params.append('limit', limitInput.value);
    if (filtroRol && filtroRol.value) params.append('rol', filtroRol.value);
    if (filtroNumero && filtroNumero.value) params.append('numero', filtroNumero.value);
    const tipos = [];
    if (tipoCliente && tipoCliente.checked) tipos.push('cliente');
    if (tipoBot && tipoBot.checked) tipos.push('bot');
    if (tipoAsesor && tipoAsesor.checked) tipos.push('asesor');
    if (tipos.length && tipos.length < 3) params.append('tipos', tipos.join(','));
    const q = params.toString();
    return q ? `?${q}` : '';
  }

  function cargarDatos() {
    const query = buildQuery();

    fetch(`/datos_totales${query}`)
      .then(response => response.json())
      .then(data => {
        document.getElementById('totalEnviados').textContent = data.enviados;
        document.getElementById('totalRecibidos').textContent = data.recibidos;
        const total = data.enviados + data.recibidos;
        const totalElem = document.getElementById('totalMensajes');
        if (totalElem) totalElem.textContent = total;

        if (chartTotales) chartTotales.destroy();
        const ctx = document.getElementById('graficoTotales').getContext('2d');
        chartTotales = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: ['Enviados', 'Recibidos'],
            datasets: [{
              label: 'Mensajes',
              data: [data.enviados, data.recibidos],
              backgroundColor: ['rgba(54, 162, 235, 0.5)', 'rgba(255, 99, 132, 0.5)'],
              borderColor: ['rgba(54, 162, 235, 1)', 'rgba(255, 99, 132, 1)'],
              borderWidth: 1
            }]
          },
          options: {
            ...commonOptions,
            scales: {
              y: { beginAtZero: true }
            }
          }
        });
      });

    fetch('/datos_roles_total')
      .then(response => response.json())
      .then(data => {
        const rolesElem = document.getElementById('cantidadRoles');
        if (rolesElem) rolesElem.textContent = data.total_roles;
      });

    fetch(`/datos_mensajes_diarios${query}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.fecha);
        const values = data.map(item => item.total);
        if (chartDiario) chartDiario.destroy();
        const ctx = document.getElementById('graficoDiario').getContext('2d');
        chartDiario = new Chart(ctx, {
          type: 'line',
          data: {
            labels: labels,
            datasets: [{
              label: 'Mensajes por día',
              data: values,
              fill: false,
              borderColor: 'rgba(153, 102, 255, 1)',
              tension: 0.1
            }]
          },
          options: {
            ...commonOptions,
            scales: {
              y: { beginAtZero: true }
            }
          }
        });
      });

    fetch(`/datos_mensajes_hora${query}`)
      .then(response => response.json())
      .then(data => {
        const valores = Array(24).fill(0);
        data.forEach(item => {
          const h = parseInt(item.hora, 10);
          if (!isNaN(h)) valores[h] = item.total;
        });
        const labels = Array.from({ length: 24 }, (_, i) => i.toString().padStart(2, '0'));
        if (chartHora) chartHora.destroy();
        const ctx = document.getElementById('graficoHora').getContext('2d');
        chartHora = new Chart(ctx, {
          type: 'line',
          data: {
            labels: labels,
            datasets: [{
              label: 'Mensajes por hora',
              data: valores,
              fill: false,
              borderColor: 'rgba(255, 206, 86, 1)',
              tension: 0.1
            }]
          },
          options: {
            ...commonOptions,
            scales: {
              y: { beginAtZero: true }
            }
          }
        });
      });

    fetch(`/datos_tablero${query}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.numero);
        const values = data.map(item => item.palabras);
        if (chartTablero) chartTablero.destroy();
        const ctx = document.getElementById('grafico').getContext('2d');
        chartTablero = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: labels,
            datasets: [{
              label: 'Palabras por chat',
              data: values,
              backgroundColor: 'rgba(54, 162, 235, 0.5)',
              borderColor: 'rgba(54, 162, 235, 1)',
              borderWidth: 1
            }]
          },
          options: {
            ...commonOptions,
            scales: {
              y: { beginAtZero: true }
            }
          }
        });
      });

    const topParams = new URLSearchParams(query.startsWith('?') ? query.slice(1) : query);
    topParams.delete('limit');
    const topQuery = topParams.toString();
    fetch(`/datos_top_numeros?limit=3${topQuery ? '&' + topQuery : ''}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.numero);
        const values = data.map(item => item.mensajes);

        const tablaTop = document.getElementById('tabla_top_numeros');
        if (tablaTop) {
          const tbody = tablaTop.querySelector('tbody');
          tbody.innerHTML = '';
          data.forEach(item => {
            const row = document.createElement('tr');
            const numCell = document.createElement('td');
            numCell.textContent = item.numero;
            const msgCell = document.createElement('td');
            msgCell.textContent = item.mensajes;
            row.appendChild(numCell);
            row.appendChild(msgCell);
            tbody.appendChild(row);
          });
        }

        if (chartTopNumeros) chartTopNumeros.destroy();
        const ctx = document.getElementById('graficoTopNumeros').getContext('2d');
        chartTopNumeros = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: labels,
            datasets: [{
              label: 'Mensajes por número',
              data: values,
              backgroundColor: 'rgba(75, 192, 192, 0.5)',
              borderColor: 'rgba(75, 192, 192, 1)',
              borderWidth: 1
            }]
          },
          options: {
            ...commonOptions,
            indexAxis: 'y',
            scales: {
              x: { beginAtZero: true }
            }
          }
        });
      });

    const palabrasParams = new URLSearchParams(query.startsWith('?') ? query.slice(1) : query);
    palabrasParams.set('limit', 5);
    const palabrasQuery = palabrasParams.toString();
    fetch(`/datos_palabras?${palabrasQuery}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.palabra);
        const values = data.map(item => item.frecuencia);

        const tablaPalabras = document.getElementById('tabla_palabras');
        if (tablaPalabras) {
          const tbody = tablaPalabras.querySelector('tbody');
          tbody.innerHTML = '';
          data.forEach(item => {
            const row = document.createElement('tr');
            const palabraCell = document.createElement('td');
            palabraCell.textContent = item.palabra;
            const freqCell = document.createElement('td');
            freqCell.textContent = item.frecuencia;
            row.appendChild(palabraCell);
            row.appendChild(freqCell);
            tbody.appendChild(row);
          });
        }

        if (chartPalabras) chartPalabras.destroy();
        const ctx = document.getElementById('grafico_palabras').getContext('2d');
        chartPalabras = new Chart(ctx, {
          type: 'wordCloud',
          data: {
            labels: labels,
            datasets: [{
              label: 'Palabras más frecuentes',
              data: values
            }]
          },
          options: {
            ...commonOptions,
            plugins: {
              legend: { display: false }
            }
          }
        });
      });

    fetch(`/datos_roles${query}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.rol);
        const values = data.map(item => item.mensajes);
        const tabla = document.getElementById('tabla_roles');
        if (tabla) {
          const tbody = tabla.querySelector('tbody');
          tbody.innerHTML = '';
          data.forEach(item => {
            const row = document.createElement('tr');
            const rolCell = document.createElement('td');
            rolCell.textContent = item.rol;
            const mensajesCell = document.createElement('td');
            mensajesCell.textContent = item.mensajes;
            row.appendChild(rolCell);
            row.appendChild(mensajesCell);
            tbody.appendChild(row);
          });
        }
        if (chartRoles) chartRoles.destroy();
        const ctx = document.getElementById('grafico_roles').getContext('2d');
        const colors = ['#FF6384','#36A2EB','#FFCE56','#4BC0C0','#9966FF','#FF9F40'];
        chartRoles = new Chart(ctx, {
          type: 'pie',
          data: {
            labels: labels,
            datasets: [{
              data: values,
              backgroundColor: labels.map((_, i) => colors[i % colors.length])
            }]
          },
          options: {
            ...commonOptions
          }
        });
      });

    fetch(`/datos_tipos${query}`)
      .then(response => response.json())
      .then(data => {
        const order = ['cliente', 'bot', 'asesor', 'otros'];
        const labelMap = { cliente: 'Clientes', bot: 'Bots', asesor: 'Asesores', otros: 'Otros' };
        const colorMap = { cliente: '#FF6384', bot: '#36A2EB', asesor: '#FFCE56', otros: '#4BC0C0' };
        const labels = [];
        const values = [];
        const colors = [];
        order.forEach(cat => {
          const entry = data.find(d => d.tipo === cat);
          if (entry) {
            labels.push(labelMap[cat]);
            values.push(entry.total);
            colors.push(colorMap[cat]);
          }
        });
        if (chartTipos) chartTipos.destroy();
        const ctx = document.getElementById('graficoTipos').getContext('2d');
        chartTipos = new Chart(ctx, {
          type: 'doughnut',
          data: {
            labels: labels,
            datasets: [{
              data: values,
              backgroundColor: colors
            }]
          },
          options: {
            ...commonOptions
          }
        });
      });

    fetch(`/datos_tipos_diarios${query}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.fecha);
        const datasets = [
          {
            label: 'Clientes',
            data: data.map(item => item.cliente || 0),
            borderColor: '#FF6384',
            backgroundColor: 'rgba(255, 99, 132, 0.2)',
            tension: 0.1,
            fill: false
          },
          {
            label: 'Bots',
            data: data.map(item => item.bot || 0),
            borderColor: '#36A2EB',
            backgroundColor: 'rgba(54, 162, 235, 0.2)',
            tension: 0.1,
            fill: false
          },
          {
            label: 'Asesores',
            data: data.map(item => item.asesor || 0),
            borderColor: '#FFCE56',
            backgroundColor: 'rgba(255, 206, 86, 0.2)',
            tension: 0.1,
            fill: false
          }
        ];
        if (chartTiposDiarios) chartTiposDiarios.destroy();
        const ctx = document.getElementById('graficoTiposDiarios').getContext('2d');
        chartTiposDiarios = new Chart(ctx, {
          type: 'line',
          data: { labels, datasets },
          options: {
            ...commonOptions,
            scales: {
              y: { beginAtZero: true }
            }
          }
        });
      });
  }

  populateFilters();
  cargarDatos();
  setInterval(cargarDatos, REFRESH_INTERVAL);
});

