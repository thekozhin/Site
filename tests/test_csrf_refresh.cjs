const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/csrf-refresh.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function browser() {
    let now = 1000000;
    const calls = [], nativeSubmits = [], xhrSends = [], forms = [], notices = new Map();
    const documentEvents = new Map(), windowEvents = new Map();
    const meta = {content: 'expired', setAttribute(name, value) { this[name] = value; }};
    const state = {fail: false, barrier: null, refreshes: 0};
    const snapshot = body => body instanceof FormData || body instanceof URLSearchParams
        ? Object.fromEntries(body.entries()) : body;
    class Form {
        constructor(method = 'post', action = 'https://setrans.online/save') {
            this.method = method; this.action = action;
            this.inputs = [{name: 'csrf_token', value: 'expired'}, {name: 'work', value: 'Ремонт дверей'}];
            forms.push(this);
        }
        submit() { nativeSubmits.push({form: this, inputs: this.inputs.map(x => ({...x})), submitter: null}); }
        requestSubmit(submitter) {
            const event = {target: this, submitter, defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() {}};
            for (const callback of documentEvents.get('submit') || []) callback(event);
            if (!event.defaultPrevented) nativeSubmits.push({form: this, inputs: this.inputs.map(x => ({...x})), submitter});
        }
        prepend(notice) { notices.set(notice.id, notice); }
    }
    class Xhr {
        constructor() { this.readyState = 0; this.headers = new Headers(); this.events = []; }
        open(method, url) { this.readyState = 1; this.method = method; this.url = url; }
        setRequestHeader(name, value) { this.headers.append(name, value); }
        send(body) { xhrSends.push({headers: new Headers(this.headers), body: snapshot(body)}); }
        abort() { this.readyState = 0; }
        dispatchEvent(event) { this.events.push(event.type); }
    }
    const document = {
        currentScript: {dataset: {refreshUrl: '/csrf-token'}},
        querySelector: () => meta,
        querySelectorAll: () => forms.flatMap(form => form.inputs.filter(x => x.name === 'csrf_token')),
        getElementById: id => notices.get(id),
        createElement: () => ({setAttribute() {}}),
        body: {prepend(notice) { notices.set(notice.id, notice); }},
        addEventListener(name, callback) {
            if (!documentEvents.has(name)) documentEvents.set(name, []);
            documentEvents.get(name).push(callback);
        },
    };
    const window = {
        async fetch(input, options = {}) {
            const url = typeof input === 'string' ? input : input.url;
            const method = options.method || input?.method || 'GET';
            calls.push({url, method, options, body: snapshot(options.body)});
            if (url === '/csrf-token') {
                state.refreshes++;
                if (state.barrier) await state.barrier;
                if (state.fail) return new Response('Unavailable', {status: 503});
                return new Response(JSON.stringify({csrf_token: `fresh-${state.refreshes}`}), {headers: {'content-type': 'application/json'}});
            }
            return new Response('{}', {headers: {'content-type': 'application/json'}});
        },
        addEventListener(name, callback) { windowEvents.set(name, callback); },
    };
    vm.runInNewContext(source, {window, document, location: {href: 'https://setrans.online/form', origin: 'https://setrans.online'},
        URL, URLSearchParams, FormData, Request, Response, Headers, AbortController,
        HTMLFormElement: Form, XMLHttpRequest: Xhr, Date: class extends Date { static now() { return now; } },
        ProgressEvent: class { constructor(type) { this.type = type; } }, setTimeout, clearTimeout});
    return {window, Form, Xhr, forms, calls, nativeSubmits, xhrSends, state, notices, meta,
        advance: milliseconds => { now += milliseconds; }, event: name => windowEvents.get(name)?.()};
}

test('fetch refreshes both stale FormData and header without altering entered work', async () => {
    const b = browser(), form = new b.Form(), data = new FormData();
    data.set('csrf_token', 'expired'); data.set('work', 'Ремонт дверей');
    await b.window.fetch('/save', {method: 'POST', body: data, headers: {'X-CSRFToken': 'expired'}});
    assert.equal(b.state.refreshes, 1);
    assert.equal(b.calls[1].options.headers.get('X-CSRFToken'), 'fresh-1');
    assert.equal(b.calls[1].body.csrf_token, 'fresh-1');
    assert.equal(b.calls[1].body.work, 'Ремонт дверей');
    assert.equal(form.inputs[0].value, 'fresh-1');
    assert.equal(form.inputs[1].value, 'Ремонт дверей');
    assert.equal(b.calls[0].options.cache, 'no-store');
});

test('long open tabs and focus changes obtain new tokens before mutations', async () => {
    const b = browser();
    await b.window.fetch('/save', {method: 'POST'});
    b.advance(7200000);
    await b.window.fetch('/save', {method: 'DELETE'});
    b.event('focus');
    await b.window.fetch('/save', {method: 'PATCH'});
    assert.equal(b.state.refreshes, 3);
    assert.equal(b.calls.at(-1).options.headers.get('X-CSRFToken'), 'fresh-3');
});

test('concurrent mutations share a refresh but each mutation is sent once', async () => {
    const b = browser(); let release;
    b.state.barrier = new Promise(resolve => { release = resolve; });
    const first = b.window.fetch('/one', {method: 'POST'});
    const second = b.window.fetch('/two', {method: 'POST'});
    assert.equal(b.calls.length, 1);
    release(); await Promise.all([first, second]);
    assert.equal(b.state.refreshes, 1);
    assert.deepEqual(b.calls.map(c => c.url), ['/csrf-token', '/one', '/two']);
});

test('failed refresh keeps work intact, sends no mutation, and allows later retry', async () => {
    const b = browser(), form = new b.Form(); b.state.fail = true;
    await assert.rejects(b.window.fetch('/save', {method: 'POST'}), /Введённые данные сохранены/);
    assert.deepEqual(b.calls.map(c => c.url), ['/csrf-token']);
    assert.equal(form.inputs[1].value, 'Ремонт дверей');
    assert.equal(b.notices.size, 1);
    b.state.fail = false;
    await b.window.fetch('/save', {method: 'POST'});
    assert.equal(b.calls.at(-1).url, '/save');
});

test('ordinary form refresh keeps the clicked save/delete submitter', async () => {
    const b = browser(), form = new b.Form();
    const submitter = {isConnected: true, name: 'action', value: 'delete', getAttribute() { return null; }};
    form.requestSubmit(submitter);
    assert.equal(b.nativeSubmits.length, 0);
    await tick(); await tick();
    assert.equal(b.nativeSubmits.length, 1);
    assert.equal(b.nativeSubmits[0].submitter, submitter);
    assert.equal(b.nativeSubmits[0].inputs[0].value, 'fresh-1');
    assert.equal(b.nativeSubmits[0].inputs[1].value, 'Ремонт дверей');
});

test('programmatic submit refreshes and suppresses duplicates while pending', async () => {
    const b = browser(), form = new b.Form();
    form.submit(); form.submit();
    await tick(); await tick();
    assert.equal(b.state.refreshes, 1);
    assert.equal(b.nativeSubmits.length, 1);
    assert.equal(b.nativeSubmits[0].inputs[0].value, 'fresh-1');
});

test('failed native form refresh preserves fields and permits another click', async () => {
    const b = browser(), form = new b.Form(); b.state.fail = true;
    form.requestSubmit(); await tick(); await tick();
    assert.equal(b.nativeSubmits.length, 0);
    assert.equal(form.inputs[1].value, 'Ремонт дверей');
    b.state.fail = false; form.requestSubmit(); await tick(); await tick();
    assert.equal(b.nativeSubmits.length, 1);
});

test('XHR replaces stale explicit CSRF header and encoded form body', async () => {
    const b = browser(), xhr = new b.Xhr(); xhr.open('POST', '/save');
    xhr.setRequestHeader('X-CSRFToken', 'expired');
    xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
    xhr.send('csrf_token=expired&work=repair'); await tick(); await tick();
    assert.equal(b.xhrSends.length, 1);
    assert.equal(b.xhrSends[0].headers.get('X-CSRFToken'), 'fresh-1');
    const data = new URLSearchParams(b.xhrSends[0].body);
    assert.equal(data.get('csrf_token'), 'fresh-1'); assert.equal(data.get('work'), 'repair');
});

test('aborted XHR is not sent when its refresh finishes', async () => {
    const b = browser(), xhr = new b.Xhr(); let release;
    b.state.barrier = new Promise(resolve => { release = resolve; });
    xhr.open('POST', '/save'); xhr.send(new FormData()); xhr.abort();
    release(); await tick(); await tick(); assert.equal(b.xhrSends.length, 0);
});

test('multipart Request refreshes the body without reusing its old boundary', async () => {
    const b = browser(), body = new FormData(); body.set('csrf_token', 'expired'); body.set('work', 'Долив ОЖ');
    const request = new Request('https://setrans.online/save', {method: 'POST', body});
    await b.window.fetch(request);
    const saved = b.calls.at(-1);
    assert.equal(saved.body.csrf_token, 'fresh-1'); assert.equal(saved.body.work, 'Долив ОЖ');
    assert.equal(saved.options.headers.has('content-type'), false);
    assert.equal(request.bodyUsed, false);
});

test('GET and other origins receive no injected token or refresh', async () => {
    const b = browser();
    await b.window.fetch('/list');
    await b.window.fetch('https://setrans.online.evil.example/save', {method: 'POST'});
    const form = new b.Form('get'); form.submit();
    assert.equal(b.state.refreshes, 0);
    assert.equal(b.calls[1].options.headers, undefined);
    assert.equal(b.nativeSubmits.length, 1);
});
