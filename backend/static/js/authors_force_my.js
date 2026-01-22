(function () {
  const mockCollaborations = {
    "\u516c\u5171\u536b\u751f\u534f\u540c\u7f51\u7edc": {
      nodes: [
        { id: "\u56fd\u5bb6\u75be\u63a7", size: 32, group: "\u4e3b\u7ba1\u90e8\u95e8" },
        { id: "\u534e\u897f\u533b\u9662", size: 28, group: "\u4e09\u7532\u533b\u9662" },
        { id: "\u4ec1\u6cfd\u533b\u9662", size: 24, group: "\u4e09\u7532\u533b\u9662" },
        { id: "\u6df1\u5733\u75be\u63a7", size: 18, group: "\u5730\u65b9\u75be\u63a7" },
        { id: "\u5b81\u590f\u57fa\u5c42\u7ad9", size: 14, group: "\u57fa\u5c42" },
        { id: "\u4e92\u8054\u7f51\u533b\u9662", size: 20, group: "\u5e73\u53f0" },
        { id: "\u793e\u533a\u517b\u62a4\u4e2d\u5fc3", size: 16, group: "\u57fa\u5c42" },
        { id: "\u5317\u5927\u516c\u5171\u536b\u751f", size: 19, group: "\u9ad8\u6821" }
      ],
      links: [
        { source: "\u56fd\u5bb6\u75be\u63a7", target: "\u534e\u897f\u533b\u9662", weight: 9 },
        { source: "\u56fd\u5bb6\u75be\u63a7", target: "\u4ec1\u6cfd\u533b\u9662", weight: 8 },
        { source: "\u56fd\u5bb6\u75be\u63a7", target: "\u5317\u5927\u516c\u5171\u536b\u751f", weight: 7 },
        { source: "\u534e\u897f\u533b\u9662", target: "\u4ec1\u6cfd\u533b\u9662", weight: 6 },
        { source: "\u534e\u897f\u533b\u9662", target: "\u6df1\u5733\u75be\u63a7", weight: 5 },
        { source: "\u4ec1\u6cfd\u533b\u9662", target: "\u6df1\u5733\u75be\u63a7", weight: 4 },
        { source: "\u6df1\u5733\u75be\u63a7", target: "\u5b81\u590f\u57fa\u5c42\u7ad9", weight: 4 },
        { source: "\u4e92\u8054\u7f51\u533b\u9662", target: "\u793e\u533a\u517b\u62a4\u4e2d\u5fc3", weight: 5 },
        { source: "\u4e92\u8054\u7f51\u533b\u9662", target: "\u5b81\u590f\u57fa\u5c42\u7ad9", weight: 3 },
        { source: "\u4e92\u8054\u7f51\u533b\u9662", target: "\u534e\u897f\u533b\u9662", weight: 4 },
        { source: "\u5317\u5927\u516c\u5171\u536b\u751f", target: "\u793e\u533a\u517b\u62a4\u4e2d\u5fc3", weight: 3 }
      ]
    }
  };

  function normalizeGraph(data) {
    if (!data) {
      return cloneData(mockCollaborations["\u516c\u5171\u536b\u751f\u534f\u540c\u7f51\u7edc"]);
    }
    if (typeof data === 'string' && mockCollaborations[data]) {
      return cloneData(mockCollaborations[data]);
    }
    if (data.nodes || data.links || data.edges) {
      const nodes = (data.nodes || data.tags || []).map((node, idx) => {
        const id = node.id || node.label || node.name || `tag-${idx}`;
        const label = node.label || node.name || id;
        const rawSize = node.size ?? node.weight ?? node.count ?? 1;
        const group = node.group || (node.community != null ? `community-${node.community}` : 'default');
        return {
          id,
          label,
          size: Math.max(4, Number(rawSize) || 1),
          group,
        };
      });
      const links = (data.links || data.edges || []).map(link => {
        const source = link.source || link.from || (link[0] ?? null);
        const target = link.target || link.to || (link[1] ?? null);
        if (!source || !target) return null;
        const weight = Number(link.weight ?? link.value ?? 1) || 1;
        return { source, target, weight };
      }).filter(Boolean);
      if (!nodes.length) {
        return cloneData(mockCollaborations["\u516c\u5171\u536b\u751f\u534f\u540c\u7f51\u7edc"]);
      }
      return { nodes, links };
    }
    return cloneData(mockCollaborations["\u516c\u5171\u536b\u751f\u534f\u540c\u7f51\u7edc"]);
  }

  function cloneData(dataset) {
    return {
      nodes: dataset.nodes.map(d => ({ ...d })),
      links: dataset.links.map(d => ({ ...d }))
    };
  }

  function showEmpty(container, text) {
    container.selectAll('*').remove();
    container.append('div')
      .attr('class', 'health-empty')
      .text(text || '暂无标签网络');
  }

  function getContainerSize(container) {
    const bounds = container.node().getBoundingClientRect();
    const parentBounds = container.node().parentNode
      ? container.node().parentNode.getBoundingClientRect()
      : { width: 360, height: 260 };

    const width = Math.max(bounds.width || 0, parentBounds.width || 0, 320);
    const height = Math.max(bounds.height || 0, parentBounds.height || 0, 260);
    return { width, height };
  }

  function renderAuthorsForce(containerSelector, graphData) {
    const container = d3.select(containerSelector);
    if (container.empty()) {
      console.warn(`[authors_force_my] container ${containerSelector} not found`);
      return;
    }

    const dataset = normalizeGraph(graphData);
    if (!dataset.nodes.length) {
      showEmpty(container, '暂无标签网络');
      return;
    }

    const { nodes, links } = dataset;

    container.selectAll('*').remove();
    container.classed('health-detail-chart', true);

    const { width, height } = getContainerSize(container);

    const svg = container.append('svg')
      .attr('width', '100%')
      .attr('height', '100%')
      .attr('viewBox', `0 0 ${width} ${height}`);

    const color = d3.scaleOrdinal()
      .domain(Array.from(new Set(nodes.map(d => d.group))))
      .range(["#4c6ef5", "#9775fa", "#2ec4b6", "#f59f00", "#ef476f", "#06d6a0"]);

    const nodeSizeExtent = d3.extent(nodes, d => d.size);
    const sizeDomain = [
      Math.max(1, nodeSizeExtent[0] ?? 1),
      Math.max((nodeSizeExtent[1] ?? 1), Math.max(1, nodeSizeExtent[0] ?? 1) + 0.1),
    ];
    // Apply a square-root scale so huge values do not dominate the visual size.
    const radiusScale = d3.scaleSqrt()
      .domain(sizeDomain)
      .range([6, 22])
      .clamp(true);

    const weightScale = d3.scaleLinear()
      .domain(d3.extent(links, d => d.weight))
      .range([1.5, 4]);

    const link = svg.append('g')
      .attr('stroke', '#c3cad9')
      .attr('stroke-opacity', 0.7)
      .selectAll('line')
      .data(links)
      .enter()
      .append('line')
      .attr('stroke-width', d => weightScale(d.weight));

    const node = svg.append('g')
      .attr('stroke', '#fff')
      .attr('stroke-width', 1.5)
      .selectAll('circle')
      .data(nodes)
      .enter()
      .append('circle')
      .attr('r', d => radiusScale(d.size))
      .attr('fill', d => color(d.group))
      .call(d3.drag()
        .on('start', dragstarted)
        .on('drag', dragged)
        .on('end', dragended));

    node.append('title')
      .text(d => `${d.label} · ${d.size}`);

    const labels = svg.append('g')
      .attr('font-size', 10)
      .attr('fill', '#3d405b')
      .attr('pointer-events', 'none')
      .selectAll('text')
      .data(nodes)
      .enter()
      .append('text')
      .text(d => d.label);

    const simulation = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(d => d.id).distance(70))
      .force('charge', d3.forceManyBody().strength(-180))
      .force('x', d3.forceX(width / 2).strength(0.08))
      .force('y', d3.forceY(height / 2).strength(0.08))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide(d => radiusScale(d.size) + 6))
      .on('tick', ticked);

    function ticked() {
      link
        .attr('x1', d => d.source.x)
        .attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x)
        .attr('y2', d => d.target.y);

      node
        .attr('cx', d => d.x)
        .attr('cy', d => d.y);

      labels
        .attr('x', d => d.x)
        .attr('y', d => d.y - radiusScale(d.size) - 4);
    }

    function dragstarted(event) {
      if (!event.active) simulation.alphaTarget(0.3).restart();
      event.subject.fx = event.subject.x;
      event.subject.fy = event.subject.y;
    }

    function dragged(event) {
      event.subject.fx = event.x;
      event.subject.fy = event.y;
    }

    function dragended(event) {
      if (!event.active) simulation.alphaTarget(0);
      event.subject.fx = null;
      event.subject.fy = null;
    }
  }

  window.renderAuthorsForce = renderAuthorsForce;
  window.renderHealthTagGraph = function(graph) {
    renderAuthorsForce('#health-tags-graph', graph);
  };
})();
