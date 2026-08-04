lambda_name = "anchorage-checkbook-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 512 MB / 60 s: every checkbook tool is a single attribute query or
# server-side statistics call against one Feature Service table -- no
# geometry, no polygon caches, no batch workloads.
lambda_memory  = 512
lambda_timeout = 60

api_quota_limit = 3000
api_rate_limit  = 5
api_burst_limit = 10

# DNS lives in Dreamhost. Two CNAMEs required: the ACM validation record
# (from `terraform output acm_validation_cname_*`) and the traffic record
# pointing at `terraform output custom_domain_target`.
custom_domain = "checkbook.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection;
# conversational MCP traffic does not need horizontal scale.
lambda_reserved_concurrency = 10

# WAF per-IP rate limit (rolling 5-minute window). ~1 rps sustained per
# IP is plenty for real users.
waf_rate_limit_per_5min = 300

# No M365 GCC Copilot consumer for the checkbook server; public /mcp only.
enable_gcc_route = false

# Dedicated WAF ACL until this MCP joins the fleet-wide shared ACL: flip
# to true ONLY after adding an `anchorage-checkbook` entry to
# `fleet_waf_members` in the mcp-stats repo (see
# mcp-stats/docs/waf-consolidation.md) -- flipping early would associate
# the stage with an ACL that carries no rule for this MCP.
use_shared_waf = false
