-- Update ollama_model_map for coder/designer/reviewer with the cloud-first chain
-- recommended on 2026-05-26 (see "Best Ollama text models for ProductFactory"
-- analysis). Merges via `||` so the other 6 personas already in the map
-- (devops, planner, analytics, documenter, refactorer, recommender) survive
-- unchanged.
--
-- system_config has no per-row id; updates apply to the singleton row.
BEGIN;

SELECT 'before' AS phase, ollama_model_map FROM system_config;

UPDATE system_config
SET ollama_model_map = ollama_model_map || jsonb_build_object(
    'coder',    jsonb_build_array('qwen3-coder-next:cloud',  'glm-5.1:cloud',   'qwen3-coder:30b-a3b-q8_0'),
    'designer', jsonb_build_array('deepseek-v4-pro:cloud',   'glm-5.1:cloud',   'qwen3.6:35b-a3b-q4_K_M'),
    'reviewer', jsonb_build_array('gpt-oss:20b-cloud',       'gemma4:31b-cloud','gemma4:26b-a4b-it-q4_K_M')
  ),
  -- Legacy text columns: keep aligned with the primary chain for personas
  -- that fall back to them when the map doesn't have an override.
  designer_model = 'deepseek-v4-pro:cloud, glm-5.1:cloud, qwen3.6:35b-a3b-q4_K_M',
  coder_model    = 'qwen3-coder-next:cloud, glm-5.1:cloud, qwen3-coder:30b-a3b-q8_0';

SELECT 'after' AS phase, ollama_model_map FROM system_config;
SELECT 'legacy_cols' AS phase, designer_model, coder_model FROM system_config;

COMMIT;
