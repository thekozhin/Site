(() => {
    const link = document.getElementById('incontrolPendingReminder');
    if (!link) return;
    let loading = false;
    async function refresh() {
        if (loading || document.hidden) return;
        loading = true;
        const abort = new AbortController();
        const timer = setTimeout(() => abort.abort(), 10000);
        try {
            const response = await fetch(link.dataset.countUrl, {
                credentials: 'same-origin', cache: 'no-store', signal: abort.signal,
            });
            if (!response.ok || response.redirected) return;
            const {count} = await response.json();
            if (!Number.isInteger(count) || count < 0) return;
            link.querySelector('[data-count]').textContent = count;
            link.setAttribute('aria-label', `Заявок к отписке: ${count}`);
            link.hidden = count === 0;
        } catch (_) {
            // При сбое оставляем последний известный счётчик.
        } finally {
            clearTimeout(timer);
            loading = false;
        }
    }
    refresh();
    document.addEventListener('visibilitychange', refresh);
    window.addEventListener('pageshow', event => { if (event.persisted) refresh(); });
    setInterval(refresh, 60000);
})();
