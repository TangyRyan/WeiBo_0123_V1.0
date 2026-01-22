(function () {
  const emotionsOrder = [
    "\u559c\u60a6",
    "\u4fe1\u4ef0",
    "\u6050\u60e7",
    "\u60ca\u559c",
    "\u4f24\u5fc3",
    "\u538c\u6076",
    "\u6c14\u6124",
    "\u671f\u76fc"
  ];
  const colors = ["#f9eb8d", "#92bc8d", "#b6c695", "#cae5f2", "#9dc5fa", "#b3abe1", "#f1a685", "#f5c888"];

  const mockEmotionData = [
    {
      account: "\u516c\u5171\u536b\u751f\u60c5\u7eea",
      emotions: {
        "\u559c\u60a6": 0.62,
        "\u4fe1\u4ef0": 0.78,
        "\u6050\u60e7": 0.24,
        "\u60ca\u559c": 0.45,
        "\u4f24\u5fc3": 0.35,
        "\u538c\u6076": 0.18,
        "\u6c14\u6124": 0.22,
        "\u671f\u76fc": 0.70
      }
    },
    {
      account: "\u6162\u75c7\u5173\u6000\u60c5\u7eea",
      emotions: {
        "\u559c\u60a6": 0.52,
        "\u4fe1\u4ef0": 0.65,
        "\u6050\u60e7": 0.18,
        "\u60ca\u559c": 0.33,
        "\u4f24\u5fc3": 0.28,
        "\u538c\u6076": 0.16,
        "\u6c14\u6124": 0.14,
        "\u671f\u76fc": 0.55
      }
    }
  ];

  function normalizeEmotions(source) {
    if (Array.isArray(source) && source.length) {
      return emotionsOrder.map((emotion, idx) => {
        const item = source.find(entry => (entry.name || entry.emotion) === emotion);
        const value = item ? Number(item.value ?? item.score ?? 0) : 0;
        return { emotion, value: clamp01(value), color: colors[idx] };
      });
    }
    if (source && typeof source === 'object') {
      if (Array.isArray(source.emotions)) {
        return normalizeEmotions(source.emotions);
      }
      if (source.account) {
        const match = mockEmotionData.find(d => d.account === source.account);
        if (match) {
          return normalizeEmotions(match.emotions);
        }
      }
      return emotionsOrder.map((emotion, idx) => ({
        emotion,
        value: clamp01(Number(source[emotion] ?? 0)),
        color: colors[idx]
      }));
    }
    if (typeof source === 'string') {
      const match = mockEmotionData.find(d => d.account === source);
      if (match) return normalizeEmotions(match.emotions);
    }
    return emotionsOrder.map((emotion, idx) => ({ emotion, value: clamp01(0.3), color: colors[idx] }));
  }

  function clamp01(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return 0;
    return Math.max(0, Math.min(1, num));
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

  function showEmpty(container, text) {
    container.selectAll('*').remove();
    container.append('div')
      .attr('class', 'health-empty')
      .text(text || '暂无情绪画像');
  }

  function plutchik(id, data = mockEmotionData[0], options = {}) {
    const container = d3.select(id);
    if (container.empty()) {
      console.warn(`[plutchik] container ${id} not found`);
      return;
    }

    const dataset = normalizeEmotions(data);
    if (!dataset.length) {
      showEmpty(container, '暂无情绪画像');
      return;
    }

    container.selectAll('*').remove();
    container.classed('health-detail-chart', true);

    const { width, height } = getContainerSize(container);
    const margin = { top: 20, right: 20, bottom: 20, left: 20 };
    const innerRadius = 20;
    const outerRadius = Math.min(width, height) / 2 - Math.max(margin.left, margin.top);

    const svg = container.append('svg')
      .attr('width', '100%')
      .attr('height', '100%')
      .attr('viewBox', `0 0 ${width} ${height}`);

    const g = svg.append('g')
      .attr('transform', `translate(${width / 2}, ${height / 2})`);

    const x = d3.scaleBand()
      .domain(emotionsOrder)
      .range([0, 2 * Math.PI])
      .align(0);

    const y = d3.scaleLinear()
      .domain([0, 1])
      .range([innerRadius, outerRadius]);

    const ticks = [0.25, 0.5, 0.75, 1.0];
    g.selectAll('.grid-circle')
      .data(ticks)
      .enter()
      .append('circle')
      .attr('class', 'grid-circle')
      .attr('r', d => y(d))
      .attr('fill', 'none')
      .attr('stroke', '#dfe3eb')
      .attr('stroke-dasharray', '4,4');

    const petalPath = 'M 0,0 C -40,-60 -40,-140 0,-180 C 40,-140 40,-60 0,0';

    g.selectAll('.arc')
      .data(dataset)
      .enter().append('path')
      .attr('class', 'arc')
      .attr('d', petalPath)
      .attr('fill', d => d.color)
      .attr('fill-opacity', 0.8)
      .attr('transform', function (d) {
        const angle = (x(d.emotion) + x.bandwidth() / 2) * 180 / Math.PI - 180;
        return `rotate(${angle}) scale(${y(d.value) / 180})`;
      });

    g.selectAll('.arc-outline')
      .data(dataset)
      .enter().append('path')
      .attr('class', 'arc-outline')
      .attr('d', petalPath)
      .attr('fill', 'none')
      .attr('stroke', d => d3.color(d.color).darker(0.5))
      .attr('stroke-width', 1)
      .attr('transform', function (d) {
        const angle = (x(d.emotion) + x.bandwidth() / 2) * 180 / Math.PI - 180;
        return `rotate(${angle}) scale(${y(d.value) / 180})`;
      });

    g.selectAll('.axis')
      .data(dataset)
      .enter().append('line')
      .attr('class', 'axis')
      .attr('x1', 0)
      .attr('y1', 0)
      .attr('x2', d => {
        const angle = x(d.emotion) + x.bandwidth() / 2 - Math.PI;
        return Math.sin(angle) * outerRadius;
      })
      .attr('y2', d => {
        const angle = x(d.emotion) + x.bandwidth() / 2 - Math.PI;
        return -Math.cos(angle) * outerRadius;
      })
      .attr('stroke', '#adb5bd')
      .attr('stroke-width', 1);

    const label = g.append('g')
      .selectAll('g')
      .data(dataset)
      .enter().append('g')
      .attr('text-anchor', 'middle')
      .attr('transform', d => {
        const angle = (x(d.emotion) + x.bandwidth() / 2) * 180 / Math.PI - 180;
        return `rotate(${angle}) translate(${outerRadius + 28},0)`;
      });

    label.append('text')
      .attr('transform', d => {
        const rawAngle = x(d.emotion) + x.bandwidth() / 2 - Math.PI;
        return (rawAngle + Math.PI / 2) % (2 * Math.PI) < Math.PI ?
          'rotate(90)translate(0,-14)' : 'rotate(-90)translate(0,20)';
      })
      .attr('class', 'principal')
      .style('font-size', 14)
      .text(d => d.emotion);

    const percentFormat = d3.format('.0%');
    label.append('text')
      .attr('transform', d => {
        const rawAngle = x(d.emotion) + x.bandwidth() / 2 - Math.PI;
        return (rawAngle + Math.PI / 2) % (2 * Math.PI) < Math.PI ?
          'rotate(90)translate(0,6)' : 'rotate(-90)translate(0,-4)';
      })
      .attr('class', 'principal_value')
      .style('font-size', 12)
      .style('font-weight', 'bold')
      .style('fill', d => d.color)
      .text(d => percentFormat(clamp01(d.value)));
  }

  window.plutchik = plutchik;
  window.renderHealthEmotion = function(emotions) {
    plutchik('#health-emotions-chart', emotions);
  };
})();