/* Обновление защиты для долгоживущих страниц без повторов изменяющих запросов. */
(() => {
    const script = document.currentScript;
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (!meta || !script?.dataset.refreshUrl || window.CsrfProtection) return;
    const refreshUrl = script.dataset.refreshUrl;
    const originalFetch = window.fetch;
    const unsafe = /^(POST|PUT|PATCH|DELETE)$/i;
    const errorMessage = 'Не удалось обновить защиту формы. Проверьте соединение и повторите действие. Введённые данные сохранены.';
    let refreshedAt = null;
    let pending = null;

    function sameOrigin(input) {
        try {
            const url = typeof input === 'string' || input instanceof URL ? input : input.url;
            return new URL(url, location.href).origin === location.origin;
        } catch (_) { return false; }
    }
    function apply(token) {
        meta.setAttribute('content', token);
        document.querySelectorAll('input[name="csrf_token"]').forEach(input => { input.value = token; });
    }
    function fresh() {
        return refreshedAt !== null && Date.now() - refreshedAt >= 0 && Date.now() - refreshedAt < 30000;
    }
    function showError(error, form) {
        let notice = document.getElementById('csrfRefreshError');
        if (!notice) {
            notice = document.createElement('div');
            notice.id = 'csrfRefreshError'; notice.className = 'alert alert-danger';
            notice.setAttribute('role', 'alert');
            (form || document.body).prepend(notice);
        }
        notice.textContent = errorMessage;
        notice.hidden = false;
    }
    function ensureFresh() {
        if (pending) return pending;
        if (fresh()) { apply(meta.content); return Promise.resolve(meta.content); }
        pending = (async () => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 10000);
            try {
                const response = await originalFetch.call(window, refreshUrl, {
                    method: 'GET', credentials: 'same-origin', cache: 'no-store',
                    headers: {Accept: 'application/json'}, signal: controller.signal,
                });
                if (!response.ok || response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
                    throw new Error(errorMessage);
                }
                const data = await response.json();
                if (typeof data.csrf_token !== 'string' || !data.csrf_token) throw new Error(errorMessage);
                apply(data.csrf_token); refreshedAt = Date.now();
                const notice = document.getElementById('csrfRefreshError');
                if (notice) notice.hidden = true;
                return data.csrf_token;
            } catch (_) { throw new Error(errorMessage); }
            finally { clearTimeout(timer); }
        })().finally(() => { pending = null; });
        return pending;
    }
    function updateBody(body, headers, token) {
        if (body instanceof FormData || body instanceof URLSearchParams) {
            if (body.has('csrf_token')) body.set('csrf_token', token);
        } else if (typeof body === 'string' && headers.get('content-type')?.includes('application/x-www-form-urlencoded')) {
            const fields = new URLSearchParams(body);
            if (fields.has('csrf_token')) { fields.set('csrf_token', token); return fields.toString(); }
        }
        return body;
    }
    window.CsrfProtection = {ensureFresh};

    window.fetch = async function (input, init = {}) {
        const method = (init.method || input?.method || 'GET').toUpperCase();
        if (!unsafe.test(method) || !sameOrigin(input)) return originalFetch.call(this, input, init);
        let token;
        try { token = await ensureFresh(); }
        catch (error) { showError(error); throw error; }
        const headers = new Headers(init.headers || input?.headers);
        headers.delete('X-CSRF-Token'); headers.set('X-CSRFToken', token);
        const options = {...init, headers};
        if (init.body !== undefined && init.body !== null) options.body = updateBody(init.body, headers, token);
        else if (input instanceof Request && input.body !== null) {
            const type = headers.get('content-type') || '';
            if (type.includes('multipart/form-data')) {
                options.body = updateBody(await input.clone().formData(), headers, token);
                headers.delete('content-type'); // Новая FormData требует новой multipart boundary.
            } else if (type.includes('application/x-www-form-urlencoded')) {
                options.body = updateBody(await input.clone().text(), headers, token);
            }
        }
        return originalFetch.call(this, input, options);
    };

    function protectedForm(form, submitter) {
        return unsafe.test(submitter?.getAttribute('formmethod') || form.method || 'GET') &&
            sameOrigin(submitter?.getAttribute('formaction') || form.action || location.href);
    }
    const waitingForms = new WeakSet();
    document.addEventListener('submit', event => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || !protectedForm(form, event.submitter)) return;
        if (fresh()) { apply(meta.content); return; }
        event.preventDefault(); event.stopImmediatePropagation();
        if (waitingForms.has(form)) return;
        waitingForms.add(form);
        const submitter = event.submitter;
        ensureFresh().then(() => {
            waitingForms.delete(form);
            if (submitter?.isConnected) form.requestSubmit(submitter);
            else form.requestSubmit();
        }).catch(error => { waitingForms.delete(form); showError(error, form); });
    }, true);

    const nativeSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function () {
        if (!protectedForm(this)) return nativeSubmit.call(this);
        if (fresh()) { apply(meta.content); return nativeSubmit.call(this); }
        if (waitingForms.has(this)) return;
        waitingForms.add(this);
        ensureFresh().then(() => {
            waitingForms.delete(this); nativeSubmit.call(this);
        }).catch(error => { waitingForms.delete(this); showError(error, this); });
    };

    const xhrs = new WeakMap();
    const open = XMLHttpRequest.prototype.open;
    const send = XMLHttpRequest.prototype.send;
    const setHeader = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function (method, url, async = true, ...rest) {
        xhrs.set(this, {protected: unsafe.test(method) && sameOrigin(url), async, headers: new Headers()});
        return open.call(this, method, url, async, ...rest);
    };
    XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
        const info = xhrs.get(this);
        if (info?.protected) {
            info.headers.set(name, value);
            if (/^x-csrf-?token$/i.test(name)) return;
        }
        return setHeader.call(this, name, value);
    };
    XMLHttpRequest.prototype.send = function (body) {
        const info = xhrs.get(this);
        if (!info?.protected) return send.call(this, body);
        const deliver = token => {
            if (this.readyState !== 1) return; // Запрос могли отменить во время обновления.
            setHeader.call(this, 'X-CSRFToken', token);
            send.call(this, updateBody(body, info.headers, token));
        };
        if (!info.async) {
            if (!fresh()) throw new Error(errorMessage);
            return deliver(meta.content);
        }
        ensureFresh().then(deliver).catch(error => {
            this.abort(); showError(error);
            this.dispatchEvent(new ProgressEvent('error'));
            this.dispatchEvent(new ProgressEvent('loadend'));
        });
    };
    // После возврата на вкладку/из истории проверяем токен заново при следующем действии.
    window.addEventListener('pageshow', () => { refreshedAt = null; });
    window.addEventListener('focus', () => { refreshedAt = null; });
})();
