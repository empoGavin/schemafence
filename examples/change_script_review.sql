-- A release script of the kind a colleague opens a review request for.
-- Nothing here is syntactically wrong. That is the point.

ALTER TABLE shop.orders ADD COLUMN channel TEXT;
ALTER TABLE shop.orders ADD COLUMN channel_code TEXT NOT NULL;
COMMENT ON COLUMN shop.orders.channel IS '下单渠道 标准:CH-002';

CREATE INDEX CONCURRENTLY idx_orders_channel ON shop.orders (channel);

ALTER TABLE shop.orders
  ADD CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES shop.users (id);

ALTER TABLE shop.orders_2023 DROP COLUMN legacy_flag;

DROP TABLE shop.orders_bak;

CREATE TABLE shop.channel_map (
  code      TEXT    NOT NULL,
  label     TEXT,
  is_active BOOLEAN NOT NULL DEFAULT TRUE
);

COMMENT ON TABLE shop.channel_map IS '渠道映射';
