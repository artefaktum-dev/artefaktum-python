import pytest

from artefaktum._config import DEFAULT_BASE_URL, is_uuid, load_config
from artefaktum.errors import MissingApiKey


def test_argument_beats_env_beats_default():
    env = {
        "ARTEFAKTUM_API_KEY": "ak_env",
        "ARTEFAKTUM_BASE_URL": "https://env.test/",
        "ARTEFAKTUM_PROJECT": "env-proj",
    }
    c = load_config(api_key="ak_arg", env=env)
    assert c.api_key == "ak_arg" and c.base_url == "https://env.test" and c.project == "env-proj"
    d = load_config(api_key="ak", env={})
    assert d.base_url == DEFAULT_BASE_URL and d.project == "default" and d.timeout == 30.0


def test_missing_key_raises_before_any_request():
    with pytest.raises(MissingApiKey):
        load_config(env={})


def test_base_url_trailing_slash_is_stripped():
    assert (
        load_config(api_key="ak", base_url="http://localhost:3000/", env={}).base_url
        == "http://localhost:3000"
    )


def test_is_uuid():
    assert is_uuid("01a0be89-1cde-74c1-ab17-8814cc9d9141") and not is_uuid("default")
