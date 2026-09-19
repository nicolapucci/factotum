from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.contracts.errors import BindingNotFoundError
from factotum.contracts.identity import Owner, OwnerKind, Subject
from factotum.contracts.ids import TenantId, UserId
from factotum.contracts.models import (
    BindingOptions,
    Locality,
    ModelBinding,
    ModelRole,
    ProviderKind,
)
from factotum.contracts.testing import (
    make_model_binding,
    make_tenant_id,
    make_user_id,
)
from factotum.model_gateway.bindings import (
    InMemoryBindingRepository,
    fallback_roles,
    load_bindings,
    load_bindings_file,
    owners_for,
    select_binding,
)

TENANT = make_tenant_id()
ALICE = make_user_id()
BOB = make_user_id()


def owned_by_user(user: UserId) -> Owner:
    return Owner.of_subject(Subject.user(user))


def binding(
    role: ModelRole = ModelRole.CHAT_MAIN,
    *,
    tenant: TenantId = TENANT,
    user: UserId | None = None,
    model: str = "some-model",
) -> ModelBinding:
    return make_model_binding(
        tenant_id=tenant,
        role=role,
        model=model,
        owner=owned_by_user(user) if user else None,
    )


class TestInMemoryRepository:
    async def test_a_binding_is_found_by_tenant_owner_and_role(self) -> None:
        mine = binding(user=ALICE)
        repository = InMemoryBindingRepository([mine])

        found = await repository.find(TENANT, owned_by_user(ALICE), ModelRole.CHAT_MAIN)

        assert found is mine

    async def test_a_missing_binding_is_none(self) -> None:
        repository = InMemoryBindingRepository([binding(user=ALICE)])

        assert await repository.find(TENANT, owned_by_user(BOB), ModelRole.CHAT_MAIN) is None
        assert await repository.find(TENANT, owned_by_user(ALICE), ModelRole.EMBEDDING) is None

    async def test_another_tenant_cannot_see_a_binding(self) -> None:
        repository = InMemoryBindingRepository([binding(user=ALICE)])

        found = await repository.find(make_tenant_id(), owned_by_user(ALICE), ModelRole.CHAT_MAIN)

        assert found is None

    async def test_a_user_and_a_tenant_may_both_bind_the_same_role(self) -> None:
        repository = InMemoryBindingRepository([binding(user=ALICE), binding()])

        assert len(repository) == 2

    def test_a_second_binding_of_the_same_role_by_the_same_owner_is_refused(self) -> None:
        repository = InMemoryBindingRepository([binding(user=ALICE)])

        with pytest.raises(ValueError, match=r"already has a binding for chat\.main"):
            repository.add(binding(user=ALICE, model="another"))

    def test_the_same_owner_id_in_another_kind_is_not_a_duplicate(self) -> None:
        shared = TENANT  # a tenant and a user must never collide, even with equal ids
        as_user = make_model_binding(tenant_id=TENANT, owner=Owner(kind=OwnerKind.USER, id=shared))
        as_tenant = make_model_binding(
            tenant_id=TENANT, owner=Owner(kind=OwnerKind.TENANT, id=shared)
        )

        assert len(InMemoryBindingRepository([as_user, as_tenant])) == 2


class TestRoleFallback:
    def test_the_fast_model_falls_back_to_the_main_one(self) -> None:
        assert fallback_roles(ModelRole.CHAT_FAST) == (ModelRole.CHAT_FAST, ModelRole.CHAT_MAIN)

    @pytest.mark.parametrize(
        "role",
        [ModelRole.CHAT_MAIN, ModelRole.EMBEDDING, ModelRole.STT, ModelRole.TTS, ModelRole.RERANK],
    )
    def test_no_other_role_has_a_fallback(self, role: ModelRole) -> None:
        assert fallback_roles(role) == (role,)


class TestOwners:
    def test_a_user_is_served_by_their_own_bindings_and_then_the_tenants(self) -> None:
        owners = owners_for(TENANT, Subject.user(ALICE))

        assert owners == (owned_by_user(ALICE), Owner.of_subject(Subject.tenant(TENANT)))

    def test_the_tenant_is_served_by_its_own_bindings_only(self) -> None:
        owners = owners_for(TENANT, Subject.tenant(TENANT))

        assert owners == (Owner.of_subject(Subject.tenant(TENANT)),)


class TestSelection:
    async def test_the_users_own_binding_wins_over_the_tenants(self) -> None:
        own = binding(user=ALICE, model="alice-own")
        repository = InMemoryBindingRepository([binding(model="tenant-wide"), own])

        chosen = await select_binding(repository, TENANT, Subject.user(ALICE), ModelRole.CHAT_MAIN)

        assert chosen is own

    async def test_the_tenants_binding_serves_a_user_who_has_none(self) -> None:
        shared = binding(model="tenant-wide")
        repository = InMemoryBindingRepository([shared, binding(user=BOB)])

        chosen = await select_binding(repository, TENANT, Subject.user(ALICE), ModelRole.CHAT_MAIN)

        assert chosen is shared

    async def test_a_device_that_acts_for_the_tenant_is_served_by_the_tenants_binding(self) -> None:
        shared = binding(model="tenant-wide")
        repository = InMemoryBindingRepository([shared, binding(user=ALICE)])

        chosen = await select_binding(
            repository, TENANT, Subject.tenant(TENANT), ModelRole.CHAT_MAIN
        )

        assert chosen is shared

    async def test_nobody_is_served_by_the_binding_of_somebody_else(self) -> None:
        repository = InMemoryBindingRepository([binding(user=BOB)])

        with pytest.raises(BindingNotFoundError):
            await select_binding(repository, TENANT, Subject.user(ALICE), ModelRole.CHAT_MAIN)

    async def test_nobody_is_served_by_the_binding_of_another_tenant(self) -> None:
        repository = InMemoryBindingRepository([binding(tenant=make_tenant_id())])

        with pytest.raises(BindingNotFoundError):
            await select_binding(repository, TENANT, Subject.tenant(TENANT), ModelRole.CHAT_MAIN)

    async def test_no_binding_says_which_role_is_missing(self) -> None:
        with pytest.raises(BindingNotFoundError, match=r"chat\.main") as caught:
            await select_binding(
                InMemoryBindingRepository(), TENANT, Subject.tenant(TENANT), ModelRole.CHAT_MAIN
            )

        assert caught.value.details == {"role": "chat.main"}

    async def test_the_fast_model_is_used_when_there_is_one(self) -> None:
        fast = binding(ModelRole.CHAT_FAST)
        repository = InMemoryBindingRepository([binding(ModelRole.CHAT_MAIN), fast])

        chosen = await select_binding(
            repository, TENANT, Subject.tenant(TENANT), ModelRole.CHAT_FAST
        )

        assert chosen is fast

    async def test_without_a_fast_model_the_main_one_does_the_work(self) -> None:
        main = binding(ModelRole.CHAT_MAIN)
        repository = InMemoryBindingRepository([main])

        chosen = await select_binding(
            repository, TENANT, Subject.tenant(TENANT), ModelRole.CHAT_FAST
        )

        assert chosen is main

    async def test_a_user_who_bound_only_a_main_model_does_not_shadow_the_tenants_fast_one(
        self,
    ) -> None:
        tenant_fast = binding(ModelRole.CHAT_FAST, model="tenant-fast")
        repository = InMemoryBindingRepository(
            [binding(ModelRole.CHAT_MAIN, user=ALICE, model="alice-main"), tenant_fast]
        )

        chosen = await select_binding(repository, TENANT, Subject.user(ALICE), ModelRole.CHAT_FAST)

        assert chosen is tenant_fast

    async def test_a_user_who_bound_a_fast_model_is_served_by_it(self) -> None:
        own_fast = binding(ModelRole.CHAT_FAST, user=ALICE, model="alice-fast")
        repository = InMemoryBindingRepository(
            [binding(ModelRole.CHAT_FAST, model="tenant-fast"), own_fast]
        )

        chosen = await select_binding(repository, TENANT, Subject.user(ALICE), ModelRole.CHAT_FAST)

        assert chosen is own_fast

    async def test_the_main_model_of_the_user_serves_the_fast_role_when_nobody_bound_it(
        self,
    ) -> None:
        own_main = binding(ModelRole.CHAT_MAIN, user=ALICE, model="alice-main")
        repository = InMemoryBindingRepository(
            [binding(ModelRole.CHAT_MAIN, model="tenant-main"), own_main]
        )

        chosen = await select_binding(repository, TENANT, Subject.user(ALICE), ModelRole.CHAT_FAST)

        assert chosen is own_main

    async def test_the_main_role_never_falls_back_to_the_fast_one(self) -> None:
        repository = InMemoryBindingRepository([binding(ModelRole.CHAT_FAST)])

        with pytest.raises(BindingNotFoundError):
            await select_binding(repository, TENANT, Subject.tenant(TENANT), ModelRole.CHAT_MAIN)

    async def test_embeddings_have_no_fallback(self) -> None:
        repository = InMemoryBindingRepository([binding(ModelRole.CHAT_MAIN)])

        with pytest.raises(BindingNotFoundError):
            await select_binding(repository, TENANT, Subject.tenant(TENANT), ModelRole.EMBEDDING)


@given(
    entries=st.sets(
        st.tuples(
            st.sampled_from(["alice", "bob", "tenant", "elsewhere"]),
            st.sampled_from([ModelRole.CHAT_MAIN, ModelRole.CHAT_FAST, ModelRole.EMBEDDING]),
        )
    ),
    subject_is=st.sampled_from(["alice", "tenant"]),
    role=st.sampled_from([ModelRole.CHAT_MAIN, ModelRole.CHAT_FAST, ModelRole.EMBEDDING]),
)
async def test_a_binding_is_only_ever_chosen_for_someone_it_belongs_to(
    entries: set[tuple[str, ModelRole]], subject_is: str, role: ModelRole
) -> None:
    elsewhere = make_tenant_id()
    bindings: list[ModelBinding] = []
    for whose, bound_role in entries:
        match whose:
            case "alice":
                bindings.append(binding(bound_role, user=ALICE))
            case "bob":
                bindings.append(binding(bound_role, user=BOB))
            case "tenant":
                bindings.append(binding(bound_role))
            case _:
                bindings.append(binding(bound_role, tenant=elsewhere))
    subject = Subject.user(ALICE) if subject_is == "alice" else Subject.tenant(TENANT)

    try:
        chosen = await select_binding(InMemoryBindingRepository(bindings), TENANT, subject, role)
    except BindingNotFoundError:
        return

    assert chosen.tenant_id == TENANT
    assert chosen.owner in owners_for(TENANT, subject)
    assert chosen.role in fallback_roles(role)


TENANT_ONLY = """
bindings:
  - role: chat.main
    provider: ollama
    model: llama3.1:8b
    base_url: http://localhost:11434
  - role: embedding
    provider: openai
    model: text-embedding-3-small
    secret_ref: env:OPENAI_API_KEY
    options:
      dimensions: 512
      timeout_s: 30
"""


class TestLoadingBindings:
    def test_each_entry_becomes_a_binding_of_the_tenant(self) -> None:
        loaded = load_bindings(TENANT_ONLY, tenant_id=TENANT)

        assert [b.role for b in loaded] == [ModelRole.CHAT_MAIN, ModelRole.EMBEDDING]
        assert {b.tenant_id for b in loaded} == {TENANT}
        assert {b.owner for b in loaded} == {Owner.of_subject(Subject.tenant(TENANT))}

    def test_the_fields_of_a_binding_are_read(self) -> None:
        chat, embedding = load_bindings(TENANT_ONLY, tenant_id=TENANT)

        assert chat.provider is ProviderKind.OLLAMA
        assert chat.model == "llama3.1:8b"
        assert chat.base_url == "http://localhost:11434"
        assert embedding.secret_ref == "env:OPENAI_API_KEY"
        assert embedding.options == BindingOptions(dimensions=512, timeout_s=30)

    def test_the_locality_can_be_declared(self) -> None:
        text = """
bindings:
  - role: chat.main
    provider: openai_compatible
    model: qwen
    base_url: https://gpu.mycompany.example
    locality: local
"""

        assert load_bindings(text, tenant_id=TENANT)[0].locality is Locality.LOCAL

    def test_an_entry_with_a_user_is_that_users_own(self) -> None:
        text = f"""
bindings:
  - role: chat.main
    provider: ollama
    model: llama3.1:8b
    base_url: http://localhost:11434
    user: {ALICE}
"""

        [own] = load_bindings(text, tenant_id=TENANT)

        assert own.owner == owned_by_user(ALICE)

    def test_every_binding_gets_its_own_id(self) -> None:
        first, second = load_bindings(TENANT_ONLY, tenant_id=TENANT)

        assert first.id != second.id

    def test_the_loaded_bindings_fill_a_repository(self) -> None:
        repository = InMemoryBindingRepository(load_bindings(TENANT_ONLY, tenant_id=TENANT))

        assert len(repository) == 2

    def test_two_entries_for_the_same_role_are_caught_when_they_are_added(self) -> None:
        text = TENANT_ONLY + TENANT_ONLY.replace("bindings:\n", "")

        with pytest.raises(ValueError, match="already has a binding"):
            InMemoryBindingRepository(load_bindings(text, tenant_id=TENANT))

    def test_an_empty_list_is_fine(self) -> None:
        assert load_bindings("bindings: []", tenant_id=TENANT) == []

    @pytest.mark.parametrize(
        "text",
        ["", "just a string", "- a\n- list", "bindings: not-a-list", "bindings:\n  key: value"],
    )
    def test_a_document_of_another_shape_is_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match="expected a mapping with a 'bindings' list"):
            load_bindings(text, tenant_id=TENANT)

    def test_a_misspelled_top_level_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"unknown top-level keys: \['bindngs'\]"):
            load_bindings("bindings: []\nbindngs: []", tenant_id=TENANT)

    def test_an_entry_that_is_not_a_mapping_is_refused(self) -> None:
        valid = "{role: chat.main, provider: ollama, model: x, base_url: 'http://h'}"

        with pytest.raises(ValueError, match="binding #2: expected a mapping"):
            load_bindings(f"bindings:\n  - {valid}\n  - just-text", tenant_id=TENANT)

    def test_a_user_that_is_not_an_id_is_refused(self) -> None:
        text = "bindings:\n  - role: chat.main\n    user: alice"

        with pytest.raises(ValueError, match="binding #1: 'user' is not a valid id"):
            load_bindings(text, tenant_id=TENANT)

    def test_an_invalid_entry_is_reported_with_where_and_why(self) -> None:
        text = "bindings:\n  - role: chat.main\n    provider: ollama\n    model: x"

        with pytest.raises(ValueError, match=r"binding #1: .*ollama needs a base_url"):
            load_bindings(text, tenant_id=TENANT, source="bindings.yaml")

    def test_an_unknown_field_is_refused(self) -> None:
        text = (
            "bindings:\n  - role: chat.main\n    provider: ollama\n    model: x\n"
            "    base_url: http://h\n    colour: blue"
        )

        with pytest.raises(ValueError, match=r"colour"):
            load_bindings(text, tenant_id=TENANT)

    def test_a_key_pasted_where_a_reference_belongs_is_not_repeated_in_the_error(self) -> None:
        text = (
            "bindings:\n  - role: chat.main\n    provider: openai\n    model: gpt\n"
            "    secret_ref: sk-should-never-be-repeated-0123456789"
        )

        with pytest.raises(ValueError, match="secret_ref") as caught:
            load_bindings(text, tenant_id=TENANT)

        assert "sk-should-never" not in str(caught.value)

    def test_the_source_is_named_in_the_error(self) -> None:
        with pytest.raises(ValueError, match=r"my-bindings\.yaml: binding #1"):
            load_bindings("bindings:\n  - {}", tenant_id=TENANT, source="my-bindings.yaml")


def test_bindings_are_read_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "bindings.yaml"
    path.write_text(TENANT_ONLY, encoding="utf-8")

    loaded = load_bindings_file(path, tenant_id=TENANT)

    assert len(loaded) == 2


def test_an_error_in_a_file_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "bindings.yaml"
    path.write_text("bindings:\n  - {}", encoding="utf-8")

    with pytest.raises(ValueError, match=r"bindings\.yaml: binding #1"):
        load_bindings_file(path, tenant_id=TENANT)


def test_the_example_bindings_file_of_the_repository_is_valid() -> None:
    example = Path(__file__).resolve().parents[3] / "config" / "bindings.example.yaml"

    loaded = load_bindings_file(example, tenant_id=TENANT)

    assert {binding.role for binding in loaded} == {
        ModelRole.CHAT_MAIN,
        ModelRole.CHAT_FAST,
        ModelRole.EMBEDDING,
    }
    assert len(InMemoryBindingRepository(loaded)) == 3
    assert {binding.provider for binding in loaded} == {ProviderKind.OLLAMA}
