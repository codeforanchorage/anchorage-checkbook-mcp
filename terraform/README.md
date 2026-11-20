# Terraform Configurations

## AWS (Primary)

Deploy OpenContext to AWS Lambda. See [Deployment Guide](../docs/DEPLOYMENT.md).

### First-time setup: Bootstrap backend

The AWS configuration uses S3 for state storage. Run the bootstrap **once** to create the backend resources. See [bootstrap/README.md](bootstrap/README.md) for details:

```bash
cd bootstrap
terraform init
terraform apply
```

### Deploy OpenContext

Use the main deploy script (recommended):

```bash
./scripts/deploy.sh
```

Or deploy manually:

```bash
cd aws
terraform init
terraform plan -var="config_file=config.yaml"
terraform apply
```

**Note:** The deploy script copies `config.yaml` into `terraform/aws/` before running Terraform. For manual runs from `terraform/aws/`, use `-var="config_file=../../config.yaml"` if running from project root.

### State backend

State lives in the S3 bucket named in [aws/backend.tf](aws/backend.tf), which the other Anchorage MCP servers share, under the workspaces `anchorage-checkbook-staging` and `anchorage-checkbook-prod`. Do not replace that file. A fork deploying to its own AWS account should create a bucket and lock table and write its own `backend.tf` before the first `terraform init`.

## Other Clouds

- **GCP:** [gcp/](gcp/) – Coming soon
- **Azure:** [azure/](azure/) – Coming soon
