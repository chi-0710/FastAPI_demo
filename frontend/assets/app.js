import {icon, hydrateIcons} from './icons.js';
import {request, apiUrl} from './api.js';
import {initLearningTips} from './learning-tips.js';

const $ = id => document.getElementById(id);
const state = {user:null, csrf:'', authMode:'login', afterLogin:false, tasks:[], filter:'all', offset:0, pageSize:8, loading:false, error:'', sequence:0, epoch:0, controller:new AbortController(), agentMode:'unknown', agentBusy:false, edit:null, deleting:null};
let toastTimer;
let authBusy = false;
let sessionReady;
hydrateIcons();
const disposeLearningTips = initLearningTips(document.getElementById('learning-tips'));
if (import.meta.hot) import.meta.hot.dispose(disposeLearningTips);
$('today').textContent = new Intl.DateTimeFormat('zh-CN', {month:'long',day:'numeric',weekday:'long'}).format(new Date()) + ' / YOUR WORKSPACE';

function notify(message, error = false) {
  $('toast').textContent = message; $('toast').className = `toast visible${error ? ' error' : ''}`;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('visible'), 4500);
}
async function openConnection() {
  await sessionReady;
  $('connection-error').textContent = '';
  $('auth-password').value = ''; $('auth-confirm').value = '';
  $('auth-fields').hidden = !!state.user; $('account-panel').hidden = !state.user;
  $('auth-title').textContent = state.user ? '你的工作空间' : '欢迎回来';
  $('auth-description').textContent = state.user ? '每一个小目标，都为你妥善保存。' : '登录后，继续你的下一步。';
  $('account-username').textContent = state.user?.username || '';
  if (!state.user) setAuthMode('login');
  $('connection-dialog').showModal();
}
function setPasswordVisibility(button, visible) {
  $(button.dataset.passwordToggle).type = visible ? 'text' : 'password';
  button.textContent = visible ? '隐藏' : '显示';
  button.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
  button.setAttribute('aria-pressed', String(visible));
}
function resetPasswordVisibility() {
  document.querySelectorAll('[data-password-toggle]').forEach(button => setPasswordVisibility(button, false));
}
document.querySelectorAll('[data-password-toggle]').forEach(button => {
  button.onclick = () => setPasswordVisibility(button, $(button.dataset.passwordToggle).type === 'password');
});
$('connection-dialog').addEventListener('close', resetPasswordVisibility);
function setAuthMode(mode) {
  resetPasswordVisibility();
  state.authMode = mode;
  const register = mode === 'register';
  $('auth-title').textContent = register ? '开启你的工作空间' : '欢迎回来';
  $('auth-description').textContent = register ? '创建账号，从一个清晰的小目标开始。' : '登录后，继续你的下一步。';
  $('connect-submit').textContent = register ? '创建账号并进入' : '登录工作空间';
  $('auth-password').minLength = register ? 6 : 1;
  $('auth-password').pattern = register ? '.*[a-zA-Z].*' : '.*';
  $('auth-password').title = register ? '密码至少 6 个字符，需包含英文字母' : '';
  $('auth-password').placeholder = register ? '至少 6 个字符，需包含英文字母' : '请输入密码';
  $('auth-password').autocomplete = register ? 'new-password' : 'current-password';
  $('confirm-field').hidden = !register; $('auth-confirm').required = register;
  $('connection-error').textContent = '';
  document.querySelectorAll('[data-auth-mode]').forEach(button => {
    const active = button.dataset.authMode === mode;
    button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
  });
}
function acceptSession(data) {
  clearSession(); state.user = data.user; state.csrf = data.csrf_token;
  $('connection-label').textContent = data.user.username;
  $('connection-detail').textContent = '个人账号 · 管理登录';
  renderTasks();
}
async function restoreSession() {
  try { acceptSession(await request('/api/v1/auth/me')); await loadTasks(); }
  catch (error) { clearSession(); if (error.status !== 401) report(error); }
}
function clearSession() {
  state.controller.abort(); state.controller = new AbortController(); state.epoch++; state.sequence++;
  state.user = null; state.csrf = ''; state.tasks = []; state.offset = 0; state.loading = false; state.error = '';
  $('connection-label').textContent = '登录工作空间'; $('connection-detail').textContent = '登录 / 注册';
  state.filter = 'all'; $('task-search').value = ''; syncFilters();
  $('messages').replaceChildren(); $('prompt').value = ''; $('auth-password').value = ''; $('auth-confirm').value = '';
  for (const id of ['task-dialog','delete-dialog']) $(id).close();
  renderTasks();
}
async function api(path, options = {}) {
  const epoch = state.epoch;
  try { return await request(path, {...options, csrf:state.csrf, signal:state.controller.signal}); }
  catch (error) {
    if (error.status === 401 && epoch === state.epoch) { clearSession(); notify('登录已过期，请重新登录', true); }
    throw error;
  }
}
function report(error) {
  if (error.code !== 'cancelled') notify(error.message + (error.requestId ? ` · 请求 ${error.requestId.slice(0,8)}` : ''), true);
}
function emptyState(title, description, actionLabel, action) {
  const wrapper = document.createElement('div'); wrapper.className = 'empty';
  wrapper.innerHTML = `<div class="empty-icon">${icon(state.user ? 'layers' : 'key')}</div><strong></strong><p></p>`;
  wrapper.querySelector('strong').textContent = title; wrapper.querySelector('p').textContent = description;
  if (actionLabel) { const button = document.createElement('button'); button.className = 'text-button'; button.textContent = actionLabel; button.onclick = action; wrapper.append(button); }
  return wrapper;
}
function renderTasks() {
  const total = state.tasks.length, done = state.tasks.filter(t => t.completed).length;
  $('stat-total').textContent = state.user ? total : '—'; $('stat-done').textContent = state.user ? done : '—';
  $('stat-rate').textContent = state.user ? `${total ? Math.round(done/total*100) : 0}%` : '—';
  $('progress-bar').style.width = `${total ? done/total*100 : 0}%`; $('task-count').textContent = total;
  $('prev-page').disabled = !state.user || state.loading || state.offset === 0;
  $('next-page').disabled = !state.user || state.loading || total < state.pageSize;
  $('page-label').textContent = !state.user ? '登录后加载你的任务' : `第 ${state.offset/state.pageSize+1} 页 · 每页 ${state.pageSize} 条`;
  const list = $('task-list'); list.replaceChildren();
  if (!state.user) { list.append(emptyState('给你的任务一个家', '登录你的账号，让下一步变得清晰。', '登录 / 注册', openConnection)); return; }
  if (state.loading) { for(let i=0;i<3;i++) { const el=document.createElement('div'); el.className='skeleton'; el.setAttribute('aria-label','加载中'); list.append(el); } return; }
  if (state.error) { list.append(emptyState('暂时没有加载成功', state.error, '重新加载', loadTasks)); return; }
  const query = $('task-search').value.trim().toLocaleLowerCase();
  const tasks = state.tasks.filter(t => t.title.toLocaleLowerCase().includes(query));
  if (!tasks.length) { list.append(emptyState(query ? '没有匹配的任务' : '留白，也是新的开始', query ? '试试其他关键词，搜索范围为当前页。' : '这里还没有任务，添加一个清晰的小目标吧。', query ? null : '创建第一条任务', () => openTask())); return; }
  tasks.forEach((task, index) => {
    const row = document.createElement('article'); row.className = `task-row${task.completed?' done':''}`; row.style.animationDelay = `${index*25}ms`;
    const check = document.createElement('button'); check.className='task-check'; check.innerHTML=icon('check'); check.setAttribute('aria-label', task.completed ? `标为未完成：${task.title}` : `完成任务：${task.title}`); check.setAttribute('aria-pressed',String(task.completed));
    check.onclick = async () => {
      check.disabled = true; const epoch = state.epoch;
      try { await api(`/api/v1/todos/${task.id}`, {method:'PUT', body:{title:task.title,completed:!task.completed,version:task.version}}); if (epoch!==state.epoch) return; notify(task.completed?'已移回进行中':'又完成了一小步，做得不错！'); await loadTasks(); }
      catch(error) { report(error); if(error.status===409) await loadTasks(); }
      finally {check.disabled=false;}
    };
    const text=document.createElement('div'); text.className='task-text';
    const title=document.createElement('div'); title.className='task-title'; title.textContent=task.title;
    const meta=document.createElement('div'); meta.className='task-meta';
    meta.textContent=`${task.completed?'已完成':'进行中'} · ${new Intl.DateTimeFormat('zh-CN',{month:'short',day:'numeric'}).format(new Date(task.created_at))}`;
    text.append(title,meta); const actions=document.createElement('div'); actions.className='row-actions';
    for(const [name,label,handler] of [['edit','编辑任务',()=>openTask(task)],['trash','删除任务',()=>{state.deleting=task;$('delete-title').textContent=task.title;$('delete-dialog').showModal();}]]) {
      const button=document.createElement('button'); button.className=`icon-button${name==='trash'?' delete':''}`; button.setAttribute('aria-label',`${label}：${task.title}`); button.title=label; button.innerHTML=icon(name); button.onclick=handler; actions.append(button);
    }
    row.append(check,text,actions); list.append(row);
  });
}
async function loadTasks() {
  if (!state.user) {renderTasks(); return;}
  const sequence=++state.sequence; state.loading=true; state.error=''; renderTasks();
  try {
    const params=new URLSearchParams({limit:state.pageSize,offset:state.offset});
    if(state.filter!=='all') params.set('completed',state.filter);
    const result=await api(`/api/v1/todos?${params}`);
    if(sequence!==state.sequence)return;
    state.tasks=result.items;
    if(!state.tasks.length && state.offset>0){state.offset-=state.pageSize;return await loadTasks();}
  } catch(error) { if(sequence!==state.sequence)return; state.tasks=[];state.error=error.message; }
  finally {if(sequence===state.sequence){state.loading=false;renderTasks();}}
}
async function openTask(task = null) {
  await sessionReady;
  if(!state.user){state.afterLogin=true;openConnection();return;}
  if($('save-task').disabled)return;
  state.edit=task; $('task-title').value=task?.title||''; $('task-error').textContent='';
  $('task-dialog-title').textContent=task?'编辑这个小目标':'新建一个小目标'; $('save-task').textContent=task?'保存修改':'创建任务';
  $('task-dialog').showModal(); $('task-title').focus();
}
async function health() {
  const badge=$('service-status');
  try {await request('/health/ready',{timeout:5000}); badge.className='service-badge online'; badge.innerHTML='<i></i>服务运行中';}
  catch {badge.className='service-badge offline';badge.innerHTML='<i></i>服务未就绪';}
}
function navigate() {
  const view=location.hash==='#agent'?'agent':'workspace';
  $('workspace-view').hidden=view!=='workspace'; $('agent-view').hidden=view!=='agent';
  $('breadcrumb-current').textContent=view==='agent'?'Agent 实验室':'工作台';
  document.querySelectorAll('[data-view]').forEach(el=>{const active=el.dataset.view===view;el.classList.toggle('active',active);if(active)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  document.title=`Flowspace · ${view==='agent'?'Agent 实验室':'工作台'}`;
}
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
$('connection-dialog').addEventListener('close',()=>{
  $('auth-password').value=''; $('auth-confirm').value='';
  if (!authBusy) state.afterLogin=false;
});
$('connection-button').onclick=()=>{state.afterLogin=false;openConnection();};
$('mobile-connection').onclick=$('connection-button').onclick;
$('new-task-button').onclick=()=>openTask();
$('focus-button').onclick=async()=>{await sessionReady;if(!state.user){openConnection();return;}document.querySelector('[data-filter="false"]').click();$('tasks-heading').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'center'});};
$('explore-agent').onclick=()=>{location.hash='agent';};
document.querySelectorAll('[data-auth-mode]').forEach(button=>button.onclick=()=>setAuthMode(button.dataset.authMode));
$('disconnect-button').onclick=async()=>{
  const button=$('disconnect-button');button.disabled=true;
  try { await api('/api/v1/auth/logout',{method:'POST'}); clearSession();$('connection-dialog').close();notify('已安全退出登录'); }
  catch(error) { if(error.status===401){$('connection-dialog').close();}else report(error); }
  finally {button.disabled=false;}
};
$('connection-form').onsubmit=async event=>{
  event.preventDefault(); if(authBusy)return;
  const username=$('auth-username').value.trim(), password=$('auth-password').value;
  if(state.authMode==='register' && password!==$('auth-confirm').value){$('connection-error').textContent='两次输入的密码不一致。';return;}
  authBusy=true; const continuation=state.afterLogin;
  $('connect-submit').disabled=true; $('connection-error').textContent='';
  document.querySelectorAll('[data-auth-mode]').forEach(button=>button.disabled=true);
  try {
    const data=await request(`/api/v1/auth/${state.authMode}`,{method:'POST',body:{username,password}});
    acceptSession(data); $('connection-dialog').close();state.afterLogin=false;
    notify('欢迎回来，开始今天的小目标吧');await loadTasks();
    if(continuation)openTask();
  } catch(error){$('connection-error').textContent=error.message;}
  finally{
    authBusy=false;$('connect-submit').disabled=false;
    document.querySelectorAll('[data-auth-mode]').forEach(button=>button.disabled=false);
  }
};
$('task-form').onsubmit=async event=>{
  event.preventDefault();const title=$('task-title').value.trim();if(!title){$('task-error').textContent='请填写任务名称，不能只有空格。';return;}
  const edit=state.edit, epoch=state.epoch; $('save-task').disabled=true;$('task-error').textContent='';
  try {
    await api(edit?`/api/v1/todos/${edit.id}`:'/api/v1/todos',{method:edit?'PUT':'POST',body:edit?{title,completed:edit.completed,version:edit.version}:{title}});
    if(epoch!==state.epoch)return;
    $('task-dialog').close();notify(edit?'修改已保存':'新目标已加入清单');
    if(!edit){state.offset=0;state.filter='all';$('task-search').value='';syncFilters();}
    await loadTasks();
  }catch(error){$('task-error').textContent=error.message;if(error.status===409){$('task-dialog').close();report(error);await loadTasks();}}
  finally{$('save-task').disabled=false;}
};
$('delete-form').onsubmit=async event=>{
  event.preventDefault();const task=state.deleting,epoch=state.epoch;if(!task)return;$('delete-confirm').disabled=true;
  try{await api(`/api/v1/todos/${task.id}?version=${task.version}`,{method:'DELETE'});if(epoch!==state.epoch)return;$('delete-dialog').close();notify('任务已删除');await loadTasks();}
  catch(error){$('delete-dialog').close();report(error);if(error.status===409||error.status===404)await loadTasks();}
  finally{$('delete-confirm').disabled=false;}
};
function syncFilters(){document.querySelectorAll('[data-filter]').forEach(button=>{const active=button.dataset.filter===state.filter;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});}
document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{state.filter=button.dataset.filter;state.offset=0;syncFilters();loadTasks();});
$('task-search').oninput=renderTasks;
$('prev-page').onclick=()=>{state.offset=Math.max(0,state.offset-state.pageSize);loadTasks();};
$('next-page').onclick=()=>{state.offset+=state.pageSize;loadTasks();};
$('refresh-button').onclick=async()=>{const button=$('refresh-button');button.disabled=true;button.firstElementChild.classList.add('spinning');await Promise.all([health(),loadTasks()]);button.disabled=false;button.firstElementChild.classList.remove('spinning');};
function message(role, text, label) {
  $('messages').querySelector('.chat-welcome')?.remove();const el=document.createElement('article');el.className=`message ${role}`;
  const heading=document.createElement('div');heading.className='message-label';heading.textContent=label;
  const body=document.createElement('div');body.className='message-body';body.textContent=text;el.append(heading,body);$('messages').append(el);el.scrollIntoView({block:'nearest'});return el;
}
$('agent-form').onsubmit=async event=>{
  event.preventDefault();if(state.agentBusy)return;if(!state.user){openConnection();return;}
  const prompt=$('prompt').value.trim();if(!prompt)return;
  if(state.agentMode==='disabled'){notify('Agent 执行器尚未启用，可在项目配置中启用 mock 模式。',true);return;}
  state.agentBusy=true;$('send-agent').disabled=true;$('clear-chat').disabled=true;
  const epoch=state.epoch;message('user',prompt,'你');$('prompt').value='';const pending=message('assistant','正在思考…','Agent');
  try{const result=await api('/api/v1/agent/invoke',{method:'POST',body:{prompt},timeout:65000});if(epoch!==state.epoch)return;pending.remove();message('assistant',result.output,result.provider==='mock'?'Agent · 模拟响应':`Agent · ${result.provider}`);}
  catch(error){pending.remove();if(epoch===state.epoch)message('error',error.message+(error.requestId?`\n请求 ID：${error.requestId}`:''),'未能完成本次请求');}
  finally{state.agentBusy=false;$('send-agent').disabled=false;$('clear-chat').disabled=false;}
};
$('prompt').onkeydown=event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();$('agent-form').requestSubmit();}};
$('clear-chat').onclick=()=>{$('messages').replaceChildren();notify('对话已清空');};
window.addEventListener('hashchange',navigate);
window.addEventListener('pagehide',()=>{state.controller.abort();});
window.addEventListener('pageshow',event=>{if(event.persisted){clearSession();sessionReady=restoreSession();health();}});
navigate();renderTasks();health();sessionReady=restoreSession();
request('/api/v1/capabilities').then(config=>{
  state.agentMode=config.agent_mode;
  const disabled=config.agent_mode==='disabled';
  $('agent-mode-label').textContent=disabled?'执行器尚未启用':'模拟执行器 · 未连接真实模型';
  $('agent-view-mode').textContent=disabled?'尚未启用':'MOCK MODE';
  $('agent-description').textContent=disabled?'当前未启用执行器。启用后即可开始尝试。':'当前为模拟模式，不会调用真实模型。';
  if(config.docs_url){document.querySelectorAll('.docs-link').forEach(link=>{link.hidden=false;link.href=apiUrl(config.docs_url);});}
}).catch(()=>{$('agent-mode-label').textContent='暂时无法获取配置';$('agent-description').textContent='无法获取运行模式，发送时将检查执行器状态。';});
