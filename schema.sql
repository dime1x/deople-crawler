
-- =====================================
-- Deople 数据库结构
-- =====================================

-- 1. 原始域名表
CREATE TABLE IF NOT EXISTS raw_domains (
    domain TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    source TEXT DEFAULT 'certstream',
    filter_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (filter_status IN ('pending', 'accepted', 'rejected')),
    filter_reason TEXT,
    processed_at INTEGER
);

-- 原始域名过滤索引
CREATE INDEX IF NOT EXISTS idx_raw_filter
ON raw_domains(filter_status, first_seen);

-- 2. 有效网站表
CREATE TABLE IF NOT EXISTS websites (
    domain TEXT PRIMARY KEY,

    -- 网站地址
    url TEXT NOT NULL,
    final_url TEXT,

    -- 网页信息
    title TEXT,
    description TEXT,
    keywords TEXT,

    -- HTTP 信息
    http_status INTEGER,
    content_type TEXT,

    -- 抓取状态
    crawl_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (crawl_status IN (
            'pending', 'processing', 'success', 'failed', 'blocked'
        )),

    crawl_attempts INTEGER NOT NULL DEFAULT 0,
    crawl_error TEXT,
    last_crawled_at INTEGER,
    next_crawl_at INTEGER,

    -- 截图信息
    image_url TEXT,
    image_key TEXT,

    image_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (image_status IN (
            'pending', 'processing', 'ready', 'failed', 'skipped'
        )),

    image_attempts INTEGER NOT NULL DEFAULT 0,

    -- 时间信息
    first_seen INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- 待抓取网站索引
CREATE INDEX IF NOT EXISTS idx_websites_crawl
ON websites(crawl_status, next_crawl_at);

-- 待截图网站索引
CREATE INDEX IF NOT EXISTS idx_websites_image
ON websites(image_status, updated_at);

-- 最新网站索引
CREATE INDEX IF NOT EXISTS idx_websites_latest
ON websites(first_seen DESC);

-- 抓取成功网站索引
CREATE INDEX IF NOT EXISTS idx_websites_success
ON websites(crawl_status, first_seen DESC);
