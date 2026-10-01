'use strict';
const icons = {
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  hash:'<path d="M5 9h15M4 15h15M11 3 7 21M17 3l-4 18"/>',
  sliders:'<path d="M4 7h8m4 0h4M4 17h3m4 0h9"/><circle cx="14" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  activity:'<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  shield:'<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6z"/><path d="m8 12 3 3 5-6"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
  layers:'<path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  refresh:'<path d="M20 11a8 8 0 0 0-14-5L3 9m0-6v6h6m-5 4a8 8 0 0 0 14 5l3-3m0 6v-6h-6"/>',
  bot:'<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 7V3m-2 0h4M1 12v4m22-4v4M9 16h6"/><circle cx="9" cy="12" r=".8"/><circle cx="15" cy="12" r=".8"/>',
  cpu:'<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"/>',
  play:'<path d="m7 4 13 8-13 8z"/>', stop:'<rect x="5" y="5" width="14" height="14" rx="2"/>',
  message:'<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2v-9.5A8.5 8.5 0 0 1 10.5 4h2A8.5 8.5 0 0 1 21 11.5Z"/><path d="M7 10h9m-9 4h6"/>',
  sparkles:'<path d="m12 3 2.3 6.7L21 12l-6.7 2.3L12 21l-2.3-6.7L3 12l6.7-2.3zM20 2v4m-2-2h4"/>',
  calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18m-13 4h2m4 0h2"/>',
  'check-circle':'<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>', check:'<path d="m5 12 4 4L19 6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>', info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
  database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
  folder:'<path d="M3 7V5a2 2 0 0 1 2-2h5l3 3h6a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
};
const $ = id => document.getElementById(id);
function icon(name) {
  const span = document.createElement('span');
  span.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.info}</svg>`;
  return span;
}
for (const node of document.querySelectorAll('[data-icon]')) node.append(icon(node.dataset.icon).firstChild);
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
const number = value => new Intl.NumberFormat('ko-KR').format(value || 0);
const formatTime = timestamp => timestamp ? new Date(timestamp * 1000).toLocaleString('ko-KR', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '아직 없음';
const labels = {overview:'대시보드', channels:'수집 채널', settings:'봇 설정', events:'운영 기록'};
let snapshot = null;
let dirty = false;
let busy = false;
let online = false;
let toastTimer;
let fetching = false;
let confirmation = null;
function go(view) { location.hash = labels[view] ? view : 'overview'; }
function route() {
  const view = labels[location.hash.slice(1)] ? location.hash.slice(1) : 'overview';
  for (const name of Object.keys(labels)) $('view-' + name).classList.toggle('hidden', name !== view);
  for (const node of document.querySelectorAll('.nav-item')) {
    const selected = node.dataset.view === view;
    node.classList.toggle('active', selected);
    if (selected) node.setAttribute('aria-current', 'page'); else node.removeAttribute('aria-current');
  }
  $('crumb').textContent = labels[view];
  document.title = `${labels[view]} · 모아`;
}
window.addEventListener('hashchange', route);
document.addEventListener('click', event => {
  const node = event.target.closest('[data-goto]');
  if (node) go(node.dataset.goto);
});
route();
function toast(text, error = false) {
  clearTimeout(toastTimer);
  $('toast').textContent = text;
  $('toast').className = 'toast' + (error ? ' error' : '');
  toastTimer = setTimeout(() => $('toast').classList.add('hidden'), 5500);
}
function confirmAction(title, description) {
  $('confirm-title').textContent = title;
  $('confirm-description').textContent = description;
  $('confirm-dialog').showModal();
  return new Promise(resolve => { confirmation = resolve; });
}
function finishConfirmation(answer) {
  $('confirm-dialog').close();
  if (confirmation) confirmation(answer);
  confirmation = null;
}
$('confirm-cancel').addEventListener('click', () => finishConfirmation(false));
$('confirm-accept').addEventListener('click', () => finishConfirmation(true));
$('confirm-dialog').addEventListener('cancel', event => { event.preventDefault(); finishConfirmation(false); });
async function api(path, payload) {
  const options = {cache:'no-store', signal:AbortSignal.timeout(20000)};
  if (payload !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type':'application/json', 'X-Admin-Token':snapshot?.csrf_token || ''};
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '요청을 완료하지 못했습니다.');
  return data;
}
function emptyChannels(container) {
  const root = element('div','empty-state');
  const badge = element('div','empty-icon'); badge.append(icon('hash')); root.append(badge);
  root.append(element('h3','', '아직 연결된 채널이 없어요'), element('p','', '봇 설정에서 서버 ID와 수집할 채널을 추가해 보세요.'));
  const button = element('button','text-button','첫 채널 연결하기 →'); button.dataset.goto='settings'; root.append(button);
  container.replaceChildren(root);
}
function renderChannels(container, channels) {
  if (!channels.length) return emptyChannels(container);
  const wrap = element('div','table-wrap');
  const table = element('table');
  const head = element('thead'); const hr = element('tr');
  for (const name of ['채널','저장된 메시지','최근 대화','최근 동기화','수집 범위']) hr.append(element('th','',name));
  head.append(hr); table.append(head);
  const body = element('tbody');
  for (const channel of channels) {
    const row = element('tr'); const name = element('td'); const namewrap = element('div','channel-name');
    namewrap.append(element('span','hash-box','#'),element('code','',channel.id)); name.append(namewrap); row.append(name);
    row.append(element('td','',number(channel.messages) + '개'), element('td','',formatTime(channel.last_message_at)), element('td','',formatTime(channel.last_synced_at)));
    const cell = element('td'); cell.append(element('span','badge ' + (channel.truncated ? 'warning' : 'muted'),channel.truncated ? '최근 일부 확인' : channel.last_synced_at ? '최근 범위 확인' : '동기화 대기')); row.append(cell); body.append(row);
  }
  table.append(body); wrap.append(table); container.replaceChildren(wrap);
}
function fillForm(settings) {
  const map = {'guild-id':'guild_id','model':'model','daily-limit':'daily_limit','retention':'retention_days','sync-limit':'sync_limit'};
  for (const [id, field] of Object.entries(map)) $(id).value = settings[field];
  $('channel-ids').value = settings.channel_ids.join('\n');
  $('question-channel-ids').value = settings.question_channel_ids.join('\n');
  $('enable-ai').checked = settings.allow_external_llm;
  if (![...$('timezone').options].some(option => option.value === settings.timezone)) {
    const option = element('option','',settings.timezone); option.value = settings.timezone; $('timezone').append(option);
  }
  $('timezone').value = settings.timezone;
  $('database-path').textContent = settings.database_path;
}
function render(data) {
  snapshot = data;
  const {bot,settings,stats,checks,events} = data;
  const running = ['starting','connected'].includes(bot.state);
  const ready = checks.every(check => check.ok);
  const statuses = {stopped:['중지됨','muted','연결 정보를 설정한 뒤 봇을 시작해 주세요.'],starting:['연결 중','warning','Discord에 연결하고 있습니다. 잠시만 기다려 주세요.'],connected:['연결됨','','이 관리 화면에서 시작한 봇이 Discord에 연결되었습니다.'],failed:['실행 실패','danger','봇이 종료되었습니다. 연결 정보와 권한을 확인해 주세요.']};
  const status = statuses[bot.state] || statuses.stopped;
  $('status-badge').textContent = status[0]; $('status-badge').className = 'badge ' + status[1];
  $('bot-description').textContent = status[2];
  $('bot-dot').classList.toggle('online', bot.state === 'connected');
  $('hero-model').textContent = settings.allow_external_llm ? settings.model : 'Gemini · 비활성';
  $('hero-channels').textContent = `${settings.channel_ids.length}개 채널`;
  $('bot-action-label').textContent = busy ? '처리 중…' : running ? '봇 중지하기' : ready ? '봇 시작하기' : '연결 설정하기';
  $('bot-action').classList.toggle('stop',running);
  $('bot-action').querySelector('[data-icon]').replaceChildren(icon(running ? 'stop' : ready ? 'play' : 'sliders').firstChild);
  $('bot-action').disabled = busy || !online;
  $('metric-messages').textContent = stats.database_error ? '—' : number(stats.message_count);
  $('metric-channels').textContent = number(settings.channel_ids.length);
  $('nav-channel-count').textContent = number(settings.channel_ids.length);
  $('metric-calls').textContent = stats.database_error ? '—' : number(stats.utc_calls);
  $('metric-limit').textContent = `/ ${number(settings.daily_limit)}회`;
  $('metric-retention').textContent = number(settings.retention_days);
  $('usage-meter').style.width = Math.min(100, stats.utc_calls / Math.max(1, Number(settings.daily_limit)) * 100) + '%';
  const total = stats.series.reduce((sum,item)=>sum+item.count,0);
  $('weekly-total').textContent = stats.database_error ? '—' : number(total);
  const max = Math.max(...stats.series.map(item=>item.count),1);
  const bars = stats.series.map(item => {
    const column = element('div','chart-column'); const bar = element('div','bar' + (item.count ? '' : ' empty'));
    bar.style.height = item.count ? (item.count/max*80)+'%' : '3px';
    bar.title = `${item.date}: ${number(item.count)}개`;
    const label = element('small','',item.date.slice(5).replace('-','.'));
    column.append(bar,label); return column;
  });
  $('chart-bars').replaceChildren(...bars);
  $('activity-chart').setAttribute('aria-label', '최근 7일 저장된 메시지. ' + stats.series.map(s=>`${s.date}: ${s.count}개`).join(', '));
  $('chart-empty').classList.toggle('hidden',total > 0 && !stats.database_error);
  $('chart-empty').textContent = stats.database_error ? '저장소를 읽을 수 없습니다. 설정과 파일 권한을 확인하세요.' : '첫 대화가 수집되면 여기에 표시됩니다.';
  const checkRows = checks.map((check,index) => {
    const row=element('div','check-row'); const badge=element('span','check-indicator'+(check.ok?' done':''));
    if(check.ok) badge.append(icon('check')); else badge.textContent=index+1;
    const copy=element('div','check-copy');copy.append(element('strong','',check.label),element('small','',check.detail));row.append(badge,copy);return row;
  });
  $('checks').replaceChildren(...checkRows);
  $('checklist-progress').textContent = `${checks.filter(c=>c.ok).length} / ${checks.length}개 준비 완료 · 실제 연결은 시작 후 확인`;
  renderChannels($('dashboard-channels'),stats.channels.slice(0,4));
  renderChannels($('all-channels'),stats.channels);
  $('guild-label').textContent = settings.guild_id ? `서버 ID · ${settings.guild_id}` : '서버가 설정되지 않았습니다.';
  const questionIds = settings.question_channel_ids;
  $('question-channel-list').replaceChildren(...(questionIds.length ? questionIds.map(id=>element('p','channel-name',`# ${id} · 채널 태그 + 요청 → 요약 스레드 → 이어서 대화`)) : [element('p','under-note','질문 채널이 없습니다. 봇 설정에서 수집 채널과 별도로 추가하세요.')]));
  $('event-list').replaceChildren(...events.map(event=>{
    const row=element('div','event-row');const badge=element('span','event-mark '+event.level);badge.append(icon(event.level==='success'?'check':event.level==='warning'?'info':'activity'));
    const time=element('time','',formatTime(event.time));time.dateTime=new Date(event.time*1000).toISOString();row.append(badge,element('p','',event.message),time);return row;
  }));
  for(const [id,name] of [['discord-secret-state','discord_token'],['gemini-secret-state','gemini_api_key']]) {
    $(id).textContent='저장됨';$(id).classList.toggle('present',settings.secrets[name]);
  }
  $('settings-fields').disabled=running||busy;
  $('settings-locked').classList.toggle('hidden',!running);
  $('save-settings').disabled=running||busy||!online;
  if(!dirty) fillForm(settings);
  $('last-updated').textContent='최근 확인 '+new Date().toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',hour12:false});
}
async function refresh(manual=false) {
  if(fetching) return;
  fetching=true;
  try { const data=await api('/api/state');online=true;$('connection-error').classList.add('hidden');render(data);if(manual)toast('최신 상태로 업데이트했습니다.'); }
  catch(error){online=false;$('connection-error').textContent='관리 서버에 연결할 수 없습니다. 실행 중인지 확인한 뒤 새로고침해 주세요.';$('connection-error').classList.remove('hidden');$('bot-action').disabled=true;$('save-settings').disabled=true;}
  finally{fetching=false;}
}
$('refresh').addEventListener('click',()=>refresh(true));
$('settings-form').addEventListener('input',()=>{dirty=true;$('save-hint').textContent='저장하지 않은 변경 사항이 있습니다.';});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
$('settings-form').addEventListener('submit',async event=>{
  event.preventDefault();if(busy||!snapshot)return;
  const form=new FormData(event.currentTarget);
  const payload=Object.fromEntries(form.entries());
  payload.allow_external_llm=$('enable-ai').checked;
  payload.channel_ids=String(payload.channel_ids).split(/[,\n]/).map(s=>s.trim()).filter(Boolean);
  payload.question_channel_ids=String(payload.question_channel_ids).split(/[,\n]/).map(s=>s.trim()).filter(Boolean);
  const removed=snapshot.settings.channel_ids.some(id=>!payload.channel_ids.includes(id));
  const guildChanged=Boolean(snapshot.settings.guild_id && payload.guild_id!==snapshot.settings.guild_id);
  if(Number(payload.retention_days)<Number(snapshot.settings.retention_days)||removed||guildChanged){
    if(!await confirmAction('수집 범위를 변경할까요?','보관 기간을 줄이거나 서버·수집 채널을 변경하면 다음 봇 시작 시 기존 범위 밖의 저장 기록이 삭제될 수 있습니다. 변경한 설정을 저장할까요?'))return;
  }
  busy=true;render(snapshot);
  try{const result=await api('/api/settings',payload);dirty=false;$('discord-token').value='';$('gemini-key').value='';snapshot=result.state;toast('설정을 저장했습니다. 봇을 시작하면 적용됩니다.');$('save-hint').textContent='설정이 안전하게 저장되었습니다.';}
  catch(error){toast(error.message,true);}
  finally{busy=false;render(snapshot);}
});
$('bot-action').addEventListener('click',async()=>{
  if(!snapshot||busy)return;
  const running=['starting','connected'].includes(snapshot.bot.state);
  if(!running&&!snapshot.checks.every(check=>check.ok)){go('settings');return;}
  if(running&&!await confirmAction('봇을 중지할까요?','중지하는 동안 메시지 수집과 요약 명령이 동작하지 않습니다. 저장된 대화와 설정은 유지됩니다.'))return;
  busy=true;render(snapshot);
  try{const result=await api(running?'/api/bot/stop':'/api/bot/start',{});snapshot=result.state;toast(running?'봇을 중지했습니다.':'봇을 시작했습니다. 연결 상태를 확인하고 있습니다.');}
  catch(error){toast(error.message,true);}
  finally{busy=false;render(snapshot);}
});
refresh();
setInterval(()=>{if(!document.hidden&&!busy)refresh();},5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!busy)refresh();});
