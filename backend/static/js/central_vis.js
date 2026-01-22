(function(){
  // 定义字段
  const discreteFields = ["领域", "地区"];
  const continuousFields = ["情绪", "时间", "风险值"];
  const allFields = discreteFields.concat(continuousFields);

  // 选择容器并清空
  const container = d3.select("#central-vis");
  container.select("svg").remove();

  // 响应式变量
  let currentWidth, currentHeight;

  // 创建 SVG
  const svg = container.append("svg").style("display","block").attr("preserveAspectRatio","xMidYMid meet");

  // 填充下拉选项
  const attrSel = d3.select("#central-attribute");
  const colorSel = d3.select("#central-colorby");

  attrSel.selectAll("option").data(allFields).enter().append("option").attr("value", d=>d).text(d=>d);
  colorSel.selectAll("option").data(allFields).enter().append("option").attr("value", d=>d).text(d=>d);

  attrSel.property("value", "领域");
  colorSel.property("value", "风险值");

  // 颜色比例尺
  const color = d3.scaleOrdinal(d3.schemeTableau10);
  const colorCont = d3.scaleSequential(d3.interpolateTurbo).domain([0,100]);
  const colorRiskScale = t => d3.interpolateRdYlBu(t);

  // tooltip（通用容器）
  const tooltip = d3.select("body").append("div").attr("class", "tooltip")
    .style("position","absolute")
    .style("pointer-events","none")
    .style("background","transparent")
    .style("padding","0px")
    .style("border-radius","6px")
    .style("font-size","12px")
    .style("color","#e8e8e8")
    .style("visibility","hidden");
  let lastAttr = null;
  let lastColor = null;
  let lastTimeRange = 'all';
  let lastSidebarGroup = null;
  let lastSidebarAttr = null;
  let lastSidebarEvent = null;

  // 兼容多种日期格式的解析器（含兜底）
  const dateParsers = [
    d3.timeParse("%Y-%m-%d"),
    d3.timeParse("%Y/%m/%d"),
    d3.timeParse("%Y-%m-%d %H:%M:%S"),
    d3.timeParse("%Y/%m/%d %H:%M:%S"),
    d3.timeParse("%Y-%m-%dT%H:%M:%S"),
  ];
  function parseDateSafe(value) {
    if (!value) return null;
    for (const parser of dateParsers) {
      const d = parser(value);
      if (d && !isNaN(d.getTime())) return d;
    }
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  // 动画时长（可调）
  const transitionDuration = 750;

  // 聚焦
  let focus = null;

  // 根组
  const rootGroup = svg.append("g");
  const nodePositionCache = new Map();

  // 颜色获取函数
 function getColor(input, colorby){
  // 如果没有传入有效对象，给个安全默认
  if (!input) {
    return continuousFields.includes(colorby) ? colorCont(0) : color("未知");
  }

  let row = input;
  if (row && typeof row === "object") {
    // 如果 row.data.data 存在，优先使用它（最深的原始事件）
    if (row.data && row.data.data && typeof row.data.data === "object") {
      row = row.data.data;
    // 否则如果 row.data 看起来像事件对象（包含关键字段），也用它
    } else if (row.data && typeof row.data === "object" &&
               (("领域" in row.data) || ("风险值" in row.data) || ("date" in row.data) || ("情绪" in row.data))) {
      row = row.data;
    }
  }

  // 如果 row 仍然 falsy 或不是对象，退回默认色
  if (!row || typeof row !== "object") {
    return continuousFields.includes(colorby) ? colorCont(0) : color("未知");
  }

  //
  if (continuousFields.includes(colorby)){
    if (colorby === "情绪"){
      const v = ( (row["情绪"] != null ? row["情绪"] : 0) + 1) / 2 * 100;
      return colorCont(v);
    } else if (colorby === "风险值"){
      const rv = (row["风险值"] != null ? +row["风险值"] : 0);
      const t = Math.max(0, Math.min(1, rv / 100));
      // 这里继续保持：高风险更接近红，低风险更接近蓝
      return colorRiskScale(1 - t);
    }
     else if (colorby === "时间"){
      const d = parseDateSafe(row.date);
      if (!d || isNaN(d.getTime())) return colorCont(0);
      const today = new Date();
      const start = d3.timeMonth.offset(today, -6);
      const t = Math.max(0, Math.min(1, (d - start) / (today - start)));
      return colorCont(t * 100);
    }
  } else {
    // 离散字段（比如 领域/地区）
    return color(row[colorby] || "未知");
  }
 }


  // 文本辅助
  function getSentimentDescription(value) {
    if (value === 1) return "极度正面";
    if (value > 0) return "正面";
    if (value === 0) return "中立";
    if (value === -1) return "极度负面";
    if (value < 0) return "负面";
    return "未知";
  }
  function getRiskDescription(value) {
    if (value >= 80) return "极高风险";
    if (value >= 50) return "高风险";
    if (value >= 20) return "中风险";
    if (value >= 0) return "低风险";
    return "未知";
  }


  // 隐藏侧边栏
  function hideSidebar(opts = {}) {
    const preserveSelection = !!(opts && opts.preserveSelection);
    try {
      //在函数内部实时查找元素
      const sidebar = d3.select("#event-sidebar");
      const eventList = d3.select("#event-list");
      const eventDetails = d3.select("#event-details");
      const detailsSummary = d3.select("#details-summary");
      const detailsPosts = d3.select("#details-posts");

      if (!sidebar.node()) return; // 安全退出

      sidebar.classed("open", false); // 移除 open 类 (CSS 负责动画)

      // 匹配CSS动画 (0.3s)
      setTimeout(() => {
        if(eventList.node()) eventList.html("");
        if(eventDetails.node()) eventDetails.style("visibility", "hidden");
        if(detailsSummary.node()) detailsSummary.text("点击右侧列表中的事件以查看详情。");
        if(detailsPosts.node()) detailsPosts.text("(热门贴文内容)");
      }, 300);
      if (!preserveSelection) {
        lastSidebarGroup = null;
        lastSidebarAttr = null;
        lastSidebarEvent = null;
      }
    } catch (err) {
       console.error("Error hiding sidebar:", err);
    }
  }
  window.hideSidebar = hideSidebar; // 暴露给全局

  // 填充侧边栏并显示
  function populateAndShowSidebar(nodeData) {
    // 【修复】在函数内部实时查找元素
    const sidebar = d3.select("#event-sidebar");
    const sidebarTitle = d3.select("#sidebar-title");
    const eventList = d3.select("#event-list");
    const closeBtn = d3.select("#close-sidebar-btn");
    const eventDetails = d3.select("#event-details");
    const detailsSummary = d3.select("#details-summary");
    const detailsPosts = d3.select("#details-posts");

    if (!sidebar.node()) {
      console.error("FATAL ERROR: Can't find #event-sidebar. Did you move it inside #central-vis in your HTML?");
      return;
    }

    lastSidebarAttr = document.getElementById("central-attribute").value;
    lastSidebarGroup = (nodeData && nodeData.data && nodeData.data.name) ? nodeData.data.name : null;
    lastSidebarEvent = null;

    // 【修复】JS只负责添加 'open' 类，CSS 负责所有样式和动画
    sidebar.classed("open", true);

    // 绑定关闭按钮
    if (closeBtn.node()) {
      closeBtn.on("click", hideSidebar);
    }


    sidebarTitle.text(`"${nodeData.data.name}" 事件列表`);
    eventList.html("");

    let leaves = [];
    try {
      leaves = nodeData.leaves ? nodeData.leaves() : (nodeData.children || []).flatMap(c => c.leaves ? c.leaves() : (c.children||[]));
    } catch (err) {
      leaves = [];
    }

    if (leaves && leaves.length > 0) {
      const sortedChildren = leaves.sort((a,b) => d3.ascending(a.data.name, b.data.name));
      sortedChildren.forEach(childNode => {
        const event = childNode.data.data || childNode.data;
        const li = eventList.append("li")
          .datum(event)
          .text(`${event.name} (风险: ${event["风险值"] || 'N/A'})`)
          .attr("title", `点击查看 ${event.name} 详情`)
          .style("padding", "8px 12px")
          .style("list-style", "none")
          .style("border-bottom", "1px solid rgba(255,255,255,0.03)")
          .style("cursor", "pointer")
          .on("click", () => {
            eventList.selectAll("li").style("background", "none").style("color", "#a8b3cf");
            li.style("background", "#162034").style("color", "#e8e8e8");
            lastSidebarEvent = event.name;
            showEventDetails(event);
          });
      });
    } else {
      eventList.append("li").text("没有可显示的事件。").style("padding","8px 12px");
    }

    detailsSummary.text("点击右侧列表中的事件以查看详情。");
    detailsPosts.text("(热门贴文内容)");
    eventDetails.style("visibility", "hidden"); // 确保详情在列表出现时先隐藏
  }

  // 显示事件详情（左下小卡）
  function showEventDetails(event) {
    try {
      // 在函数内部实时查找元素
      const eventDetails = d3.select("#event-details");
      const detailsSummary = d3.select("#details-summary");
      const detailsPosts = d3.select("#details-posts");

      if (!eventDetails.node()) return; // 安全退出

      lastSidebarEvent = (event && event.name) ? event.name : lastSidebarEvent;

      // CSS 负责所有定位和样式，JS 只负责设为可见
      eventDetails.style("visibility", "visible");

      const riskVal = event["风险值"] || 0;
      const sentVal = event["情绪"] || 0;

      const riskLabel = event.__levelLabel || event.risk_level_label || getRiskDescription(riskVal);
      const initialSummary = `${event.name}: ${event.领域 || '未知'} · ${event.地区 || '未知'} · 情绪 ${getSentimentDescription(sentVal)} (${(sentVal||0).toFixed(2)}) · 风险 ${riskLabel} (${(riskVal||0).toFixed(1)})`;
      detailsSummary.text(initialSummary);
      detailsPosts.text("正在加载贴文...");

      const detailUrl = `/api/risk/event?name=${encodeURIComponent(event.name)}&date=${encodeURIComponent(event.date || event.last_seen_at || '')}`;
      fetch(detailUrl)
        .then(response => (response.ok ? response.json() : Promise.reject(response.statusText)))
        .then(detail => {
      const summarySource = detail?.summary || detail?.title || event.name;
      const structured = [
        `${summarySource}`,
        detail?.llm?.health_major ? `类别：${detail.llm.health_major}` : null,
        detail?.llm?.health_minor ? `子类：${detail.llm.health_minor}` : null,
        `风险：${riskLabel} (${(riskVal||0).toFixed(1)})`
      ].filter(Boolean).join(" · ");
      detailsSummary.text(truncateSummary(structured, 120));
          renderPosts(detail.posts || []);
        })
        .catch(() => {
          detailsPosts.text("无法加载贴文数据");
        });
    } catch (err) {
      console.error("Error showing event details:", err);
    }
  }

  function formatPostTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value || '';
    }
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const dd = String(date.getDate()).padStart(2, '0');
    const hh = String(date.getHours()).padStart(2, '0');
    const min = String(date.getMinutes()).padStart(2, '0');
    return `${mm}-${dd} ${hh}:${min}`;
  }

  function renderPosts(posts) {
    const container = d3.select("#details-posts");
    if (!container.node()) return;
    if (!Array.isArray(posts) || !posts.length) {
      container.text("暂无贴文");
      return;
    }
    const html = posts.slice(0, 4).map(post => {
      const text = post.content_text || post.content || post.summary || post.text || '';
      const time = formatPostTime(post.published_at || post.publishedAt || post.published_at);
      return `
        <div style="margin-bottom:8px;">
          <div style="font-weight:600;border-bottom:1px solid rgba(255,255,255,0.08);padding-bottom:4px;margin-bottom:4px;">
            ${post.account_name || '未知账号'} ${time ? `· ${time}` : ''}
          </div>
          <div style="font-size:12px;line-height:1.4;">${text}</div>
        </div>
      `;
    }).join("");
    container.html(html);
  }

  function truncateSummary(text, maxLen = 120) {
    if (!text) return "";
    if (text.length <= maxLen) return text;
    const trimmed = text.slice(0, maxLen).trim();
    const lastSpace = trimmed.lastIndexOf(" ");
    return (lastSpace > 0 ? trimmed.slice(0, lastSpace) : trimmed) + "…";
  }



  // ---------------- renderBubbles ----------------
 function renderBubbles(data, attribute, colorby){
  // ==== 1. 预清理：平滑移除与 beeswarm/risk/x-axis 相关的残留元素 ====
  rootGroup.selectAll(".beeswarm-leaf, .region-dot-risk, .risk-grid, .risk-labels, .x-axis")
    .interrupt()
    .transition().duration(transitionDuration/1.5)
    .style("opacity", 0)
    .attr("r", 0)
    .remove();
  // 清理旧动画，保留元素供数据绑定复用，避免切换时跳闪
  rootGroup.selectAll("g.pack-parent, g.pack-leaf-g").interrupt();

  // 隐藏 tooltip
  tooltip.style("visibility","hidden").html("");

  // 重置 rootGroup 的 transform (防止从蜂群图切换时坐标错乱)
  rootGroup.interrupt()
    .transition().duration(transitionDuration)
    .attr("transform", "translate(0,0)")
    .style("opacity", 1);

  // ==== 2. 构造 pack 数据 ====
  const groups = d3.group(data, d => d[attribute] || "未知");
  const heatExtent = d3.extent(data, d => Number(d["热度"]) || 0);
  const resolvedHeatExtent = heatExtent.every(v => Number.isFinite(v)) ? heatExtent : [0, 1];
  const areaScale = d3.scaleLinear().domain(resolvedHeatExtent).range([25, 256]).clamp(true);

  const packData = {
    name: "root",
    children: Array.from(groups, ([k, arr]) => ({
      name: k,
      children: arr.map(e => ({ name: e.name, data: e, value: areaScale(Number(e["热度"]) || 0) }))
    }))
  };

  const packRoot = d3.pack()
    .size([currentWidth, currentHeight])
    .padding(10)(
      d3.hierarchy(packData)
        .sum(d => d.value)
        .sort((a, b) => b.value - a.value)
    );

  // ==== 3. 保持 focus 在新树上有效 ====
  if (focus && focus.depth > 0) {
    const newFocusNode = packRoot.descendants().find(d => d.data.name === focus.data.name);
    focus = newFocusNode || packRoot;
  } else {
    focus = packRoot;
  }

  // ==== 4. 分离父节点（groups）和叶子（事件）数组 ====
  const parentNodes = packRoot.descendants().filter(d => d.children && d.parent);
  const leafNodes   = packRoot.descendants().filter(d => !d.children);

  // ==== 5. 父节点绑定（外层大圆 & label path & label） ====
  const parentSel = rootGroup.selectAll("g.pack-parent").data(parentNodes, d => d.data.name);

  // exit 父节点：平滑缩小并移除
  parentSel.exit().interrupt()
    .transition().duration(transitionDuration)
    .attr("transform", `translate(${currentWidth/2},${currentHeight/2})scale(0)`)
    .style("opacity", 0)
    .remove();

  // enter 父节点（从 focus 中心入场）
  const parentEnter = parentSel.enter().append("g").attr("class","pack-parent")
    .attr("transform", d => `translate(${focus.x},${focus.y})`)
    .style("opacity", 0);

  // 父圆（外圈）
  parentEnter.append("circle")
    .attr("class","pack-parent-circle")
    .attr("r", 0)
    .attr("fill", "none")
    .attr("stroke", "#0f1e39")
    .attr("stroke-width", 1)
    .style("pointer-events", "none");

  // label path 占位（真实 id 与 d 属性在 merge 时写入）
  parentEnter.append("path")
    .attr("class","label-path")
    .attr("fill", "none")
    .attr("stroke", "none")
    .style("pointer-events", "none");

  // label text（textPath 会在 merge 时创建）
  parentEnter.append("text")
    .attr("class","group-label")
    .style("fill", "#1a03e7")
    .style("font-weight", "bold")
    .style("pointer-events", "auto")
    .style("cursor", "pointer");

  // merge 父节点并更新位置、半径、label path 与 textPath
  const parentMerged = parentEnter.merge(parentSel);

  parentMerged.transition().duration(transitionDuration)
    .attr("transform", d => `translate(${d.x},${d.y})`)
    .style("opacity", 1);

  parentMerged.select("circle.pack-parent-circle")
    .transition().duration(transitionDuration)
    .attr("r", d => d.r || 0);

  // 为每个父节点生成唯一 id 并把 textPath 绑定到该 path（避免冲突）
  parentMerged.each(function(d,i){
    const g = d3.select(this);
    // 生成较安全的 id：把名字里的空白与特殊字符简单替换
    const safeName = (d.data.name || "group").replace(/[^\w\-]/g, "_");
    const pid = `path-for-${safeName}_${i}`;
    const labelPadding = 5;
    const radius = (d.r || 0) + labelPadding;
    g.select("path.label-path").attr("id", pid).attr("d", `M ${-radius},0 A ${radius},${radius} 0 0,1 ${radius},0`);

    // 清理已有 textPath 并重建（确保文字更新）
    const txt = g.select("text.group-label");
    txt.selectAll("textPath").remove();
    txt.append("textPath")
      // 使用 xlink:href 保持向后兼容（部分环境需要）
      .attr("xlink:href", `#${pid}`)
      .attr("startOffset", "50%")
      .attr("text-anchor", "middle")
      .attr("dominant-baseline", "middle")
      .text(d.data.name)
      .on("click", (evt, d) => { // 确保 (evt, d) 都在
        evt.stopPropagation();
        // console.log("1. CLICKED on text:", d.data.name);
        populateAndShowSidebar(d);
      });
  });

  // 将父 group 提升到顶层，保证 label 清晰
  rootGroup.selectAll("g.pack-parent").raise();

  // ==== 6. 叶子节点绑定（事件小圆） ====
  const leafSel = rootGroup.selectAll("g.pack-leaf-g").data(leafNodes, d => d.data.name);

  // exit 叶子：收缩并移除
  leafSel.exit().interrupt()
    .transition().duration(transitionDuration/1.5)
    .attr("transform", `translate(${currentWidth/2},${currentHeight/2})scale(0)`)
    .style("opacity", 0)
    .remove();

  // enter 叶子：从 focus 中心入场
  const leafEnter = leafSel.enter().append("g").attr("class","pack-leaf-g")
    .attr("transform", d => {
      const prev = nodePositionCache.get(d.data.name);
      return prev ? `translate(${prev.x},${prev.y})` : `translate(${focus.x},${focus.y})`;
    })
    // 所有新进入的事件气泡（包含“曾出现又被过滤掉后再出现”的点）
    // 都从透明 + 半径 0 开始，保证每次切换动画一致
    .style("opacity", 0);

  leafEnter.append("circle")
    .attr("class","pack-leaf")
    .attr("r", 0)
    .attr("fill", d => getColor(d.data.data, colorby))
    .style("cursor", "pointer");

  // merge 叶子并设置位置、半径、颜色
  const leafMerged = leafEnter.merge(leafSel);

  leafMerged.transition().duration(transitionDuration)
    .attr("transform", d => `translate(${d.x},${d.y})`)
    .style("opacity", 1);

  leafMerged.select("circle.pack-leaf")
    .transition().duration(transitionDuration)
    .attr("r", d => Math.max(3, d.r || 3))
    .attr("fill", d => getColor(d.data.data, colorby))
    .attr("stroke", "none");

  // 叶子交互：tooltip、hover、click（click 放大所属父组或显示详情）
  leafMerged.on("mouseover", function(evt, d){
    d3.select(this).select("circle").attr("stroke","#6886ff").attr("stroke-width",2);
    const e = d.data.data;
    const riskVal = e["风险值"] || 0;
    const sentVal = e["情绪"] || 0;
    tooltip.style("visibility","visible").html(
      `<div style="background:#0b1726;border:1px solid rgba(255,255,255,0.03);padding:8px;border-radius:6px;color:#e8f1ff;">
         <div style="font-weight:bold;margin-bottom:6px;">${e.name}</div>
         <div>领域: ${e["领域"] || '未知'}</div>
         <div>热度: ${e["热度"] || 'N/A'}</div>
         <div>时间: ${e.date || 'N/A'}</div>
         <div>地区: ${e["地区"] || '未知'}</div>
         <div>情绪: ${getSentimentDescription(sentVal)} (${(sentVal||0).toFixed(2)})</div>
         <div>风险: ${getRiskDescription(riskVal)} (${(riskVal||0).toFixed(1)})</div>
       </div>`
    );
  }).on("mousemove", function(evt){
    tooltip.style("left",(evt.pageX+12)+"px").style("top",(evt.pageY+12)+"px");
  }).on("mouseout", function(evt, d){
    d3.select(this).select("circle").attr("stroke","none").attr("stroke-width",0);
    tooltip.style("visibility","hidden");
  }).on("click", function(evt, d){
    evt.stopPropagation();
    const parent = d.parent;
    if (parent && parent.depth > 0) {
      zoomTo(parent); // (注意: 您的代码中没有 zoomTo 函数，这里假设它存在或将来会添加)
    } else {
      showEventDetails(d.data.data);
    }
  });

  // 确保父群组（带 label 的）在上层
  leafNodes.forEach(d => {
    nodePositionCache.set(d.data.name, {x: d.x, y: d.y});
  });

  rootGroup.selectAll("g.pack-parent").raise();
  return packRoot;
}


  // ---------------- renderBeeswarm ----------------
  function renderBeeswarm(data, attribute, colorby, isExpandingTimeRange){
    hideSidebar({ preserveSelection: true });
    isExpandingTimeRange = !!isExpandingTimeRange;

    // 当前时间筛选（周/月/三月），后续用于轴范围与力导向参数
    const activeBtn = d3.select(".time-filter button.active").node();
    const timeRange = activeBtn ? activeBtn.dataset.range : 'week';

    // === 关键清理：平滑移除 pack (气泡图) 相关元素，避免残留 ===
    // 1) 淡出并移除 pack 内的 leaf / parent circle
    rootGroup.selectAll(".pack-leaf, .pack-parent-circle")
      .interrupt()
      .transition().duration(transitionDuration/1.5)
      .attr("r", d => nodePositionCache.has(d.id) ? 5 : 0)
      .style("opacity", d => nodePositionCache.has(d.id) ? 1 : 0)
      .remove();

    // 2) 淡出并移除 pack 的 g 容器
    rootGroup.selectAll("g.pack-parent, g.pack-leaf-g")
      .interrupt()
      .transition().duration(transitionDuration/1.5)
      .style("opacity", 0)
      .attr("transform", `translate(${currentWidth/2},${currentHeight/2})scale(0)`)
      .remove();

    // 3) 淡出并移除 pack 的 label
    rootGroup.selectAll(".group-label")
      .interrupt()
      .transition().duration(transitionDuration/1.5)
      .style("opacity", 0)
      .remove();

    // 4) 淡出并移除可能的风险带（如果存在）
    rootGroup.selectAll(".risk-grid, .region-dot-risk, .risk-labels, .vertical-grid-line, .risk-number-labels") // <-- 【已更新】添加 .vertical-grid-line 清理
      .interrupt()
      .transition().duration(transitionDuration/1.5)
      .style("opacity", 0)
      .remove();

    // 5) 淡出旧 beeswarm 点（若切换到 beeswarm 再切回会重复），保持平滑
    rootGroup.selectAll(".beeswarm-leaf")
      .interrupt()
      .transition().duration(transitionDuration/1.5)
      .attr("r", 0)
      .style("opacity", 0)
      .remove();

    // 清理 x-axis（svg 或 rootGroup）
    svg.selectAll(".x-axis").interrupt().transition().duration(transitionDuration/2).style("opacity",0).remove();
    rootGroup.selectAll(".x-axis").interrupt().transition().duration(transitionDuration/2).style("opacity",0).remove();

    focus = null;

    const padding = {left:100, right:20, top:20, bottom:40}; // left 增加以显示风险级别文字
    const innerW = currentWidth - padding.left - padding.right;
    const innerH = currentHeight - padding.top - padding.bottom;
    //坐标轴位置（y轴方向）
    const axisBaseline = Math.max(padding.top, currentHeight - 100);

    const heatExtent = d3.extent(data, d => Number(d["热度"]) || 0);
    const resolvedHeatExtent = heatExtent.every(v => Number.isFinite(v)) ? heatExtent : [0, 1];
    const radiusScale = d3.scaleSqrt().domain(resolvedHeatExtent).range([6, 13]).clamp(true);

    //存储x轴定义域
    let domain;

    if (attribute === "情绪") {
      const rawValues = data
        .map(d => {
          const val = Number(d["情绪"]);
          return Number.isFinite(val) ? val : null;
        })
        .filter(v => v !== null);
      if (rawValues.length) {
        let minVal = d3.min(rawValues);
        let maxVal = d3.max(rawValues);
        if (minVal === maxVal) {
          const centerPad = 0.1;
          minVal -= centerPad;
          maxVal += centerPad;
        }
        const span = maxVal - minVal;
        const padding = Math.max(span * 0.1, 0.05);
        domain = [
          Math.max(-1, minVal - padding),
          Math.min(1, maxVal + padding),
        ];
        if (domain[0] === domain[1]) {
          domain = [-1, 1];
        }
      } else {
        domain = [-1, 1];
      }
    } else if (attribute === "风险值") {
      //在attribute === "风险值"分支里被覆盖
      domain = [0, 100];
    } else if (attribute === "时间") {
      //时间轴范围
      const today = new Date();
      let startDate;
      if (timeRange === 'week') {
        startDate = d3.timeDay.offset(today, -7);
      } else if (timeRange === 'month') {
        startDate = d3.timeMonth.offset(today, -1);
      } else if (timeRange === 'three_months') {
        startDate = d3.timeMonth.offset(today, -3);
      } else {
        const dates = data.map(d => parseDateSafe(d.date)).filter(d => d && !isNaN(d.getTime()));
        startDate = dates.length ? d3.min(dates) : d3.timeMonth.offset(today, -3);
      }
      domain = [startDate, today];
    }

    const x = d3.scaleLinear().range([0, innerW]);
    const xTime = d3.scaleTime().range([0, innerW]);

    if (attribute === "时间"){
      xTime.domain(domain);
      x.domain(xTime.domain());
      data.forEach(d => d.__x = xTime(parseDateSafe(d.date)));
    } else {
      x.domain(domain);
      data.forEach(d => d.__x = x(d[attribute]));
    }

    if (attribute === "风险值") {
      //三个风险等级对象。每个对象有 key (用于在数据中查找) 和 label (用于显示)。
      const riskLevels = [
        { key: "risk_high", label: "高风险" },
        { key: "risk_mid", label: "中风险" },
        { key: "risk_low", label: "低风险" },
      ];

      const gridGroup = rootGroup.append("g").attr("class","risk-grid").style("opacity",0);
      const rowSpacing = Math.max(innerH / (riskLevels.length + 1), 60);
      // 用于存储每个风险等级（risk_high等）对应的Y轴坐标
      const bandPositions = {};
      riskLevels.forEach((level, idx) => {
        const y = padding.top + rowSpacing * (idx + 1);
        //记录该风险等级的Y坐标
        bandPositions[level.key] = y;
        gridGroup.append("line")
          .attr("x1", padding.left).attr("x2", padding.left + innerW)
          .attr("y1", y).attr("y2", y)
          .attr("stroke", "rgba(249, 242, 242, 0.03)")
          .attr("stroke-width", 1)
          .attr("stroke-dasharray", "4 6");
      });
      // 风险等级标签
      const labelGroup = gridGroup.append("g").attr("class","risk-labels").style("opacity",0);
      labelGroup.selectAll("text")
        .data(riskLevels)
        .enter()
        .append("text")
        .attr("x", padding.left - 50)
        .attr("y", level => bandPositions[level.key])
        .attr("dominant-baseline","middle")
        .style("fill","#030303ff")
        .style("font-size","20px")
        .text(level => level.label);

      gridGroup.transition().duration(transitionDuration).style("opacity",1);
      labelGroup.transition().duration(transitionDuration).style("opacity",1);

      const range = timeRange || 'week';
      const today = new Date();
      let startDate;
      if (range === 'week') {
        startDate = d3.timeDay.offset(today, -7);
      } else if (range === 'month') {
        startDate = d3.timeMonth.offset(today, -1);
      } else if (range === 'three_months') {
        startDate = d3.timeMonth.offset(today, -3);
      } else {
        const dates = data.map(d => parseDateSafe(d.date)).filter(d => d && !isNaN(d.getTime()));
        startDate = dates.length ? d3.min(dates) : d3.timeMonth.offset(today, -3);
      }
      const axisOffset = padding.left + 50;
      const axisRight = padding.left + innerW - 8;
      const xTime = d3.scaleTime()
        .domain([startDate, today])
        .range([axisOffset, axisRight])
        .clamp(true);

      const axisGroup = svg.selectAll(".x-axis").data([null]);
      axisGroup.exit().interrupt().transition().duration(transitionDuration / 2).style("opacity", 0).remove();
      const axisEnter = axisGroup.enter().append("g")
        .attr("class", "x-axis")
        .style("opacity", 0);
      const axis = d3.axisBottom(xTime).tickFormat(d3.timeFormat("%m-%d"));
      if (timeRange === 'week') {
        axis.ticks(d3.timeDay.every(1));
      } else if (timeRange === 'month') {
        axis.ticks(d3.timeWeek.every(1));
      } else if (timeRange === 'three_months') {
        axis.ticks(d3.timeMonth.every(1));
      }
      const axisNode = axisEnter.merge(axisGroup);
      axisNode
        .attr("transform", `translate(0,${axisBaseline})`)
        .transition().duration(transitionDuration)
        .style("opacity", 1)
        .call(axis)
        .select(".domain").style("display", "none");
      axisNode.selectAll("text").style("fill", "#000");
      axisNode.selectAll("line").attr("stroke", "#000");

      //绘制垂直网格线
      const verticalHolder = gridGroup.append("g").attr("class","risk-verticals");
      const verticalTicks = axis.scale().ticks(8);
      verticalTicks.forEach(value => {
        const xPos = xTime(value);
        verticalHolder.append("line")
          .attr("class", "vertical-grid-line")
          .attr("x1", xPos).attr("x2", xPos)
          .attr("y1", padding.top)
          //垂直网格线与x轴刻度相连
          .attr("y2", axisBaseline)
          .attr("stroke", "rgba(16, 4, 4, 0.08)")
          .attr("stroke-width", 1)
          .style("opacity", 0)
          .transition().duration(transitionDuration).style("opacity", 1);
      });

      //分配节点位置
      const resolveLevel = entry => {
        for (const level of riskLevels) {
          if ((Number(entry[level.key]) || 0) > 0) {
            return level;
          }
        }
        const fallbackKey = typeof entry.risk_level === "string" ? `risk_${entry.risk_level}` : null;
        if (fallbackKey) {
          const fallback = riskLevels.find(level => level.key === fallbackKey);
          if (fallback) return fallback;
        }
        const score = Number(entry["风险值"] ?? entry.risk ?? 0);
        if (score >= 50) return riskLevels[0];
        if (score >= 20) return riskLevels[1];
        return riskLevels[2];
      };

      data.forEach(d => {
        //调用resolveLevel函数确定风险等级
        const level = resolveLevel(d);
        //确定用于Tooltip显示的风险值
        const valueCandidates = [
          Number(d[level.key]) || 0,
          Number(d["风险值"] ?? 0),
          Number(d.risk ?? 0),
        ];
        const rv = Math.max(...valueCandidates);
        d.__riskValue = rv;
        d.__levelKey = level.key;
        d.__levelLabel = level.label;
        d.__bandY = bandPositions[level.key];
        const date = parseDateSafe(d.date);
        const safeDate = date && !isNaN(date.getTime()) ? date : startDate;
        d.__x = xTime(safeDate);
      });
      //计算最终位置（D3 力导向模拟）
      const nodes = data.map(d => {
        const heatVal = Number(d["热度"]) || 0;
        return {
          x: d.__x,
          y: d.__bandY,
          data: d,
          id: d.name,
          radius: radiusScale(heatVal),
        };
      });
      //初始化力导向模拟
      const sim = d3.forceSimulation(nodes)
      .force("x", d3.forceX(d => d.x).strength(0.7))
      .force("y", d3.forceY(d => d.y).strength(0.6))
      .force("collide", d3.forceCollide(d => (d.radius || 7) + 3))
        .stop();
      for (let i=0;i<300;i++) sim.tick();
      const minX = xTime(startDate);
      const maxX = xTime(today);
      nodes.forEach(node => {
        node.x = Math.min(maxX, Math.max(minX, node.x));
      });

      // 绘制风险值蜂群点
      const startX = currentWidth / 2;
      const startY = currentHeight / 2;
      const circles = rootGroup.selectAll(".region-dot-risk")
        .data(nodes, d => d.id);

      circles.exit()
        .interrupt()
        .transition().duration(transitionDuration/1.5)
        .attr("r", 0)
        .style("opacity", 0)
        .remove();

      const circlesEnter = circles.enter().append("circle")
        .attr("class", "region-dot-risk beeswarm-leaf")
        .attr("cx", d => {
          const prev = nodePositionCache.get(d.id);
          return prev ? prev.x : startX;
        })
        .attr("cy", d => {
          const prev = nodePositionCache.get(d.id);
          return prev ? prev.y : startY;
        })
        // 所有新进入的风险点都从透明 + 半径 0 开始，保证每次切换动画一致
        .attr("r", 0)
        .style("opacity", 0)
        .attr("stroke", "#1f2835")
        .attr("stroke-width", 1)
        //绑定鼠标悬停事件
        .on("mouseover", function(evt, d){
          //（stroke）添加高亮描边
          const hoverR = (d.radius || 0) + 3;
          d3.select(this).transition().duration(120).attr("r", hoverR).attr("stroke","#7fb3ff").attr("stroke-width",2);
          const e = d.data;
          const riskVal = (e.__riskValue ?? e["风险值"]) || 0;
          const sentVal = e["情绪"] || 0;
          const sentimentColor = (sentVal >= 0.2) ? "#66ffb3" : (sentVal <= -0.2 ? "#ff8a8a" : "#ffd966");
          const riskColor = e.__levelKey === "risk_high" ? "#ff9b6b" : (e.__levelKey === "risk_mid" ? "#ffd36b" : "#77d2a8");
          const riskLabel = e.__levelLabel || e.risk_level_label || getRiskDescription(riskVal);
          const cardHtml = `
            <div style="min-width:240px;background:linear-gradient(180deg, rgba(10,26,58,0.95), rgba(6,18,37,0.95));border:1px solid rgba(255,255,255,0.06);box-shadow:0 8px 24px rgba(0,0,0,0.6);border-radius:6px;padding:12px;color:#e8f1ff;font-family:Arial;">
              <div style="font-weight:700;font-size:14px;margin-bottom:8px;color:#bfe1ff;">事件详情</div>
              <div style="font-size:13px;font-weight:600;margin-bottom:6px;color:#ffffff;">${e.name}</div>
              <div style="font-size:12px;line-height:1.6;color:#cbe6ff;">
                <div>领域: <span style="color:#ade1ff">${e.领域 || '未知'}</span></div>
                <div>热度: <span style="color:#a8e0ff">${e['热度'] || 'N/A'}</span></div>
                <div>发生时间: <span style="color:#a8e0ff">${e.date || 'N/A'}</span></div>
                <div>地区: <span style="color:#a8e0ff">${e.地区 || '未知'}</span></div>
                <div>情绪: <span style="color:${sentimentColor}">${getSentimentDescription(sentVal)} (${(sentVal||0).toFixed(2)})</span></div>
                <div>风险: <span style="color:${riskColor}">${riskLabel} (${(riskVal||0).toFixed(1)})</span></div>
              </div>
            </div>
          `;
          tooltip.html(cardHtml).style("visibility","visible");
        })
        //绑定鼠标移动事件
        .on("mousemove", function(evt){
          const cardWidth = 260;
          let left = evt.pageX + 12;
          if (left + cardWidth > window.innerWidth - 8) {
            left = evt.pageX - cardWidth - 12;
          }
          tooltip.style("left", left + "px").style("top",(evt.pageY - 20) + "px");
        })
        //绑定鼠标移出事件：恢复圆点的半径 (5) 和描边 (none)，并隐藏 tooltip
        .on("mouseout", function(evt, d){
          d3.select(this).transition().duration(120).attr("r", d.radius).attr("stroke","none").attr("stroke-width",1);
          tooltip.style("visibility","hidden").html("");
        });

      circles.merge(circlesEnter)
        .transition().duration(transitionDuration)
        // 将圆点平滑移动到它们在力导向模拟中计算出的最终 x (时间) 和 y (泳道) 坐标
        .attr("cx", d => d.x)
        .attr("cy", d => d.y)
        .attr("r", d => d.radius)
        .style("opacity", 1)
        .attr("fill", d => getColor(d.data, colorby));

      //遍历所有节点，将它们的名字(id)和最终的 x, y 坐标存入
      nodes.forEach(node => {
        nodePositionCache.set(node.id, {x: node.x, y: node.y});
      });

      rootGroup.interrupt().transition().duration(transitionDuration).attr("transform", `translate(0,0)`);
      //立即退出 renderBeeswarm 函数 防止函数继续执行后续为“情绪”和“时间”视图准备的代码
      return;
    }

    // 非风险值常规蜂群（时间/情绪）
    //映射成图表所需的结构化数据
    const nodes = data.map(d => {
        const heatVal = Number(d["热度"]) || 0;
        return {
        x: d.__x + padding.left,
        y: currentHeight/2,
        data: d,
        id: d.name,
        radius: radiusScale(heatVal)
      };
    });
    // 力导向模拟
    const collidePadding = 4; // 额外的碰撞缓冲，避免重叠
    const sim = d3.forceSimulation(nodes)
      .force("x", d3.forceX(d => d.x).strength(1))
      .force("y", d3.forceY(currentHeight/2))
      .force("collide", d3.forceCollide(d => (d.radius || 6) + collidePadding))
      .stop();
    const tickCount = timeRange === 'week' ? 320 : 240;
    for (let i=0;i<tickCount;i++) sim.tick();

    //使用D3的 Enter/Update/Exit 模式在 SVG的根部（svg，而不是 rootGroup）绘制X轴
    let gAxis = svg.selectAll(".x-axis").data([null]);
    // exit 轴：淡出并移除
    gAxis.exit()
         .interrupt()
         .transition()
         .duration(transitionDuration)
         .style("opacity", 0)
         .remove();
    //如果X轴不存在，则创建一个新的 <g> 元素
    const gAxisEnter = gAxis.enter().append("g")
      .attr("class", "x-axis")
      .style("opacity", 0);
    let bottomAxis;

    if (attribute === "时间") {
      bottomAxis = d3.axisBottom(xTime);
      if (timeRange === 'week') {
        bottomAxis
          .ticks(d3.timeDay.every(1))
          .tickFormat(d3.timeFormat("%m-%d"));
      } else if (timeRange === 'month') {
        bottomAxis
          .ticks(d3.timeWeek.every(1))
          .tickFormat(d3.timeFormat("%m-%d"));
      } else if (timeRange === 'three_months') {
        bottomAxis
          .ticks(d3.timeMonth.every(1))
          .tickFormat(d3.timeFormat("%m-%d"));
      } else {
        bottomAxis
          .ticks(d3.timeMonth.every(1))
          .tickFormat(d3.timeFormat("%Y-%m-%d"));
      }
    } else {
      bottomAxis = d3.axisBottom(x);
    }

    gAxisEnter.merge(gAxis)
      .attr("transform", `translate(${padding.left},${axisBaseline})`)
      .transition().duration(transitionDuration)
      .style("opacity", 1)
      //实际绘制轴的刻度和标签
      .call(bottomAxis);

    rootGroup.interrupt()
             .transition()
             .duration(transitionDuration)
             .attr("transform", `translate(${padding.left},${padding.top})`);

    // circles: enter from center -> transition to nodes (平滑)
    const circles = rootGroup.selectAll("circle.beeswarm-leaf")
      // 将 nodes 数据绑定到这些圆点上
      .data(nodes, d => d.id);

    circles.exit()
           .interrupt()
           .transition()
           .duration(transitionDuration/1.5)
           .attr("r", 0)
           .style("opacity", 0)
           .remove();
    //为所有新数据创建 <circle> 元素
    const circlesEnter = circles.enter().append("circle")
      .attr("class", "beeswarm-leaf")
      // 所有新进入的点（包括曾出现后被过滤掉再出现的点）
      // 一律从透明 + 半径 0 开始，保证多次切换动画一致
      .attr("r", 0)
      .style("opacity", 0)
      .attr("fill", d => getColor(d.data, colorby))
      .attr("cx", d => {
        const prev = nodePositionCache.get(d.id);
        const fallback = currentWidth/2 - padding.left; // �ӻ��������볡
        return prev ? prev.x - padding.left : fallback;
      })
      .attr("cy", d => {
        const prev = nodePositionCache.get(d.id);
        const fallback = currentHeight/2 - padding.top;
        return prev ? prev.y - padding.top : fallback;
      })
      //绑定鼠标悬停事件
      .on("mouseover", function(evt, d){
        const hoverR = (d.radius || 0) + 3;
        d3.select(this).transition().duration(150).attr("r", hoverR).attr("stroke","#6886ff").attr("stroke-width",2);
        const e = d.data;
        const riskVal = e["风险值"] || 0;
        const sentVal = e["情绪"] || 0;
        tooltip.style("visibility","visible").html(
            `<div style="background:#0b1726;border:1px solid rgba(255,255,255,0.03);padding:8px;border-radius:6px;color:#e8f1ff;">
              <div style="font-weight:bold;margin-bottom:6px;">${e.name}</div>
               <div>领域: ${e["领域"] || '未知'}</div>
               <div>热度: ${e["热度"] || 'N/A'}</div>
               <div>时间: ${e.date || 'N/A'}</div>
               <div>地区: ${e["地区"] || '未知'}</div>
               <div>情绪: ${getSentimentDescription(sentVal)} (${(sentVal||0).toFixed(2)})</div>
               <div>风险: ${getRiskDescription(riskVal)} (${(riskVal||0).toFixed(1)})</div>
            </div>`
        );
      })
      //绑定鼠标移动事件
      .on("mousemove", function(evt){
        tooltip.style("left",(evt.pageX+8)+"px").style("top",(evt.pageY+8)+"px");
      })
      //绑定鼠标移出事件：恢复圆点的半径 (5) 和描边 (none)，并隐藏 tooltip
      .on("mouseout", function(evt, d){
        d3.select(this).transition().duration(150).attr("r", d.radius).attr("stroke","none");
        tooltip.style("visibility","hidden").html("");
      });

    const circlesCombined = circlesEnter.merge(circles);

    circlesCombined.transition().duration(transitionDuration)
      .attr("r", d => d.radius)
      .style("opacity", 1)
      .attr("fill", d => getColor(d.data, colorby))
      .attr("cx", d => d.x - padding.left)
      .attr("cy", d => d.y - padding.top);
    //循环遍历所有节点，将它们的最终 x 和 y 坐标（node.x, node.y）存入
    nodes.forEach(node => {
      nodePositionCache.set(node.id, {x: node.x, y: node.y});
    });
  }

  // 主渲染入口：接收数据、读取用户选项、过滤数据、调用具体渲染函数
  function updateNodeColors(colorby){
    // 蜂群图与风险蜂群的颜色更新（节点 datum.data 即原始行数据）
    rootGroup.selectAll(".beeswarm-leaf, .region-dot-risk")
      .transition().duration(400)
      .style("fill", d => getColor(d.data, colorby));

    // 气泡图叶子节点的颜色更新（节点 datum.data.data 为原始行数据）
    rootGroup.selectAll("circle.pack-leaf")
      .transition().duration(400)
      .style("fill", d => {
        const row = d && d.data && d.data.data ? d.data.data : (d && d.data ? d.data : d);
        return getColor(row, colorby);
      });
    }

  window.renderCentral = function(data){
    // 读取用户选择的属性与配色
    const attr = document.getElementById("central-attribute").value;
    const colorby = document.getElementById("central-colorby").value;

    const sidebarWasOpen = d3.select("#event-sidebar").classed("open");
    const prevSidebarGroup = lastSidebarGroup;
    const prevSidebarAttr = lastSidebarAttr;
    const prevSidebarEvent = lastSidebarEvent;

    let processedData = data;
    const activeBtn = d3.select(".time-filter button.active").node();
    const timeRange = activeBtn ? activeBtn.dataset.range : 'all';

    // 判断当前时间范围是否比上一次更“长”（一周 -> 一月 -> 三个月 -> 全部）
    const rangeRankMap = { week: 0, month: 1, three_months: 2, all: 3 };
    const prevRange = lastTimeRange || 'all';
    const prevRank = rangeRankMap[prevRange] != null ? rangeRankMap[prevRange] : 3;
    const currRank = rangeRankMap[timeRange] != null ? rangeRankMap[timeRange] : 3;
    const isExpandingTimeRange = currRank > prevRank;

    if (timeRange !== 'all') {
      //时间
      const today = new Date();
      let startDate;
      if (timeRange === 'week') {
        startDate = d3.timeDay.offset(today, -7);
      } else if (timeRange === 'month') {
        startDate = d3.timeMonth.offset(today, -1);
      } else if (timeRange === 'three_months') {
        startDate = d3.timeMonth.offset(today, -3);
      }

      processedData = data.filter(d => {
        const date = parseDateSafe(d.date);
        return date && date >= startDate && date <= today;
      });
    }

    const colorOnlyUpdate = lastAttr === attr && lastColor !== colorby;
    if (colorOnlyUpdate) {
      updateNodeColors(colorby);
      lastColor = colorby;
      return;
    }


    rootGroup.style("visibility", "visible");

    // 当切换回离散属性时，提前平滑移除风险相关视图，防止残留（保守处理）
    if (discreteFields.includes(attr)) {
      rootGroup.selectAll(".risk-grid, .region-dot-risk, .risk-labels")
        .interrupt()
        .transition().duration(transitionDuration/2).style("opacity",0).remove();
      rootGroup.selectAll(".x-axis").interrupt().transition().duration(transitionDuration/2).style("opacity",0).remove();
      svg.selectAll(".x-axis").interrupt().transition().duration(transitionDuration/2).style("opacity",0).remove();
      tooltip.style("visibility","hidden").html("");
    }

    let packRoot = null;
    if (discreteFields.includes(attr)) {
      packRoot = renderBubbles(processedData, attr, colorby);
    } else {
      renderBeeswarm(processedData, attr, colorby, isExpandingTimeRange);
    }
    lastAttr = attr;
    lastColor = colorby;
    lastTimeRange = timeRange;

    if (packRoot && sidebarWasOpen && prevSidebarGroup && prevSidebarAttr === attr) {
      const targetNode = packRoot.descendants().find(d => d.depth === 1 && d.data && d.data.name === prevSidebarGroup);
      if (targetNode) {
        populateAndShowSidebar(targetNode);
        if (prevSidebarEvent) {
          const listSel = d3.select("#event-list");
          const items = listSel.selectAll("li");
          const match = items.filter(d => d && d.name === prevSidebarEvent);
          if (!match.empty()) {
            items.style("background", "none").style("color", "#a8b3cf");
            match.style("background", "#162034").style("color", "#e8e8e8");
            match.each(d => { if (d) showEventDetails(d); });
            lastSidebarEvent = prevSidebarEvent;
          }
        }
      }
    }
  }

  // 事件绑定
  d3.select("#central-attribute").on("change", () => {
    focus = null;
    attrSel.property("disabled", false);
    if (window.__centralData__) {
      window.renderCentral(window.__centralData__);
    }
  });
  d3.select("#central-colorby").on("change", () => {
    const attr = document.getElementById("central-attribute").value;
    if (discreteFields.includes(attr)) {
      focus = null;
    }
    if (window.__centralData__) {
      window.renderCentral(window.__centralData__);
    }
  });
  d3.selectAll(".time-filter button").on("click", function() {
    d3.selectAll(".time-filter button").classed("active", false);
    d3.select(this).classed("active", true);
    const attr = document.getElementById("central-attribute").value;
    if (discreteFields.includes(attr)) {
      focus = null;
    }
    if (window.__centralData__) {
      window.renderCentral(window.__centralData__);
    }
  });


  // 响应式：在窗口大小改变时被调用的函数
  function handleResize() {
    const newWidth = container.node().getBoundingClientRect().width;
    const newHeight = container.node().getBoundingClientRect().height;

    if (newWidth <= 0 || newHeight <= 0) return;

    currentWidth = newWidth;
    currentHeight = newHeight;

    svg.attr("width", currentWidth).attr("height", currentHeight)
      .attr("viewBox", `0 0 ${currentWidth} ${currentHeight}`);

    if (window.__centralData__) {
      hideSidebar(); // 窗口缩放时隐藏侧边栏
      if (discreteFields.includes(d3.select("#central-attribute").property("value"))) {
        focus = null;
      }
      window.renderCentral(window.__centralData__);
    }
  }
   
  /*  HEALTH BUTTON SETUP  */
 (function setupHealthButton() {
  // 建立绑定函数（放在内部以使用当前作用域的 svg/rootGroup/tooltip 等）
  function bindHealthBtn() {
    const btn = d3.select("#health-hot-btn");
    const panel = d3.select("#health-panel");

    if (!btn.node()) {
      console.warn("[health] 按钮 #health-hot-btn 未找到 —— 请确认 HTML 中存在该 id");
      return;
    }
    if (!panel.node()) {
      console.warn("[health] 面板 #health-panel 未找到 —— 请确认 HTML 中存在该 id");
      // 仍然绑定按钮，但绑定动作会先检查 panel 是否存在
    }

    btn.on("click", function() {
      // 如果面板不存在就提示并 return
      if (!panel.node()) {
        console.warn("[health] 点击触发但未找到 #health-panel；无法显示健康视图。");
        return;
      }

      //检查健康面板是否为隐藏状态
      const isHidden = panel.style("display") === "none" || panel.style("display") === "";
      if (isHidden) {
        // 显示健康面板，隐藏主 svg（保留数据）
        panel.style("display", "block");
        try { 
              d3.select("#central-vis svg")
                .style("visibility", "hidden"); 
            } catch(e) {}
        // 取出健康事件（简单筛选：领域或标题含“健/医/疫苗/医院”等）
        //window.__centralData__：在 client.js 中定义和填充的全局变量
        const all = window.__centralData__ || [];
        const healthEvents = all.filter(d => {
          const domain = (d["领域"]||"").toString();
          const name = (d.name||"").toString();
          return /健康|医|疫苗|医院|防疫|接种|疫情|疫/.test(domain + " " + name);
        });
        // 优先调用你如果已实现的 renderHealthTimeline，否则用控制台提示
        if (typeof renderHealthTimeline === "function") {
          renderHealthTimeline(healthEvents);
        } else {
          console.warn("[health] renderHealthTimeline 未定义；请确保已实现时间轴渲染函数。");
        }
      } else {
        // 隐藏健康面板并恢复主视图
        panel.style("display", "none");
        try { 
              d3.select("#central-vis svg")
                .style("visibility", "visible"); 
            } catch(e) {}
        if (typeof window.renderCentral === "function") {
          window.renderCentral(window.__centralData__ || []);
        }
      }
    });

    console.info("[health] 按钮绑定已完成，点击将切换健康面板（id: #health-hot-btn / #health-panel）。");
  }

  // 如果 DOM 尚未准备好：在 DOMContentLoaded 时绑定；否则立即绑定
  if (!d3.select("#health-hot-btn").node() || !d3.select("#health-panel").node()) {
    // 在 DOMContentLoaded 时再尝试绑定（兼顾 script 加载顺序）
    document.addEventListener("DOMContentLoaded", bindHealthBtn);
    // 另外尝试短延时绑定以覆盖多数情况
    setTimeout(bindHealthBtn, 300);
  } else {
    bindHealthBtn();
  }
})();


  window.addEventListener("resize", handleResize);
  handleResize();

})();
