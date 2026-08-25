"""Invariants on the shipped deployment configuration.

Unlike the rest of the suite, these tests read the REAL files this fork
deploys (``config-anchorage-checkbook.yaml``, ``config.yaml``,
``terraform/aws/*.tfvars``) rather than fixtures. They exist because the
values below are only wrong at runtime, in production, under load -- there is
no unit under test that would otherwise notice.

Which file is authoritative: ``config.yaml`` is what ``scripts/deploy.sh``
packages, but it is GITIGNORED, so a fresh clone (and CI) does not have it.
``config-anchorage-checkbook.yaml`` is the tracked source of truth. The two
must stay byte-identical; that is pinned below, and the ladder is asserted
against the tracked copy so these tests are meaningful in CI.

Scope: the enabled plugin is ``anchorage_checkbook``. The other plugins
shipped in this fork are ``enabled: false`` and are deliberately not asserted
on -- an inert plugin's timeout cannot hang a Lambda.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_TRACKED = REPO_ROOT / "config-anchorage-checkbook.yaml"
CONFIG_DEPLOYED = REPO_ROOT / "config.yaml"
TFVARS = REPO_ROOT / "terraform" / "aws"

# The one plugin this fork actually runs (one fork = one MCP server).
PLUGIN = "anchorage_checkbook"

# API Gateway REST integrations time out at 29 seconds. This is a hard AWS
# service limit -- not a quota, not adjustable by support -- so every timeout
# underneath it must be strictly smaller. See terraform/aws/api_gateway.tf,
# which uses aws_api_gateway_rest_api.
API_GATEWAY_INTEGRATION_TIMEOUT = 29


def _load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tfvar_int(env: str, name: str) -> int:
    """Read a bare integer variable out of one environment's tfvars."""
    text = (TFVARS / f"{env}.tfvars").read_text(encoding="utf-8")
    match = re.search(rf"^{name}\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    assert match, f"{name} not found in {env}.tfvars"
    return int(match.group(1))


class TestTimeoutLadder:
    """API Gateway (29s) > Lambda > plugin HTTP, strictly decreasing.

    Inverting any pair produces a failure mode that unit tests cannot see:

    * Lambda above the gateway -- the client gets a 504 at 29s while the
      Lambda keeps burning a reserved-concurrency slot for the remainder.
    * Plugin above the Lambda -- a hung upstream never yields a readable
      "upstream timed out" tool error; the Lambda is killed mid-flight and
      the caller gets an opaque 502.
    """

    def test_lambda_timeout_below_api_gateway_ceiling(self):
        lambda_timeout = _load(CONFIG_TRACKED)["aws"]["lambda_timeout"]
        assert lambda_timeout < API_GATEWAY_INTEGRATION_TIMEOUT, (
            f"lambda_timeout={lambda_timeout}s is at or above API Gateway's "
            f"hard {API_GATEWAY_INTEGRATION_TIMEOUT}s integration timeout"
        )

    def test_plugin_timeout_below_lambda_timeout(self):
        config = _load(CONFIG_TRACKED)
        lambda_timeout = config["aws"]["lambda_timeout"]
        plugin_timeout = config["plugins"][PLUGIN]["timeout"]
        assert plugin_timeout < lambda_timeout, (
            f"{PLUGIN} timeout={plugin_timeout}s is at or above "
            f"lambda_timeout={lambda_timeout}s"
        )

    def test_plugin_timeout_leaves_headroom_to_return(self):
        """The plugin must finish AND render its response inside the Lambda.

        Checkbook responses are assembled after the upstream call returns --
        code/label splitting, money formatting, provenance -- so the gap has
        to cover more than the HTTP round trip.
        """
        config = _load(CONFIG_TRACKED)
        headroom = (
            config["aws"]["lambda_timeout"] - config["plugins"][PLUGIN]["timeout"]
        )
        assert headroom >= 5, (
            f"only {headroom}s between the plugin timeout and the Lambda "
            "timeout; a slow upstream would leave no time to format and "
            "return the error"
        )


class TestTerraformAgreesWithConfig:
    """config.yaml wins for these, but drift still misleads whoever reads
    the tfvars -- see the locals block in terraform/aws/main.tf.

    Both environments are checked: one config file feeds staging AND prod,
    so a value set there lands on both.
    """

    @pytest.mark.parametrize("env", ["staging", "prod"])
    @pytest.mark.parametrize("key", ["lambda_timeout", "lambda_memory"])
    def test_tfvars_matches_config(self, env, key):
        assert _tfvar_int(env, key) == _load(CONFIG_TRACKED)["aws"][key], (
            f"{key} differs between config-anchorage-checkbook.yaml "
            f"(authoritative) and {env}.tfvars; main.tf reads the config "
            f"first, so the tfvars value is silently ignored"
        )


class TestConfigCopiesInSync:
    """The tracked config and the deployed config must be identical.

    deploy.sh packages ``config.yaml``, which is gitignored;
    ``config-anchorage-checkbook.yaml`` is the tracked copy that code review
    actually sees. Nothing enforces the relationship at deploy time, so a fix
    applied to only one of them ships half-applied -- or, worse, is reviewed
    in one file and deployed from the other.
    """

    def test_identical(self):
        if not CONFIG_DEPLOYED.exists():
            pytest.skip(
                "config.yaml is gitignored and absent (fresh clone / CI); "
                "the tracked copy is asserted on directly"
            )
        assert CONFIG_DEPLOYED.read_bytes() == CONFIG_TRACKED.read_bytes(), (
            "config.yaml and config-anchorage-checkbook.yaml have drifted "
            "apart; deploy.sh ships config.yaml"
        )
