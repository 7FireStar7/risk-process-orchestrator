-- =====================================================================
-- 003_rule_required_when.sql : условные правила («обязателен, только если ...»)
-- Выполнить один раз в базе orchestrator после 001_schema.sql
-- =====================================================================
ALTER TABLE validation_rule ADD COLUMN IF NOT EXISTS required_when jsonb;
