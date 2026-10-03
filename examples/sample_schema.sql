-- ============================================================
-- schemafence 示例库：一个「看起来正常、其实布满陷阱」的小型 schema
--
-- 用途：README 里的 5 分钟 demo 直接跑它 —— 不需要你自己的任何数据。
-- 这些写法都是从真实生产库里挑出来的，共同点是：
-- 人看着正常，AI 生成的 SQL 也能跑通，但结果是错的，而且不报错。
-- ============================================================

CREATE SCHEMA IF NOT EXISTS shop;
SET search_path TO shop;

-- ---------- 用户 ----------
CREATE TABLE shop.users (
  id          BIGSERIAL PRIMARY KEY,
  email       TEXT,
  phone       TEXT        NOT NULL,
  status      SMALLINT    NOT NULL DEFAULT 1,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE  shop.users        IS '注册用户主表';
COMMENT ON COLUMN shop.users.email  IS '邮箱，老数据可能为空';
COMMENT ON COLUMN shop.users.status IS '1=正常 2=冻结 3=注销';

-- ---------- 订单：一句注释都没有，而且藏了两个陷阱 ----------
CREATE TABLE shop.orders (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT,
  status      SMALLINT         NOT NULL,
  amount      DOUBLE PRECISION NOT NULL,
  currency    CHAR(3)          NOT NULL DEFAULT 'CNY',
  create_time TIMESTAMP        NOT NULL DEFAULT now()
);

-- ---------- 订单归档表：和 orders 只差一个后缀 ----------
CREATE TABLE shop.orders_archive (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT,
  status      SMALLINT         NOT NULL,
  amount      DOUBLE PRECISION NOT NULL,
  currency    CHAR(3)          NOT NULL DEFAULT 'CNY',
  create_time TIMESTAMP        NOT NULL DEFAULT now()
);
COMMENT ON TABLE shop.orders_archive IS '历史订单归档，只存档、不再更新';

-- ---------- 退款 ----------
CREATE TABLE shop.refunds (
  id            BIGSERIAL PRIMARY KEY,
  order_id      BIGINT        NOT NULL,
  refund_amount NUMERIC(12,2) NOT NULL,
  reason        TEXT,
  refunded_at   TIMESTAMP     NOT NULL DEFAULT now()
);
COMMENT ON TABLE  shop.refunds        IS '订单退款记录';
COMMENT ON COLUMN shop.refunds.reason IS '退款原因，可为空';

-- ---------- 黑名单：user_id 可空 —— NOT IN 陷阱的经典现场 ----------
CREATE TABLE shop.blacklist (
  user_id  BIGINT,
  reason   TEXT,
  added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE  shop.blacklist          IS '风控黑名单';
COMMENT ON COLUMN shop.blacklist.user_id  IS '命中用户 ID；按手机号命中时留空';

-- ---------- 用户画像：和 users 很容易混 ----------
CREATE TABLE shop.user_profile (
  user_id  BIGINT PRIMARY KEY,
  nickname TEXT,
  level    SMALLINT NOT NULL DEFAULT 1
);
COMMENT ON TABLE shop.user_profile IS '用户画像扩展信息';

-- ---------- 行为事件：时间用字符串存（历史遗留） ----------
CREATE TABLE shop.user_events (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT,
  event_type  TEXT   NOT NULL,
  occurred_at TEXT   NOT NULL,
  payload     JSONB
);
COMMENT ON TABLE  shop.user_events             IS '用户行为事件';
COMMENT ON COLUMN shop.user_events.occurred_at IS '事件时间；历史遗留，以字符串存储';

-- ============================================================
-- 一点数据，供 live 模式做「基数合理性」检查
-- ============================================================

INSERT INTO shop.users (email, phone, status) VALUES
  ('a@example.com', '13800000001', 1),
  ('b@example.com', '13800000002', 1),
  (NULL,            '13800000003', 2),
  ('d@example.com', '13800000004', 3);

INSERT INTO shop.orders (user_id, status, amount, create_time) VALUES
  (1,    20, 199.00, now() - interval '1 day'),
  (2,    20,  88.50, now() - interval '2 day'),
  (NULL, 20,  45.00, now() - interval '3 day'),
  (1,    30,  12.34, now() - interval '4 day'),
  (3,    20, 999.99, now() - interval '5 day');

INSERT INTO shop.blacklist (user_id, reason) VALUES
  (3,    '恶意退款'),
  (NULL, '手机号命中');

INSERT INTO shop.refunds (order_id, refund_amount, reason) VALUES
  (4, 12.34, '重复下单');
