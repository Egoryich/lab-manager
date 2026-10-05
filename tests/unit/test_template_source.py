import pytest
from lab_manager.catalog_schemas import TemplateCreate
from pydantic import ValidationError


def test_lxc_template_records_an_exact_proxmox_volume():
    body = TemplateCreate(
        name="Debian 13",
        version_label="13.6-1",
        runtime_kind="LXC",
        guest_family="LINUX",
        source_ref="local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst",
    )
    assert body.source_ref == "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst"
    with pytest.raises(ValidationError):
        TemplateCreate(
            name="Wrong path",
            version_label="v1",
            runtime_kind="LXC",
            guest_family="LINUX",
            source_ref="local:vztmpl/../debian.tar.zst",
        )
