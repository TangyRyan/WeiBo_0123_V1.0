(function () {
  const mockWordCloudData = [
    {
      account: "\u5065\u5eb7\u70ed\u641c\u8bcd\u4e91",
      words: [
        { text: "\u547c\u5438\u79d1\u4e49\u8bca", freq: 36 },
        { text: "\u57fa\u5c42\u968f\u8bbf", freq: 22 },
        { text: "\u65b0\u578b\u75ab\u82d7", freq: 40 },
        { text: "\u91cd\u75c7\u5e8a\u4f4d", freq: 28 },
        { text: "\u4e92\u8054\u7f51\u533b\u9662", freq: 19 },
        { text: "\u6162\u963b\u80ba\u76d1\u6d4b", freq: 25 },
        { text: "\u5352\u4e2d\u7b5b\u67e5", freq: 16 },
        { text: "\u5bb6\u5ead\u533b\u751f", freq: 30 },
        { text: "\u513f\u7ae5\u62a4\u82d7", freq: 18 },
        { text: "\u5fc3\u7406\u758f\u5bfc", freq: 21 }
      ]
    },
    {
      account: "\u6162\u75c7\u62a4\u7406\u58f0\u91cf",
      words: [
        { text: "\u8840\u7cd6\u8fbe\u6807", freq: 26 },
        { text: "\u8840\u538b\u7ba1\u7406", freq: 23 },
        { text: "\u667a\u80fd\u8155\u8868", freq: 17 },
        { text: "\u818f\u98df\u8bc4\u4f30", freq: 14 },
        { text: "\u8fd0\u52a8\u5904\u65b9", freq: 28 },
        { text: "\u7528\u836f\u63d0\u9192", freq: 20 }
      ]
    }
  ];

  const palette = ["#5e60ce", "#48bfe3", "#64dfdf", "#80ffdb", "#ff8fab", "#ffbe0b", "#ff006e", "#8338ec"];

  function normalizeWords(source) {
    if (Array.isArray(source)) {
      return source
        .map(entry => {
          if (!entry) return null;
          const text = entry.text || entry.word || entry.label || entry.name;
          if (!text) return null;
          const raw = entry.weight ?? entry.freq ?? entry.count ?? entry.value ?? 1;
          const weight = Number.isFinite(Number(raw)) ? Number(raw) : 1;
          return { text, weight: Math.max(1, Math.abs(weight)) };
        })
        .filter(Boolean);
    }
    if (source && Array.isArray(source.words)) {
      return normalizeWords(source.words);
    }
    return [];
  }

  function resolveWords(wordsOrAccountName) {
    const normalized = normalizeWords(wordsOrAccountName);
    if (normalized.length) return normalized;
    if (typeof wordsOrAccountName === "string") {
      const match = mockWordCloudData.find(d => d.account === wordsOrAccountName);
      if (match) {
        const fallback = normalizeWords(match.words);
        if (fallback.length) return fallback;
      }
    }
    return normalizeWords(mockWordCloudData[0].words);
  }

  function getContainerSize(container) {
    const bounds = container.node().getBoundingClientRect();
    const parentBounds = container.node().parentNode
      ? container.node().parentNode.getBoundingClientRect()
      : { width: 360, height: 260 };

    const width = Math.max(bounds.width || 0, parentBounds.width || 0, 320);
    const height = Math.max(bounds.height || 0, parentBounds.height || 0, 240);
    return { width, height };
  }

  function showEmpty(container, text) {
    container.selectAll('*').remove();
    container.append('div')
      .attr('class', 'health-empty')
      .text(text || '暂无词云数据');
  }

  function wordCloud(id, wordsOrAccountName, options = {}) {
    const container = d3.select(id);
    if (container.empty()) {
      console.warn(`[wordCloud] container ${id} not found`);
      return 0;
    }

    const words = resolveWords(wordsOrAccountName);
    if (!words.length) {
      showEmpty(container, '暂无词云数据');
      return 0;
    }

    const minWeight = Number(options.minWeight ?? 2) || 2;
    const topN = Math.max(6, Number(options.topN ?? 60));
    const filtered = words
      .filter(item => item.weight >= minWeight)
      .sort((a, b) => b.weight - a.weight)
      .slice(0, topN);

    if (!filtered.length) {
      showEmpty(container, '暂无词云数据');
      return 0;
    }

    container.selectAll('*').remove();
    container.classed('health-detail-chart', true);

    const { width, height } = getContainerSize(container);

    const root = container.append('svg')
      .attr('width', '100%')
      .attr('height', '100%')
      .attr('viewBox', `0 0 ${width} ${height}`);

    const svg = root.append('g')
      .attr('transform', `translate(${width / 2}, ${height / 2})`);

    const extent = d3.extent(filtered, d => d.weight);
    const rawMin = extent[0] ?? 1;
    const rawMax = extent[1] ?? rawMin;
    const safeMin = Math.max(1, rawMin);
    const safeMax = Math.max(rawMax, safeMin + 0.1);

    const sizeScale = d3.scaleSqrt()
      .domain([safeMin, safeMax])
      .range([14, 50])
      .clamp(true);

    const colorScale = d3.scaleQuantize()
      .domain([safeMin, safeMax])
      .range(palette);

    const preparedWords = filtered.map(d => ({
      text: d.text,
      weight: d.weight,
      size: sizeScale(d.weight),
      color: colorScale(d.weight),
    }));

    function draw(wordItems) {
      svg.selectAll('text')
        .data(wordItems)
        .enter()
        .append('text')
        .style('font-size', d => `${d.size}px`)
        .style('font-weight', 600)
        .style('fill', d => d.color)
        .attr('text-anchor', 'middle')
        .attr('transform', d => `translate(${d.x},${d.y}) rotate(${d.rotate || 0})`)
        .text(d => d.text);
    }

    const layoutOptions = {
      padding: options.padding ?? 2,
      font: options.font || 'Source Han Sans, sans-serif',
      rotate: options.rotate ?? (() => 0),
    };

    if (d3.layout && typeof d3.layout.cloud === 'function') {
      d3.layout.cloud()
        .size([width, height])
        .words(preparedWords)
        .padding(layoutOptions.padding)
        .spiral('rectangular')
        .rotate(layoutOptions.rotate)
        .font(layoutOptions.font)
        .fontSize(d => d.size)
        .on('end', draw)
        .start();
    } else {
      const columns = Math.max(1, Math.ceil(Math.sqrt(preparedWords.length)));
      const rowHeight = height / columns;
      const colWidth = width / columns;

      const fallbackWords = preparedWords.map((d, i) => {
        const row = Math.floor(i / columns);
        const col = i % columns;
        return {
          ...d,
          x: -width / 2 + col * colWidth + colWidth / 2,
          y: -height / 2 + row * rowHeight + rowHeight / 2.5,
          rotate: 0
        };
      });

      draw(fallbackWords);
    }

    return filtered.length;
  }

  window.wordCloud = wordCloud;
  window.renderHealthWordCloud = function(words) {
    wordCloud('#health-wordcloud-chart', words);
  };
})();
