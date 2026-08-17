from agentcongress.llm.deepseek import DEFAULT_DEEPSEEK_BASE_URL, DEFAULT_DEEPSEEK_MODEL, DeepSeekProvider
from agentcongress.llm.registry import PROTOCOLS, create_provider, provider_defaults
from agentcongress.listeners import DeepSeekFloorObserver


def test_deepseek_provider_pins_defaults() -> None:
    provider = DeepSeekProvider(api_key="key")
    assert provider.name == "deepseek"
    assert provider.model == DEFAULT_DEEPSEEK_MODEL
    assert provider.base_url == DEFAULT_DEEPSEEK_BASE_URL


def test_registry_creates_deepseek_provider_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("TEST_DS_KEY", "secret")
    provider = create_provider("deepseek", api_key_env="TEST_DS_KEY")
    assert provider.name == "deepseek"
    assert provider.base_url == DEFAULT_DEEPSEEK_BASE_URL
    assert provider.api_key == "secret"
    assert "deepseek" in PROTOCOLS
    assert provider_defaults("deepseek")["api_key_env"] == "DEEPSEEK_API_KEY"


def test_deepseek_floor_observer_uses_request_floor_tool_loop(monkeypatch) -> None:
    monkeypatch.setenv("TEST_DS_KEY", "secret")
    observer = DeepSeekFloorObserver(api_key_env="TEST_DS_KEY")
    assert observer.observer.default_loop is not None
    assert [tool.name for tool in observer.observer.default_loop.tools] == ["request_floor"]
    assert observer.observer.default_loop.max_tool_rounds == 2
