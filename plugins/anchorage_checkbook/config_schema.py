"""Pydantic configuration schema for the Anchorage Open Checkbook plugin."""

from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Municipality of Anchorage Open Checkbook hosted Feature Service
# (muniorg.maps.arcgis.com / MOAGIS, org Ce3DhLRthdwbHlfF). Public, no
# auth, capabilities Query,Extract. This is the SERVICE ROOT -- the
# plugin addresses individual tables as <service_url>/<table_id>.
DEFAULT_SERVICE_URL = (
    "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/"
    "MOA_OpenCheckbook_Hosted/FeatureServer"
)


class AnchorageCheckbookPluginConfig(BaseModel):
    """Configuration schema for the Anchorage Open Checkbook plugin.

    Validates plugin configuration for the MOA Open Checkbook hosted
    tables. Unlike the parcels plugin there is no ``field_map`` -- the
    per-table field semantics (measure fields, entity field, coded
    fields) are structural to this dataset and live in the ``TABLES``
    registry in plugin.py; another city's checkbook would need its own
    registry, not a field rename.
    """

    enabled: bool = Field(default=False, description="Whether plugin is enabled")
    service_url: str = Field(
        default=DEFAULT_SERVICE_URL,
        description=(
            "ArcGIS Feature Service ROOT URL for the Open Checkbook "
            "tables (.../FeatureServer, no trailing table id)"
        ),
    )
    city_name: str = Field(
        default="Municipality of Anchorage",
        description="Name of the city/municipality (used in tool text)",
    )
    timeout: int = Field(
        default=30, ge=1, le=300, description="HTTP request timeout in seconds"
    )

    @field_validator("service_url")
    @classmethod
    def validate_service_url(cls, v: str) -> str:
        """Validate that the service URL is a well-formed http(s) URL."""
        if not v:
            raise ValueError("service URL cannot be empty")
        try:
            result = urlparse(v)
            if not result.scheme or not result.netloc:
                raise ValueError("URL must include scheme (http/https) and hostname")
            if result.scheme not in ("http", "https"):
                raise ValueError("URL scheme must be http or https")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Invalid URL format: {e}")
        return v.rstrip("/")

    model_config = ConfigDict(extra="forbid")
