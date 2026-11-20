###############################################################################
# CloudWatch alarms for the MCP server.
#
# Surfaces Lambda errors/throttles, API Gateway 5XX spikes, and Lambda
# duration approaching the configured timeout. (The per-repo 4XX probing
# alarm was retired; the fleet POST /mcp alarm in mcp-stats covers it.)
#
# If `alarm_sns_topic_arn` is set, alarms notify that topic. Otherwise the
# alarms are created with no actions — you can still see them in the
# CloudWatch console and wire notifications up later.
###############################################################################

locals {
  alarm_actions = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${local.lambda_name}-lambda-errors"
  alarm_description   = "Lambda function is returning errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mcp_server.function_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${local.lambda_name}-lambda-throttles"
  alarm_description   = "Lambda function is being throttled (concurrency limit hit)"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mcp_server.function_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "lambda_duration_near_timeout" {
  alarm_name         = "${local.lambda_name}-lambda-duration-near-timeout"
  alarm_description  = "Lambda p95 duration is approaching the upstream time budget"
  namespace          = "AWS/Lambda"
  metric_name        = "Duration"
  extended_statistic = "p95"
  period             = 300
  evaluation_periods = 2
  # 90% of the plugin's time budget (18s of 20s). A tool call is cut off at
  # that budget and returns a readable error, so durations stop at about 20s:
  # a threshold tied to the Lambda timeout (22.4s) sat in a range a slow
  # upstream never reaches, and could only fire once calls were being killed.
  # This one fires when most calls are running into the budget.
  threshold           = local.plugin_timeout * 1000 * 0.9
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mcp_server.function_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "apigw_5xx" {
  alarm_name          = "${local.lambda_name}-apigw-5xx"
  alarm_description   = "API Gateway is returning 5XX responses"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5XXError"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ApiName = aws_api_gateway_rest_api.mcp_api.name
    Stage   = aws_api_gateway_stage.prod.stage_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}
