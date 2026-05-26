-- Update ollama_model_map to use the models ACTUALLY available in this user's
-- Ollama Cloud subscription. Discovered 2026-05-26 via designer-session HTTP
-- 404 — the `:cloud` suffix and `qwen3.6/qwen3-coder-next/gemma4` names from
-- the public library page don't match what's served against the user's API key.
--
-- Available models (per agent's reachability probe):
--   glm-5.1, kimi-k2.6, deepseek-v4-pro, gpt-oss:20b, minimax-m2.7,
--   deepseek-v3.2, cogito-2.1:671b, ministral-3:{3b,8b,14b}
--
-- Strategy:
--   coder    : minimax-m2.7 (coding+agentic) → deepseek-v4-pro (reasoning,
--              1M ctx) → deepseek-v3.2 (proven older)
--   designer : deepseek-v4-pro (3 reasoning modes for design judgement) →
--              glm-5.1 (agentic engineering) → kimi-k2.6 (general fallback)
--   reviewer : gpt-oss:20b (small/cheap, sufficient for review) →
--              glm-5.1 → ministral-3:14b (smallest acceptable)
--   planner  : glm-5.1 (agentic+coding) → kimi-k2.6 → deepseek-v3.2
--   others   : keep kimi-k2.6 → glm-4.7 (← wait, glm-4.7 not available — use
--              glm-5.1 instead). Let's also update those.
--
-- Hot-reloaded — system_config is read fresh per cycle, no orchestrator
-- restart needed.
BEGIN;

SELECT 'before' AS phase, ollama_model_map FROM system_config;

UPDATE system_config
SET ollama_model_map = jsonb_build_object(
    'coder',       jsonb_build_array('minimax-m2.7',     'deepseek-v4-pro', 'deepseek-v3.2'),
    'designer',    jsonb_build_array('deepseek-v4-pro',  'glm-5.1',         'kimi-k2.6'),
    'reviewer',    jsonb_build_array('gpt-oss:20b',      'glm-5.1',         'ministral-3:14b'),
    'planner',     jsonb_build_array('glm-5.1',          'kimi-k2.6',       'deepseek-v3.2'),
    'architect',   jsonb_build_array('deepseek-v4-pro',  'glm-5.1',         'kimi-k2.6'),
    'devops',      jsonb_build_array('minimax-m2.7',     'glm-5.1',         'kimi-k2.6'),
    'analytics',   jsonb_build_array('deepseek-v4-pro',  'glm-5.1',         'kimi-k2.6'),
    'documenter',  jsonb_build_array('glm-5.1',          'kimi-k2.6',       'ministral-3:14b'),
    'refactorer',  jsonb_build_array('minimax-m2.7',     'deepseek-v4-pro', 'deepseek-v3.2'),
    'recommender', jsonb_build_array('glm-5.1',          'kimi-k2.6')
  ),
  -- Legacy text columns: keep aligned with the primary chain.
  designer_model = 'deepseek-v4-pro, glm-5.1, kimi-k2.6',
  coder_model    = 'minimax-m2.7, deepseek-v4-pro, deepseek-v3.2';

SELECT 'after' AS phase, ollama_model_map FROM system_config;

COMMIT;
