"""A launch ``base_url`` is refused by its shape, never by being an external-process route.

Regression for #149114: ``external_process`` providers (agent CLIs over stdio or ACP) carry a
``process://<slug>``, ``acp://<vendor>`` or ``acp+tcp://host:port`` route, and Desktop echoes the
stored ``base_url`` back on ``session.resume``. The launch check accepted only http(s), so every such
session was unresumable. The check is a shape check, not a provider check: a ``process://`` URL under
a provider that does not run a process is accepted here too, exactly as an http URL the provider does
not serve is, and fails at that provider's runtime resolution like any other wrong endpoint.
"""
import pytest

from gateway.session_policy import build_policy
from hermes_state_runtime import RuntimeStoreError

_CFG = {'platform_toolsets': {'cli': ['terminal']}}
_SLUG = 'claude-subscription-directsdk-experimental'


@pytest.fixture
def process_provider(monkeypatch):
    """A registered ``external_process`` provider whose route is ``process://<slug>``."""
    from providers.base import ProviderProfile
    profile = ProviderProfile(name=_SLUG, auth_type='external_process', base_url=f'process://{_SLUG}')
    monkeypatch.setattr('providers.get_provider_profile', lambda name: profile if name == profile.name else None)
    return profile


def _launch(tmp_path, **params):
    return build_policy({'cwd': str(tmp_path), 'source': 'cli', **params}, dict(_CFG))


@pytest.mark.parametrize('url', [f'process://{_SLUG}', f'process://{_SLUG}/', 'PROCESS://Proc-Slug',
                                 'acp://copilot', 'acp+tcp://127.0.0.1:8765'])
def test_an_external_process_route_resumes_on_its_stored_base_url(tmp_path, process_provider, url):
    policy = _launch(tmp_path, provider=process_provider.name, base_url=url)
    assert (policy.provider, policy.base_url) == (process_provider.name, url)


@pytest.mark.parametrize('url', ['process:///x', 'process://', f'process://{_SLUG}?x=1',
                                 f'process://{_SLUG}#f', f'process://user:pw@{_SLUG}', f'process://user@{_SLUG}',
                                 'acp+tcp://user:pw@127.0.0.1:8765', 'ftp://example.invalid', 'file:///etc/passwd',
                                 'http://user:secret@localhost/v1', 'https://h.example/v1?k=1', 'https:///v1'])
def test_a_base_url_with_no_host_credentials_query_fragment_or_unknown_scheme_is_refused(tmp_path, process_provider,
                                                                                         url):
    with pytest.raises(RuntimeStoreError) as caught:
        _launch(tmp_path, provider=process_provider.name, base_url=url)
    refusal = caught.value
    assert (refusal.reason, refusal.field) == ('invalid_params', 'base_url')
    # The detail names the schemes the check actually admits, not a stale "http(s)".
    assert all(scheme in refusal.detail for scheme in ('http', 'https', 'process', 'acp', 'acp+tcp')), refusal.detail


@pytest.mark.parametrize('url', ['http://127.0.0.1:1234/v1', 'https://proxy.example/v1'])
def test_an_http_route_is_unchanged(tmp_path, url):
    assert _launch(tmp_path, provider='custom', base_url=url).base_url == url


def test_a_process_url_under_a_non_process_provider_is_a_shape_pass_not_a_route_check(tmp_path):
    """The launch check reads the URL's shape only; whether the provider serves it is runtime resolution's."""
    assert _launch(tmp_path, provider='custom', base_url=f'process://{_SLUG}').base_url == f'process://{_SLUG}'
