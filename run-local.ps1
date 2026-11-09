$env:OPENAI_BASE_URL = 'http://127.0.0.1:8788/v1'
$env:OPENAI_MODEL = 'deepseek-v4.1-flash'
$env:OPENAI_API_KEY = 'root-scout-local'
$env:OPENAI_REASONING_EFFORT = 'low'
# Local launcher is deliberately unlimited per case; use direct CLI with ROOT_SCOUT_MAX_TOKENS for the old hard budget.
$env:ROOT_SCOUT_MAX_TOKENS = $null
$env:ROOT_SCOUT_MAX_OUTPUT_TOKENS = $null
$env:ROOT_SCOUT_MAX_OUTPUT_TOKENS_CONTRACT_PLAN = $null
$env:ROOT_SCOUT_MAX_OUTPUT_TOKENS_ADJUDICATE = $null
$env:ROOT_SCOUT_MAX_OUTPUT_TOKENS_INTERVENE = $null
$env:ROOT_SCOUT_MAX_OUTPUT_TOKENS_RESIDUAL = $null
$env:ROOT_SCOUT_MAX_OUTPUT_TOKENS_EXPLORE_V2 = $null
$env:ROOT_SCOUT_OUTPUT_POLICY = 'adaptive'
if ($env:ROOT_SCOUT_OUTPUT_POLICY -eq 'adaptive' -and -not $env:ROOT_SCOUT_MODEL_OUTPUT_LIMIT -and $env:OPENAI_MODEL -eq 'deepseek-v4.1-flash' -and $env:OPENAI_BASE_URL -eq 'http://127.0.0.1:8788/v1') {
    $env:ROOT_SCOUT_MODEL_OUTPUT_LIMIT = '128000'
}

& python (Join-Path $PSScriptRoot 'root_scout.py') @args
exit $LASTEXITCODE
