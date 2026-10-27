/* Standalone UI prototype. All execution is deterministic and local.
 * No model, microphone, network, credentials, or production workflow access.
 */
(() => {
  'use strict';
  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const paths = {
    back: '<path d="M20 12H4m6-6-6 6 6 6"/>',
    paperclip: '<path d="m9 17 8-8a3 3 0 0 0-4-4L4 14a5 5 0 0 0 7 7L21 11a7 7 0 0 0-10-10L2 10m5 5 8-8"/>',
    folder: '<path d="M3 7V4h6l3 3h9v13H3V7Z"/>',
    minus: '<path d="M5 12h14"/>',
    workflow: '<rect x="3" y="3" width="6" height="6" rx="1.5"/><rect x="15" y="15" width="6" height="6" rx="1.5"/><path d="M9 6h7a2 2 0 0 1 2 2v7M6 9v9h9"/>',
    history: '<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l3 2"/>',
    sliders: '<path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    sparkles: '<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3ZM20 2v4m-2-2h4"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5"/>',
    'chevron-down': '<path d="m6 9 6 6 6-6"/>',
    'chevron-right': '<path d="m9 6 6 6-6 6"/>',
    upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5"/>',
    play: '<path d="m8 5 11 7-11 7V5Z"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    'check-circle': '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    'x-circle': '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/>',
    circle: '<circle cx="12" cy="12" r="8"/>',
    shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9S4 17 4 12V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    audio: '<path d="M4 9v6m4-10v14m4-17v20m4-16v12m4-9v6"/>',
    mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/>',
    'mic-off': '<path d="m3 3 18 18M9 9v3a3 3 0 0 0 5 2M9 5a3 3 0 0 1 6 0v5M5 10v2a7 7 0 0 0 12 5m2-5v-2M12 19v3m-4 0h8"/>',
    focus: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5"/><circle cx="12" cy="12" r="3"/>',
    'arrow-up': '<path d="M12 19V5m-6 6 6-6 6 6"/>',
    'arrow-right': '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    phone: '<path d="m5 3 4 1 1 5-3 2a15 15 0 0 0 6 6l2-3 5 1 1 4c-1 5-8 2-13-3S0 4 5 3Z"/>',
    message: '<path d="M4 4h16v13H8l-4 4V4Z"/><path d="M8 8h8m-8 4h5"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18M8 15h2m4 0h2"/>',
    branch: '<circle cx="6" cy="4" r="2"/><circle cx="6" cy="20" r="2"/><circle cx="18" cy="6" r="2"/><path d="M6 6v12m0-4c6 0 12-2 12-6"/>',
    flag: '<path d="M5 21V3c5-4 9 4 14 0v11c-5 4-9-4-14 0"/>',
    tool: '<path d="M14 4a6 6 0 0 0-7 8l-5 5 5 5 5-5a6 6 0 0 0 8-7l-4 4-6-6 4-4Z"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5M5 7a8 8 0 0 1 14-2l1 3M4 16l1 3a8 8 0 0 0 14-2"/>',
    warning: '<path d="m12 3 10 18H2L12 3ZM12 9v5m0 3v1"/>',
    book: '<path d="M12 5c-4-3-9-1-9-1v15s5-2 9 1c4-3 9-1 9-1V4s-5-2-9 1Zm0 0v15"/>',
    users: '<circle cx="9" cy="7" r="4"/><path d="M2 21v-2a7 7 0 0 1 14 0v2M17 4a4 4 0 0 1 0 8m1 4a5 5 0 0 1 4 5"/>',
  };
  const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.circle}</svg>`;
  function decorate(scope = document) { $$('[data-icon]', scope).forEach((node) => { node.innerHTML = icon(node.dataset.icon); node.removeAttribute('data-icon'); }); }
  const CASES = [
    { id: 'normal', title: '正常完成预约', tag: '主流程', user: '我想约周二上午十点，我叫林然。', reply: '林然，您想预约周二上午十点，对吗？', final: '对，请帮我预约。', expected: '收集姓名和时间，明确确认后提交预约。', tool: 'create_booking', args: '{ name: "林然", time: "周二 10:00" }' },
    { id: 'change', title: '确认前临时改口', tag: '上下文更新', user: '等等，改成周三下午三点吧。', reply: '好的，已经为您预约了。', fixedReply: '好的，更新为周三下午三点。请确认，这个时间可以吗？', final: '可以，就这个时间。', expected: '以最新时间覆盖旧值，再次确认后提交。', tool: 'create_booking', args: '{ time: "周二 10:00" }', fixedArgs: '{ time: "周三 15:00", confirmed: true }', error: '提交了旧时间，而且没有再次确认。', node: 'confirm' },
    { id: 'missing', title: '客户未提供姓名', tag: '必填信息', user: '明天下午两点有空位吗？', reply: '我先帮您确认一下。请问该如何称呼您？', final: '我叫林然。', expected: '补齐必填信息，不能使用猜测的姓名。', tool: 'check_availability', args: '{ time: "明天 14:00" }' },
    { id: 'decline', title: '客户取消意向', tag: '退出分支', user: '算了，我还需要考虑一下。', reply: '没问题，我不会提交预约。您想好了随时可以联系我们。', expected: '尊重拒绝，结束对话，不提交预约。', tool: '未调用预约工具', args: 'booking_submitted: false' },
    { id: 'duplicate', title: '重复确认预约', tag: '重复请求', user: '确定，确定，请帮我预约。', reply: '已为您预约成功，周二上午十点见。', expected: '同一预约只能提交一次。', tool: 'create_booking', args: '{ submissions: 1, time: "周二 10:00" }' },
    { id: 'timeout', title: '预约工具超时', tag: '异常处理', user: '可以，帮我确认这个预约吧。', reply: '已经预约成功，期待您的到来。', fixedReply: '预约系统暂时没有返回结果，我还不能确认预约成功。我可以为您转交人工跟进。', expected: '如实说明尚未成功，给出后续处理方式。', tool: 'create_booking', args: '{ error: "timeout", booking_id: null }', error: '工具超时后仍告知客户预约成功。', node: 'fallback' },
  ];
  const NODE_DEFS = [
    { id: 'greeting', title: '开始通话', desc: '介绍服务，了解预约意向', icon: 'message', prompt: '你是一位亲切、简洁的预约助理。介绍服务并询问用户希望预约什么时间。', transition: '用户表达预约意向', vars: ['language: zh-CN'] },
    { id: 'collect', title: '收集预约信息', desc: '姓名 · 日期 · 时间', icon: 'book', prompt: '逐项询问姓名、日期和时间。信息不完整时继续追问，不要猜测。', transition: '姓名和时间完整', vars: ['customer_name', 'appointment_time'] },
    { id: 'confirm', title: '确认预约', desc: '复述信息，提交预约', icon: 'calendar', prompt: '复述预约信息，用户同意后调用预约工具。', fixedPrompt: '用户修改任何字段后，覆盖旧值并重新复述最新姓名和时间。获得明确确认后，使用最新值调用预约工具。', transition: '工具成功 → 结束；异常 → 异常处理', vars: ['create_booking', 'appointment_time'] },
    { id: 'fallback', title: '异常处理', desc: '预约失败时提供下一步', icon: 'branch', prompt: '处理预约返回结果，并向用户说明预约情况。', fixedPrompt: '仅在工具明确返回成功和预约编号时告知成功。超时或失败时，如实说明未确认成功，并提供人工跟进方式。', transition: '说明后续处理方式，结束通话', vars: ['booking_status', 'handoff_reason'] },
  ];
  const STORAGE = 'dograh-workbench-prototype-v1';
  function initialAgent(id = 'sample', name = '预约助手', fresh = false) {
    return { id, name, fresh, view: 'build', buildTab: 'canvas', selectedNode: 'confirm', selectedCase: 'change', version: fresh ? 0 : 2, published: fresh ? 0 : 1, revision: 0, fixed: false, prompts: {}, suiteRevision: 1, enabledCases: CASES.map((c) => c.id), maxTurns: 12, model: '组织默认模型', temperature: '0.4', baseline: null, candidate: null, history: fresh ? [] : [{ version: 1, title: '初始发布', detail: '基础预约流程', published: true }, { version: 2, title: '当前工作版本', detail: '加入确认步骤与异常处理', published: false }], messages: fresh ? [{ role: 'assistant', text: '告诉我 Agent 要帮谁、完成什么事情。你可以输入，也可以切换到语音协作演示。' }] : [{ role: 'user', text: '帮我做一个中文预约助手，收集姓名和时间，确认后再提交预约。' }, { role: 'assistant', text: '预约流程已经准备好了。接下来，我们可以用不同的客户场景验证它的表现。', checklist: ['4 个对话节点已配置', '预约工具使用测试数据', '6 个仿真场景已准备'], link: 'test', linkText: '查看测试场景' }] };
  }
  let agents = [initialAgent()];
  let state = agents[0];
  let role = 'admin';
  let theme = 'light';
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE) || 'null');
    if (saved?.schema === 1 && Array.isArray(saved.agents) && saved.agents.length && saved.agents.every((agent) => typeof agent.id === 'string' && Array.isArray(agent.messages) && Array.isArray(agent.enabledCases) && Array.isArray(agent.history) && agent.prompts && ['build', 'test', 'tune'].includes(agent.view))) {
      agents = saved.agents; state = agents.find((agent) => agent.id === saved.current) || agents[0];
      theme = saved.editorShell === 2 && saved.theme === 'dark' ? 'dark' : 'light';
    }
  } catch { /* Corrupt or unavailable local storage falls back to the example. */ }
  let panelMode = 'builder';
  let testerTab = 'audio';
  let inspectorOpen = false;
  let zoom = 1;
  let job = null;
  let voice = { open: false, phase: 'listening', muted: false, caption: '点击下方示例，体验说话驱动工作台。' };
  let voiceTimer;
  let toastTimer;
  let storageWarningShown = false;
  function persist() {
    try { localStorage.setItem(STORAGE, JSON.stringify({ schema: 1, editorShell: 2, agents, current: state.id, theme })); }
    catch { if (!storageWarningShown) { storageWarningShown = true; toast('浏览器无法保存演示状态；刷新后将恢复初始示例。'); } }
  }
  function toast(text) { clearTimeout(toastTimer); $('#toast').textContent = text; $('#toast').hidden = false; toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 4200); }
  function canEdit() { return role !== 'viewer'; }
  function requireEdit() { if (!canEdit()) { toast('当前为查看者预览：可以查看结果，不能修改或运行任务。'); return false; } return true; }
  const cases = () => CASES.filter((item) => state.enabledCases.includes(item.id));
  const node = (id) => NODE_DEFS.find((item) => item.id === id) || NODE_DEFS[2];
  const promptFor = (id) => state.prompts[id] ?? node(id).prompt;
  const pendingNode = () => Object.keys(state.promptDrafts || {}).find((id) => state.promptDrafts[id] !== promptFor(id));
  function requireSaved() {
    const id = pendingNode();
    if (!id) return true;
    state.view = 'build'; state.buildTab = 'canvas'; state.selectedNode = id; inspectorOpen = true; render();
    toast('节点有未保存的修改，请先保存，再执行测试或采用候选。'); return false;
  }
  const reportCurrent = (report) => report && report.revision === state.revision && report.suiteRevision === state.suiteRevision;
  const canPublish = () => role === 'admin' && !state.fresh && !job && !pendingNode() && reportCurrent(state.baseline) && state.baseline.passed === cases().length && state.version !== state.published;
  function addMessage(roleName, text, extra = {}) { state.messages.push({ role: roleName, text, ...extra }); state.messages = state.messages.slice(-70); persist(); renderConversation(); }
  function renderConversation() {
    const conversation = $('#conversation');
    const previousScroll = conversation.scrollTop;
    const follow = conversation.scrollHeight - previousScroll - conversation.clientHeight < 70;
    conversation.innerHTML = state.messages.map((message) => `<article class="message ${message.role === 'user' ? 'user' : 'assistant'} ${message.voice ? 'voice' : ''}"><div class="message-meta">${message.role === 'user' ? '' : icon('sparkles')}<span>${message.role === 'user' ? '你' : '构建助手'}</span>${message.voice ? '<span>语音字幕 · 演示</span>' : ''}</div><div class="message-body">${escape(message.text).replace(/\n/g, '<br>')}${(message.attachments || []).map((file) => attachmentCard(file, false)).join('')}${message.checklist ? `<ul class="checklist">${message.checklist.map((item) => `<li>${icon('check')}${escape(item)}</li>`).join('')}</ul>` : ''}${message.link ? `<button class="message-link" data-action="view" data-target="${escape(message.link)}">${icon(message.link === 'test' ? 'play' : 'sparkles')}${escape(message.linkText)}${icon('arrow-right')}</button>` : ''}</div></article>`).join('');
    if (follow) conversation.scrollTop = conversation.scrollHeight;
  }
  function renderChrome() {
    document.documentElement.dataset.theme = theme;
    $('#builder-content').hidden = panelMode !== 'builder';
    $('#tester-content').hidden = panelMode !== 'tester';
    $('#builder-switch').setAttribute('aria-pressed', String(panelMode === 'builder'));
    $('#tester-switch').setAttribute('aria-pressed', String(panelMode === 'tester'));
    renderAttachments();
    if (panelMode === 'tester') renderTester();
    $('#assistant-panel').classList.toggle('voice-open', voice.open);
    $('#agent-name').textContent = state.name;
    $('#workspace-title').textContent = state.fresh ? '创建你的 Agent' : state.name;
    $('#agent-description').textContent = state.fresh ? '从一个目标开始，用文字或语音把它变成可测试的对话。' : '收集预约信息、确认时间，为客户完成一次可靠的预约。';
    $('#working-version').textContent = state.fresh ? '尚未生成' : `v${state.version} (${state.published === state.version ? 'Published' : 'Draft'})`;
    $('#draft-state').textContent = state.fresh ? '等待构建' : state.published === state.version ? '与线上一致' : '未发布修改';
    $('#footer-context').textContent = `${state.fresh ? '尚未构建' : `工作版本 v${state.version}`} · ${state.published ? `线上版本 v${state.published}` : '尚未发布'}`;
    $('#saved-status').innerHTML = `${icon('check')}${job ? '任务进行中' : pendingNode() ? '节点修改未保存' : '本机已保存'}`;
    $('#test-count').textContent = String(cases().length);
    $('#tune-dot').hidden = !(reportCurrent(state.baseline) && state.baseline.failed > 0);
    $('#team-label').textContent = `OpenInsight 空间 · ${{ admin: '管理员', editor: '编辑者', viewer: '查看者' }[role]}`;
    $('#save-button').disabled = !pendingNode() || !canEdit() || !!job;
    $('#publish-button').disabled = !canPublish();
    $('#publish-button').title = role !== 'admin' ? '仅管理员可以发布（权限演示）' : canPublish() ? '预览发布流程' : '当前版本通过全部场景后可预览发布';
    $('#assistant-context').textContent = state.fresh ? '新建 Agent / 需求描述' : `${state.name} / ${state.view === 'build' ? node(state.selectedNode).title : state.view === 'test' ? '测试结果' : '候选修改'}`;
    $$('[data-view]').forEach((button) => { button.setAttribute('aria-selected', String(button.dataset.view === state.view)); });
    $('#workspace-content').setAttribute('aria-labelledby', `tab-${state.view}`);
    $('#voice-toggle').setAttribute('aria-pressed', String(voice.open));
    $('#quick-actions').innerHTML = state.fresh ? '<button data-action="example-requirement">用预约助手示例开始</button>' : `<button data-action="run-tests" ${job || !canEdit() ? 'disabled' : ''}>跑一轮测试</button><button data-action="explain">解释当前节点</button><button data-action="view" data-target="tune">查看调优建议</button>`;
    renderTask();
  }
  function render() { renderChrome(); renderMain(); renderConversation(); renderVoice(); persist(); }
  function setView(view) { if (!['build', 'test', 'tune'].includes(view)) return; state.view = view; render(); }
  function renderMain() {
    const target = $('#workspace-content');
    if (state.fresh) { target.innerHTML = welcomeView(); renderAttachments(); return; }
    target.innerHTML = state.view === 'build' ? buildView() : state.view === 'test' ? testView() : tuneView();
  }
  function welcomeView() {
    return `<section class="welcome"><div class="welcome-mark">${icon('sparkles')}</div><div class="eyebrow">BUILD WITH CODEX</div><h2>让一个想法，<br>开始对话。</h2><p>描述 Agent 的服务对象、任务和边界。构建助手会把需求整理成流程，再陪你测试和改进。</p><form id="welcome-form" class="welcome-form" data-dropzone="true"><label class="sr-only" for="welcome-request">Agent 需求</label><textarea class="field-input" id="welcome-request" placeholder="例如：为我的门店创建预约助手，收集姓名和时间，确认后提交预约…" ${job || !canEdit() ? 'disabled' : ''}>${escape(state.requirementDraft || '')}</textarea><div id="welcome-attachments" class="attachment-list"></div><button type="button" class="upload-dropzone" data-action="attach" ${job || !canEdit() ? 'disabled' : ''}>${icon('paperclip')}<strong>添加业务资料或拖入文件</strong><span>PDF、Word、表格、文本、图片 · 每份 ≤ 20 MB</span></button><div class="welcome-actions"><button type="button" class="button" data-action="voice">${icon('audio')}用语音描述</button><button class="button primary" type="submit" ${job || !canEdit() ? 'disabled' : ''}>${icon('sparkles')}开始构建</button></div></form><div class="welcome-examples"><button data-action="example-requirement">门店预约</button><button data-action="example-requirement" data-example="售后">售后问题分流</button><button data-action="example-requirement" data-example="回访">客户满意度回访</button></div><p class="setting-note">原型以预约流程演示构建结果；真实需求生成在后续接入。</p></section>`;
  }
  function buildView() {
    const tabs = [['canvas', 'workflow', 'Canvas'], ['model', 'sparkles', 'Model'], ['variables', 'book', 'Variables'], ['settings', 'sliders', 'Settings']];
    return `<div class="build-toolbar"><div class="segmented" aria-label="构建内容">${tabs.map(([id, glyph, label]) => `<button data-build-tab="${id}" aria-pressed="${state.buildTab === id}">${icon(glyph)}${label}</button>`).join('')}</div></div>${state.buildTab === 'canvas' ? canvasView() : settingsView()}`;
  }
  function canvasView() {
    const selected = node(state.selectedNode);
    const positions = { greeting: [225, 20], collect: [65, 170], confirm: [385, 290], fallback: [65, 410] };
    return `<div class="build-body"><section class="flow-canvas" aria-label="预约流程画布"><div class="canvas-watermark">dograh</div><span class="canvas-label">${escape(state.name)} <span>· 流程示例</span></span><div class="diagram-viewport"><div class="diagram-stage" style="transform:scale(calc(var(--diagram-scale, .82) * ${zoom}))"><svg class="diagram-edges" viewBox="0 0 660 590" fill="none" aria-hidden="true"><path d="M320 118V145H160V170M160 268V315H385M480 388V445H255M160 508V550H320V562M480 388V550H320M480 290V212H255"/><text x="184" y="140">预约意向</text><text x="280" y="309">信息完整</text><text x="292" y="439">工具异常</text><text x="488" y="495">预约成功</text><text x="285" y="205">修改信息</text></svg>${NODE_DEFS.map((item) => `<button class="diagram-node ${item.id} " style="left:${positions[item.id][0]}px;top:${positions[item.id][1]}px" data-node="${item.id}" aria-pressed="${inspectorOpen && state.selectedNode === item.id}"><span class="node-kind">${item.id === 'greeting' ? 'Start Node' : item.id === 'fallback' ? 'Fallback Node' : 'Agent Node'}</span><span class="node-heading">${icon(item.icon)}${item.title}</span><span class="node-description">${escape(promptFor(item.id))}</span><span class="node-foot">${item.id === 'confirm' ? 'create_booking' : item.id === 'collect' ? '2 个必填变量' : item.id === 'fallback' ? 'handoff_to_staff' : '预约主流程'}</span></button>`).join('')}<div class="diagram-end">${icon('flag')}结束通话</div></div></div><div class="canvas-controls"><button class="icon-button" data-action="zoom-in" aria-label="放大画布">${icon('plus')}</button><button class="icon-button" data-action="zoom-out" aria-label="缩小画布">${icon('minus')}</button><button class="icon-button" data-action="zoom-fit" aria-label="适应画布">${icon('focus')}</button><span>${Math.round(zoom * 100)}%</span></div><div class="canvas-help">点选节点编辑 · 从右侧开始 AI 构建</div></section>${inspectorOpen ? `<aside class="inspector" aria-label="节点详情"><div class="inspector-top">${icon(selected.icon)}${selected.title}<button class="icon-button" data-action="close-inspector" aria-label="关闭节点详情">${icon('close')}</button></div><label class="field-label" for="node-prompt">节点提示词</label><textarea id="node-prompt" class="field-input" ${job || !canEdit() ? 'disabled' : ''}>${escape(state.promptDrafts?.[selected.id] ?? promptFor(selected.id))}</textarea><div><span class="field-label">工具与变量</span><div class="field-tags">${selected.vars.map((item) => `<span class="field-tag">${item}</span>`).join('')}</div><span class="field-label" style="margin-top:16px">转移条件</span><p class="setting-note">${selected.transition}</p></div><button class="button primary" data-action="save-node" ${job || !canEdit() ? 'disabled' : ''}>${icon('check')}保存修改</button><button class="button ghost" data-action="explain">${icon('sparkles')}让助手解释此节点</button></aside>` : ''}</div>`;
  }
  function settingsView() {
    if (['tools', 'settings'].includes(state.buildTab)) return `<section class="settings-view"><h2>Agent 工具</h2><p>工作流可以使用的业务能力。仿真时使用预设测试响应。</p>${[['calendar', 'create_booking', '创建预约 · 包含姓名、时间与确认状态'], ['clock', 'check_availability', '查询可预约时段'], ['users', 'handoff_to_staff', '将未解决的问题交给人工']].map(([image, title, detail]) => `<div class="tool-row"><div class="tool-icon">${icon(image)}</div><div><strong>${title}</strong><p>${detail}</p></div><span class="badge warm">仿真替身</span></div>`).join('')}<p class="setting-note">此原型不调用真实业务接口。</p></section>`;
    if (state.buildTab === 'variables') return `<section class="settings-view"><h2>对话变量</h2><p>这些字段由 Agent 在对话中收集，并传给预约工具。</p>${[['customer_name', '客户姓名', '必填 · 字符串'], ['appointment_time', '预约时间', '必填 · 日期和时间'], ['booking_status', '预约状态', '工具返回'], ['handoff_reason', '人工跟进原因', '异常时填写']].map(([key, label, detail]) => `<div class="tool-row"><div><strong>${label} <span class="mono muted">${key}</span></strong><p>${detail}</p></div></div>`).join('')}</section>`;
    return `<section class="settings-view"><h2>模型与对话设置</h2><p>为 Agent 选择对话模型。构建助手的配置由工作空间统一管理。</p><form id="model-form" class="settings-grid"><div class="settings-field full"><label class="field-label" for="model-select">对话模型</label><select class="field-input" id="model-select" ${job || !canEdit() ? 'disabled' : ''}>${['组织默认模型', '低延迟模型预设', '高质量模型预设'].map((name) => `<option ${state.model === name ? 'selected' : ''}>${name}</option>`).join('')}</select><div class="setting-note">演示预设，后续关联组织内已配置的模型。</div></div><div class="settings-field"><label class="field-label" for="temperature">随机性</label><input class="field-input" id="temperature" type="number" min="0" max="1" step="0.1" value="${escape(state.temperature)}" ${job || !canEdit() ? 'disabled' : ''}></div><div class="settings-field"><span class="field-label">对话语言</span><div class="field-input">中文（普通话）</div></div><div class="settings-field full"><button class="button" ${job || !canEdit() ? 'disabled' : ''}>${icon('check')}保存工作版本</button></div></form></section>`;
  }
  function resultFor(item, candidateRun) {
    const repaired = item.node && state.prompts[item.node] === node(item.node).fixedPrompt;
    return item.error && !(candidateRun || repaired) ? 'failed' : 'passed';
  }
  function statusIcon(status) { return `<span class="case-status ${status === 'passed' ? 'good' : status === 'failed' ? 'bad' : 'dim'}">${icon(status === 'passed' ? 'check-circle' : status === 'failed' ? 'x-circle' : status === 'running' ? 'clock' : 'circle')}</span>`; }
  function testView() {
    const report = state.baseline;
    const current = reportCurrent(report);
    const running = job?.type === 'test';
    const item = cases().find((entry) => entry.id === state.selectedCase) || cases()[0];
    const progress = running ? job.completed : 0;
    return `<div class="view-header"><div><h2>让真实场景，检验每个细节。</h2><p>模拟客户与 Agent 多轮对话，检查流程、信息收集和工具调用。</p></div><div class="view-header-actions"><button class="button small" data-action="suite">${icon('sliders')}场景设置</button><button class="button primary small" data-action="run-tests" ${job || !canEdit() ? 'disabled' : ''}>${icon('play')}${report ? '重新测试' : '运行测试'}</button></div></div><div class="suite-meta"><span>预约场景集 v${state.suiteRevision}</span><span class="sep">/</span><span>${cases().length} 个场景</span><span class="sep">/</span><span>每场景最多 ${state.maxTurns} 轮</span><span class="badge subtle">工具使用测试数据</span></div><div class="evaluation-summary"><div class="score ${current && report.failed ? 'accent' : current ? 'good' : 'muted'}">${running ? progress : current ? report.passed : '—'}<small> / ${cases().length}</small></div><div class="summary-body"><strong>${running ? '正在运行文本仿真' : current ? report.failed ? `${report.failed} 个场景需要改进` : '全部场景通过' : report ? '工作版本已更新，请重新测试' : '准备好验证你的 Agent'}</strong><p>${running ? `已完成 ${progress} 个场景 · 示例执行` : current ? `工作版本 v${report.version} · 示例结果 · ${report.failed ? '点选失败场景查看证据' : '可以查看结果或预览发布'}` : '测试会记录对话、工具参数和每条断言的结果。'}</p>${running ? `<div class="progress-track"><div class="progress-fill" style="width:${progress / cases().length * 100}%"></div></div>` : ''}</div>${current && report.failed ? `<button class="button small" data-action="view" data-target="tune">${icon('sparkles')}修复失败场景</button>` : ''}</div><div class="test-layout"><div class="case-list"><div class="case-list-label">测试场景</div>${cases().map((entry, index) => { const status = running ? index < progress ? resultFor(entry, job.fixed) : index === progress ? 'running' : 'pending' : current ? report.results[entry.id] : 'pending'; return `<button class="case-button" data-case="${entry.id}" aria-pressed="${entry.id === item.id}">${statusIcon(status)}<span><strong>${entry.title}</strong><small>${entry.tag} · ${status === 'passed' ? '通过' : status === 'failed' ? '失败' : status === 'running' ? '运行中' : '待测试'}</small></span></button>`; }).join('')}</div><div class="case-detail">${current ? caseDetail(item, report) : `<div class="case-detail-header"><h3>${item.title}</h3><span class="badge subtle">${running ? '执行中' : '场景定义'}</span></div><span class="field-label">模拟客户会说</span><p style="font-size:12px">“${item.user}”</p><span class="field-label" style="margin-top:24px">通过条件</span><p class="setting-note">${item.expected}</p><div class="empty-state" style="min-height:130px;padding:20px 0"><p>${running ? '对话与执行证据会在本轮完成后出现。' : '运行后，在这里查看完整对话和工具调用。'}</p></div>`}</div></div>`;
  }
  function caseDetail(item, report) {
    const failed = report.results[item.id] === 'failed';
    const fixed = !failed && Boolean(item.error);
    return `<div class="case-detail-header"><h3>${item.title}</h3><span class="badge ${failed ? 'fail' : 'success'}">${failed ? '失败' : '通过'}</span></div><div class="transcript-line"><span class="speaker-avatar">客</span><div><div class="speaker">模拟客户</div><p>${item.user}</p></div></div><div class="transcript-line"><span class="speaker-avatar agent">A</span><div><div class="speaker">${escape(state.name)}</div><p>${fixed ? item.fixedReply : item.reply}</p></div></div>${item.final ? `<div class="transcript-line"><span class="speaker-avatar">客</span><div><div class="speaker">模拟客户</div><p>${item.final}</p></div></div>` : ''}<div class="tool-event">${icon('tool')} ${item.tool}<code>${escape(fixed && item.fixedArgs ? item.fixedArgs : item.args)}</code></div><div class="assertion ${failed ? '' : 'pass'}"><strong>${icon(failed ? 'x-circle' : 'check-circle')} ${failed ? '断言未通过' : '断言通过'}</strong>${failed ? item.error : item.expected}</div>${failed ? `<button class="plain" data-action="fix-case" data-id="${item.id}">${icon('sparkles')}让助手分析并修复${icon('arrow-right')}</button>` : ''}`;
  }
  function tuneView() {
    const baseline = state.baseline;
    const header = `<div class="view-header"><div><h2>每一次修改，都有依据。</h2><p>从失败证据出发，比较候选修改，再用同一组场景回归。</p></div><span class="badge subtle">保留当前版本</span></div>`;
    if (!reportCurrent(baseline)) return `${header}<div class="empty-state"><div class="empty-icon">${icon('sparkles')}</div><h3>先完成一轮测试</h3><p>助手会结合当前版本的失败对话提出修改，测试结果更新后再开始调优。</p><button class="button primary" data-action="run-tests" ${job || !canEdit() ? 'disabled' : ''}>${icon('play')}运行文本仿真</button></div>`;
    if (!baseline.failed && !state.candidate) return `${header}<div class="empty-state"><div class="empty-icon good">${icon('check-circle')}</div><h3>当前测试场景全部通过</h3><p>可以继续增加更复杂的场景，或查看版本历史并预览发布。</p><button class="button" data-action="suite">${icon('sliders')}查看场景设置</button></div>`;
    const candidate = state.candidate;
    const failedCases = cases().filter((item) => baseline.results[item.id] === 'failed');
    const boundary = `<div class="optimization-boundary"><span>${icon('focus')}只修改失败节点</span><span>${icon('shield')}保持测试条件一致</span><span>${icon('refresh')}最多 3 轮迭代</span></div>`;
    if (!candidate) return `${header}<section class="tune-content">${failedCases.map((item) => `<div class="tune-issue">${icon('warning')}<span>${item.error}</span><span>${node(item.node).title}</span></div>`).join('')}${boundary}<button class="button primary" data-action="generate-fix" ${job || !canEdit() ? 'disabled' : ''}>${icon('sparkles')}${job?.type === 'tune' ? '正在分析与生成修改…' : '生成修复方案'}</button><p class="setting-note">生成候选版本后，先回归测试，再决定是否采用。</p></section>`;
    const regression = candidate.report;
    const stale = candidate.baseRevision !== state.revision || candidate.suiteRevision !== state.suiteRevision;
    return `${header}<section class="tune-content"><div class="tune-issue">${icon('sparkles')}<span>候选 v${candidate.version} · 修复 ${candidate.nodes.length} 个节点</span><span>基于工作版本 v${candidate.baseVersion}</span></div>${boundary}${candidate.nodes.map((id) => `<div class="diff-section"><div class="diff-section-heading"><span>${icon(node(id).icon)} ${node(id).title}</span><span class="badge subtle">提示词修改</span></div><div class="diff-grid"><div class="diff-cell"><span class="diff-label">当前工作版本</span><p>${escape(candidate.before[id])}</p></div><div class="diff-cell added"><span class="diff-label">候选修改</span><p>${escape(node(id).fixedPrompt)}</p></div></div></div>`).join('')}<table class="regression-table"><thead><tr><th>同一场景集</th><th>当前 v${candidate.baseVersion}</th><th>候选 v${candidate.version}</th></tr></thead><tbody>${cases().map((item) => `<tr><td>${item.title}</td><td class="${baseline.results[item.id] === 'failed' ? 'bad' : 'good'}">${baseline.results[item.id] === 'failed' ? '失败' : '通过'}</td><td class="${regression ? 'good' : 'dim'}">${regression ? '通过' : job?.type === 'regression' ? '运行中' : '待回归'}</td></tr>`).join('')}</tbody></table><div class="candidate-actions"><span class="candidate-note">${stale ? '工作版本已变化，需要重新生成候选' : regression ? '示例回归通过 · 采用后仍需发布' : '回归通过后可以采用'}</span><button class="button ghost" data-action="discard-candidate" ${job || !canEdit() ? 'disabled' : ''}>放弃候选</button>${regression ? `<button class="button primary" data-action="apply-candidate" ${job || stale || !canEdit() ? 'disabled' : ''}>${icon('check')}采用候选 v${candidate.version}</button>` : `<button class="button primary" data-action="regression" ${job || stale || !canEdit() ? 'disabled' : ''}>${icon('play')}运行回归测试</button>`}</div></section>`;
  }
  function renderTask() {
    const target = $('#task-status'); target.hidden = !job;
    if (job) target.innerHTML = `<div class="task-row"><span class="task-dot"></span><span>${escape(job.labels[Math.min(job.completed, job.labels.length - 1)])}</span><button data-action="stop-task">停止任务</button></div><div class="progress-track"><div class="progress-fill" style="width:${job.completed / job.labels.length * 100}%"></div></div>`;
  }
  function startJob(type, labels, complete) {
    if (!requireEdit()) return;
    if (job) { toast('已有任务正在运行，可以等待或先停止。'); return; }
    const active = { type, labels, completed: 0, fixed: type === 'regression', agentId: state.id, timer: null };
    job = active; render();
    const tick = () => {
      if (job !== active || state.id !== active.agentId) return;
      active.completed += 1;
      if (active.completed === labels.length) { job = null; complete(); render(); return; }
      renderChrome(); renderMain();
      active.timer = setTimeout(tick, 800);
    };
    active.timer = setTimeout(tick, 800);
  }
  function stopTask(announce = true) {
    if (!job) { if (announce) toast('当前没有正在运行的任务。'); return; }
    clearTimeout(job.timer); job = null;
    if (announce) addMessage('assistant', '任务已停止，未完成的结果不会用于采用或发布。已保存的工作版本仍然保留。');
    render();
  }
  function makeReport(fixed, version = state.version) {
    const results = Object.fromEntries(cases().map((item) => [item.id, resultFor(item, fixed)]));
    const passed = Object.values(results).filter((value) => value === 'passed').length;
    return { results, passed, failed: cases().length - passed, version, revision: state.revision, suiteRevision: state.suiteRevision };
  }
  function runTests() {
    if (!requireEdit() || job) return;
    if (!requireSaved()) return;
    if (state.fresh) { toast('先生成 Agent，再运行测试。'); return; }
    state.view = 'test';
    addMessage('assistant', `开始验证工作版本 v${state.version}，共 ${cases().length} 个场景。你可以继续查看画布，也可以停止任务。`);
    startJob('test', cases().map((item) => `仿真中 · ${item.title}`), () => {
      state.baseline = makeReport(false); state.candidate = null;
      const firstFailure = cases().find((item) => state.baseline.results[item.id] === 'failed');
      state.selectedCase = firstFailure?.id || cases()[0].id;
      addMessage('assistant', state.baseline.failed ? `本轮示例测试 ${state.baseline.passed}/${cases().length} 通过。发现 ${state.baseline.failed} 个问题：${cases().filter((item) => state.baseline.results[item.id] === 'failed').map((item) => item.error).join(' ')}可以查看证据，再生成修复方案。` : `本轮示例测试 ${state.baseline.passed}/${cases().length} 通过。当前工作版本已通过所选场景，可以继续增加测试，或预览发布。`, { link: state.baseline.failed ? 'tune' : 'test', linkText: state.baseline.failed ? '查看失败与调优建议' : '查看测试结果' });
      voiceSummary(state.baseline.failed ? '测试完成，发现需要改进的场景。' : '当前测试场景全部通过。');
    });
  }
  function generateFix() {
    if (!requireEdit() || job) return;
    if (!requireSaved()) return;
    if (!reportCurrent(state.baseline) || !state.baseline.failed) { state.view = 'tune'; render(); return; }
    state.view = 'tune';
    startJob('tune', ['读取失败对话与工具参数', '定位节点提示词与转移条件', '生成候选修改'], () => {
      const nodes = [...new Set(cases().filter((item) => state.baseline.results[item.id] === 'failed').map((item) => item.node))];
      state.candidate = { version: state.version + 1, baseVersion: state.version, baseRevision: state.revision, suiteRevision: state.suiteRevision, nodes, before: Object.fromEntries(nodes.map((id) => [id, promptFor(id)])), report: null };
      addMessage('assistant', `候选 v${state.candidate.version} 已准备好。修改了${nodes.map((id) => `“${node(id).title}”`).join('和')}，接下来用原测试集回归，确认没有破坏其他场景。`, { link: 'tune', linkText: '比较修改并运行回归' });
      voiceSummary('修复方案已经显示在画面中，接下来可以运行回归测试。');
    });
  }
  function runRegression() {
    if (!requireEdit() || job || !state.candidate) return;
    const candidate = state.candidate;
    if (candidate.baseRevision !== state.revision || candidate.suiteRevision !== state.suiteRevision) { toast('候选已过期，请基于当前工作版本重新生成。'); return; }
    state.view = 'tune';
    startJob('regression', cases().map((item) => `回归中 · ${item.title}`), () => {
      candidate.report = makeReport(true, candidate.version);
      addMessage('assistant', `候选 v${candidate.version} 的示例回归 ${cases().length}/${cases().length} 通过。原有正常场景保持通过，可以采用这个候选作为工作版本。`, { link: 'tune', linkText: '查看对比并采用候选' });
      voiceSummary('回归测试通过，修改没有引入新的失败场景。');
    });
  }
  function applyCandidate() {
    if (!requireEdit() || job) return;
    if (!requireSaved()) return;
    const candidate = state.candidate;
    if (!candidate?.report || candidate.baseRevision !== state.revision || candidate.suiteRevision !== state.suiteRevision) { toast('先完成当前候选的回归测试。'); return; }
    candidate.nodes.forEach((id) => { state.prompts[id] = node(id).fixedPrompt; });
    state.fixed = true; state.version = candidate.version; state.revision += 1;
    state.baseline = { ...candidate.report, revision: state.revision };
    state.history.push({ version: state.version, title: '采用调优候选', detail: `修复 ${candidate.nodes.length} 个节点 · ${cases().length}/${cases().length} 示例回归通过`, published: false });
    state.candidate = null; state.view = 'build'; state.buildTab = 'canvas'; state.selectedNode = 'confirm';
    addMessage('assistant', `已采用 v${state.version} 作为工作版本。${state.published ? `线上仍为 v${state.published}。` : '尚未发布。'}你可以检查画布，再决定发布。`);
    render(); toast(`已采用候选 v${state.version}，尚未发布。`);
  }
  function saveManualChange(change, description) {
    if (!requireEdit() || job) return;
    change(); state.version += 1; state.revision += 1; state.candidate = null; state.fixed = false;
    state.history.push({ version: state.version, title: '手动修改', detail: description, published: false });
    addMessage('assistant', `已保存${description}，工作版本更新为 v${state.version}。此前测试已标记过期，需要重新验证。`);
    render(); toast('修改已保存，请重新运行测试。');
  }
  function buildAgent(requirement, isVoice = false) {
    if (!requireEdit() || job || !attachmentsReady()) return false;
    const attachments = pendingAttachments();
    const instruction = requirement.trim() || (attachments.length ? '请参考这些附件，整理需求并构建 Agent。' : '');
    if (!instruction) return false;
    addMessage('user', instruction, { voice: isVoice, attachments: consumeAttachments() });
    state.requirementDraft = ''; state.chatDraft = ''; $('#chat-input').value = '';
    startJob('build', ['整理需求与成功条件', '生成节点与工具配置', '准备测试场景'], () => {
      state.fresh = false; state.version = 1; state.view = 'build'; state.buildTab = 'canvas';
      state.history.push({ version: 1, title: '生成初版', detail: '原型预约流程示例', published: false });
      addMessage('assistant', '构建演示完成：已载入预约流程示例和 6 个测试场景。正式接入 Codex 后，这里会根据你的需求生成具体流程。', { checklist: ['工作版本已创建', '测试工具已配置', '可以继续文字或语音协作'], link: 'test', linkText: '验证这个 Agent' });
      voiceSummary('构建演示完成，现在可以查看流程或开始测试。');
    });
    return true;
  }
  function explain() { const selected = node(state.selectedNode); addMessage('assistant', `“${selected.title}”负责：${selected.desc}。\n当前指令是：${promptFor(selected.id)}\n下一步：${selected.transition}。`); voiceSummary(`${selected.title}负责${selected.desc}。详情已加入会话。`); }
  function submitInstruction(text, isVoice = false) {
    if (!requireEdit()) return false;
    if (/停止.*任务|取消.*测试|停止测试/.test(text.trim())) {
      state.chatDraft = ''; $('#chat-input').value = '';
      addMessage('user', text.trim(), { voice: isVoice }); stopTask(); return true;
    }
    if (!attachmentsReady()) return false;
    const value = text.trim() || (pendingAttachments().length ? '请参考这些附件，协助我完善 Agent。' : '');
    if (!value) return false;
    if (state.fresh) return buildAgent(value, isVoice);
    const attachments = consumeAttachments();
    state.chatDraft = ''; $('#chat-input').value = '';
    addMessage('user', value, { voice: isVoice, attachments });
    if (attachments.length) {
      addMessage('assistant', `已在这条消息中保留 ${attachments.length} 份附件引用，作为${[...new Set(attachments.map((file) => file.purpose))].join('、')}。\n当前原型可预览附件，尚未解析内容或据此修改流程。接入后会先提取资料要点，再生成可比较的修改建议。`);
      renderAttachments(); return true;
    }
    if (/停止.*任务|取消.*测试|停止测试/.test(value)) { stopTask(); return; }
    if (/解释|说明|为什么/.test(value)) { explain(); return; }
    if (/发布/.test(value)) { publishDialog(); return; }
    if (/采用/.test(value)) { applyCandidate(); return; }
    if (/回归/.test(value)) { runRegression(); return; }
    if (/修复|调优|优化/.test(value)) { generateFix(); return; }
    if (/测试|仿真/.test(value)) { runTests(); return; }
    addMessage('assistant', '已记录这条需求。当前是交互原型，支持演示“运行测试”“修复失败场景”“运行回归”“采用候选”和“解释当前节点”。接入 Codex 后，这里会执行自由描述的修改。');
  }
  function renderVoice() {
    const panel = $('#voice-panel'); panel.hidden = !voice.open;
    if (!voice.open) return;
    const phaseText = voice.phase === 'disconnected' ? '连接已中断' : voice.muted ? '麦克风已静音' : voice.phase === 'speaking' ? '助手正在回应' : '正在聆听';
    panel.innerHTML = `<div class="voice-heading"><strong>与构建助手交谈</strong><span class="badge warm">语音状态演示</span></div><div class="voice-status"><span class="voice-orb">${icon(voice.muted ? 'mic-off' : 'audio')}</span><div class="voice-status-text ${voice.phase === 'disconnected' ? 'voice-disconnected' : ''}">${phaseText}<small>${voice.phase === 'speaking' ? '可以随时打断回复' : '与你当前查看的 Agent 保持同步'}</small></div><div class="waveform ${voice.phase === 'speaking' && !voice.muted ? 'active' : ''}">${'<i></i>'.repeat(9)}</div></div><div class="voice-controls"><button class="button" data-action="voice-mute" ${voice.phase === 'disconnected' ? 'disabled' : ''}>${icon(voice.muted ? 'mic-off' : 'mic')}${voice.muted ? '取消静音' : '静音'}</button><button class="button" data-action="voice-interrupt" ${voice.phase !== 'speaking' ? 'disabled' : ''}>${icon('stop')}打断回复</button><button class="button end-call" data-action="voice-end">结束</button></div>${pendingAttachments().length ? `<div class="voice-attachment-context">${icon('paperclip')}下条语音指令将附带 ${pendingAttachments().length} 份资料</div>` : ''}<div class="voice-caption" aria-live="polite">${escape(voice.caption)}</div>${voice.phase === 'disconnected' ? '<button class="button full" data-action="voice-reconnect" style="margin-top:12px">重新连接演示</button>' : `<div class="voice-examples">${pendingAttachments().length ? '<button data-voice-command="附件">模拟说：根据附件完善流程</button>' : ''}<button data-voice-command="${state.fresh ? '构建' : '测试'}" ${voice.muted ? 'disabled' : ''}>模拟说：${state.fresh ? '创建预约助手' : '跑一轮测试'}</button><button data-voice-command="修复" ${voice.muted || state.fresh ? 'disabled' : ''}>修复失败场景</button><button data-voice-command="解释" ${voice.muted ? 'disabled' : ''}>解释一下</button>${state.candidate ? `<button data-voice-command="回归" ${voice.muted ? 'disabled' : ''}>运行回归</button>` : ''}${job ? `<button data-voice-command="停止任务" ${voice.muted ? 'disabled' : ''}>停止任务</button>` : ''}</div>`}<div class="voice-hint">字幕与交互演示，不采集或播放音频。<button class="plain" style="font-size:9px" data-action="voice-disconnect">演示断线</button></div>`;
  }
  function voiceSummary(text) {
    if (!voice.open || voice.phase === 'disconnected') return;
    clearTimeout(voiceTimer); voice.phase = 'speaking'; voice.caption = `助手：${text}`; renderVoice();
    voiceTimer = setTimeout(() => { if (!voice.open || voice.phase === 'disconnected') return; voice.phase = 'listening'; renderVoice(); }, 5500);
  }
  function openVoice() { panelMode = 'builder'; voice.open = !voice.open; voice.phase = 'listening'; voice.muted = false; clearTimeout(voiceTimer); renderChrome(); renderVoice(); if (voice.open) $('#voice-panel').scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
  function voiceCommand(command) {
    if (!voice.open || voice.muted || voice.phase === 'disconnected') return;
    const text = { 附件: '请根据我附上的文件完善 Agent 流程。', 构建: '帮我创建一个中文预约助手，确认信息后再提交预约。', 测试: '帮我跑一轮文本仿真测试。', 修复: '修复刚才失败的场景。', 解释: '解释一下当前节点。', 回归: '运行回归测试。', 停止任务: '停止当前任务。' }[command];
    clearTimeout(voiceTimer); voice.phase = 'listening'; voice.caption = `你：${text}`; renderVoice();
    const hadJob = Boolean(job);
    const accepted = submitInstruction(text, true);
    if (accepted === false) return;
    if (command === '停止任务') { voiceSummary(hadJob ? '当前任务已停止。' : '当前没有正在运行的任务。'); return; }
    if (command === '附件') { voiceSummary('附件引用已加入会话，原型尚未解析文件内容。'); return; }
    if (command !== '解释') voiceSummary('收到，执行状态会同步显示在工作台中。');
  }
  function dialog(title, body, footer = '') {
    $('#dialog-content').innerHTML = `<div class="dialog-header"><h2 id="dialog-title">${escape(title)}</h2><button class="icon-button" data-action="close-dialog" aria-label="关闭弹窗">${icon('close')}</button></div><div class="dialog-body">${body}</div>${footer ? `<div class="dialog-footer">${footer}</div>` : ''}`;
    if (!$('#dialog').open) $('#dialog').showModal();
  }
  function closeDialog() { $('#dialog').close(); }
  function historyDialog() {
    dialog('版本历史', `<p>所有记录都是本机原型数据。采用工作版本与发布是两个独立操作。</p>${state.history.slice().reverse().map((entry) => `<div class="history-item"><span class="history-point"></span><div><strong>v${entry.version} · ${escape(entry.title)}</strong><p>${escape(entry.detail)}</p></div><span class="badge ${entry.version === state.published ? 'success' : 'subtle'}">${entry.version === state.published ? '线上' : entry.version === state.version ? '工作版本' : '历史'}</span></div>`).join('') || '<p>生成第一个 Agent 后，版本历史会出现在这里。</p>'}`);
  }
  function publishDialog() {
    if (!canPublish()) { toast(role !== 'admin' ? '仅管理员可发布（权限预览）。' : '请先让当前工作版本通过全部场景。'); return; }
    dialog('发布工作版本', `<p>将工作版本 <strong>v${state.version}</strong> 设为已发布版本。</p><div class="permission-box">${icon('check-circle')} 所选场景 ${cases().length}/${cases().length} 通过<br>${icon('layers')} ${state.published ? `当前发布版本：v${state.published}` : '首次发布'}<br>这是发布交互演示，不会改变真实 Agent 或电话流量。</div>`, '<button class="button" data-action="close-dialog">返回检查</button><button class="button primary" data-action="confirm-publish">确认发布演示</button>');
  }
  function newDialog() {
    if (!requireEdit()) return;
    dialog('新建 Agent', '<p>为新的 Agent 起个名字，再进入工作台描述它的任务。</p><form id="new-agent-form"><label class="field-label" for="new-agent-name">Agent 名称</label><input id="new-agent-name" class="field-input" maxlength="36" required placeholder="例如：门店预约助手"><p class="setting-note">新 Agent 会独立保存会话、版本和测试记录。</p><button class="button primary full" style="margin-top:14px">进入工作台</button></form>');
  }
  function agentsDialog() {
    dialog('我的 Agents', `<p>OpenInsight 空间 · 本机演示数据</p>${agents.map((agent) => `<button class="agent-option" data-agent="${escape(agent.id)}">${icon('workflow')}<span><strong>${escape(agent.name)}</strong><small>${agent.fresh ? '等待构建' : `工作版本 v${agent.version} · ${agent.messages.length} 条会话消息`}</small></span>${icon(agent.id === state.id ? 'check' : 'chevron-right')}</button>`).join('')}`, `<button class="button primary" data-action="new" ${!canEdit() ? 'disabled' : ''}>${icon('plus')}新建 Agent</button>`);
  }
  function teamDialog() {
    dialog('空间与成员权限', `<p>面向团队用户的权限预览。这里切换的是示例角色，不代表真实登录或后端权限控制。</p><div class="dialog-field"><label class="field-label" for="role-select">以不同身份体验</label><select class="field-input" id="role-select"><option value="admin" ${role === 'admin' ? 'selected' : ''}>管理员 · 构建、测试、调优、发布</option><option value="editor" ${role === 'editor' ? 'selected' : ''}>编辑者 · 构建、测试、调优</option><option value="viewer" ${role === 'viewer' ? 'selected' : ''}>查看者 · 查看流程与结果</option></select></div><div class="permission-box">正式产品中，每次工具调用都需要校验组织、用户角色、Agent 和工作版本。文字与语音采用相同权限。</div>`, '<button class="button primary" data-action="apply-role">应用预览身份</button>');
  }
  function settingsDialog() {
    dialog('工作台设置', `<div class="dialog-field"><span class="field-label">自动调优范围</span><div class="field-input">提示词、节点转移与工具参数</div></div><div class="dialog-field"><span class="field-label">执行限制</span><div class="field-input">最多 3 轮调优 · 每场景最多 ${state.maxTurns} 轮对话</div></div><div class="dialog-field"><span class="field-label">业务工具</span><div class="field-input">仿真使用测试响应，不提交真实预约</div></div><div class="permission-box">语音助手与文字助手共享会话。打断语音回复只停止讲话；“停止任务”会取消正在执行的构建或测试。</div>`, '<button class="button" data-action="team">查看成员权限</button><button class="button primary" data-action="close-dialog">完成</button>');
  }
  function suiteDialog() {
    dialog('配置文本仿真', `<form id="suite-form"><label class="field-label" for="max-turns">每场景最大对话轮数</label><input class="field-input" id="max-turns" type="number" min="3" max="30" value="${state.maxTurns}" ${job || !canEdit() ? 'disabled' : ''}><p class="setting-note">选择本轮包含的场景，至少保留一个。</p>${CASES.map((item) => `<label style="display:flex;gap:10px;align-items:center;padding:11px 0;border-bottom:1px solid var(--line-soft);font-size:12px"><input type="checkbox" name="cases" value="${item.id}" ${state.enabledCases.includes(item.id) ? 'checked' : ''} ${job || !canEdit() ? 'disabled' : ''}><span>${item.title}<small style="display:block;font-size:10px;color:var(--muted);margin-top:4px">${item.expected}</small></span></label>`).join('')}<p id="suite-error" class="form-error" hidden></p><button class="button primary full" style="margin-top:20px" ${job || !canEdit() ? 'disabled' : ''}>保存测试集版本</button></form>`);
  }
  function voiceTestDialog() { dialog('试打 Agent', `<p>这里面向客户验证 Agent 的声音、响应速度和打断表现。</p><div class="permission-box">当前测试对象：${escape(state.name)} · 工作版本 v${state.version}<br>工作台的“语音协作”用于向构建助手下指令。<br>本原型展示入口，真实语音试打将在产品中复用现有测试能力。</div>`, '<button class="button primary" data-action="close-dialog">明白</button>'); }
  // File bytes stay in memory; only names, purposes and message references persist.
  const fileCache = new Map();
  let previewUrl = null;
  const FILE_EXTENSIONS = new Set(['pdf', 'docx', 'txt', 'md', 'csv', 'xlsx', 'json', 'png', 'jpg', 'jpeg', 'webp']);
  const pendingAttachments = () => state.pendingAttachments ||= [];
  const fileSize = (size) => size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`;
  function attachmentCard(file, pending) {
    return `<div class="attachment-card"><button type="button" class="attachment-open" data-action="attachment-preview" data-id="${escape(file.id)}" aria-label="预览 ${escape(file.name)}"><span class="file-type">${escape(file.ext.toUpperCase())}</span><span><strong>${escape(file.name)}</strong><small>${fileSize(file.size)} · ${pending ? fileCache.has(file.id) ? '已选择 · 本机文件' : '需重新选择原文件' : escape(file.purpose)}</small></span></button>${pending ? `<button type="button" class="icon-button" data-action="attachment-remove" data-id="${escape(file.id)}" aria-label="移除 ${escape(file.name)}">${icon('close')}</button><label class="attachment-purpose">用途<select data-file-purpose="${escape(file.id)}" aria-label="${escape(file.name)} 的用途">${['构建参考', '测试样例', '调优证据'].map((purpose) => `<option ${purpose === file.purpose ? 'selected' : ''}>${purpose}</option>`).join('')}</select></label>` : ''}</div>`;
  }
  function renderAttachments() {
    const files = pendingAttachments();
    ['#chat-attachments', '#welcome-attachments'].forEach((selector) => { const target = $(selector); if (target) target.innerHTML = files.map((file) => attachmentCard(file, true)).join(''); });
    $$('[data-action="attach"]').forEach((button) => { button.disabled = !canEdit(); });
    $('#chat-input').disabled = !canEdit();
    $('#chat-form .send-button').disabled = !canEdit();
    if (!canEdit()) $$('[data-file-purpose], [data-action="attachment-remove"]').forEach((control) => { control.disabled = true; });
  }
  function attachmentsReady() {
    if (pendingAttachments().some((file) => !fileCache.has(file.id))) { toast('有附件需要重新选择原文件，或移除后再发送。'); return false; }
    return true;
  }
  function consumeAttachments() {
    const files = pendingAttachments().map((file) => ({ ...file }));
    state.attachments ||= [];
    files.forEach((file) => { if (!state.attachments.some((entry) => entry.id === file.id)) state.attachments.push(file); });
    state.pendingAttachments = [];
    renderAttachments(); renderVoice();
    return files;
  }
  function addFiles(files) {
    if (!requireEdit()) return;
    let added = 0;
    const errors = [];
    for (const file of files) {
      const ext = file.name.split('.').pop().toLowerCase();
      if (!FILE_EXTENSIONS.has(ext)) { errors.push(`${file.name}：暂不支持该格式`); continue; }
      if (!file.size || file.size > 20 * 1024 * 1024) { errors.push(`${file.name}：请选择 0–20 MB 的非空文件`); continue; }
      const duplicate = pendingAttachments().find((item) => item.name === file.name && item.size === file.size);
      if (duplicate) { if (!fileCache.has(duplicate.id)) { fileCache.set(duplicate.id, file); added++; } else errors.push(`${file.name}：已添加`); continue; }
      if (pendingAttachments().length >= 5) { errors.push('每条消息最多添加 5 份附件'); break; }
      const id = `file-${crypto.randomUUID()}`;
      fileCache.set(id, file);
      pendingAttachments().push({ id, name: file.name, size: file.size, ext, purpose: state.view === 'test' ? '测试样例' : state.view === 'tune' ? '调优证据' : '构建参考' });
      added++;
    }
    persist(); renderAttachments(); renderVoice();
    if (errors.length) toast(errors.join('；'));
    else if (added) toast(`已添加 ${added} 份附件，仅在本机预览。`);
  }
  function findAttachment(id) { return [...pendingAttachments(), ...(state.attachments || []), ...state.messages.flatMap((message) => message.attachments || [])].find((file) => file.id === id); }
  async function previewAttachment(id) {
    const file = findAttachment(id); if (!file) return;
    const bytes = fileCache.get(id);
    if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
    const metadata = `<div class="file-preview-meta"><span class="file-type">${escape(file.ext.toUpperCase())}</span><span>${fileSize(file.size)} · ${escape(file.purpose)}</span><span class="badge subtle">本机附件</span></div>`;
    const limitation = '<p class="setting-note">原型未上传或解析文件，内容不会发送给模型。刷新后需重新选择原文件才能预览。</p>';
    dialog(file.name, `${metadata}<div id="file-preview-content"><p>${bytes ? '正在准备本机预览…' : '此附件的名称和消息引用已保留。原文件仅在选择它的标签页会话中有效，请重新添加后预览。'}</p></div>${limitation}`);
    const container = $('#file-preview-content');
    if (!bytes) return;
    try {
      if (['txt', 'md', 'csv', 'json'].includes(file.ext)) {
        const text = await bytes.slice(0, 24000).text();
        if (!container.isConnected) return;
        container.innerHTML = `<pre class="file-text-preview">${escape(text)}</pre>${file.size > 24000 ? '<p class="setting-note">仅预览前 24 KB，完整文件仍随本次引用保留。</p>' : ''}`;
      } else if (['png', 'jpg', 'jpeg', 'webp'].includes(file.ext)) {
        previewUrl = URL.createObjectURL(bytes);
        container.innerHTML = `<img class="file-image-preview" src="${previewUrl}" alt="${escape(file.name)}">`;
      } else container.innerHTML = `<div class="file-document-preview">${icon('book')}<strong>${escape(file.name)}</strong><p>已选择 ${file.ext.toUpperCase()} 文件</p><span>本原型展示附件引用；正文解析将在后端接入。</span></div>`;
    } catch { if (container.isConnected) container.textContent = '文件读取失败，请移除后重新选择。'; }
  }
  function attachmentLibrary() {
    const files = state.attachments || [];
    dialog('会话附件', `<p>附件与具体消息关联，可作为后续文字或语音指令的参考。文件名会保留，文件内容仅在当前标签页会话中可用。</p>${files.length ? files.map((file) => `<div class="library-file">${attachmentCard(file, false)}<button class="button small" data-action="attachment-reuse" data-id="${escape(file.id)}" ${!canEdit() ? 'disabled' : ''}>再次引用</button></div>`).join('') : '<div class="empty-state"><div class="empty-icon">' + icon('paperclip') + '</div><h3>还没有已发送的附件</h3><p>在输入框添加资料，随消息发送后会显示在这里。</p></div>'}`);
  }
  function renderTester() {
    $('#tester-content').innerHTML = `<div class="tester-tabs"><button data-action="tester-audio" aria-pressed="${testerTab === 'audio'}">${icon('mic')}Test Audio</button><button data-action="tester-chat" aria-pressed="${testerTab === 'chat'}">${icon('message')}Test Chat</button></div><div class="tester-context">测试对象：${escape(state.name)} · 工作版本 v${state.version}</div>${testerTab === 'audio' ? `<div class="audio-test-card"><span class="audio-test-icon">${icon('phone')}</span><h2>Call this agent in the browser</h2><p>与你的 Agent 进行语音通话，检验声音、响应和真实对话体验。</p><button class="button primary" data-action="voice-test">${icon('phone')}Run Test</button><small>原型入口演示 · 正式版复用现有语音测试</small></div>` : `<div class="tester-chat-log">${(state.testChat || [{ role: 'assistant', text: '你好，我是预约助手。请问有什么可以帮您？（示例对话）' }]).map((message) => `<div class="tester-chat-message ${message.role}"><small>${message.role === 'user' ? '你 · 模拟客户' : escape(state.name)}</small><p>${escape(message.text)}</p></div>`).join('')}</div><form id="tester-chat-form" class="tester-chat-form"><label class="sr-only" for="tester-chat-input">向 Agent 发送测试消息</label><input id="tester-chat-input" ${!canEdit() || state.fresh ? 'disabled' : ''} class="field-input" placeholder="扮演客户，发送测试消息…" value="${escape(state.testChatDraft || '')}"><button class="send-button" aria-label="发送测试消息" ${!canEdit() || state.fresh ? 'disabled' : ''}>${icon('arrow-up')}</button></form><p class="assistant-note">固定回复演示 · 与构建助手的指令会话分开</p>`}<button class="batch-test-link" data-action="run-tests" ${job || !canEdit() || state.fresh ? 'disabled' : ''}>${icon('play')}运行批量文本仿真${icon('arrow-right')}</button>`;
  }
  $('#attachment-input').addEventListener('change', (event) => { addFiles([...event.target.files]); event.target.value = ''; });
  document.addEventListener('change', (event) => {
    if (event.target.dataset.filePurpose && canEdit()) { const file = pendingAttachments().find((entry) => entry.id === event.target.dataset.filePurpose); if (file) file.purpose = event.target.value; persist(); renderAttachments(); }
  });
  document.addEventListener('dragover', (event) => { if ([...event.dataTransfer.types].includes('Files')) { event.preventDefault(); const zone = event.target.closest('[data-dropzone]'); if (zone) zone.classList.add('drag-over'); } });
  document.addEventListener('dragleave', (event) => { const zone = event.target.closest('[data-dropzone]'); if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove('drag-over'); });
  document.addEventListener('drop', (event) => { event.preventDefault(); $$('[data-dropzone]').forEach((zone) => zone.classList.remove('drag-over')); if (event.target.closest('[data-dropzone]')) addFiles([...event.dataTransfer.files]); });
  document.addEventListener('paste', (event) => { if (event.target.closest('[data-dropzone]') && event.clipboardData.files.length) { event.preventDefault(); addFiles([...event.clipboardData.files]); } });
  $('#dialog').addEventListener('close', () => { if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; } });
  const actions = {
    agents: agentsDialog, new: newDialog, history: historyDialog, settings: settingsDialog, team: teamDialog,
    attach: () => { if (requireEdit()) $('#attachment-input').click(); },
    'attachment-library': attachmentLibrary,
    'attachment-preview': (button) => previewAttachment(button.dataset.id),
    'attachment-remove': (button) => { if (!requireEdit()) return; state.pendingAttachments = pendingAttachments().filter((file) => file.id !== button.dataset.id); renderAttachments(); renderVoice(); persist(); },
    'attachment-reuse': (button) => { if (!requireEdit()) return; const file = (state.attachments || []).find((item) => item.id === button.dataset.id); if (!file) return; if (!fileCache.has(file.id)) { toast('请重新选择原文件；刷新后不会保留文件内容。'); return; } if (pendingAttachments().length >= 5) { toast('每条消息最多附带 5 份文件。'); return; } if (!pendingAttachments().some((item) => item.id === file.id)) pendingAttachments().push({ ...file }); closeDialog(); renderAttachments(); renderVoice(); persist(); },
    'show-builder': () => { panelMode = 'builder'; renderChrome(); },
    'show-tester': () => { panelMode = 'tester'; renderChrome(); },
    'tester-audio': () => { testerTab = 'audio'; renderTester(); },
    'tester-chat': () => { testerTab = 'chat'; renderTester(); },
    'save-all': () => {
      const ids = Object.keys(state.promptDrafts || {}).filter((id) => state.promptDrafts[id] !== promptFor(id));
      if (!ids.length) { toast('当前没有未保存的节点修改。'); return; }
      if (ids.some((id) => !state.promptDrafts[id].trim())) { toast('节点提示词不能为空。'); return; }
      saveManualChange(() => { ids.forEach((id) => { state.prompts[id] = state.promptDrafts[id].trim(); }); state.promptDrafts = {}; }, `${ids.length} 个节点的提示词`);
    },
    'close-inspector': () => { inspectorOpen = false; renderMain(); },
    'zoom-in': () => { zoom = Math.min(1.3, zoom + .1); renderMain(); },
    'zoom-out': () => { zoom = Math.max(.5, zoom - .1); renderMain(); },
    'zoom-fit': () => { zoom = 1; renderMain(); },
    theme: () => { theme = theme === 'dark' ? 'light' : 'dark'; render(); },
    assistant: () => $('#assistant-panel').scrollIntoView({ behavior: 'smooth', block: 'start' }),
    view: (button) => setView(button.dataset.target), voice: openVoice, 'voice-test': voiceTestDialog,
    'run-tests': runTests, 'generate-fix': generateFix, regression: runRegression, 'apply-candidate': applyCandidate, explain,
    'stop-task': () => stopTask(), 'close-dialog': closeDialog, publish: publishDialog, suite: suiteDialog,
    'fix-case': (button) => { const item = CASES.find((entry) => entry.id === button.dataset.id); if (item) { addMessage('user', `请分析并修复“${item.title}”的失败。`); state.selectedNode = item.node || 'confirm'; generateFix(); } },
    'discard-candidate': () => { if (!requireEdit() || job) return; state.candidate = null; render(); toast('候选已放弃，当前工作版本保持不变。'); },
    'save-node': () => { const value = $('#node-prompt')?.value.trim(); if (!value) { toast('提示词不能为空。'); return; } if (value === promptFor(state.selectedNode)) { toast('没有需要保存的修改。'); return; } saveManualChange(() => { state.prompts[state.selectedNode] = value; if (state.promptDrafts) delete state.promptDrafts[state.selectedNode]; }, `“${node(state.selectedNode).title}”的提示词`); },
    'confirm-publish': () => { if (!canPublish()) { closeDialog(); toast('发布条件已变化，请重新检查。'); return; } state.published = state.version; state.history.push({ version: state.version, title: '发布演示', detail: '已通过当前测试集，演示发布完成', published: true }); closeDialog(); addMessage('assistant', `发布演示完成，v${state.version} 已标记为已发布。真实业务数据未被修改。`); render(); toast(`发布演示完成 · v${state.version}`); },
    'apply-role': () => { const value = $('#role-select').value; if (['admin', 'editor', 'viewer'].includes(value)) { role = value; if (role === 'viewer') stopTask(false); } closeDialog(); render(); toast('已切换示例身份，仅影响本机原型。'); },
    'voice-mute': () => { voice.muted = !voice.muted; renderVoice(); },
    'voice-interrupt': () => { clearTimeout(voiceTimer); voice.phase = 'listening'; voice.caption = '已打断语音回复，正在运行的任务仍会继续。'; renderVoice(); },
    'voice-end': () => { clearTimeout(voiceTimer); voice.open = false; renderChrome(); renderVoice(); toast('语音演示已结束，文字会话和执行中的任务保留。'); },
    'voice-disconnect': () => { clearTimeout(voiceTimer); voice.phase = 'disconnected'; voice.caption = '语音连接中断，文字会话仍可使用，后台任务不会自动取消。'; renderVoice(); },
    'voice-reconnect': () => { voice.phase = 'listening'; voice.caption = '已恢复演示，可以继续使用相同会话。'; renderVoice(); },
    'example-requirement': (button) => { const text = button.dataset.example === '售后' ? '创建一个售后助手，了解订单问题并转给合适的人工客服。' : button.dataset.example === '回访' ? '创建一个客户回访助手，询问满意度并记录改善建议。' : '创建一个中文门店预约助手，收集姓名和时间，确认后提交预约。'; const field = $('#welcome-request'); if (field) { field.value = text; state.requirementDraft = text; persist(); field.focus(); } else { $('#chat-input').value = text; state.chatDraft = text; persist(); $('#chat-input').focus(); } },
  };
  document.addEventListener('click', (event) => {
    const target = event.target.closest('button'); if (!target || target.disabled) return;
    if (target.dataset.view) { setView(target.dataset.view); return; }
    if (target.dataset.buildTab) { state.buildTab = target.dataset.buildTab; renderMain(); persist(); return; }
    if (target.dataset.node) { state.selectedNode = target.dataset.node; inspectorOpen = true; renderChrome(); renderMain(); persist(); return; }
    if (target.dataset.case) { state.selectedCase = target.dataset.case; renderMain(); persist(); return; }
    if (target.dataset.voiceCommand) { voiceCommand(target.dataset.voiceCommand); return; }
    if (target.dataset.agent) { const agent = agents.find((entry) => entry.id === target.dataset.agent); if (agent) { stopTask(false); voice.open = false; clearTimeout(voiceTimer); state = agent; inspectorOpen = false; $('#chat-input').value = state.chatDraft || ''; closeDialog(); render(); } return; }
    if (target.dataset.action && actions[target.dataset.action]) actions[target.dataset.action](target);
  });
  document.addEventListener('submit', (event) => {
    event.preventDefault();
    if (event.target.id === 'chat-form') { const input = $('#chat-input'); submitInstruction(input.value); }
    if (event.target.id === 'tester-chat-form') { if (!requireEdit() || state.fresh) return; const input = $('#tester-chat-input'); const value = input.value.trim(); if (value) { state.testChatDraft = ''; state.testChat ||= []; state.testChat.push({ role: 'user', text: value }, { role: 'assistant', text: '这是测试对话占位示例：请问您希望预约什么时间？正式接入将使用现有 Dograh 文本运行时。' }); persist(); renderTester(); } }
    if (event.target.id === 'welcome-form') buildAgent($('#welcome-request').value);
    if (event.target.id === 'new-agent-form') {
      if (!requireEdit()) return;
      const name = $('#new-agent-name').value.trim(); if (!name) return;
      stopTask(false); clearTimeout(voiceTimer); voice.open = false;
      state = initialAgent(`agent-${Date.now()}`, name, true); agents.push(state); panelMode = 'builder'; inspectorOpen = false; $('#chat-input').value = ''; closeDialog(); render();
    }
    if (event.target.id === 'model-form') { const model = $('#model-select').value; const temperature = $('#temperature').value; saveManualChange(() => { state.model = model; state.temperature = temperature; }, '模型设置'); }
    if (event.target.id === 'suite-form') {
      if (!requireEdit() || job) return;
      const selected = $$('input[name=cases]:checked', event.target).map((input) => input.value);
      if (!selected.length) { $('#suite-error').hidden = false; $('#suite-error').textContent = '至少选择一个测试场景。'; return; }
      state.enabledCases = selected; state.maxTurns = Number($('#max-turns').value); state.suiteRevision += 1; state.candidate = null; closeDialog(); render(); toast('测试集已更新，请重新运行测试。');
    }
  });
  $('#chat-input').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('#chat-form').requestSubmit(); } });
  document.addEventListener('input', (event) => {
    if (event.target.id === 'tester-chat-input') { state.testChatDraft = event.target.value; persist(); }
    if (event.target.id === 'chat-input') { state.chatDraft = event.target.value; persist(); }
    if (event.target.id === 'welcome-request') { state.requirementDraft = event.target.value; persist(); }
    if (event.target.id === 'node-prompt' && canEdit() && !job) {
      state.promptDrafts ||= {};
      state.promptDrafts[state.selectedNode] = event.target.value;
      renderChrome(); persist();
    }
  });
  $('#reset-demo').addEventListener('click', () => dialog('重置演示', '<p>清除当前浏览器里本原型的 Agent、会话与版本记录，恢复预约助手示例。不会修改真实数据。</p>', '<button class="button" data-action="close-dialog">保留记录</button><button class="button primary" id="confirm-reset">重置演示数据</button>'));
  document.addEventListener('click', (event) => { if (event.target.closest('#confirm-reset')) { stopTask(false); clearTimeout(voiceTimer); voice.open = false; fileCache.clear(); $('#chat-input').value = ''; panelMode = 'builder'; inspectorOpen = false; agents = [initialAgent()]; state = agents[0]; role = 'admin'; closeDialog(); render(); toast('已恢复初始原型。'); } });
  $('#dialog').addEventListener('click', (event) => { if (event.target === $('#dialog')) { const bounds = $('#dialog').getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeDialog(); } });
  $('#chat-input').value = state.chatDraft || '';
  decorate(); render();
})();
