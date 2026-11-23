/* Обновление только списка и счётчиков; формы и навигация остаются на месте. */
class IndexLiveUpdates {
    constructor(root) {
        this.root = root;
        this.list = document.getElementById('taskList');
        this.search = document.querySelector('.form-control-dark');
        this.status = document.getElementById('indexRefreshStatus');
        this.interval = Number(root.dataset.refreshInterval) || 300000;
        this.mutations = 0;
        this.revision = 0;
        this.lastInputAt = 0;
        this.keyboardNavigation = false;
        this.inFlight = false;
        this.stopped = false;
        this.lastHtml = {};
        this.search?.addEventListener('input', () => this.filter());
        document.addEventListener('input', () => { this.lastInputAt = Date.now(); });
        document.addEventListener('keydown', event => {
            if (event.key === 'Tab') this.keyboardNavigation = true;
        });
        document.addEventListener('pointerdown', () => { this.keyboardNavigation = false; });
        document.addEventListener('visibilitychange', () => {
            clearTimeout(this.timer);
            if (!document.hidden && !this.inFlight) {
                this.schedule(Math.max(0, this.nextRefreshAt - Date.now()));
            }
        });
        window.addEventListener('pagehide', () => {
            this.stopped = true;
            clearTimeout(this.timer);
            this.abortController?.abort();
        });
        window.addEventListener('pageshow', event => {
            if (event.persisted) {
                this.stopped = false;
                this.schedule(0);
            }
        });
        this.filter();
        this.schedule(this.interval);
    }

    schedule(delay) {
        clearTimeout(this.timer);
        this.nextRefreshAt = Date.now() + delay;
        if (this.stopped || document.hidden) return;
        // Следующий запрос планируется только после завершения предыдущего.
        this.timer = setTimeout(() => this.refresh(), delay);
    }

    beginMutation() {
        this.mutations += 1;
        this.revision += 1;
        let settled = false;
        return () => {
            if (settled) return;
            settled = true;
            this.mutations -= 1;
            this.revision += 1;
            if (!this.inFlight) this.schedule(1000);
        };
    }

    isBusy() {
        if (this.mutations || Date.now() - this.lastInputAt < 3000) return true;
        if (document.body.classList.contains('modal-open') ||
            document.querySelector('.modal.show, .dropdown-menu.show')) return true;
        const active = document.activeElement;
        if (!active || active === document.body || active === this.search) return false;
        // Не убирать сфокусированную кнопку/ссылку при работе с клавиатуры.
        return (this.keyboardNavigation && this.list.contains(active)) ||
            active.matches('input, textarea, select, [contenteditable="true"]');
    }

    filter() {
        const term = (this.search?.value || '').toLowerCase();
        this.list.querySelectorAll('.list-group-item').forEach(item => {
            item.style.display = item.textContent.toLowerCase().includes(term) ? '' : 'none';
        });
    }

    updateCsrf(token) {
        if (typeof token !== 'string' || !token) return;
        document.querySelector('meta[name="csrf-token"]')?.setAttribute('content', token);
        document.querySelectorAll('input[name="csrf_token"]').forEach(input => { input.value = token; });
    }

    apply(data) {
        const x = window.scrollX;
        const y = window.scrollY;
        let anchor;
        if (this.list.getBoundingClientRect().top < 0) {
            anchor = Array.from(this.list.querySelectorAll('[data-task-id]')).find(item =>
                item.getClientRects().length && item.getBoundingClientRect().bottom > 0);
        }
        const anchorId = anchor?.dataset.taskId;
        const anchorTop = anchor?.getBoundingClientRect().top;
        const parts = [
            ['tasks_html', this.list],
            ['counters_html', document.getElementById('indexCounters')],
            ['contract_counters_html', document.getElementById('indexContractCounters')],
        ];
        parts.forEach(([key, target]) => {
            if (data[key] !== this.lastHtml[key]) {
                // Снимаем экземпляры Bootstrap со старых кнопок перед удалением.
                target.querySelectorAll('[data-bs-toggle="dropdown"]').forEach(button => {
                    bootstrap.Dropdown.getInstance(button)?.dispose();
                });
                target.innerHTML = data[key];
                this.lastHtml[key] = data[key];
            }
        });
        this.filter();
        const updatedAnchor = anchorId && this.list.querySelector(`[data-task-id="${CSS.escape(anchorId)}"]`);
        const offset = updatedAnchor?.getClientRects().length
            ? updatedAnchor.getBoundingClientRect().top - anchorTop : 0;
        window.scrollTo({left: x, top: y + offset, behavior: 'instant'});
    }

    async refresh() {
        if (this.inFlight || this.stopped || document.hidden) return;
        if (this.isBusy()) {
            this.status.textContent = 'Список обновится после завершения ввода.';
            this.schedule(2000);
            return;
        }
        this.inFlight = true;
        const revision = this.revision;
        let delay = this.interval;
        this.abortController = new AbortController();
        const timeout = setTimeout(() => this.abortController.abort(), 15000);
        try {
            const url = new URL(this.root.dataset.refreshUrl, window.location.origin);
            url.searchParams.set('sort_by', this.root.dataset.sortBy);
            url.searchParams.set('order', this.root.dataset.order);
            const response = await fetch(url, {
                headers: {'X-Index-Refresh': '1', 'Accept': 'application/json'},
                credentials: 'same-origin', cache: 'no-store', signal: this.abortController.signal,
            });
            if (response.redirected || response.status === 401 || response.status === 403) {
                this.stopped = true;
                this.status.textContent = 'Обновление приостановлено. Проверьте вход в аккаунт и обновите страницу.';
                return;
            }
            if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
                throw new Error('Ответ обновления недоступен');
            }
            const data = await response.json();
            if (['tasks_html', 'counters_html', 'contract_counters_html'].some(key => typeof data[key] !== 'string')) {
                throw new Error('Неполный ответ обновления');
            }
            if (data.park !== this.root.dataset.park || data.sort_by !== this.root.dataset.sortBy ||
                data.order !== this.root.dataset.order) {
                this.stopped = true;
                this.status.textContent = 'Парк или сортировка изменены в другой вкладке. Обновите страницу.';
                return;
            }
            this.updateCsrf(data.csrf_token);
            // Пользователь мог начать ввод или изменить заявку, пока шёл запрос.
            // Такой снимок отбрасываем, после завершения действия запросим новый.
            if (document.hidden || this.isBusy() || revision !== this.revision) {
                delay = 2000;
                return;
            }
            this.apply(data);
            const time = new Date().toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
            this.status.textContent = `Список обновлён в ${time}`;
        } catch (error) {
            if (!this.stopped) {
                this.status.textContent = 'Не удалось обновить список. Повторим автоматически; текущие данные сохранены.';
            }
            delay = 30000;
        } finally {
            clearTimeout(timeout);
            this.inFlight = false;
            this.schedule(delay);
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('indexLiveView');
    if (root) window.indexLiveUpdates = new IndexLiveUpdates(root);
});
