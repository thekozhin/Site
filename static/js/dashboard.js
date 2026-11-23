(() => {
  const status = document.getElementById('dashboardChartStatus');
  if (typeof Chart === 'undefined') {
    status.textContent = 'Не удалось загрузить график. Значения доступны ниже в разделе «Данные графика».';
    return;
  }
  const data = JSON.parse(document.getElementById('dashboardChartData').textContent);
  new Chart(document.getElementById('dashboardChart'), {
    type: 'line',
    data: { labels: data.labels, datasets: [
      { label: 'КТГ, %', data: data.ktg, yAxisID: 'ktg', borderColor: '#316cf4', backgroundColor: '#316cf420', pointRadius: 2, tension: 0.15 },
      { label: 'Машины в ремонте, шт.', data: data.repair, yAxisID: 'repair', borderColor: '#e38927', backgroundColor: '#e3892720', pointRadius: 2, tension: 0.15 }
    ] },
    options: {
      responsive: true, maintainAspectRatio: false, spanGaps: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'bottom' }, tooltip: { callbacks: {
        label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toLocaleString('ru-RU', { maximumFractionDigits: 2 })}${ctx.dataset.yAxisID === 'ktg' ? '%' : ''}`
      } } },
      scales: {
        ktg: { position: 'left', min: 0, max: 100, title: { display: true, text: 'КТГ, %' } },
        repair: { position: 'right', beginAtZero: true, grid: { drawOnChartArea: false }, title: { display: true, text: 'Машины в ремонте, шт.' } },
        x: { ticks: { maxTicksLimit: 12, maxRotation: 45 } }
      }
    }
  });
  status.textContent = '';
})();
