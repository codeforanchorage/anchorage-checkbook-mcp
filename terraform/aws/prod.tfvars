lambda_name = "anchorage-checkbook-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 512 MB / 28 s: every checkbook tool is a single attribute query or
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

# Without this the four alarms in cloudwatch_alarms.tf change state and tell
# no one. This is the topic the fleet POST /mcp 4xx alarm for this server
# (defined in mcp-stats) already notifies. Staging deliberately sets none:
# its alarms are visible in the console but do not notify.
alarm_sns_topic_arn = "arn:aws:sns:us-west-2:420839047325:anchorage-gis-mcp-prod-alarms"

api_quota_limit = 3000
# These two feed BOTH the usage plan and the stage-wide method settings
# (api_gateway.tf), so they are the effective throttle on the public /mcp
# route. Raised 5/10 -> 15/30 on 2026-11-17 for ~11 more attorneys onboarding
# via Claude.ai, whose requests share Anthropic's egress IPs.
api_rate_limit  = 15
api_burst_limit = 30

# DNS lives in Dreamhost. Two CNAMEs required: the ACM validation record
# (from `terraform output acm_validation_cname_*`) and the traffic record
# pointing at `terraform output custom_domain_target`.
custom_domain = "checkbook.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection if the
# WAF is bypassed. Raised 10 -> 30 on 2026-11-17 for the attorney onboarding
# (Anchorage GIS hit its cap of 10 on 2026-11-10: 8 throttles, 5xx alarm).
lambda_reserved_concurrency = 30

# WAF per-IP rate limit (rolling 5-minute window). INFORMATIONAL while
# use_shared_waf = true: the live limit is the fleet ACL's, raised to 2000
# for this MCP on 2026-11-17 (mcp-stats 26f2caa). Claude.ai connector traffic
# arrives from Anthropic's egress IPs, so many users share one per-IP
# counter. Kept equal so a rollback to a dedicated ACL restores the same limit.
waf_rate_limit_per_5min = 2000

# No M365 GCC Copilot consumer for the checkbook server; public /mcp only.
enable_gcc_route = false

# Use the fleet-wide WAF instead of a dedicated ACL for this MCP. A dedicated
# ACL costs ~$8/mo in fixed AWS charges regardless of traffic; the shared ACL
# keeps this MCP's 2000/5min limit as its own counter, aggregated on
# (IP, Host) so it stays independent of the other MCPs sharing that limit.
#
# The effective limit lives in mcp-stats' `fleet_waf_members` under the key
# `anchorage-checkbook` (added 2026-08-04) — change it there, not here. The
# rate-limit value above is retained so that rolling back
# (use_shared_waf = false) restores the original limit.
# See mcp-stats/docs/waf-consolidation.md.
use_shared_waf = true
