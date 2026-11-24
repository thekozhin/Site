/* Редактор отдельных решений. Данные API выводятся только через textContent. */
window.IncontrolPlan = class IncontrolPlan {
    constructor(controller) {
        this.c=controller; this.form=controller.form;
        this.container=document.getElementById('incontrolChildPlan');
        this.emptyNotice=document.getElementById('incontrolEmptyNotice');
        this.field=this.form.elements.child_decisions;
        this.work=this.form.elements.completed_work;
        this.workWrapper=this.work.closest('.mb-3');
        this.confirm=document.getElementById('confirmCompleteBtn');
        this.modeInputs=[...this.form.querySelectorAll('input[name="main_outcome"]')];
        this.workLabel=this.workWrapper.querySelector('label');
        this.details=document.getElementById('closingDetails');
        this.description=document.getElementById('closureDescription');
        this.lastMode=this.mainMode();
        this.reasonDraft=this.lastMode==='reject'?this.work.value:'';
        this.generalDraft=this.lastMode!=='reject'?this.work.value:'';
        this.modeInputs.forEach(input=>input.addEventListener('change',()=>{
            this.applyMode();
            if (!this.c.offlineActive && !this.c.job && !this.c.busy) this.c.loadPlan();
        }));
        this.work.addEventListener('input',()=>this.update());
    }
    render(state, locked=false) {
        this.container.replaceChildren(); this.container.hidden=false;
        this.emptyNotice.hidden=true;this.emptyNotice.textContent='';
        this.items=state.child_plan || [];
        if (state.state==='choose') {
            this.items=null;
            const label=document.createElement('label'); label.className='form-label'; label.textContent='Выберите основную заявку InControl для этого ремонта';
            const select=document.createElement('select');select.className='form-select mb-2';select.setAttribute('aria-label','Основная заявка InControl');
            const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='Выберите заявку…';placeholder.disabled=true;placeholder.selected=true;select.append(placeholder);
            for (const candidate of state.candidates) {
                const option=document.createElement('option');option.value=candidate.id;
                option.textContent=`№${candidate.id} — ${candidate.title}${candidate.description ? ': '+candidate.description : ''}`;select.append(option);
            }
            if (state.allow_unrelated) {
                const option = new Option('Ни одна заявка не относится к этому ремонту', 'unrelated');
                select.append(option);
            }
            select.addEventListener('change',async()=>{
                const data=new FormData();data.set('action','select');data.set('remote_id',select.value);
                this.c.setBusy(true);this.c.message.textContent='Загружаем связанные задачи…';
                this.c.error.hidden=true;this.c.retryButton.hidden=true;
                try {await this.c.json(this.c.job.action_url,data);await this.c.poll();}catch(e){this.c.fail(e);}
            });
            this.container.append(label,select);this.workWrapper.hidden=true;this.work.required=false;this.confirm.disabled=true;
            return;
        }
        if (!this.items.length) {
            if (state.remote_closed) {
                this.container.textContent=`Заявка InControl №${state.remote_closed} уже отписана.`;
            } else {
                this.emptyNotice.textContent = state.unrelated_remote
                    ? 'У этого ремонта нет связанной заявки InControl'
                    : 'Заявки в InControl не найдено';
                this.emptyNotice.hidden=false;
            }
            this.cards=[];this.locked=locked;this.applyMode();return;
        }
        this.workWrapper.hidden=true;this.work.required=false;this.locked=locked;
        const heading=document.createElement('p');heading.className='fw-semibold mb-2';heading.textContent='Описание заявки:';this.container.append(heading);
        const saved=new Map((state.decisions || []).map(item=>[item.id,item]));
        if (locked && state.decisions?.length) this.field.value=JSON.stringify(state.decisions);
        this.cards=[];
        for (const [index,item] of this.items.entries()) {
            const prior=saved.get(item.id);
            const normalized=String(item.phase || '').toLowerCase().replace(/[.]/g,'').trim();
            const terminal=normalized==='выполнено' || normalized==='отклонено';
            let action=prior?.action || (terminal ? (normalized==='выполнено'?'complete':'reject') : '');
            const card=document.createElement('div');card.className='mb-3';
            const row=document.createElement('div');row.className='d-flex flex-wrap align-items-start justify-content-between gap-2 mb-1';
            const title=document.createElement('div');title.className='fw-semibold';title.textContent=`- ${item.title}`;title.title=`Связанная задача №${item.id}`;
            const buttons=document.createElement('div');buttons.className='btn-group btn-group-sm';buttons.setAttribute('role','group');buttons.setAttribute('aria-label',`Результат задачи ${item.id}`);
            const area=document.createElement('textarea');area.className='form-control';area.rows=2;area.maxLength=10000;
            area.setAttribute('aria-label',`Работы или причина отклонения задачи ${item.id}`);
            area.placeholder='';area.value=prior?.work || item.existing_work || '';
            area.disabled=locked || (terminal && item.services && normalized==='выполнено');
            const plan=this;
            const record={item,area,get action(){return plan.items.length===1 && !terminal && !locked ? plan.mainMode() : action;}};
            const choices=[];
            for (const [value,text,style] of (this.items.length>1 ? [['complete','Выполнено','success'],['reject','Отклонено','danger']] : [])) {
                const button=document.createElement('button');button.type='button';button.textContent=text;button.disabled=locked || terminal;
                const paint=()=>{button.className=`btn btn-${action===value?'':'outline-'}${style}`;button.setAttribute('aria-pressed',String(action===value));};
                choices.push(paint);button.addEventListener('click',()=>{action=value;choices.forEach(fn=>fn());this.update();});paint();buttons.append(button);
            }
            area.addEventListener('input',()=>this.update());row.append(title);if(this.items.length>1)row.append(buttons);card.append(row,area);
            if (terminal) {const note=document.createElement('div');note.className='small text-muted mt-1';note.textContent=`В InControl уже ${normalized}; повторно этап не меняем.`;card.append(note);}
            this.container.append(card);this.cards.push(record);
        }
        this.applyMode();
    }
    setConfirmLabel(text) {
        [...this.confirm.childNodes].filter(node=>node.nodeType===Node.TEXT_NODE).forEach(node=>node.remove());
        this.confirm.append(document.createTextNode(' '+text));
    }
    mainMode() {return this.modeInputs?.find(input=>input.checked)?.value || '';}
    applyMode() {
        const mode=this.mainMode();
        this.details.hidden=!mode;
        this.c.showOutcomeFields(mode);
        if (!mode) {this.confirm.disabled=true;return;}
        if (!this.locked) this.form.querySelector('#hidden_action').value=mode==='reject'?'reject':'';
        const hasChildren=Boolean(this.items?.length);
        if (this.lastMode!==mode) {
            if (this.lastMode==='reject') this.reasonDraft=this.work.value;
            else if (!hasChildren) this.generalDraft=this.work.value;
            if (mode==='reject') this.work.value=this.reasonDraft;
            else if (!hasChildren) this.work.value=this.generalDraft;
        }
        this.lastMode=mode;
        if (!Array.isArray(this.items)) {
            this.description.hidden=true;this.workWrapper.hidden=true;this.work.required=false;
            this.confirm.disabled=true;return;
        }
        this.container.hidden=!this.container.hasChildNodes() || (hasChildren && mode==='reject');
        this.description.hidden=hasChildren || mode==='reject';
        this.workWrapper.hidden=hasChildren && mode==='complete';
        this.work.required=!this.workWrapper.hidden;
        this.workLabel.textContent=mode==='reject'?'Причина отклонения:':'Выполненные работы:';
        if (mode==='reject' && !this.locked) this.field.value='';
        this.setConfirmLabel(this.c.offlineActive ? 'Закрыть в Сетранс' : 'Завершить');
        this.update();
    }
    update() {
        if (!this.mainMode() || this.locked || !Array.isArray(this.items)) {this.confirm.disabled=true;return;}
        if (this.mainMode()==='reject') {this.confirm.disabled=!this.work.value.trim();return;}
        if (!this.items.length) {
            this.confirm.disabled=!this.work.value.trim();return;
        }
        this.confirm.disabled=!this.cards?.every(card=>card.action && card.area.value.trim());
    }
    showOfflineDetails() {
        this.locked = false;
        this.items = [];
        this.cards = [];
        this.container.replaceChildren();
        this.emptyNotice.hidden = true;
        this.field.value = '';
        this.applyMode();
    }

    prepare() {
        if (!this.items) {this.c.showFlash('Сначала выберите основную заявку InControl.');return false;}
        if (this.mainMode()==='reject') {
            if (!this.work.value.trim()) {this.c.showFlash('Укажите общую причину отклонения.');return false;}
            this.field.value='';return 'reject';
        }
        if (!this.items.length) {
            this.field.value='';return 'complete';
        }
        if (!this.cards.every(card=>card.action && card.area.value.trim())) {this.c.showFlash('Заполните работы и выберите результат каждой задачи.');return false;}
        const decisions=this.cards.map(card=>({id:card.item.id,action:card.action,work:card.area.value.trim()})).sort((a,b)=>a.id-b.id);
        this.field.value=JSON.stringify(decisions);
        this.work.value=decisions.map(d=>d.work).join('\n');
        return 'complete';
    }
};
