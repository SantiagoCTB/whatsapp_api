document.addEventListener('DOMContentLoaded', () => {
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

  function buildQuery() {
    const params = new URLSearchParams();
    if (startInput.value) params.append('start', startInput.value);
    if (endInput.value) params.append('end', endInput.value);
    if (limitInput && limitInput.value) params.append('limit', limitInput.value);
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

        const ctx = document.getElementById('graficoTotales').getContext('2d');
        new Chart(ctx, {
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
            scales: {
              y: { beginAtZero: true }
            }
          }
        });
      });

    fetch(`/datos_mensajes_diarios${query}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.fecha);
        const values = data.map(item => item.total);
        const ctx = document.getElementById('graficoDiario').getContext('2d');
        new Chart(ctx, {
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
        const ctx = document.getElementById('graficoHora').getContext('2d');
        new Chart(ctx, {
          type: 'bar',
          data: {
            labels: labels,
            datasets: [{
              label: 'Mensajes por hora',
              data: valores,
              backgroundColor: 'rgba(255, 206, 86, 0.5)',
              borderColor: 'rgba(255, 206, 86, 1)',
              borderWidth: 1
            }]
          },
          options: {
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
        const ctx = document.getElementById('grafico').getContext('2d');
        new Chart(ctx, {
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
            scales: {
              y: { beginAtZero: true }
            }
          }
        });
      });

    fetch(`/datos_top_numeros${query}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.numero);
        const values = data.map(item => item.mensajes);
        const ctx = document.getElementById('graficoTopNumeros').getContext('2d');
        new Chart(ctx, {
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
            indexAxis: 'y',
            scales: {
              x: { beginAtZero: true }
            }
          }
        });
      });

    fetch(`/datos_palabras${query}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.palabra);
        const values = data.map(item => item.frecuencia);
        const ctx = document.getElementById('grafico_palabras').getContext('2d');
        new Chart(ctx, {
          type: 'wordCloud',
          data: {
            labels: labels,
            datasets: [{
              label: 'Palabras más frecuentes',
              data: values
            }]
          },
          options: {
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
        const ctx = document.getElementById('grafico_roles').getContext('2d');
        const colors = ['#FF6384','#36A2EB','#FFCE56','#4BC0C0','#9966FF','#FF9F40'];
        new Chart(ctx, {
          type: 'pie',
          data: {
            labels: labels,
            datasets: [{
              data: values,
              backgroundColor: labels.map((_, i) => colors[i % colors.length])
            }]
          }
        });
      });

    fetch(`/datos_tipos${query}`)
      .then(response => response.json())
      .then(data => {
        const labels = data.map(item => item.tipo);
        const values = data.map(item => item.total);
        const ctx = document.getElementById('graficoTipos').getContext('2d');
        const colors = ['#FF6384','#36A2EB','#FFCE56','#4BC0C0','#9966FF','#FF9F40','#8E5EA2','#3CBA9F','#E8C3B9','#C45850'];
        new Chart(ctx, {
          type: 'doughnut',
          data: {
            labels: labels,
            datasets: [{
              data: values,
              backgroundColor: labels.map((_, i) => colors[i % colors.length])
            }]
          }
        });
      });
  }

  cargarDatos();
});

