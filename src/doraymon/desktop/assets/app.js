'use strict';
const $ = id => document.getElementById(id);
const state = {view:'overview', catalog:{shops:[],coverage:{},tasks:[]}, contacts:[], contactType:'friends', foodMode:'all', page:1, admin:{connected:false}, collectionControl:false, actionId:null,sending:false};
const titles = {overview:'概览',qq:'QQ 管理',food:'食物库',tasks:'采集任务',settings:'设置'};
const actionNames = {private:'发送私聊',mention:'群内 @',friend_request:'申请加好友',approve_friend:'处理好友申请',friends:'读取好友',groups:'读取群',members:'读取群成员'};
const statusNames = {running:'执行中',confirmed:'已确认',uncertain:'待核对',failed:'失败',unsupported:'接入未支持',completed:'已完成',partial:'部分完成',blocked:'获取受阻',paused:'已暂停',queued:'待处理',interrupted:'已中断',needs_review:'待核对'};
function el(tag, text, cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;}
async function call(name,...args){const api=window.pywebview?.api;if(!api||typeof api[name]!=='function')throw new Error('桌面连接尚未建立');return await api[name](...args);}
let toastTimer;function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,5000);}
function changeView(name){state.view=name;for(const n of document.querySelectorAll('.view'))n.classList.toggle('active',n.id===name);for(const n of document.querySelectorAll('[data-view]')){n.classList.toggle('active',n.dataset.view===name);if(n.dataset.view===name)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');}$('page-title').textContent=titles[name];if(name==='qq')loadHistory();if(name==='settings')loadReplySettings();}
document.querySelectorAll('[data-view]').forEach(n=>n.onclick=()=>changeView(n.dataset.view));document.querySelectorAll('[data-go]').forEach(n=>n.onclick=()=>changeView(n.dataset.go));
function badge(value){return el('span',statusNames[value]||value,'badge '+value);}
function timeText(value){if(!value)return '—';const date=new Date(value);return Number.isNaN(date.getTime())?'—':date.toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit'});}
async function loadStatus(){let value;try{value=await call('get_status');}catch{value={qq:'unknown',connection:'unknown',backend:'unknown',phase:'unavailable',admin:{connected:false}};}
for(const key of ['qq','connection','backend']){const status=value[key]||'unknown';$(key+'-state').textContent=status==='online'?'已连接':status==='offline'?'未连接':'尚未确认';$(key+'-dot').className='status-dot '+(status==='unknown'?'neutral':status);}
const all=['qq','connection','backend'].every(k=>value[k]==='online');$('global-state').textContent=all?'运行中':'等待连接';$('global-dot').className='status-dot '+(all?'online':'neutral');$('checked-time').textContent=value.checked_at?'检查于 '+timeText(value.checked_at):'';
const messages={waiting_bot_login:'在 QQ 登录机器人账号后，点击启动机器人。',stale:'监督状态已过期，请重新连接后刷新。',unavailable:'尚未读取到机器人状态，请检查安装位置或启动机器人。',stopped:'机器人已停止，自动启动已关闭。',error:'QQ 接入异常，请检查账号登录后重新连接。'};
$('runtime-recovery').textContent=messages[value.phase]||'';$('runtime-recovery').hidden=!messages[value.phase];state.admin=value.admin||{connected:false};state.collectionControl=!!(value.collection_control||state.admin.collection_control);if(state.admin.owner){$('owner-number').textContent=state.admin.owner;$('settings-owner').textContent=state.admin.owner;}updateActionType();renderTasks();}
for(const n of document.querySelectorAll('[data-runtime]'))n.onclick=async()=>{const buttons=[...document.querySelectorAll('[data-runtime]')];buttons.forEach(b=>b.disabled=true);const original=[...n.childNodes].map(c=>c.cloneNode(true));n.textContent='处理中…';try{const result=await call('runtime_action',n.dataset.runtime);toast(result.message||'操作完成');}catch{toast('桌面连接不可用，请检查程序是否完整安装。');}finally{n.replaceChildren(...original);buttons.forEach(b=>b.disabled=false);await loadStatus();}};
$('open-qq').onclick=async()=>{try{const r=await call('open_qq');if(!r.ok)toast(r.message);}catch{toast('请从 Windows 开始菜单打开 QQ。');}};
document.querySelectorAll('[data-settings]').forEach(n=>n.onclick=()=>call('open_settings',n.dataset.settings).catch(()=>toast('请通过桌面程序打开管理页。')));
let replySaving=false;
let replyGroups=[];
const replyDrafts=new Map();
function replyFormValues(){return {at_reply:$('reply-at').checked,normal_frequency:Number($('reply-normal').value)/100,conversation_frequency:Number($('reply-conversation').value)/100,conversation_minutes:Number($('reply-minutes').value)};}
function showReplyGroup(){
  const row=replyGroups.find(group=>group.group_id===$('reply-group').value);
  $('reply-settings-fields').disabled=!row;
  $('reply-conversation').setCustomValidity('');
  if(!row){$('reply-settings-state').textContent='尚未配置可管理的群聊。';return;}
  const draft=replyDrafts.get(row.group_id),s=draft||row.settings;
  $('reply-at').checked=s.at_reply;$('reply-normal').value=Number((s.normal_frequency*100).toFixed(1));$('reply-conversation').value=Number((s.conversation_frequency*100).toFixed(1));$('reply-minutes').value=s.conversation_minutes;
  $('reply-settings-state').textContent=draft?'该群有未保存的更改':row.applied?'该群设置已生效':'该群设置已保存，尚未应用';
}
async function loadReplySettings(){
  if(replySaving)return;
  $('reply-settings-fields').disabled=true;$('reply-group').disabled=true;$('reload-reply-settings').disabled=true;
  try{
    const result=await call('get_reply_settings');
    if(!Array.isArray(result.groups))throw new Error(result.error||'回复设置暂时无法读取。');
    replyGroups=result.groups;
    const selected=$('reply-group').value;
    $('reply-group').replaceChildren();
    for(const row of replyGroups)$('reply-group').add(new Option(`群 ${row.group_id}`,row.group_id));
    if(replyGroups.some(row=>row.group_id===selected))$('reply-group').value=selected;
    if(!replyGroups.length)$('reply-group').add(new Option('没有可管理的群聊',''));
    $('reply-group').disabled=!replyGroups.length;
    showReplyGroup();
  }catch(error){if(!replyGroups.length)$('reply-group').replaceChildren(new Option('群聊暂时无法读取',''));$('reply-settings-state').textContent=error.message;}
  finally{$('reload-reply-settings').disabled=false;}
}
$('reply-group').onchange=showReplyGroup;
$('reload-reply-settings').onclick=()=>{replyDrafts.delete($('reply-group').value);loadReplySettings();};
$('reply-settings-fields').addEventListener('input',()=>{replyDrafts.set($('reply-group').value,replyFormValues());$('reply-settings-state').textContent='该群有未保存的更改';$('reply-conversation').setCustomValidity('');});
$('reply-settings-form').onsubmit=async event=>{
  event.preventDefault();if(replySaving)return;
  const group_id=$('reply-group').value;if(!group_id)return;
  const settings=replyFormValues();
  if(settings.conversation_minutes&&settings.conversation_frequency<settings.normal_frequency){$('reply-conversation').setCustomValidity('连续对话活跃度不能低于平时活跃度。');$('reply-conversation').reportValidity();return;}
  replySaving=true;$('reply-settings-fields').disabled=true;$('reply-group').disabled=true;$('reload-reply-settings').disabled=true;$('reply-settings-state').textContent='正在保存并重启聊天后端…';
  try{const result=await call('save_reply_settings',{group_id,settings});if(result.ok){replyDrafts.delete(group_id);const row=replyGroups.find(group=>group.group_id===group_id);row.settings=settings;row.applied=!!result.applied;}$('reply-settings-state').textContent=result.message||'设置响应未确认，请重新读取。';}
  catch{$('reply-settings-state').textContent='设置响应未确认，请重新读取后检查。';}
  finally{replySaving=false;$('reply-settings-fields').disabled=false;$('reply-group').disabled=false;$('reload-reply-settings').disabled=false;await loadStatus();}
};
async function loadHistory(){try{const value=await call('get_history');const rows=value.actions||[];$('recent-actions').replaceChildren();$('actions-empty').hidden=rows.length>0;for(const row of rows.slice(0,8)){const tr=el('tr');tr.append(el('td',actionNames[row.action]||row.action),el('td',row.target||'—'));const cell=el('td');cell.append(badge(row.state));tr.append(cell,el('td',timeText(row.time)));$('recent-actions').append(tr);}}catch{$('recent-actions').replaceChildren();$('actions-empty').hidden=false;}}
function updateActionType(){const type=$('action-type').value;const approve=type==='approve_friend';$('group-field').hidden=type!=='mention';$('message-field').hidden=approve;$('approve-field').hidden=!approve;$('target-label').textContent=approve?'好友申请请求标识':'目标 QQ 号';$('target-id').inputMode=approve?'text':'numeric';$('message-text').required=type==='private'||type==='mention';$('execute-label').textContent=actionNames[type];$('group-id').required=type==='mention';
const unsupported=type==='friend_request'&&!state.admin.capabilities?.friend_request;$('execute-button').disabled=state.sending||!state.admin.connected||unsupported;$('capability-note').hidden=state.admin.connected&&!unsupported;$('capability-note').textContent=unsupported?'当前 QQ 接入未提供主动申请加好友接口。可处理对方发来的申请。':'管理连接未建立，启动机器人后刷新状态。';}
$('action-type').onchange=()=>{updateActionType();state.actionId=null;};$('action-form').addEventListener('input',()=>state.actionId=null);
$('action-form').onsubmit=async event=>{event.preventDefault();state.sending=true;const button=$('execute-button');button.disabled=true;state.actionId??=crypto.randomUUID();const operation={action:$('action-type').value,target:$('target-id').value.trim(),group:$('group-id').value.trim(),text:$('message-text').value,approve:$('approve-value').value==='yes',request_id:state.actionId};
let result;try{result=await call('execute',operation);}catch{result={state:'uncertain',message:'操作响应未确认，请先在 QQ 核对。'};}const area=$('action-result');area.hidden=false;area.textContent=result.message||statusNames[result.state]||'操作已记录';if(result.message_id)area.append(el('div','消息编号 '+result.message_id));area.className='action-result'+(result.state==='confirmed'?'':' error');if(result.state==='confirmed'){$('message-text').value='';state.actionId=null;}state.sending=false;updateActionType();await loadHistory();};
document.querySelectorAll('[data-contacts]').forEach(n=>n.onclick=()=>{state.contactType=n.dataset.contacts;document.querySelectorAll('[data-contacts]').forEach(b=>b.classList.toggle('active',b===n));state.contacts=[];renderContacts();});
$('load-contacts').onclick=async()=>{const button=$('load-contacts');button.disabled=true;try{const value=await call('execute',{action:state.contactType,request_id:crypto.randomUUID()});if(value.state==='confirmed')state.contacts=value.data||[];else toast(value.message||'联系人未读到');renderContacts();}catch{toast('管理连接不可用，启动机器人后重试。');}finally{button.disabled=false;}};
function renderContacts(){const query=$('contacts-search').value.trim().toLowerCase();const rows=state.contacts.filter(r=>[r.id,r.name,r.remark].join(' ').toLowerCase().includes(query));$('contact-list').replaceChildren();if(!rows.length){$('contact-list').append(el('p',state.contacts.length?'没有匹配的联系人':'点击刷新读取联系人','empty'));return;}for(const row of rows.slice(0,150)){const n=el('div',undefined,'contact-row');const d=el('div');d.append(el('strong',row.remark||row.name),el('span',row.id));const b=el('button','选用','button small');b.onclick=()=>{if(state.contactType==='groups'){$('action-type').value='mention';$('group-id').value=row.id;}else $('target-id').value=row.id;updateActionType();state.actionId=null;};n.append(d,b);$('contact-list').append(n);}}
$('contacts-search').oninput=renderContacts;
async function loadCatalog(){try{state.catalog=await call('get_catalog');}catch{state.catalog={shops:[],coverage:{},tasks:[],mode:'unavailable'};}const shops=state.catalog.shops||[],coverage=state.catalog.coverage||{};$('nav-count').textContent=shops.length?String(shops.length):'';$('food-total').textContent=shops.length;$('total-count').textContent=shops.length;$('hot-count').textContent=coverage.hot_total?`${coverage.hot_included||0} / ${coverage.hot_target||100}`:'未建立';$('drink-count').textContent=shops.filter(s=>s.food_group==='饮品').length;$('review-count').textContent=shops.filter(s=>!s.listed_location||s.location_status==='needs_review').length;
const previous=$('area-filter').value;$('area-filter').replaceChildren(new Option('全部区域',''));const areas=[...new Set(shops.map(s=>s.area_name||s.district).filter(Boolean))].sort();for(const area of areas)$('area-filter').add(new Option(area,area));if(areas.includes(previous))$('area-filter').value=previous;
const tag=$('tag-filter').value,tags=[...new Set(shops.flatMap(s=>s.tags||[]))].sort();$('tag-filter').replaceChildren(new Option('全部标签',''));for(const value of tags)$('tag-filter').add(new Option(value,value));if(tags.includes(tag))$('tag-filter').value=tag;
$('catalog-mode').hidden=['connected','local'].includes(state.catalog.mode);$('catalog-mode').textContent=state.catalog.mode==='snapshot'?'当前显示离线快照，WSL 可用后刷新最新资料。':'门店资料尚未载入。';renderFood();renderTasks();}
function inactive(shop){return ['historical','closed','source_reports_suspended','source_reports_closed'].includes(shop.activity_status);}
function activityText(shop){return {verified_open:'有近期营业依据',source_reports_suspended:'来源标为暂停营业',source_reports_closed:'来源标为已停业',closed:'已停业',historical:'历史门店'}[shop.activity_status]||'尚未核实';}
function foodRows(){const hot=state.catalog.hot_candidates||[],pool=state.catalog.ranking_candidates||hot;if(state.foodMode==='pending')return pool.filter(c=>!c.shop_id).map(c=>({display_name:c.name,listed_location:c.place_address||c.address,location_status:'needs_review',candidate:c,rankings:c.rankings}));const ids=new Set(state.catalog.coverage?.hot_ids||hot.map(c=>c.shop_id).filter(Boolean));return(state.catalog.shops||[]).filter(s=>state.foodMode!=='hot'||ids.has(s.id));}
function filteredShops(){const query=$('food-search').value.trim().toLowerCase(),review=$('review-filter').value;return foodRows().filter(s=>(!query||[s.display_name,s.brand,s.listed_location,...s.aliases||[]].join(' ').toLowerCase().includes(query))&&(!$('area-filter').value||(s.area_name||s.district)===$('area-filter').value)&&(!$('category-filter').value||s.food_group===$('category-filter').value)&&(!$('tag-filter').value||(s.tags||[]).includes($('tag-filter').value))&&(!review||(review==='inactive'?inactive(s):review==='review'?s.location_status==='needs_review'||!s.listed_location:s.listed_location&&s.location_status!=='needs_review')));}
function renderFood(){const note=$('rankings-note'),pool=state.catalog.ranking_candidates||state.catalog.hot_candidates||[];note.hidden=state.foodMode==='all';note.textContent=state.foodMode==='pending'?`全部来源名单共 ${pool.length} 个候选。`:'综合候选优先收录多来源出现的门店，原榜单年份与名次见来源。';const rows=filteredShops(),size=40,pages=Math.max(1,Math.ceil(rows.length/size));state.page=Math.min(state.page,pages);$('food-rows').replaceChildren();$('food-empty').hidden=rows.length>0;$('food-empty').textContent=state.foodMode==='pending'&&!foodRows().length?'当前名单没有待匹配条目':(state.catalog.shops||[]).length?'没有匹配的门店':'尚未载入门店资料';for(const shop of rows.slice((state.page-1)*size,state.page*size)){const tr=el('tr',undefined,'food-row');const name=el('td'),button=el('button',shop.display_name,'shop-name text-button');button.onclick=()=>shop.candidate?showCandidate(shop.candidate):showShop(shop);name.append(button,el('span',shop.listed_location||'位置待核对','shop-address'));const kind=el('td',shop.brand||'—');kind.append(el('span',shop.food_group||shop.category||'','category-line'));const status=el('td'),review=shop.candidate||shop.location_status==='needs_review'||!shop.listed_location;status.append(el('span',shop.candidate?'分店待核对':inactive(shop)?activityText(shop):review?'位置待核对':'已记录地址','badge '+(review||inactive(shop)?'review':'')));tr.append(name,kind,el('td',shop.area_name||shop.district||'待核对'),status,el('td',`${(shop.candidate?shop.rankings:shop.sources||shop.source_refs||[]).length} 项`));$('food-rows').append(tr);}$('food-page-note').textContent=`共 ${rows.length} 个${state.foodMode==='pending'?'待匹配条目':'门店'}`;$('page-number').textContent=`${state.page} / ${pages}`;$('previous-page').disabled=state.page<=1;$('next-page').disabled=state.page>=pages;}
document.querySelectorAll('[data-food-mode]').forEach(n=>n.onclick=()=>{state.foodMode=n.dataset.foodMode;state.page=1;for(const id of ['area-filter','category-filter','tag-filter','review-filter']){$(id).value='';$(id).disabled=state.foodMode==='pending';}document.querySelectorAll('[data-food-mode]').forEach(b=>b.classList.toggle('active',b===n));renderFood();});
for(const id of ['food-search','area-filter','category-filter','tag-filter','review-filter'])$(id).addEventListener('input',()=>{state.page=1;renderFood();});$('previous-page').onclick=()=>{state.page--;renderFood();};$('next-page').onclick=()=>{state.page++;renderFood();};$('refresh-catalog').onclick=loadCatalog;$('export-catalog').onclick=()=>call('export_catalog').then(r=>r.ok&&toast(r.message)).catch(()=>toast('请通过桌面程序导出资料。'));
function dateText(value){if(!value)return '未注明';if(/^\d{4}-\d{2}-\d{2}$/.test(value))return value;const d=new Date(value);return Number.isNaN(d.getTime())?String(value):d.toLocaleString('zh-CN',{hour12:false});}
function sourceTitle(source){const title=source.title||source.list_title||'来源资料',url=source.url||source.source_url||'';if(!url.startsWith('https://www.amap.com/ranking/nanchang/food'))return title;const district={'360102':'东湖区','360103':'西湖区','360104':'青云谱区','360111':'青山湖区','360112':'新建区','360113':'红谷滩区'}[url.split('/').at(-1)]||'全市';return `${title} · ${district}`;}
function showShop(shop){
  $('detail-title').textContent=shop.display_name;$('detail-meta').replaceChildren();
  for(const[label,value]of [['品牌',shop.brand||'待核对'],['地址',shop.listed_location||'待核对'],['区域',shop.area_name||shop.district||'待核对'],['类别',shop.category||shop.food_group],['标签',(shop.tags||[]).join('、')||'—'],['经营状态',activityText(shop)],['资料时间',dateText(shop.checked_at||state.catalog.generated_at)]]){$('detail-meta').append(el('dt',label),el('dd',value||'—'));}
  $('detail-sources').replaceChildren();const places=new Set();
  for(const source of shop.sources||[]){
    const n=el('div',undefined,'source');const label=source.publisher||source.title||'来源资料';
    if(source.url){const link=el('a',label);link.href=source.url;link.onclick=event=>{event.preventDefault();call('open_external',source.url).catch(()=>toast('请在桌面程序中打开来源。'));};n.append(link);}else n.append(el('strong',label));
    if(source.title&&source.title!==shop.display_name&&source.title!==label)n.append(el('p',sourceTitle(source)));
    const dates=[`原文日期 ${dateText(source.published_date)}`,`读取 ${dateText(source.checked_at||shop.checked_at)}`];if(source.page_updated_date)dates.push(`页面更新 ${dateText(source.page_updated_date)}`);if(source.edition)dates.unshift(`榜单年度 ${source.edition}`);n.append(el('p',dates.join(' · ')));$('detail-sources').append(n);
    if(source.place_url&&!places.has(source.place_url)){places.add(source.place_url);const point=el('a','高德门店资料');point.href=source.place_url;point.onclick=e=>{e.preventDefault();call('open_external',source.place_url);};n.append(point,el('p',`${source.place_name||''} ${source.place_address||''}`));}
  }
  if(shop.activity_basis?.source_url){const basis=el('div',undefined,'source');const link=el('a',`${activityText(shop)} · 状态来源`);link.href=shop.activity_basis.source_url;link.onclick=e=>{e.preventDefault();call('open_external',shop.activity_basis.source_url);};basis.append(link,el('p',`读取 ${dateText(shop.activity_basis.checked_at)}`));$('detail-sources').append(basis);}
  if(!$('detail-sources').childElementCount)$('detail-sources').append(el('p','来源资料尚未载入'));
  $('detail-menu-heading').hidden=$('detail-menu').hidden=false;$('detail-menu').textContent=shop.menu_text||'尚未收录可核对的完整菜单';$('shop-detail').showModal();
}
function showCandidate(candidate){
  $('detail-title').textContent=candidate.name;$('detail-meta').replaceChildren();
  const fields=[['匹配状态','分店待核对'],['名单地址',candidate.address||'未注明']];if(candidate.place_address)fields.push(['高德地址',candidate.place_address]);fields.push(['待核对原因',candidate.match_basis]);
  for(const[label,value]of fields)$('detail-meta').append(el('dt',label),el('dd',value));
  $('detail-sources').replaceChildren();
  for(const row of candidate.rankings){const source=(state.catalog.coverage.ranking_sources||[]).find(s=>String(s.id)===String(row.rankingId));const n=el('div',undefined,'source'),link=el('a',sourceTitle(row));const url=row.source_url||source?.url;link.href=url;link.onclick=e=>{e.preventDefault();call('open_external',url);};n.append(link);const dates=[row.publisher||source?.publisher,`原文日期 ${dateText(row.published_date)}`,`读取 ${dateText(row.checked_at)}`];if(row.edition)dates.push(`榜单年度 ${row.edition}`);if(row.page_updated_date)dates.push(`页面更新 ${dateText(row.page_updated_date)}`);n.append(el('p',dates.filter(Boolean).join(' · ')));$('detail-sources').append(n);}
  if(candidate.place_url){const n=el('div',undefined,'source'),link=el('a',candidate.place_name||'高德门店资料');link.href=candidate.place_url;link.onclick=e=>{e.preventDefault();call('open_external',candidate.place_url);};n.append(link);$('detail-sources').append(n);}
  $('detail-menu-heading').hidden=$('detail-menu').hidden=true;$('shop-detail').showModal();
}
$('close-detail').onclick=()=>$('shop-detail').close();$('shop-detail').onclick=event=>{if(event.target===$('shop-detail')){const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)event.target.close();}};
function renderTasks(){
  const tasks=state.catalog.tasks||[];
  $('task-rows').replaceChildren();$('tasks-empty').hidden=tasks.length>0;
  for(const task of tasks){
    const tr=el('tr'),name=el('td',task.source_kind==='ranking'?sourceTitle(task):task.title||task.name);
    if(task.message)name.append(el('span',task.message,'category-line'));
    const status=el('td');status.append(badge(task.status||'queued'));
    const pending=task.pending_entries===undefined?task.needs_review??'—':Math.max(task.pending_entries,task.needs_review||0);
    tr.append(name,status,el('td',task.imported??'—'),el('td',pending),el('td',dateText(task.updated_at)));
    $('task-rows').append(tr);
  }
  $('coverage-notes').replaceChildren();
  const c=state.catalog.coverage||{};
  const notes=[['热门100',c.hot_total?`已匹配 ${c.hot_included||0} / ${c.hot_target||100}；全部名单待匹配 ${c.ranking_missing??c.hot_missing??0}，名单待补 ${c.hot_pending||0}`:'综合榜单尚未建立'],['品牌分店',c.branch_missing===undefined?'分店清单尚未完成核对':`待补 ${c.branch_missing} 个分店`],['饮品覆盖',c.drink_source_status||'区域与品牌清单待核对'],['菜单资料','完整菜单尚未核对']];
  if(c.drink_brands_without_records?.length)notes.splice(3,0,['饮品尚无记录品牌',c.drink_brands_without_records.join('、')]);
  for(const[note,value]of notes){const row=el('div',undefined,'coverage-note');row.append(el('strong',note),el('span',value));$('coverage-notes').append(row);}
  const controllable=state.collectionControl&&['connected','local'].includes(state.catalog.mode);
  $('collection-state').textContent=statusNames[state.catalog.collection?.state]||'等待采集';
  $('pause-collection').disabled=!controllable;$('resume-collection').disabled=!controllable;
}
for(const[id,action]of [['pause-collection','pause'],['resume-collection','resume']])$(id).onclick=async()=>{const buttons=[$('pause-collection'),$('resume-collection')];buttons.forEach(b=>b.disabled=true);try{const result=await call('collection_action',action);toast(result.message);await loadCatalog();}catch{toast('采集控制入口尚未连接。');}finally{renderTasks();}};
async function refresh(){await Promise.allSettled([loadStatus(),loadCatalog(),loadHistory()]);}$('refresh').onclick=refresh;window.addEventListener('pywebviewready',refresh);setTimeout(refresh,300);setInterval(()=>loadStatus(),12000);setInterval(()=>loadCatalog(),25000);
