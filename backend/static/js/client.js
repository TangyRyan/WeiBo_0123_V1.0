
const API_BASE = location.origin;
function $(s){return document.querySelector(s);} function create(el,cls){const e=document.createElement(el); if(cls) e.className=cls; return e;}

async function loadDaily30(){
  const res = await fetch(`${API_BASE}/api/daily_30`);
  const js = await res.json();
  const list = $("#daily30");    // 获取目标 HTML 元素
  list.innerHTML="";

  js.data.forEach(d=>{
    const li = create("li");
    li.textContent = `${d.date} | 热度:${Math.round(d.heat)} | 风险:${d.risk.toFixed(1)}`; 
    list.appendChild(li); 
  });
}

function connectHotlistWS(){
  console.log("尝试连接热榜 WebSocket..."); // <--- 添加日志
  const ws = new WebSocket(`${location.origin.replace("http","ws")}/ws/hotlist`);

  ws.onopen = () => { // <--- 添加 onopen 处理器
      console.log("热榜 WebSocket 连接已打开");
  };

  ws.onmessage = ev => {
    console.log("收到热榜 WebSocket 数据:", ev.data); // <--- 添加日志
    try{
      const js = JSON.parse(ev.data);
      renderHotlist(js.items || []);
    } catch(e) {
      console.error("解析热榜 WebSocket 数据时出错:", e); // <--- 添加错误日志
    }
  };

  ws.onerror = (error) => { // <--- 添加 onerror 处理器
      console.error("热榜 WebSocket 错误:", error);
  };

  ws.onclose = (event) => { // <--- 添加 onclose 处理器
    console.log("热榜 WebSocket 连接已关闭:", event.code, event.reason);
    // 只有在非正常关闭时才尝试重连，避免无限循环
    if (event.code !== 1000) {
        console.log("将在2秒后尝试重新连接热榜 WebSocket...");
        setTimeout(connectHotlistWS, 2000);
    }
  };
}

// 同样地，为 connectRiskWS 函数也添加类似的日志
function connectRiskWS(){
  console.log("尝试连接风险预警 WebSocket..."); // <--- 添加日志
  const ws = new WebSocket(`${location.origin.replace("http","ws")}/ws/risk_warnings`);

  ws.onopen = () => { // <--- 添加 onopen 处理器
      console.log("风险预警 WebSocket 连接已打开");
  };

  ws.onmessage = ev => {
    console.log("收到风险预警 WebSocket 数据:", ev.data); // <--- 添加日志
    try{
      const js = JSON.parse(ev.data);
      renderRiskWarnings(js.events || []);
    } catch(e) {
      console.error("解析风险预警 WebSocket 数据时出错:", e); // <--- 添加错误日志
    }
  };

  ws.onerror = (error) => { // <--- 添加 onerror 处理器
      console.error("风险预警 WebSocket 错误:", error);
  };

  ws.onclose = (event) => { // <--- 添加 onclose 处理器
    console.log("风险预警 WebSocket 连接已关闭:", event.code, event.reason);
    if (event.code !== 1000) {
        console.log("将在2秒后尝试重新连接风险预警 WebSocket...");
        setTimeout(connectRiskWS, 2000);
    }
  };
}
function renderHotlist(items){
  const box = $("#hotlist"); box.innerHTML="";
  items.forEach(it => {
    const title = it.name || it.title || it.topic || "(未知话题)";
    const hotValue = it.hot ?? it.heat ?? it.score ?? 0;
    const div = create("div","hot-item");
    div.innerHTML = `<span class="rank">${it.rank ?? ""}</span><span class="name">${title}</span><span class="hot">🔥${hotValue}</span>`;
    div.onclick = ()=> loadEvent(title);
    box.appendChild(div);
  });
}

function connectRiskWS(){
  const ws = new WebSocket(`${location.origin.replace("http","ws")}/ws/risk_warnings`);
  ws.onmessage = ev => { 
    try{ 
      const js = JSON.parse(ev.data); 
      renderRiskWarnings(js.events || []);
    }catch(e){} 
  };
  ws.onclose = () => setTimeout(connectRiskWS, 2000);
}
// 风险预警列表数据动态渲染到页面
function renderRiskWarnings(list){
  const box = $("#risk"); 
  box.innerHTML = "";
  list.forEach((it,i)=>
    { 
      const div = create("div","risk-item");
      div.innerHTML = `<span class="rank">${i+1}</span><span class="name">${it.name}</span><span class="score">⚠️${(it.risk_score||0).toFixed(1)}</span>`;
      // 绑定点击事件（后续需要看需不需要删除）
      div.onclick = ()=> loadEvent(it.name); 
      box.appendChild(div);
    });
}

async function loadEvent(name){
  const res = await fetch(`${API_BASE}/api/event?name=${encodeURIComponent(name)}`);
  const js = await res.json(); if(js.error) return;
  $("#event-title").textContent = js.name;
  $("#event-overview").innerHTML = js.summary_html || "暂无概览";
  $("#event-dims").textContent = `情绪:${(js.llm?.sentiment ?? 0).toFixed(2)} 地区:${js.llm?.region || "-"} 类型:${js.llm?.topic_type || "-"} 风险:${(js.risk_score ?? 0).toFixed(1)} 维度:${Object.entries(js.risk_dims || {}).map(([k,v])=>k+':'+v.toFixed(1)).join(' / ')}`;
  const posts = $("#event-posts"); posts.innerHTML="";
  (js.posts||[]).forEach(p=>{ const el=create("div","post");
    const text = String(p.content_text || "").replace(/[\s\u00a0\u200b\ufeff]*展开c?\s*$/u, "……");
    el.innerHTML=`<div class="meta">${p.account_name} | ${p.published_at}</div><div class="text">${text}</div><div class="stats">转:${p.reposts} 评:${p.comments} 赞:${p.likes}</div>`;
    posts.appendChild(el);
  });
}

async function loadCentral(range){
  const res = await fetch(`${API_BASE}/api/central_data?range=${range}&t=${Date.now()}`);
  // const res = await fetch(`${API_BASE}/api/central_data?range=${range}`);
  const js = await res.json(); window.__centralData__ = js.data || [];
  if (window.renderCentral) window.renderCentral(window.__centralData__);
}

window.addEventListener("DOMContentLoaded", ()=>{
  loadDaily30();
  connectHotlistWS(); 
  connectRiskWS(); 
  // 预取较大时间窗的数据，前端本地过滤避免频繁请求慢接口
  loadCentral("three_months");
  // document.getElementById('central-range').addEventListener('change', e => loadCentral(e.target.value));
});
