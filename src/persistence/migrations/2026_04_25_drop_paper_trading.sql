-- ADR-010: Paper Trading 模块删除 + Demo 重定位为组合演练账户
-- 在 db.live 和 db.demo 各执行一次：
--   psql -h <host> -U <user> -d <live_db_name> -f 2026_04_25_drop_paper_trading.sql
--   psql -h <host> -U <user> -d <demo_db_name> -f 2026_04_25_drop_paper_trading.sql
--
-- 执行前提：
--   - paper_trading 模块已从代码中删除（Phase 3）
--   - 已 2026-04-23 全面重置，无 in-flight paper trading 数据
--
-- 不可逆：DROP TABLE 后历史 paper sessions / outcomes 永久丢失。
-- 已上 git 备份的 schema/paper_trading.py / paper_trading_repo.py 可从 history 恢复。

BEGIN;

-- 1. 删除 paper_trading 相关表（CASCADE 处理可能存在的 FK）
DROP TABLE IF EXISTS paper_trade_outcomes CASCADE;
DROP TABLE IF EXISTS paper_trading_sessions CASCADE;

-- 2. 删除 experiments.paper_session_id 字段
ALTER TABLE experiments DROP COLUMN IF EXISTS paper_session_id;

-- 3. 重命名 experiments.paper_sharpe / paper_win_rate → demo_validation_sharpe / demo_validation_win_rate
ALTER TABLE experiments RENAME COLUMN paper_sharpe TO demo_validation_sharpe;
ALTER TABLE experiments RENAME COLUMN paper_win_rate TO demo_validation_win_rate;

-- 4. 更新 status check constraint：移除 'paper_trading'，新增 'demo_validation'
ALTER TABLE experiments DROP CONSTRAINT IF EXISTS experiments_status_check;
ALTER TABLE experiments
  ADD CONSTRAINT experiments_status_check
  CHECK (status IN ('research', 'backtest', 'demo_validation', 'live', 'abandoned'));

-- 5. 现存 status='paper_trading' 数据迁移到 'demo_validation'
UPDATE experiments
   SET status = 'demo_validation', updated_at = NOW()
 WHERE status = 'paper_trading';

COMMIT;

-- 验证（手动执行）：
--   \dt paper_trading_*       -- 应该返回空（表已删除）
--   \d experiments            -- 应该看到 demo_validation_sharpe / demo_validation_win_rate 字段
--                             -- 不应看到 paper_session_id / paper_sharpe / paper_win_rate
--   SELECT DISTINCT status FROM experiments;  -- 不应包含 'paper_trading'
