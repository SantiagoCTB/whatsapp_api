document.addEventListener('DOMContentLoaded', () => {
  const menuToggle = document.getElementById('menu-toggle');
  const sidebar = document.querySelector('.sidebar');
  if (menuToggle && sidebar) {
    menuToggle.addEventListener('click', () => {
      sidebar.classList.toggle('open');
    });
  }

  fetch('/datos_totales')
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

  fetch('/datos_tablero')
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

  fetch('/datos_top_numeros')
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

  fetch('/datos_palabras')
    .then(response => response.json())
    .then(data => {
      const labels = data.map(item => item.palabra);
      const values = data.map(item => item.frecuencia);
      const ctx = document.getElementById('grafico_palabras').getContext('2d');
      new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            label: 'Palabras más frecuentes',
            data: values,
            backgroundColor: 'rgba(255, 159, 64, 0.5)',
            borderColor: 'rgba(255, 159, 64, 1)',
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

  fetch('/datos_roles')
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
});
