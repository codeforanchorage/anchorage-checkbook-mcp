lambda_name = "anchorage-checkbook-mcp-staging"
stage_name  = "staging"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 512 MB / 60 s: every checkbook tool is a single attribute query or
# server-side statistics call against one Feature Service table -- no
# geometry, no polygon caches, no batch workloads.
lambda_memory = 512
# Kept in sync with config.yaml, which WINS for this variable and for
# lambda_memory (main.tf reads `local.config.aws.*` first and only falls back
# to these vars). lambda_name uses the OPPOSITE precedence -- this file wins
# there -- so check main.tf per variable rather than assuming. 28s sits just
# under API Gateway's hard, non-adjustable 29s integration timeout so the
# Lambda self-terminates before the gateway gives up.
lambda_timeout = 28

api_quota_limit = 1000
api_rate_limit  = 5
api_burst_limit = 10

custom_domain = ""

lambda_reserved_concurrency = 5
waf_rate_limit_per_5min     = 300
enable_gcc_route            = false

# Use the fleet-wide WAF instead of a dedicated ACL. A dedicated ACL costs
# ~$8/mo in fixed AWS charges (~$5/ACL + $1/rule) regardless of traffic.
#
# Staging has no custom domain, so its host is the raw execute-api name, which
# is NOT a member of the fleet ACL's Host-scoped rate rules. It therefore lands
# on the catch-all `rate-limit-unmatched-host` rule: 300/5min per IP -- exactly
# what the dedicated ACL enforced -- plus the same KnownBadInputs and
# CommonRuleSet managed groups. No membership entry in mcp-stats is needed.
#
# The rate-limit value above stops being read once this is true; it is retained
# so that rolling back (use_shared_waf = false) restores the original limit.
use_shared_waf = true
