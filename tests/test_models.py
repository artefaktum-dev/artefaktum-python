from __future__ import annotations

from datetime import timezone

from artefaktum.models import (
    Artifact,
    CreatedKey,
    Download,
    Page,
    Project,
    Relation,
    Resolution,
    UploadTicket,
    UsagePoint,
)

from .fixtures import ARTIFACT, DOWNLOAD, KEY, PROJECT, RELATION, UPLOAD, USAGE


def test_artifact_parses_nested_version_and_ignores_unknown_fields():
    a = Artifact.from_json(ARTIFACT)
    assert a.id == "0199-a1" and a.tags == ["churn"] and a.metadata == {"k": 1}
    assert a.latest_version is not None and a.latest_version.sha256.startswith("e3b0")
    assert a.latest_version.filename == "report.pdf"  # renamed from original_filename
    assert a.created_at.tzinfo == timezone.utc and a.created_at.hour == 10
    assert a.superseded is False and a.stale_upstream is False and a.semantic_ready is True
    assert not hasattr(a, "some_future_field")


def test_artifact_without_version_and_missing_optional_flags():
    data = {**ARTIFACT, "latest_version": None}
    for k in ("semantic_ready", "superseded", "superseded_by", "stale_upstream"):
        data.pop(k)
    a = Artifact.from_json(data)
    assert a.latest_version is None and a.superseded is False and a.superseded_by is None


def test_page_wraps_items_with_cursor():
    page = Page.from_json({"items": [PROJECT], "next_cursor": "abc"}, Project.from_json)
    assert [p.slug for p in page.items] == ["default"] and page.next_cursor == "abc"


def test_upload_ticket_relation_usage():
    t = UploadTicket.from_json(UPLOAD)
    assert t.artifact.version_id == "0199-v1" and t.upload.headers == {
        "content-type": "application/pdf"
    }
    assert Relation.from_json(RELATION).relation_type == "derived_from"
    assert UsagePoint.from_json(USAGE["items"][0]).value == 40


def test_models_are_frozen():
    import dataclasses

    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        Project.from_json(PROJECT).slug = "x"  # type: ignore[misc]


# -- credentials never reach a repr ----------------------------------------------------
#
# A `print(created_key)` or a failing assertion would otherwise put a plaintext
# long-lived API key, or the signature of a signed URL, into a log or a CI transcript.

SECRET = "ak_live_0199abcdef0123456789"  # noqa: S105 -- a fixture, not a real key
SIGNED_URL = "https://storage.test/bucket/o.bin?sig=SIGNATURE&exp=1"


def test_created_key_repr_redacts_the_secret():
    created = CreatedKey.from_json({"key": KEY, "secret": SECRET})
    text = repr(created)
    assert SECRET not in text
    assert "ak_***" in text
    assert created.secret == SECRET  # the attribute still carries the real value


def test_download_repr_redacts_the_signature_but_keeps_the_object_identifiable():
    download = Download.from_json({**DOWNLOAD, "url": SIGNED_URL})
    text = repr(download)
    assert "sig=" not in text and "SIGNATURE" not in text
    assert "https://storage.test/bucket/o.bin?…" in text
    assert "0199-v1" in text
    assert download.url == SIGNED_URL


def test_upload_instructions_repr_redacts_the_signature_even_when_nested():
    ticket = UploadTicket.from_json({**UPLOAD, "upload": {**UPLOAD["upload"], "url": SIGNED_URL}})
    for text in (repr(ticket.upload), repr(ticket)):
        assert "sig=" not in text and "SIGNATURE" not in text
        assert "https://storage.test/bucket/o.bin?…" in text
    assert ticket.upload.url == SIGNED_URL


def test_resolution_repr_redacts_the_nested_upload():
    resolution = Resolution.from_json(
        {
            "status": "create",
            "reservation": UPLOAD["artifact"],
            "upload": {**UPLOAD["upload"], "url": SIGNED_URL},
        }
    )
    assert "sig=" not in repr(resolution)


def test_repr_of_an_unsigned_url_keeps_it_whole():
    download = Download.from_json({**DOWNLOAD, "url": "https://storage.test/o.bin"})
    assert "'https://storage.test/o.bin'" in repr(download)
