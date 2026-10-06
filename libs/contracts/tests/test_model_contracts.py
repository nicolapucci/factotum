from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from factotum.contracts.identity import Owner, Subject
from factotum.contracts.models import (
    ADDRESS_REQUIRED,
    DEFAULT_SPEECH_LANGUAGES,
    HOSTED_PROVIDERS,
    BindingOptions,
    ModelBinding,
    ModelRole,
    Profile,
    ProviderKind,
    StructuredOutputSupport,
)
from factotum.contracts.testing import (
    make_device_id,
    make_model_binding,
    make_tenant_id,
    make_user_id,
)


class TestModelRole:
    def test_roles_have_stable_values(self) -> None:
        assert ModelRole("chat.main") is ModelRole.CHAT_MAIN
        assert ModelRole("embedding") is ModelRole.EMBEDDING

    def test_only_the_chat_roles_are_chat_roles(self) -> None:
        assert {role for role in ModelRole if role.is_chat} == {
            ModelRole.CHAT_MAIN,
            ModelRole.CHAT_FAST,
        }


class TestBindingOptions:
    def test_the_defaults_are_prudent(self) -> None:
        options = BindingOptions()

        assert options.max_input_tokens == 8192
        assert options.timeout_s == 60
        assert options.temperature is None
        assert options.native_structured_output is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("max_input_tokens", 100),
            ("max_output_tokens", 0),
            ("temperature", 2.5),
            ("temperature", -0.1),
            ("timeout_s", 0),
            ("timeout_s", 601),
            ("num_ctx", 128),
            ("dimensions", 0),
        ],
    )
    def test_out_of_range_values_are_rejected(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError, match=field):
            BindingOptions.model_validate({field: value})

    def test_unknown_options_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="verbose"):
            BindingOptions.model_validate({"verbose": True})

    def test_italian_is_what_is_heard_unless_the_binding_says_otherwise(self) -> None:
        assert BindingOptions().languages == DEFAULT_SPEECH_LANGUAGES == ("it",)

    def test_a_binding_stored_before_languages_were_a_choice_hears_italian_too(self) -> None:
        # What the hub keeps is the binding as JSON: a row written without the option has none.
        stored = BindingOptions.model_validate_json('{"max_input_tokens": 4096}')

        assert stored.languages == ("it",)

    def test_leaving_the_guess_to_the_model_is_asked_for_and_is_kept(self) -> None:
        options = BindingOptions(languages=None)

        assert options.languages is None
        assert BindingOptions.model_validate({"languages": None}).languages is None
        assert BindingOptions.model_validate_json(options.model_dump_json()).languages is None

    @pytest.mark.parametrize("written", [["it"], ["it", "en"], ("it",), ["haw"]])
    def test_languages_written_as_a_list_are_kept_as_a_tuple_of_codes(
        self, written: list[str] | tuple[str, ...]
    ) -> None:
        options = BindingOptions.model_validate({"languages": written})

        assert options.languages == tuple(written)

    @pytest.mark.parametrize(
        ("languages", "reason"),
        [
            ([], "at least 1 item"),
            (["it", "it"], "listed twice"),
            (["IT"], "not a language code"),
            (["italian"], "not a language code"),
            (["i"], "not a language code"),
            ([""], "not a language code"),
            (["it", "en", "fr", "de", "es", "pt", "nl", "pl", "ru"], "at most 8 items"),
        ],
    )
    def test_a_list_of_languages_that_is_not_one_is_rejected(
        self, languages: list[str], reason: str
    ) -> None:
        with pytest.raises(ValidationError, match=reason):
            BindingOptions.model_validate({"languages": languages})

    def test_no_words_are_given_to_a_model_of_speech_unless_the_binding_says_so(self) -> None:
        assert BindingOptions().vocabulary is None
        assert BindingOptions.model_validate_json('{"max_input_tokens": 4096}').vocabulary is None

    def test_the_words_of_a_home_are_kept_in_the_order_they_were_written(self) -> None:
        options = BindingOptions.model_validate(
            {"vocabulary": ["garage", "router", "Home Assistant"]}
        )

        assert options.vocabulary == ("garage", "router", "Home Assistant")
        assert BindingOptions.model_validate_json(options.model_dump_json()) == options

    @pytest.mark.parametrize(
        ("vocabulary", "reason"),
        [
            ([""], "1 to 64 characters"),
            (["x" * 65], "1 to 64 characters"),
            (["garage", "Garage"], "listed twice"),
            (["two\nlines"], "no line break"),
            ([f"{index:02d}" + "w" * 58 for index in range(11)], "600 characters in all"),
            (["word"] * 101, "at most 100 items"),
        ],
    )
    def test_a_vocabulary_that_is_not_words_that_people_say_is_rejected(
        self, vocabulary: list[str], reason: str
    ) -> None:
        with pytest.raises(ValidationError, match=reason):
            BindingOptions.model_validate({"vocabulary": vocabulary})

    def test_languages_survive_json_as_they_do_in_the_hubs_store(self) -> None:
        options = BindingOptions(languages=("it", "en"))

        assert BindingOptions.model_validate_json(options.model_dump_json()) == options


class TestModelBinding:
    def test_a_local_ollama_needs_only_an_address(self) -> None:
        binding = make_model_binding(tenant_id=make_tenant_id())

        assert binding.provider is ProviderKind.OLLAMA
        assert binding.secret_ref is None

    @pytest.mark.parametrize("provider", sorted(HOSTED_PROVIDERS))
    def test_a_hosted_provider_needs_a_secret_reference(self, provider: ProviderKind) -> None:
        with pytest.raises(ValidationError, match="needs a secret_ref"):
            make_model_binding(tenant_id=make_tenant_id(), provider=provider, base_url=None)

    @pytest.mark.parametrize("provider", sorted(HOSTED_PROVIDERS))
    def test_a_hosted_provider_has_a_well_known_address(self, provider: ProviderKind) -> None:
        binding = make_model_binding(
            tenant_id=make_tenant_id(),
            provider=provider,
            base_url=None,
            secret_ref="env:PROVIDER_API_KEY",
        )

        assert binding.base_url is None

    @pytest.mark.parametrize("provider", sorted(ADDRESS_REQUIRED))
    def test_a_self_hosted_server_needs_an_address(self, provider: ProviderKind) -> None:
        with pytest.raises(ValidationError, match="needs a base_url"):
            make_model_binding(tenant_id=make_tenant_id(), provider=provider, base_url=None)

    def test_the_address_loses_its_trailing_slash(self) -> None:
        binding = make_model_binding(tenant_id=make_tenant_id(), base_url="http://host:11434/")

        assert binding.base_url == "http://host:11434"

    @pytest.mark.parametrize("base_url", ["ftp://host", "host:11434", "file:///etc", "http://"])
    def test_the_address_must_be_http_or_https(self, base_url: str) -> None:
        with pytest.raises(ValidationError, match="base_url"):
            make_model_binding(tenant_id=make_tenant_id(), base_url=base_url)

    @pytest.mark.parametrize("ref", ["env:OPENAI_API_KEY", "env:K"])
    def test_an_environment_reference_is_accepted(self, ref: str) -> None:
        binding = make_model_binding(tenant_id=make_tenant_id(), secret_ref=ref)

        assert binding.secret_ref == ref

    @pytest.mark.parametrize("ref", ["OPENAI_API_KEY", "env:lower", "vault:x", "env:", "sk-abc"])
    def test_anything_that_is_not_a_reference_is_rejected(self, ref: str) -> None:
        with pytest.raises(ValidationError, match="secret_ref"):
            make_model_binding(tenant_id=make_tenant_id(), secret_ref=ref)

    def test_a_key_pasted_where_a_reference_belongs_is_not_echoed_back(self) -> None:
        # pydantic normally quotes the rejected value; for a key that would reach the logs.
        pasted = "sk-proj-THISISAREALLOOKINGSECRETKEY1234567890"

        with pytest.raises(ValidationError) as caught:
            make_model_binding(tenant_id=make_tenant_id(), secret_ref=pasted)

        assert pasted not in str(caught.value)
        # errors() still carries the input unless asked not to: API error bodies must use this.
        assert "THISISAREAL" not in repr(caught.value.errors(include_input=False))

    def test_a_binding_belongs_to_a_user_or_to_the_tenant_never_to_a_device(self) -> None:
        with pytest.raises(ValidationError, match="user or to the tenant"):
            make_model_binding(tenant_id=make_tenant_id(), owner=Owner.of_device(make_device_id()))

    def test_a_user_can_own_a_binding_to_override_the_tenants(self) -> None:
        owner = Owner.of_subject(Subject.user(make_user_id()))

        binding = make_model_binding(tenant_id=make_tenant_id(), owner=owner)

        assert binding.owner == owner

    def test_a_binding_does_not_say_whether_the_model_is_local(self) -> None:
        # The deployment and the provider the user picked are the policy (ADR 0010): the system
        # neither records nor enforces where a model runs. A payload that still declares it is
        # refused, not ignored, so nobody believes it protects anything.
        payload = make_model_binding(tenant_id=make_tenant_id()).model_dump(mode="json")

        with pytest.raises(ValidationError):
            ModelBinding.model_validate({**payload, "locality": "local"})

    def test_a_binding_round_trips_through_json(self) -> None:
        binding = make_model_binding(
            tenant_id=make_tenant_id(),
            provider=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://lmstudio:1234/v1",
            secret_ref="env:LMSTUDIO_KEY",
            options=BindingOptions(max_input_tokens=32_000, temperature=0.2),
        )

        assert ModelBinding.model_validate_json(binding.model_dump_json()) == binding

    def test_unknown_fields_are_rejected(self) -> None:
        payload: dict[str, Any] = make_model_binding(tenant_id=make_tenant_id()).model_dump(
            mode="json"
        )
        payload["api_key"] = "sk-should-never-be-accepted"

        with pytest.raises(ValidationError, match="api_key") as caught:
            ModelBinding.model_validate(payload)

        assert "sk-should-never-be-accepted" not in str(caught.value)

    def test_the_id_is_a_uuid(self) -> None:
        assert make_model_binding(tenant_id=make_tenant_id()).id.version == 4
        assert uuid4().version == 4

    @pytest.mark.parametrize("provider", [ProviderKind.WHISPER_LOCAL, ProviderKind.PIPER_LOCAL])
    def test_a_provider_in_the_gateways_own_process_needs_neither_an_address_nor_a_key(
        self, provider: ProviderKind
    ) -> None:
        binding = make_model_binding(
            tenant_id=make_tenant_id(), provider=provider, base_url=None, secret_ref=None
        )

        assert binding.base_url is None
        assert binding.secret_ref is None


def test_a_profile_says_what_a_role_can_do_without_naming_the_model() -> None:
    profile = Profile(
        role=ModelRole.CHAT_MAIN,
        max_input_tokens=32_000,
        streaming=True,
        structured_output=StructuredOutputSupport.NATIVE,
    )

    assert set(profile.model_dump()) == {
        "role",
        "max_input_tokens",
        "max_output_tokens",
        "streaming",
        "structured_output",
    }


def test_a_role_that_produces_no_text_has_no_structured_output_support() -> None:
    profile = Profile(
        role=ModelRole.EMBEDDING,
        max_input_tokens=8192,
        streaming=False,
    )

    assert profile.structured_output is None


def test_a_profile_needs_a_positive_context() -> None:
    with pytest.raises(ValidationError):
        Profile(
            role=ModelRole.CHAT_FAST,
            max_input_tokens=0,
            structured_output=StructuredOutputSupport.EMULATED,
        )
