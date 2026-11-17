import {state} from '../stores/session.js';import {esc} from '../utils/html.js';import {table} from '../components/common/table.js';import {badge} from '../components/common/status.js';
const stateText={pending:'待检',publishing:'公开中',published:'已公开',withdrawn:'已撤下'};
const combosText=items=>(items||[]).map(i=>i.label+' '+i.total_completed).join(' / ')||'—';
const approved=()=>state.snapshots.filter(s=>s.state==='done'&&s.approval==='approved');
const strip=text=>String(text||'').replace(/^整包拒发：/,'');
const leakRows=(leaking,withdrawnColumn)=>leaking.flatMap(l=>(l.combinations||[]).map(c=>{
 const source=c.source==='history'?'历史披露 #'+c.publication_id:'本次包';
 const combo=c.label+' ('+c.class_codes.join(',')+')';
 return withdrawnColumn?[l.class_code,source,combo,c.total_completed,c.withdrawn?'是':'否']:[l.class_code,source,combo,c.total_completed];
}));
function publicPanel(){
 const rows=state.publicationsPublic||[];
 return `<section class="panel"><h2>公开发布 / 已在公开页面呈现的组合</h2><p class="notice">公开页面与下载只呈现通过检查的组合名称与合计；班级明细、可行范围与泄露诊断仅管理页可见。已撤下内容不再展示，但仍计入历史披露。</p>${rows.length?rows.map(p=>`<article class="rec"><h3>发布 #${p.publication_id} · 快照 #${p.snapshot_id} ${badge('已公开')}</h3><p>${esc(p.published_at||'')} · 第 ${p.revision} 次联审</p>${table(['组合','结课合计'],(p.items||[]).map(i=>[i.label,i.total_completed]))}<button data-pub-export="${p.publication_id}">下载公开 JSON</button></article>`).join(''):'<p>尚无已公开的组合。待检包通过发布后才进入公开页面。</p>'}</section>`;
}
function precheckPanel(){
 const r=state.precheck;
 if(!r)return '<section class="panel"><h2>管理页预检</h2><p>预检按该快照历次已公开（含已撤下）合计，逐班求非负整数可行范围，不落账、不影响签核。结课不足 5 人的班级范围宽度小于 3 时整包拒发。</p><p>填写组合后点击“预检”，确认范围与拒发原因。</p></section>';
 return `<section class="panel"><h2>管理页预检</h2><p>预检按该快照历次已公开（含已撤下）合计，逐班求非负整数可行范围，不落账、不影响签核。结课不足 5 人的班级范围宽度小于 3 时整包拒发。</p><p class="notice">${r.passed?'预检通过，可提交待检发布。':'整包拒发：'+esc(strip(r.reason))}</p>${(r.ranges||[]).length?table(['班级','结课(内部)','实到上限','可行范围','宽度','是否小班'],r.ranges.map(x=>[x.class_code,x.completed,x.attended,`[${x.min}, ${x.max}]`,x.width,x.small?'是':'否'])):''}${(r.leaking||[]).length?`<h3>泄露组合</h3>${table(['班级','来源','组合','结课合计'],leakRows(r.leaking,false))}`:''}</section>`;
}
function ledgerPanel(){
 const rows=state.publicationLedger||[];const canAct=state.user.role==='admin';
 const body=rows.map(p=>{
  const actions=canAct?`${p.state==='pending'?`<button data-pub-publish="${p.id}">发布</button>`:''}${p.state==='published'?`<button data-pub-withdraw="${p.id}">撤下</button>`:''}<button data-pub-select="${p.id}">查看范围</button>`:'—';
  return `<tr><td>${esc(p.id)}</td><td>${esc(p.snapshot_id)}</td><td>${esc(stateText[p.state]||p.state)}</td><td>v${esc(p.revision||1)}</td><td>${esc(combosText(p.items))}</td><td>${esc(p.reason?strip(p.reason).slice(0,90):'—')}</td><td>${actions}</td></tr>`;
 }).join('')||'<tr><td colspan="7">尚无发布台账。选择已签核快照编制待检包。</td></tr>';
 return `<section class="panel"><h2>发布台账 / 待检与已公开</h2><p>发布从待检到已公开；检查后新增公开合计或并发发布会在发布时重新联审。重试不重复记账，中断由恢复流程补完。撤下不删除历史披露，草稿与待检不计入。</p><div class="scroll"><table><thead><tr>${['ID','快照','状态','联审','组合与合计','拒发原因','操作'].map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div></section>`;
}
function detailPanel(){
 const d=state.publicationSelected;if(!d)return '';
 const r=d.check_result||{};
 return `<section class="panel"><h2>发布 #${esc(d.id)} 检查详情</h2><p>快照 #${esc(d.snapshot_id)} · ${esc(stateText[d.state]||d.state)} · 联审 v${esc(d.revision)} · 检查人 ${esc(d.checked_by)} ${esc(d.checked_at||'')}</p>${d.reason?`<p class="notice">${esc(d.reason)}</p>`:''}${(r.ranges||[]).length?table(['班级','结课(内部)','实到上限','可行范围','宽度','覆盖','小班'],r.ranges.map(x=>[x.class_code,x.completed,x.attended,`[${x.min}, ${x.max}]`,x.width,x.covered?'是':'否',x.small?'是':'否'])):''}${(r.leaking||[]).length?`<h3>泄露组合</h3>${table(['班级','来源','组合','合计','已撤下'],leakRows(r.leaking,true))}`:''}</section>`;
}
export function render(){
 const blocked=state.user.role!=='admin';
 const options=approved().map(s=>`<option value="${s.id}">#${s.id} ${esc(s.result.metric.name)} v${esc(s.result.metric.version)} · ${esc(s.date_from)}~${esc(s.date_to)}</option>`).join('');
 return `${publicPanel()}${blocked?'':`<div class="split"><section>${precheckPanel()}</section><aside class="side"><h2>编制待检发布</h2><form id="publication"><label>签核快照<select name="snapshot_id" required>${options||'<option value="">暂无已签核快照</option>'}</select></label><label>公开组合<textarea name="combos" rows="4" required placeholder="组合名称: QH-01,QH-02&#10;另一组合: QH-01,QH-02,QH-03"></textarea></label><p class="eyebrow">每行一组，冒号前为名称，冒号后用逗号分隔班级</p><button type="button" id="precheck">预检</button> <button class="primary">提交待检发布</button></form><p>待检不计入披露；发布时才按最新历史联合重审。</p></aside></div>${ledgerPanel()}${detailPanel()}`}`;
}
