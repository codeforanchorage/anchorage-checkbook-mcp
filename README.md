# OpenContext — Anchorage Open Checkbook fork

<p align="center">
  <img src="docs/opencontext_logo.png" alt="OpenContext Logo" width="400">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

This fork of [OpenContext](https://github.com/thealphacubicle/OpenContext)
deploys the **Anchorage Open Checkbook MCP** (`anchorage_checkbook`
plugin) — read-only tools over the Municipality of Anchorage's
unaudited expenditure, payroll, procurement, and revenue tables, with
the dataset's traps (duplicate double-loads, snapshot dates, net
amounts, unnormalized vendor names) encoded in tool behavior. See
[docs/CHECKBOOK.md](docs/CHECKBOOK.md) and
[METHODOLOGY.md](METHODOLOGY.md) — this server's totals deliberately
differ from the public MOA dashboard (duplicates are filtered by
default).

---

## Quick Start

```bash
# 1. Configure (this fork ships its config ready to go)
cp config-anchorage-checkbook.yaml config.yaml

# 2. Test locally
pip install aiohttp
python3 scripts/local_server.py

# 3. Deploy
./scripts/deploy.sh --environment staging
```

Connect via **Claude Connectors** (same steps on both Claude.ai and Claude Desktop):

1. Go to **Settings** → **Connectors** (or **Customize** → **Connectors** on claude.ai)
2. Click **Add custom connector**
3. Enter a name (e.g. "Boston OpenData") and your API Gateway URL

Get the URL: `cd terraform/aws && terraform output -raw api_gateway_url`

See [Getting Started](docs/GETTING_STARTED.md) for full setup.

---

## Documentation


| Doc                                        | Description                                     |
| ------------------------------------------ | ----------------------------------------------- |
| [Getting Started](docs/GETTING_STARTED.md) | Setup and usage                                 |
| [Architecture](docs/ARCHITECTURE.md)       | System design and plugins                       |
| [Deployment](docs/DEPLOYMENT.md)           | AWS, Terraform, monitoring                      |
| [Testing](docs/TESTING.md)                 | Local testing (Terminal, Claude, MCP Inspector) |
| [Anchorage Checkbook](docs/CHECKBOOK.md)   | Open Checkbook MCP server (**this fork's plugin**) |
| [Methodology](METHODOLOGY.md)              | The dedup-by-default decision and why totals differ from the MOA dashboard |
| [Anchorage Parcels](docs/PARCELS.md)       | Parcel/assessment MCP server (deployed from its own fork) |


---

## Examples

- **Boston OpenData (CKAN):** [examples/boston-opendata/config.yaml](examples/boston-opendata/config.yaml)
- **Custom plugin:** [examples/custom-plugin/](examples/custom-plugin/)

---

## Contributing

Pre-commit hooks (optional):

```bash
pip install pre-commit
pre-commit install
```

Hooks: Ruff, yamllint, gofmt. Run manually: `pre-commit run --all-files`.

---

## License

MIT — see [LICENSE](LICENSE).

**Author:** Srihari Raman, City of Boston Department of Innovation and Technology
