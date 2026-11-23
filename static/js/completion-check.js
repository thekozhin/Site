/* Форма остаётся на странице до получения результата фоновой проверки. */
window.CompletionCheckController = class CompletionCheckController {
    constructor(form, options) {
        this.form = form;
        this.options = options;
        this.busy = false;
        this.submitting = false;
        this.savedDisabled = new Map();
        this.message = document.getElementById('completionCheckMessage');
        this.spinner = document.getElementById('completionCheckSpinner');
        this.notification = null;
        this.offlinePanel = document.getElementById('incontrolOfflineOffer');
        this.offlineButton = document.getElementById('incontrolOfflineClose');
        this.offlineButton.addEventListener('click', () => this.closeOffline());
    }

    setBusy(value) {
        this.busy = value;
        this.form.setAttribute('aria-busy', String(value));
        this.spinner.hidden = !value;
        this.message.hidden = !value;
        if (value) {
            this.form.querySelectorAll('input, textarea, button').forEach(element => {
                this.savedDisabled.set(element, element.disabled);
                element.disabled = true;
            });
        } else {
            this.savedDisabled.forEach((disabled, element) => { element.disabled = disabled; });
            this.savedDisabled.clear();
        }
    }

    showOutcomeFields(mode) {
        const details = document.getElementById('completionWorkDetails');
        details.hidden = mode !== 'complete';
        details.disabled = details.hidden && !this.options.resumeId;
        this.form.elements.mileage.required = !details.hidden;
        const submit = document.getElementById('confirmCompleteBtn');
        submit.classList.toggle('btn-success', mode === 'complete');
        submit.classList.toggle('btn-danger', mode === 'reject');
    }

    clearFlash() {
        if (!this.notification) return;
        bootstrap.Toast.getInstance(this.notification)?.dispose();
        this.notification.remove();
        this.notification = null;
    }

    showFlash(message) {
        this.clearFlash();
        let container = document.querySelector('.toast-container');
        if (!container) {
            container = document.createElement('div');
            container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
            document.body.append(container);
        }
        // Те же оформление и длительность, что у flash в base.html.
        const toastElement = document.createElement('div');
        toastElement.className = 'toast align-items-center text-bg-danger border-0';
        toastElement.setAttribute('role', 'alert');
        toastElement.setAttribute('aria-live', 'assertive');
        toastElement.setAttribute('aria-atomic', 'true');
        const row = document.createElement('div');
        row.className = 'd-flex';
        const body = document.createElement('div');
        body.className = 'toast-body';
        body.textContent = message;
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close btn-close-white me-2 m-auto';
        close.setAttribute('data-bs-dismiss', 'toast');
        close.setAttribute('aria-label', 'Закрыть');
        row.append(body, close);
        toastElement.append(row);
        container.append(toastElement);
        this.notification = toastElement;
        toastElement.addEventListener('hidden.bs.toast', () => {
            bootstrap.Toast.getInstance(toastElement)?.dispose();
            toastElement.remove();
            if (this.notification === toastElement) this.notification = null;
        }, {once: true});
        new bootstrap.Toast(toastElement, {delay: 4000}).show();
    }

    async request(url, method = 'GET') {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10000);
        try {
            const response = await fetch(url, {
                method, credentials: 'same-origin', cache: 'no-store',
                headers: {
                    'Accept': 'application/json',
                    'X-CSRFToken': this.form.elements.csrf_token.value,
                },
                signal: controller.signal,
            });
            if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
                throw new Error('Не удалось получить ответ сервера. Проверьте соединение и вход в систему. Введённые данные сохранены в форме.');
            }
            const result = await response.json();
            if (!response.ok) throw new Error(result.message || 'Не удалось выполнить проверку. Повторите попытку.');
            return result;
        } finally {
            clearTimeout(timer);
        }
    }

    offerOffline(field, id, needsDetails = false) {
        this.offlineProof = {field, id, needsDetails};
        this.offlinePanel.hidden = false;
        this.offlineButton.hidden = false;
    }

    clearOfflineOffer() {
        this.offlineProof = null;
        this.offlinePanel.hidden = true;
    }

    closeOffline() {
        const proof = this.offlineProof;
        if (!proof || this.submitting || this.offlineActive) return;
        this.setBusy(false);
        this.form.elements[proof.field].value = proof.id;
        if (proof.needsDetails) {
            this.offlineActive = true;
            this.offlineButton.hidden = true;
            this.plan?.showOfflineDetails();
            if (this.panel) this.panel.hidden = true;
            const signatureHint = document.getElementById('incontrolSignatureHint');
            if (signatureHint) signatureHint.hidden = true;
            this.form.elements.completed_work.focus();
            return;
        }
        this.finish('');
    }

    async submit() {
        if (this.busy || this.submitting) return;
        this.clearFlash();
        if (this.offlineActive || !this.options.required) {
            this.finish('');
            return;
        }
        this.clearOfflineOffer();
        this.form.elements.offline_check_id.value = '';
        this.setBusy(true);
        this.form.elements.completion_check_id.value = '';
        this.message.textContent = 'Проверяем InControl…';
        try {
            let result = await this.request(this.options.startUrl, 'POST');
            const deadline = Date.now() + 50000;
            const statusUrl = result.status_url;
            while (result.state === 'pending') {
                if (!statusUrl || Date.now() >= deadline) {
                    throw new Error('Проверка не завершилась вовремя. Заявка не закрыта. Повторите проверку.');
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
                result = await this.request(statusUrl);
            }
            if (result.state === 'unavailable') {
                this.setBusy(false);
                this.offerOffline('offline_check_id', result.check_id);
                return;
            }
            if (result.state !== 'ready') {
                throw new Error(result.message || 'Проверка не разрешила закрытие заявки. Повторите проверку.');
            }
            this.message.textContent = 'Проверка завершена. Сохраняем закрытие заявки…';
            this.finish(result.check_id);
        } catch (error) {
            this.setBusy(false);
            const message = error.name === 'AbortError'
                ? 'Сервер не ответил вовремя. Заявка не закрыта. Повторите проверку.'
                : error.message;
            this.showFlash(message);
        }
    }

    async finish(checkId) {
        if (this.submitting) return;
        this.submitting = true;
        try { await window.CsrfProtection?.ensureFresh(); }
        catch (error) {
            this.submitting = false; this.setBusy(false); this.showFlash(error.message); return;
        }
        this.setBusy(false);
        this.spinner.hidden = false;
        this.message.hidden = false;
        this.message.textContent = 'Сохраняем…';
        this.form.elements.completion_check_id.value = checkId || '';
        this.form.querySelectorAll('button[type="submit"]').forEach(button => { button.disabled = true; });
        // Восстановленные поля включены в обычный POST; печать и перенаправления сохраняются.
        HTMLFormElement.prototype.submit.call(this.form);
    }
};
