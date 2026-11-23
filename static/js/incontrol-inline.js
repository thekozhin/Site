/* Закрытие и подпись остаются в исходной форме. Код из письма не сохраняется. */
window.InlineIncontrolController = class InlineIncontrolController extends CompletionCheckController {
    constructor(form, options) {
        super(form, options);
        this.panel = document.getElementById('inlineIncontrol');
        this.error = document.getElementById('inlineIncontrolError');
        this.codePanel = document.getElementById('inlineIncontrolCodePanel');
        this.codeInput = document.getElementById('inlineIncontrolCode');
        this.continueButton = document.getElementById('inlineIncontrolContinue');
        this.retryButton = document.getElementById('inlineIncontrolRetry');
        this.resendButton = document.getElementById('inlineIncontrolResend');
        this.resendButton.addEventListener('click', () => this.resend());
        this.continueButton.addEventListener('click', () => this.sendCode());
        this.retryButton.addEventListener('click', () => this.retry());
        this.codeInput.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); this.sendCode(); }
        });
        this.plan = new IncontrolPlan(this);
        if (options.resumeId) {
            this.job = {run_id: options.resumeId, status_url: options.statusPattern.replace('RUN', options.resumeId), action_url: options.actionPattern.replace('RUN', options.resumeId)};
            this.setBusy(true);
            this.panel.hidden = false;
            this.poll();
        } else if (this.plan.mainMode()) { this.plan.applyMode(); this.loadPlan(); }
    }
    async loadPlan() {
        const data=new FormData(this.form);
        this.setBusy(true); this.panel.hidden=false;this.message.textContent='Загружаем заявки и связанные задачи InControl…';
        try {
            this.job=await this.json(this.options.planUrl,data);
            if (this.job.resume_url) {location.assign(this.job.resume_url);return;}
            await this.poll();
        } catch(e) {this.fail(e);}
    }
    async json(url, body) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(url, {method: body ? 'POST' : 'GET', body,
                credentials:'same-origin', cache:'no-store', signal:controller.signal,
                headers:{Accept:'application/json', 'X-CSRFToken':this.form.elements.csrf_token.value}});
            if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
                const html = new DOMParser().parseFromString(await response.text(), 'text/html');
                const text = [...html.querySelectorAll('.toast-body,.alert')].map(e => e.textContent.trim()).filter(Boolean).join(' ');
                const error=new Error(text || 'Сервер не подтвердил запуск. Проверьте данные и вход в систему.');
                error.isFormValidation=!response.redirected && response.status===200;throw error;
            }
            const result = await response.json();
            if (!response.ok) {const error=new Error(result.message || 'Не удалось выполнить действие. Проверьте состояние.');error.status=response.status;throw error;}
            return result;
        } finally { clearTimeout(timer); }
    }
    async submit() {
        if (this.offlineActive) return super.submit();
        if (this.busy || this.submitting) return;
        const data = new FormData(this.form);
        this.setBusy(true);
        this.panel.hidden = false;
        this.error.hidden = true;
        this.message.textContent = 'Проверяем InControl и подписываем документы…';
        try {
            this.job = await this.json(this.form.getAttribute('action') || location.href, data);
            const url = new URL(location.href);
            url.searchParams.set('rejection_run', this.job.run_id);
            history.replaceState(null, '', url);
            await this.poll();
        } catch (e) {
            if (e.isFormValidation || [400,409,422].includes(e.status)) {this.setBusy(false);this.plan.update();}
            this.fail(e);
        }
    }
    fail(e) {
        this.error.textContent = e.name === 'AbortError' ? 'Сервер не ответил вовремя. Повторите проверку состояния.' : e.message;
        this.error.hidden = false;
        this.spinner.hidden = true;
        this.message.textContent = 'Закрытие ещё не подтверждено.';
        this.retryButton.hidden = false;
        this.retryButton.disabled = false;
        if (!this.job) this.setBusy(false);
        if (this.state?.state === 'code') { this.codeInput.disabled=false; this.continueButton.disabled=false; }
    }
    async poll() {
        try {
            const deadline = Date.now() + 180000;
            while (Date.now() < deadline) {
                const result = await this.json(this.job.status_url);
                this.state = result;
                if (result.state === 'choose' || result.state === 'plan') {
                    this.setBusy(false);this.plan.render(result);this.error.hidden=true;this.retryButton.hidden=true;return;
                }
                if (result.decisions?.length && !this.plan.cards) this.plan.render(result,true);
                if (result.state === 'unavailable') {
                    this.setBusy(false);
                    this.codePanel.hidden = true;
                    this.error.hidden = true;
                    this.retryButton.hidden = false;
                    this.retryButton.disabled = false;
                    if (result.planning) {
                        this.plan.update();
                    } else {
                        this.plan.locked = true;
                        this.plan.update();
                    }
                    this.offerOffline('offline_run_id', this.job.run_id, result.planning);
                    return;
                }
                if (result.state === 'done' || result.state === 'no_remote') {
                    await window.CsrfProtection?.ensureFresh();
                    this.setBusy(false);
                    this.submitting = true;
                    this.form.querySelectorAll('button').forEach(button => { button.disabled = true; });
                    let field = this.form.elements.incontrol_rejection_run;
                    if (!field) {
                        field = document.createElement('input'); field.type='hidden'; field.name='incontrol_rejection_run'; this.form.appendChild(field);
                    }
                    field.value = this.job.run_id;
                    HTMLFormElement.prototype.submit.call(this.form);
                    return;
                }
                if (result.state === 'code') {
                    this.spinner.hidden = true;
                    this.message.textContent = 'InControl принял запрос кода. Доставка письма пока не подтверждена.';
                    this.codePanel.hidden = false;
                    this.codeInput.disabled = false;
                    this.continueButton.disabled = false;
                    this.resendButton.disabled = true;
                    clearTimeout(this.resendTimer);
                    const delay = Math.max(0, (Number(result.signature_requested_at || 0) * 1000 + 10000) - Date.now());
                    this.resendTimer = setTimeout(() => { this.resendButton.disabled = false; }, delay);
                    this.codeInput.focus();
                    return;
                }
                if (result.state === 'confirm') { await this.postAction('retry'); }
                else if (result.state === 'error') throw new Error(result.message || 'Проверка прервалась. Повторите её.');
                else if (result.state === 'consumed') throw new Error('Заявка уже сохранена. Обновите список заявок.');
                await new Promise(resolve => setTimeout(resolve, 1500));
            }
            throw new Error('Проверка заняла слишком много времени. Проверьте сохранённое состояние.');
        } catch (e) { this.fail(e); }
    }
    async postAction(action, code) {
        const data = new FormData(); data.set('action',action);
        if (action === 'sign' || action === 'resend') {
            data.set('step',this.state.step); data.set('document_id',this.state.document_id);
            if (code) data.set('code',code);
        }
        await this.json(this.job.action_url,data);
    }
    async sendCode() {
        const code = this.codeInput.value.trim();
        if (!/^[0-9]{4}$/.test(code)) { this.error.textContent='Введите четыре цифры кода.'; this.error.hidden=false; return; }
        this.continueButton.disabled=true;
        this.codeInput.disabled=true;
        this.error.hidden=true;
        try {
            await this.postAction('sign',code);
            this.codeInput.value=''; this.codePanel.hidden=true;
            this.spinner.hidden=false;
            this.message.textContent='Подписываем документы и проверяем результат…';
            await this.poll();
        } catch (e) { this.fail(e); }
    }
    async resend() {
        this.resendButton.disabled=true; this.continueButton.disabled=true; this.codeInput.disabled=true;
        this.error.hidden=true;
        try {
            await this.postAction('resend');
            this.codeInput.value=''; this.codePanel.hidden=true; this.spinner.hidden=false;
            this.message.textContent='Запрашиваем новый код в InControl…';
            await this.poll();
        } catch (e) { this.fail(e); this.resendButton.disabled=false; }
    }
    async retry() {
        if (!this.job) {await this.loadPlan();return;}
        this.clearOfflineOffer();
        this.form.elements.offline_run_id.value = '';
        if (!this.busy) this.setBusy(true);
        this.retryButton.disabled=true; this.error.hidden=true;
        this.codePanel.hidden=true; this.spinner.hidden=false;
        try { await this.postAction('retry'); await this.poll(); }
        catch (e) { this.fail(e); }
    }
};
