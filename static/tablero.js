document.addEventListener('DOMContentLoaded', () => {
  const REFRESH_INTERVAL = 60000;
  let chartTotales,
      chartDiario,
      chartHora,
      chartSemana,
      chartTablero,
      chartTopNumeros,
      chartPalabras,
      chartRoles,
      chartTipos,
      chartTiposDiarios,
      chartSinAsesor,
      chartNumerosSinAsesor;
  const commonOptions = {
    animation: { duration: 1000 },
    interaction: { mode: 'nearest', intersect: false },
    // Allow charts to respect the explicit canvas height
    maintainAspectRatio: false,
    responsive: true
  };
  const startInput = document.getElementById('fechaInicio');
  const endInput = document.getElementById('fechaFin');
  const limitInput = document.getElementById('limit');
  const filtersToggle = document.getElementById('filters-toggle');
  const filtersPanel = document.querySelector('.filters-panel');
  const applyFilters = document.getElementById('apply-filters');
  const rolInput = document.getElementById('filtroRol');
  const numeroInput = document.getElementById('filtroNumero');
  const textoInput = document.getElementById('filtroTexto');
  const clearFilters = document.getElementById('clear-filters');
  const tipoCliente = document.getElementById('tipoCliente');
  const tipoBot = document.getElementById('tipoBot');
  const tipoAsesor = document.getElementById('tipoAsesor');

  if (filtersToggle && filtersPanel) {
    filtersToggle.setAttribute('aria-expanded', 'false');
    filtersPanel.classList.remove('open');
    filtersPanel.hidden = true;
    filtersToggle.addEventListener('click', () => {
      const nextState = !filtersPanel.classList.contains('open');
      filtersPanel.classList.toggle('open', nextState);
      filtersPanel.hidden = !nextState;
      filtersToggle.setAttribute('aria-expanded', String(nextState));
    });
  }

  if (applyFilters && filtersPanel) {
    applyFilters.addEventListener('click', () => {
      cargarDatos();
      cargarRoles();
      cargarNumeros();
      filtersPanel.classList.remove('open');
      filtersPanel.hidden = true;
      if (filtersToggle) {
        filtersToggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  if (clearFilters) {
    clearFilters.addEventListener('click', () => {
      if (startInput) startInput.value = '';
      if (endInput) endInput.value = '';
      if (limitInput) limitInput.value = '10';
      if (rolInput) rolInput.selectedIndex = 0;
      if (numeroInput) numeroInput.selectedIndex = 0;
      if (textoInput) textoInput.value = '';
      [tipoCliente, tipoBot, tipoAsesor].forEach(cb => {
        if (cb) cb.checked = true;
      });
      cargarDatos();
    });
  }

  function cargarRoles() {
    if (!rolInput) return;
    fetch('/lista_roles')
      .then(res => res.json())
      .then(data => {
        rolInput.innerHTML = '<option value="">Todos</option>';
        data.forEach(rol => {
          const opt = document.createElement('option');
          opt.value = rol.id;
          opt.textContent = rol.name;
          rolInput.appendChild(opt);
        });
      });
  }

  function cargarNumeros() {
    if (!numeroInput) return;
    fetch('/lista_numeros')
      .then(res => res.json())
      .then(data => {
        numeroInput.innerHTML = '<option value="">Todos</option>';
        data.forEach(numero => {
          const opt = document.createElement('option');
          opt.value = numero;
          opt.textContent = numero;
          numeroInput.appendChild(opt);
        });
      });
  }

  function buildQuery() {
    const params = new URLSearchParams();
    if (startInput.value) params.append('start', startInput.value);
    if (endInput.value) params.append('end', endInput.value);
    if (limitInput && limitInput.value) params.append('limit', limitInput.value);
    if (rolInput && rolInput.value) {
      const rolId = rolInput.value;
      params.append('rol', rolId);
    }
    if (numeroInput && numeroInput.value) params.append('numero', numeroInput.value);
    if (textoInput && textoInput.value.trim()) {
      params.append('texto', textoInput.value.trim());
    }
    const tipos = [];
    if (tipoCliente && tipoCliente.checked) tipos.push('cliente');
    if (tipoBot && tipoBot.checked) tipos.push('bot');
    if (tipoAsesor && tipoAsesor.checked) tipos.push('asesor');
    if (tipos.length && tipos.length < 3) params.append('tipos', tipos.join(','));
    const q = params.toString();
    return q ? `?${q}` : '';
  }

  function showCardMessage(elemId, message) {
    const elem = document.getElementById(elemId);
    const card = elem ? elem.closest('.card') : null;
    if (!card) return;
    let msgElem = card.querySelector('.card-message');
    if (!message) {
      if (msgElem) msgElem.remove();
      return;
    }
    if (!msgElem) {
      msgElem = document.createElement('p');
      msgElem.className = 'card-message';
      card.appendChild(msgElem);
    }
    msgElem.textContent = message;
  }

  function cargarNumerosSinAsesor(query) {
    fetch(`/datos_numeros_sin_asesor${query}`)
      .then(response => response.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartNumerosSinAsesor) chartNumerosSinAsesor.destroy();
          const tabla = document.getElementById('tabla_numeros_sin_asesor');
          if (tabla) tabla.querySelector('tbody').innerHTML = '';
          showCardMessage('graficoNumerosSinAsesor', 'No hay datos disponibles');
          return;
        }
        const labels = data.map(item => item.numero);
        const values = data.map(item => item.mensajes);

        const tabla = document.getElementById('tabla_numeros_sin_asesor');
        if (tabla) {
          const tbody = tabla.querySelector('tbody');
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

        if (chartNumerosSinAsesor) chartNumerosSinAsesor.destroy();
        showCardMessage('graficoNumerosSinAsesor');
        const ctx = document.getElementById('graficoNumerosSinAsesor').getContext('2d');
        chartNumerosSinAsesor = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: labels,
            datasets: [{
              label: 'Mensajes por número',
              data: values,
              backgroundColor: 'rgba(153, 102, 255, 0.5)',
              borderColor: 'rgba(153, 102, 255, 1)',
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
      })
      .catch(err => {
        console.error(err);
        if (chartNumerosSinAsesor) chartNumerosSinAsesor.destroy();
        const tabla = document.getElementById('tabla_numeros_sin_asesor');
        if (tabla) tabla.querySelector('tbody').innerHTML = '';
        showCardMessage('graficoNumerosSinAsesor', 'Error al cargar datos');
      });
  }

  function cargarDatos() {
    const query = buildQuery();

    fetch(`/datos_totales${query}`)
      .then(response => response.json())
      .then(data => {
        if (!data || Object.keys(data).length === 0) {
          document.getElementById('totalMensajes').textContent = 'No hay datos disponibles';
          showCardMessage('graficoTotales', 'No hay datos disponibles');
          return;
        }
        document.getElementById('totalEnviados').textContent = data.enviados;
        document.getElementById('totalRecibidos').textContent = data.recibidos;
        const total = data.enviados + data.recibidos;
        const totalElem = document.getElementById('totalMensajes');
        if (totalElem) totalElem.textContent = total;

        if (chartTotales) chartTotales.destroy();
        showCardMessage('graficoTotales');
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
      })
      .catch(err => {
        console.error(err);
        document.getElementById('totalMensajes').textContent = 'Error al cargar datos';
        if (chartTotales) chartTotales.destroy();
        showCardMessage('graficoTotales', 'Error al cargar datos');
      });

    fetch('/datos_roles_total')
      .then(response => response.json())
      .then(data => {
        const rolesElem = document.getElementById('cantidadRoles');
        if (!data || Object.keys(data).length === 0) {
          if (rolesElem) rolesElem.textContent = 'No hay datos disponibles';
          return;
        }
        if (rolesElem) rolesElem.textContent = data.total_roles;
      })
      .catch(err => {
        console.error(err);
        const rolesElem = document.getElementById('cantidadRoles');
        if (rolesElem) rolesElem.textContent = 'Error al cargar datos';
      });

    fetch(`/datos_mensajes_diarios${query}`)
      .then(response => response.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartDiario) chartDiario.destroy();
          showCardMessage('graficoDiario', 'No hay datos disponibles');
          return;
        }
        const labels = data.map(item => item.fecha);
        const values = data.map(item => item.total);
        if (chartDiario) chartDiario.destroy();
        showCardMessage('graficoDiario');
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
      })
      .catch(err => {
        console.error(err);
        if (chartDiario) chartDiario.destroy();
        showCardMessage('graficoDiario', 'Error al cargar datos');
      });

    fetch(`/datos_mensajes_hora${query}`)
      .then(response => response.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartHora) chartHora.destroy();
          showCardMessage('graficoHora', 'No hay datos disponibles');
          return;
        }
        const valores = Array(24).fill(0);
        data.forEach(item => {
          const h = parseInt(item.hora, 10);
          if (!isNaN(h)) valores[h] = item.total;
        });
        const labels = Array.from({ length: 24 }, (_, i) => i.toString().padStart(2, '0'));
        if (chartHora) chartHora.destroy();
        showCardMessage('graficoHora');
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
      })
      .catch(err => {
        console.error(err);
        if (chartHora) chartHora.destroy();
        showCardMessage('graficoHora', 'Error al cargar datos');
      });

    fetch(`/datos_mensajes_semana${query}`)
      .then(res => {
        if (!res.ok) throw new Error('Network response was not ok');
        return res.json();
      })
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartSemana) chartSemana.destroy();
          const tabla = document.getElementById('tabla_semana');
          if (tabla) tabla.querySelector('tbody').innerHTML = '';
          showCardMessage('graficoSemana', 'No hay datos disponibles');
          return;
        }
        const labels = data.map(item => item.dia);
        const values = data.map(item => item.total);
        const tabla = document.getElementById('tabla_semana');
        if (tabla) {
          const tbody = tabla.querySelector('tbody');
          tbody.innerHTML = '';
          data.forEach(item => {
            const row = document.createElement('tr');
            const diaCell = document.createElement('td');
            diaCell.textContent = item.dia;
            const msgCell = document.createElement('td');
            msgCell.textContent = item.total;
            row.appendChild(diaCell);
            row.appendChild(msgCell);
            tbody.appendChild(row);
          });
        }
        if (chartSemana) chartSemana.destroy();
        showCardMessage('graficoSemana');
        const ctx = document.getElementById('graficoSemana').getContext('2d');
        chartSemana = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: labels,
            datasets: [{
              label: 'Mensajes por día',
              data: values,
              backgroundColor: 'rgba(75, 192, 192, 0.5)',
              borderColor: 'rgba(75, 192, 192, 1)',
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
      })
      .catch(err => {
        console.error(err);
        if (chartSemana) chartSemana.destroy();
        const tabla = document.getElementById('tabla_semana');
        if (tabla) {
          const tbody = tabla.querySelector('tbody');
          if (tbody) tbody.innerHTML = '<tr><td colspan="2">Error al cargar datos</td></tr>';
        }
        showCardMessage('graficoSemana', 'Error al cargar datos');
      });

    fetch(`/datos_chats_por_usuario${query}`)
      .then(response => response.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartTablero) chartTablero.destroy();
          showCardMessage('grafico', 'No hay datos disponibles');
          return;
        }
        const labels = data.map(item => item.usuario);
        const values = data.map(item => item.chats);
        if (chartTablero) chartTablero.destroy();
        showCardMessage('grafico');
        const ctx = document.getElementById('grafico').getContext('2d');
        chartTablero = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: labels,
            datasets: [{
              label: 'Chats asignados',
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
      })
      .catch(err => {
        console.error(err);
        if (chartTablero) chartTablero.destroy();
        showCardMessage('grafico', 'Error al cargar datos');
      });

    const topParams = new URLSearchParams(query.startsWith('?') ? query.slice(1) : query);
    topParams.delete('limit');
    const topQuery = topParams.toString();
    fetch(`/datos_top_numeros?limit=3${topQuery ? '&' + topQuery : ''}`)
      .then(response => response.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartTopNumeros) chartTopNumeros.destroy();
          const tablaTop = document.getElementById('tabla_top_numeros');
          if (tablaTop) tablaTop.querySelector('tbody').innerHTML = '';
          showCardMessage('graficoTopNumeros', 'No hay datos disponibles');
          return;
        }
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
        showCardMessage('graficoTopNumeros');
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
      })
      .catch(err => {
        console.error(err);
        if (chartTopNumeros) chartTopNumeros.destroy();
        const tablaTop = document.getElementById('tabla_top_numeros');
        if (tablaTop) tablaTop.querySelector('tbody').innerHTML = '';
        showCardMessage('graficoTopNumeros', 'Error al cargar datos');
      });

    const palabrasParams = new URLSearchParams(query.startsWith('?') ? query.slice(1) : query);
    palabrasParams.set('limit', 5);
    const palabrasQuery = palabrasParams.toString();
    fetch(`/datos_palabras?${palabrasQuery}`)
      .then(response => response.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartPalabras) chartPalabras.destroy();
          const tablaPalabras = document.getElementById('tabla_palabras');
          if (tablaPalabras) tablaPalabras.querySelector('tbody').innerHTML = '';
          showCardMessage('grafico_palabras', 'No hay datos disponibles');
          return;
        }
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
        showCardMessage('grafico_palabras');
        const ctx = document.getElementById('grafico_palabras').getContext('2d');
        chartPalabras = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: labels,
            datasets: [{
              label: 'Frecuencia',
              data: values,
              backgroundColor: 'rgba(153, 102, 255, 0.5)',
              borderColor: 'rgba(153, 102, 255, 1)',
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
      })
      .catch(err => {
        console.error(err);
        if (chartPalabras) chartPalabras.destroy();
        const tablaPalabras = document.getElementById('tabla_palabras');
        if (tablaPalabras) tablaPalabras.querySelector('tbody').innerHTML = '';
        showCardMessage('grafico_palabras', 'Error al cargar datos');
      });

    fetch(`/datos_roles${query}`)
      .then(response => response.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartRoles) chartRoles.destroy();
          const tabla = document.getElementById('tabla_roles');
          if (tabla) tabla.querySelector('tbody').innerHTML = '';
          showCardMessage('grafico_roles', 'No hay datos disponibles');
          return;
        }
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
        showCardMessage('grafico_roles');
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
      })
      .catch(err => {
        console.error(err);
        if (chartRoles) chartRoles.destroy();
        const tabla = document.getElementById('tabla_roles');
        if (tabla) tabla.querySelector('tbody').innerHTML = '';
        showCardMessage('grafico_roles', 'Error al cargar datos');
      });

    fetch(`/datos_tipos${query}`)
      .then(response => response.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartTipos) chartTipos.destroy();
          showCardMessage('graficoTipos', 'No hay datos disponibles');
          return;
        }
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
        showCardMessage('graficoTipos');
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
      })
      .catch(err => {
        console.error(err);
        if (chartTipos) chartTipos.destroy();
        showCardMessage('graficoTipos', 'Error al cargar datos');
      });

    fetch(`/datos_sin_asesor${query}`)
      .then(response => response.json())
      .then(data => {
        const total = data && typeof data.sin_asesor === 'number' ? data.sin_asesor : null;
        if (total === null || total === 0) {
          if (chartSinAsesor) chartSinAsesor.destroy();
          showCardMessage('graficoSinAsesor', total === 0 ? 'No hay datos disponibles' : 'Error al cargar datos');
          return;
        }
        if (chartSinAsesor) chartSinAsesor.destroy();
        showCardMessage('graficoSinAsesor');
        const ctx = document.getElementById('graficoSinAsesor').getContext('2d');
        chartSinAsesor = new Chart(ctx, {
          type: 'doughnut',
          data: {
            labels: ['Sin Asesor'],
            datasets: [{
              data: [total],
              backgroundColor: ['#FF6384']
            }]
          },
          options: {
            ...commonOptions
          }
        });
      })
      .catch(err => {
        console.error(err);
        if (chartSinAsesor) chartSinAsesor.destroy();
        showCardMessage('graficoSinAsesor', 'Error al cargar datos');
      });

    cargarNumerosSinAsesor(query);

    fetch(`/datos_tipos_diarios${query}`)
      .then(response => response.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          if (chartTiposDiarios) chartTiposDiarios.destroy();
          showCardMessage('graficoTiposDiarios', 'No hay datos disponibles');
          return;
        }
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
        showCardMessage('graficoTiposDiarios');
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
      })
      .catch(err => {
        console.error(err);
        if (chartTiposDiarios) chartTiposDiarios.destroy();
        showCardMessage('graficoTiposDiarios', 'Error al cargar datos');
      });
  }

  cargarRoles();
  cargarNumeros();
  cargarDatos();
  setInterval(cargarDatos, REFRESH_INTERVAL);
});
