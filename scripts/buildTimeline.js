#!/usr/bin/env node
"use strict";

const fs = require("fs/promises");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const SOURCE_DIR = path.resolve(ROOT, "data", "health", "events", "2025-11-18");
const OUTPUT_DIR = path.resolve(ROOT, "public", "data");
const OUTPUT_FILE = path.join(OUTPUT_DIR, "timeline-health.json");

function normalizeDate(rawDate, fallbackTs) {
  if (rawDate) {
    const parsed = new Date(rawDate);
    if (!Number.isNaN(parsed.valueOf())) {
      return parsed.toISOString().slice(0, 10);
    }
  }
  if (typeof fallbackTs === "number") {
    const parsed = new Date(fallbackTs * 1000);
    if (!Number.isNaN(parsed.valueOf())) {
      return parsed.toISOString().slice(0, 10);
    }
  }
  return null;
}

function coerceNumber(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

async function loadEventFiles() {
  const events = [];
  const entries = await fs.readdir(SOURCE_DIR, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".json")) {
      continue;
    }
    const fullPath = path.join(SOURCE_DIR, entry.name);
    const raw = await fs.readFile(fullPath, "utf8");
    try {
      const data = JSON.parse(raw);
      const date = normalizeDate(data.date, data.start_ts);
      if (!date) {
        console.warn("[timeline] %s 缺失 date 字段，已跳过。", entry.name);
        continue;
      }
      const category = data.category || data.health_minor || "未分类";
      events.push({
        event_id: data.event_id || path.basename(entry.name, ".json"),
        source_file: path.relative(ROOT, fullPath).replace(/\\/g, "/"),
        date,
        title: data.title || data.summary || path.basename(entry.name, ".json"),
        category,
        health_minor: data.health_minor || null,
        sentiment: coerceNumber(data.sentiment),
        heat_peak: coerceNumber(data.heat_peak),
        region: data.region || "未知",
        summary: data.summary || "",
        tags: Array.isArray(data.tags) ? data.tags : [],
      });
    } catch (err) {
      console.warn("[timeline] 解析 %s 失败：%s", entry.name, err.message);
    }
  }
  return events;
}

function buildTimeline(events) {
  const grouped = new Map();
  const categoryTotals = new Map();
  for (const event of events) {
    let bucket = grouped.get(event.date);
    if (!bucket) {
      bucket = {
        date: event.date,
        count: 0,
        events: [],
        categories: new Map(),
      };
      grouped.set(event.date, bucket);
    }
    bucket.count += 1;
    bucket.events.push(event);
    const cat = event.category || "未分类";
    bucket.categories.set(cat, (bucket.categories.get(cat) || 0) + 1);
    categoryTotals.set(cat, (categoryTotals.get(cat) || 0) + 1);
  }

  const timeline = Array.from(grouped.values())
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((bucket) => ({
      date: bucket.date,
      count: bucket.count,
      events: bucket.events
        .slice()
        .sort((a, b) => (b.heat_peak || 0) - (a.heat_peak || 0)),
      categories: Array.from(bucket.categories.entries())
        .map(([name, count]) => ({ name, count }))
        .sort((a, b) => {
          if (b.count === a.count) {
            return a.name.localeCompare(b.name, "zh-Hans-CN");
          }
          return b.count - a.count;
        }),
    }));

  const totals = Array.from(categoryTotals.entries())
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => {
      if (b.count === a.count) {
        return a.name.localeCompare(b.name, "zh-Hans-CN");
      }
      return b.count - a.count;
    });

  return { timeline, categoryTotals: totals };
}

async function main() {
  const events = await loadEventFiles();
  if (!events.length) {
    throw new Error("未在 " + SOURCE_DIR + " 中找到任何事件数据。");
  }
  const { timeline, categoryTotals } = buildTimeline(events);
  await fs.mkdir(OUTPUT_DIR, { recursive: true });
  const payload = {
    generated_at: new Date().toISOString(),
    source_dir: path.relative(ROOT, SOURCE_DIR).replace(/\\/g, "/"),
    total_events: events.length,
    total_days: timeline.length,
    category_totals: categoryTotals,
    timeline,
  };
  await fs.writeFile(OUTPUT_FILE, JSON.stringify(payload, null, 2), "utf8");
  console.info(
    "[timeline] 已生成 %s（%d 条事件，%d 天）。",
    path.relative(ROOT, OUTPUT_FILE).replace(/\\/g, "/"),
    payload.total_events,
    payload.total_days,
  );
}

if (require.main === module) {
  main().catch((err) => {
    console.error("[timeline] 构建失败：", err);
    process.exitCode = 1;
  });
}
