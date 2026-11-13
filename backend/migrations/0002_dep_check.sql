-- dagpilot 依赖体检（MySQL 8.0）
-- 运行时由 SQLAlchemy Base.metadata.create_all 建表；此文件是等价 DDL，
-- 供 DBA 走迁移流程 / 预审索引用，字段与 src/models 一一对应。

-- 调度器上报的原始作业运行记录：重试 / 迟到上报 / 旧版本重发都混在这张表
CREATE TABLE IF NOT EXISTS job_run_records (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  pipeline_id    INT NOT NULL,
  job_id         INT NOT NULL,
  biz_date       DATE NOT NULL,
  attempt        INT NOT NULL DEFAULT 1,
  record_version INT NOT NULL DEFAULT 1,
  status         VARCHAR(16) NOT NULL DEFAULT 'success',
  start_at       DATETIME NULL,
  end_at         DATETIME NULL,
  ingested_at    DATETIME NOT NULL,
  source         VARCHAR(40) NOT NULL DEFAULT 'crontab',
  seed_key       VARCHAR(160) NULL,
  created_at     DATETIME NOT NULL,
  UNIQUE KEY uq_runrec_seed_key (seed_key),
  KEY ix_runrec_pipeline (pipeline_id),
  KEY ix_runrec_job (job_id),
  KEY ix_runrec_biz_date (biz_date),
  CONSTRAINT fk_runrec_pipeline FOREIGN KEY (pipeline_id) REFERENCES pipelines (id),
  CONSTRAINT fk_runrec_job FOREIGN KEY (job_id) REFERENCES jobs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 体检批次：草稿 → 整批发布 → 被取代；同指纹重复发起不新增
CREATE TABLE IF NOT EXISTS dep_check_batches (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  batch_no          VARCHAR(40) NOT NULL,
  scope             VARCHAR(16) NOT NULL DEFAULT 'all',
  status            VARCHAR(16) NOT NULL DEFAULT 'draft',
  version           INT NOT NULL DEFAULT 0,
  input_fingerprint VARCHAR(64) NOT NULL,
  item_count        INT NOT NULL DEFAULT 0,
  stats             JSON,
  note              TEXT,
  created_by        VARCHAR(160) NOT NULL DEFAULT 'system',
  created_at        DATETIME NOT NULL,
  published_by      VARCHAR(160) NULL,
  published_at      DATETIME NULL,
  UNIQUE KEY uq_check_batch_no (batch_no),
  KEY ix_check_batch_status (status),
  KEY ix_check_batch_fingerprint (input_fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 逐条体检结论：现有依赖边 + 补边候选，证据与反例以 JSON 快照留存
CREATE TABLE IF NOT EXISTS dep_check_items (
  id               INT AUTO_INCREMENT PRIMARY KEY,
  batch_id         INT NOT NULL,
  item_kind        VARCHAR(16) NOT NULL DEFAULT 'edge',
  pipeline_code    VARCHAR(40) NOT NULL,
  upstream_code    VARCHAR(64) NOT NULL,
  downstream_code  VARCHAR(64) NOT NULL,
  dep_type         VARCHAR(16) NULL,
  lag_minutes      INT NOT NULL DEFAULT 0,
  verdict          VARCHAR(16) NOT NULL,
  sample_days      INT NOT NULL DEFAULT 0,
  support_days     INT NOT NULL DEFAULT 0,
  reason           TEXT,
  evidence         JSON,
  counterexamples  JSON,
  created_at       DATETIME NOT NULL,
  KEY ix_check_item_batch (batch_id),
  KEY ix_check_item_kind (item_kind),
  KEY ix_check_item_pipeline (pipeline_code),
  KEY ix_check_item_verdict (verdict),
  CONSTRAINT fk_check_item_batch FOREIGN KEY (batch_id) REFERENCES dep_check_batches (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
